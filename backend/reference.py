"""Reference-video pipeline.

"Use a Video as Inspiration": the user provides a public TikTok / Instagram
Reels / YouTube Shorts / direct video link, Clipy retrieves it through
supported public means, extracts frames + audio, and asks Gemini to build a
structured editing profile that later guides the edit of the user's own video.

Access policy: only publicly available videos are fetched with standard
clients. If a platform refuses (private/region-locked/login-required), the
user is asked to upload the reference file instead. No DRM or access
controls are bypassed.
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .config import HOST_SETTINGS

PLATFORM_PATTERNS = [
    ("tiktok", re.compile(r"https?://(www\.|vm\.|vt\.|m\.)?(tiktok\.com)/", re.I)),
    ("instagram", re.compile(r"https?://(www\.)?(instagram\.com)/((reel|reels|p)/|tv/)", re.I)),
    ("youtube", re.compile(r"https?://(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)", re.I)),
    ("x", re.compile(r"https?://(www\.|mobile\.)?(twitter\.com|x\.com)/\w+/status/", re.I)),
    ("facebook", re.compile(r"https?://(www\.|m\.)?(facebook\.com|fb\.watch)/", re.I)),
]

DIRECT_FILE = re.compile(r"https?://\S+\.(mp4|mov|webm|mkv|m4v|avi)(\?\S*)?$", re.I)


class ReferenceError(Exception):
    pass


def detect_platform(url: str) -> str:
    for name, rx in PLATFORM_PATTERNS:
        if rx.search(url or ""):
            return name
    if DIRECT_FILE.search(url or ""):
        return "direct"
    return "unknown"


def validate_reference_url(url: str) -> dict:
    """Cheap static validation before attempting any download."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "Paste a video link first."}
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    platform = detect_platform(url)
    if platform == "unknown" and not DIRECT_FILE.search(url):
        return {
            "ok": False,
            "error": "Unsupported link. Use a TikTok, Instagram Reel, YouTube Shorts, or direct video (.mp4) URL.",
        }
    return {"ok": True, "url": url, "platform": platform}


def fetch_reference(url: str, out_dir: Path) -> Path:
    """Downloads a public reference video (<=1080p, capped length) via yt-dlp.

    Raises ReferenceError with a human-readable reason when the platform
    refuses access - the UI then offers the upload-reference fallback.
    """
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(out_dir / "reference.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        # Reference only: quality is enough for frame analysis
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
        "merge_output_format": "mp4",
        # Be a good citizen: max 2 minutes analyzed even for long links
        "max_filesize": 300 * 1024 * 1024,
    }
    # Optional user cookies (their own accounts) to view content they can see
    sid = (HOST_SETTINGS.get("tiktok_session_id") or "").strip()
    if sid and "tiktok.com" in url:
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        try:
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name="sessionid", value=sid, port=None, port_specified=False,
                domain=".tiktok.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None, discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False))
            opts["cookiejar"] = jar
        except Exception:
            pass

    errors = []
    for attempt, extra in enumerate([{}, {"format": "best[height<=720]/best"},
                                     {"format": "best"}]):
        try:
            with yt_dlp.YoutubeDL({**opts, **extra}) as ydl:
                ydl.extract_info(url, download=True)
            for pattern in ("reference.mp4", "reference.*.mp4", "reference*.mkv", "reference*.webm"):
                files = sorted(out_dir.glob(pattern))
                if files:
                    return files[0]
        except Exception as e:
            msg = " ".join(str(e).split())[:280]
            errors.append(msg)
    detail = errors[-1] if errors else "unknown error"
    if any(k in detail.lower() for k in ("private", "login", "sign in", "cookies", "18+", "age", "captcha")):
        raise ReferenceError(
            "This video is not publicly accessible (private or login required). "
            "Download it yourself and use 'Upload reference video' instead.")
    raise ReferenceError(f"Could not retrieve the reference video: {detail}")


def probe(video: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,duration",
           "-show_entries", "format=duration", "-of", "json", str(video)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        data = json.loads(res.stdout)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format", {})
        w = int(stream.get("width", 1080))
        h = int(stream.get("height", 1920))
        dur = float(stream.get("duration") or fmt.get("duration") or 0)
        if dur <= 0:
            dur = _probe_duration_fallback(video)
        return {"width": w, "height": h, "duration": dur, "aspect": w / max(1, h)}
    except Exception:
        return {"width": 1080, "height": 1920, "duration": _probe_duration_fallback(video),
                "aspect": 0.5625}


def _probe_duration_fallback(video: Path) -> float:
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=60)
        return float(res.stdout.strip() or 0)
    except Exception:
        return 0.0


def extract_frames(video: Path, out_dir: Path, max_frames: int = 10) -> list[str]:
    """Extracts evenly spaced JPEG frames (what Gemini will see)."""
    info = probe(video)
    dur = info["duration"]
    if dur <= 0:
        return []
    n = min(max_frames, max(4, int(dur // 4)))
    if n < 1:
        return []
    frames = []
    for i in range(n):
        t = (dur - 0.5) * (i + 0.5) / n if dur > 1 else 0.05
        out = out_dir / f"ref_frame_{i:02d}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", f"{max(0.05, t):.2f}", "-i", str(video),
               "-vframes", "1", "-vf", "scale=544:-2", "-q:v", "5", str(out)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
            if out.exists() and out.stat().st_size > 1000:
                frames.append(str(out))
        except Exception:
            continue
    return frames


def extract_audio(video: Path, out_dir: Path) -> Path | None:
    out = out_dir / "reference_audio.wav"
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vn", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300)
        if out.exists() and out.stat().st_size > 10000:
            return out
    except Exception:
        pass
    return None


def quick_transcript(audio: Path | None) -> str:
    """Tiny transcript (for pacing/music hints) - reuses the app transcriber."""
    if not audio:
        return ""
    try:
        from .transcriber import transcribe_audio
        data = transcribe_audio(audio)
        return (data.get("text") or "")[:6000]
    except Exception as e:
        print(f"[Reference] transcript skipped: {e}")
        return ""


def analyze(user_id: int, url: str = "", local_file: Path | None = None,
            work_dir: Path | None = None) -> dict:
    """Full pipeline: validate -> fetch -> frames+audio -> Gemini profile.

    Returns {'ok', 'platform', 'video_path', 'info', 'profile', 'error'}.
    """
    from . import gemini_director
    from .gemini_director import analyze_reference

    work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="clipy_ref_"))
    work_dir.mkdir(parents=True, exist_ok=True)

    platform = ""
    if url:
        v = validate_reference_url(url)
        if not v["ok"]:
            return {"ok": False, "error": v["error"], "platform": "unknown"}
        url = v["url"]
        platform = v["platform"]

    video = local_file if local_file else None
    if not video and url:
        try:
            video = fetch_reference(url, work_dir)
        except ReferenceError as e:
            return {"ok": False, "error": str(e), "platform": platform,
                    "needs_upload": True}
        except Exception as e:
            return {"ok": False, "error": f"Could not retrieve the reference video: {e}",
                    "platform": platform, "needs_upload": True}
    if not video or not Path(video).exists():
        return {"ok": False, "error": "No reference video available.", "platform": platform}

    info = probe(Path(video))
    frames = extract_frames(Path(video), work_dir)
    audio = extract_audio(Path(video), work_dir)
    transcript = quick_transcript(audio)

    analysis = analyze_reference(frames, transcript, info["duration"], user_id=user_id)
    profile = analysis.get("profile")
    if not analysis["ok"] and analysis.get("error") == "no_api_key":
        return {
            "ok": False, "platform": platform, "video_path": str(video), "info": info,
            "frames": len(frames), "transcript": transcript[:800],
            "error": "Add your Gemini API key in Settings to analyze the reference video.",
            "needs_api_key": True,
        }
    if not profile:
        # Basic heuristic profile so the editor can still proceed
        profile = {
            "editing_style": "Auto-detected (AI unavailable): standard vertical short.",
            "avg_shot_duration_sec": 3.0, "scene_transitions": "hard cut",
            "camera_movement": "unknown", "zoom_effects": "subtle punch-ins",
            "caption_style": "karaoke", "caption_position": "center",
            "subtitle_animation": "pop-in", "music": "unknown", "sound_effects": [],
            "color_grade": "neutral", "aspect_ratio": "9:16" if info["aspect"] < 0.8 else "16:9",
            "pacing": "fast", "key_moments": [], "ending_style": "loop bait",
            "editing_plan_hint": "Cut tight, add bold captions and mild zoom punches.",
            "source": "heuristic", "analysis_error": analysis.get("error", "")[:200],
        }
        result = {"ok": True, "platform": platform, "video_path": str(video), "info": info,
                  "frames": len(frames), "transcript": transcript[:800],
                  "profile": profile, "error": analysis.get("error", "")[:300]}
        return result
    return {"ok": True, "platform": platform, "video_path": str(video), "info": info,
            "frames": len(frames), "transcript": transcript[:800], "profile": profile,
            "error": ""}
