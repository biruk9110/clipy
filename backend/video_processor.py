"""FFmpeg rendering engine.

Upgrades over the legacy engine:
  * Multiple aspect ratios: 9:16 (TikTok/Reels/Shorts), 16:9 (YouTube),
    1:1 (Instagram feed), 4:5 (Instagram feed)
  * 720p / 1080p output
  * Caption styles: karaoke (word-by-word), bold blocks, minimal
  * Optional silence trimming with re-mapped subtitle timings
  * Zoom punch effects driven by the AI editing plan
  * Loudness-normalized audio (-14 LUFS)
"""
import json
import re
import subprocess
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import UPLOADS_DIR, OUTPUT_DIR, HOST_SETTINGS

# ---------------------------------------------------------------------------
# Probing & downloading
# ---------------------------------------------------------------------------
def probe_video(video_path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,duration",
           "-show_entries", "format=duration", "-of", "json", str(video_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
        data = json.loads(res.stdout)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format", {})
        width = int(stream.get("width", 1920))
        height = int(stream.get("height", 1080))
        dur_str = stream.get("duration") or fmt.get("duration") or "0"
        duration = float(dur_str)
        if duration <= 0:
            res2 = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
                capture_output=True, text=True, timeout=60)
            duration = float(res2.stdout.strip() or 0)
        return {"width": width, "height": height, "duration": duration,
                "aspect": width / max(1, height)}
    except Exception as e:
        print(f"[Probe] Error: {e}")
        return {"width": 1920, "height": 1080, "duration": 0, "aspect": 1.77}


def probe_has_audio(video_path: Path) -> bool:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return "audio" in (res.stdout or "").lower()
    except Exception:
        return False


def download_video(url: str, output_dir: Path):
    """Downloads a public source video with yt-dlp (multi-client fallbacks)."""
    import yt_dlp

    output_template = str(output_dir / "%(id)s.%(ext)s")
    base_opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "format": "bestvideo[height<=1080]+bestaudio/bestvideo*+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
    }

    is_youtube = any(d in url.lower() for d in ("youtube.com", "youtu.be"))
    is_tiktok = "tiktok.com" in url.lower()

    for cf in [Path("cookies.txt"), Path("data/cookies.txt")]:
        if cf.is_file() and cf.stat().st_size > 0:
            base_opts["cookiefile"] = str(cf)
            break

    yt_cookies_text = (HOST_SETTINGS.get("youtube_cookies") or "").strip()
    if yt_cookies_text and is_youtube:
        c_path = output_dir / "yt_cookies.txt"
        c_path.write_text(yt_cookies_text, encoding="utf-8")
        base_opts["cookiefile"] = str(c_path)

    try:
        session_id = (HOST_SETTINGS.get("tiktok_session_id") or "").strip()
        if session_id and is_tiktok:
            import http.cookiejar
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name="sessionid", value=session_id, port=None, port_specified=False,
                domain=".tiktok.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None, discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False))
            base_opts["cookiejar"] = jar
    except Exception as e:
        print(f"[yt-dlp] Cookie setup skipped: {e}")

    def _short_err(e) -> str:
        msg = " ".join(str(e).split())
        if "sign in to confirm" in msg.lower():
            return "YouTube requested bot verification. Try another video, or upload the MP4 directly."
        return msg[:300] or type(e).__name__

    errors = []
    title = ""

    strategies = [base_opts]
    if is_youtube:
        strategies.insert(0, {
            **base_opts,
            "extractor_args": {"youtube": {
                "player_client": ["android", "ios", "web"],
                "player_skip": ["configs", "webpage"]}}})
        strategies.append({
            **base_opts,
            "format": "bestvideo*+bestaudio/best",
            "extractor_args": {"youtube": {
                "player_client": ["tv_embedded", "web_embedded", "android", "ios"]}}})

    for strat in strategies:
        try:
            with yt_dlp.YoutubeDL(strat) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    title = info.get("title", "")
            for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.m4a"):
                files = sorted(output_dir.glob(pattern))
                if files:
                    return files[0], title
        except Exception as e:
            errors.append(_short_err(e))
            print(f"[yt-dlp] Strategy error: {errors[-1]}")

    for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.m4a"):
        files = sorted(output_dir.glob(pattern))
        if files:
            return files[0], title

    detail = errors[-1] if errors else "yt-dlp returned no video file (unsupported or private link?)"
    raise RuntimeError(f"Download failed: {detail}")


# ---------------------------------------------------------------------------
# Target dimensions / layout helpers
# ---------------------------------------------------------------------------
def target_dims(aspect: str, resolution: str | int) -> tuple[int, int]:
    base = 1080 if str(resolution) == "1080" else 720
    if aspect == "16:9":
        return (int(base * 16 / 9), base)
    if aspect == "1:1":
        return (base, base)
    if aspect == "4:5":
        return (int(base * 4 / 5), base)
    return (base, int(base * 16 / 9))  # 9:16 default


FONTS_DIR = Path(__file__).resolve().parent / "fonts"


def get_hook_font(size: int):
    candidates = [
        FONTS_DIR / "Anton.ttf", FONTS_DIR / "Inter.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"), Path("C:/Windows/Fonts/impact.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
    try:
        return ImageFont.truetype("arialbd.ttf", size)
    except Exception:
        pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def create_hook_image(hook_text: str, output_path: Path, width=1080, height=200):
    """Bold hook banner scaled to the output resolution."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin_x = int(width * 0.04)
    margin_y = int(height * 0.07)
    card_rect = [margin_x, margin_y, width - margin_x, height - margin_y]
    draw.rounded_rectangle(card_rect, radius=int(height * 0.12), fill=(15, 17, 26, 235),
                           outline=(250, 204, 21, 245), width=max(3, int(height * 0.02)))
    hook_text = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D]+",
        "", hook_text or "").strip() or "Wait for it!"

    words = hook_text.split()
    max_w = (width - margin_x * 2) - int(width * 0.055)
    best_font_size = int(height * 0.28)
    best_lines = []
    for test_size in [int(height * s) for s in (0.28, 0.26, 0.24, 0.22, 0.20, 0.18)]:
        if test_size < 14:
            continue
        cand_font = get_hook_font(test_size)
        cand_lines, cand_curr = [], []
        for w in words:
            cand_curr.append(w)
            bbox = draw.textbbox((0, 0), " ".join(cand_curr), font=cand_font)
            if (bbox[2] - bbox[0]) > max_w:
                cand_curr.pop()
                if cand_curr:
                    cand_lines.append(" ".join(cand_curr))
                cand_curr = [w]
        if cand_curr:
            cand_lines.append(" ".join(cand_curr))
        best_font_size, best_lines = test_size, cand_lines
        if len(cand_lines) <= 2:
            break

    font = get_hook_font(best_font_size)
    lines = best_lines
    line_spacing = int(best_font_size * 0.18)
    line_heights = []
    for line in lines:
        l_bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(max(l_bbox[3] - l_bbox[1], int(best_font_size * 0.85)))
    total_text_h = sum(line_heights) + (len(lines) - 1) * line_spacing
    curr_y = max(margin_y + int(height * 0.05), (height - total_text_h) / 2)
    for i, line in enumerate(lines):
        line_bbox = draw.textbbox((0, 0), line, font=font)
        line_w = line_bbox[2] - line_bbox[0]
        curr_x = (width - line_w) / 2
        for word in line.split():
            w_bbox = draw.textbbox((0, 0), word + " ", font=font)
            w_width = w_bbox[2] - w_bbox[0]
            clean_w = word.strip('.,!?:;"\'()[]')
            color = (250, 204, 21, 255) if (clean_w.isupper() and len(clean_w) > 1
                                            and any(c.isalpha() for c in clean_w)) else (255, 255, 255, 255)
            draw.text((curr_x, curr_y), word + " ", font=font, fill=color,
                      stroke_width=3, stroke_fill=(0, 0, 0, 230))
            curr_x += w_width
        curr_y += line_heights[i] + line_spacing
    img.save(str(output_path), "PNG")
    return output_path


def generate_ass_subtitles(words: list, offset_seconds: float, output_path: Path,
                           play_w: int = 1080, play_h: int = 1920,
                           style: str = "karaoke", time_map=None):
    """Animated subtitles. style: karaoke | bold | minimal.

    time_map: optional list of (src_start, src_end, dst_start) produced by
    silence trimming, used to remap word timings onto the compressed timeline.
    """
    if time_map:

        def remap(t: float) -> float:
            for src_start, src_end, dst_start in time_map:
                if src_start <= t < src_end:
                    return dst_start + (t - src_start)
            # beyond kept ranges: clamp to the last mapped point
            for src_start, src_end, dst_start in reversed(time_map):
                if t >= src_end:
                    return dst_start + (src_end - src_start)
            return t
    else:
        remap = lambda t: t

    if style == "bold":
        fontsize, outline, margin_v = int(play_w * 0.058), 5, int(play_h * 0.085)
        chunk_size, karaoke = 5, False
    elif style == "minimal":
        fontsize, outline, margin_v = int(play_w * 0.045), 3, int(play_h * 0.06)
        chunk_size, karaoke = 6, False
    else:  # karaoke
        fontsize, outline, margin_v = int(play_w * 0.059), 5, int(play_h * 0.19)
        chunk_size, karaoke = 4, True

    play_w, play_h = max(2, play_w), max(2, play_h)
    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,{fontsize},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},3,2,{int(play_w*0.055)},{int(play_w*0.055)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []

    def fmt_time(t):
        hrs = int(t // 3600)
        mins = int((t % 3600) // 60)
        secs = t % 60
        return f"{hrs:01d}:{mins:02d}:{secs:05.2f}"

    clip_words = []
    for w in words:
        start_rel = remap(w["start"] - offset_seconds)
        end_rel = remap(w["end"] - offset_seconds)
        if end_rel > 0:
            clip_words.append({
                "word": w["word"],
                "start": max(0.0, start_rel),
                "end": max(0.15, end_rel),
            })

    for i in range(0, len(clip_words), chunk_size):
        group = clip_words[i:i + chunk_size]
        if not group:
            continue
        g_start = group[0]["start"]
        g_end = group[-1]["end"]
        if karaoke:
            styled = []
            for w in group:
                dur_cs = int(max(1, (w["end"] - w["start"]) * 100))
                styled.append(f"{{\\k{dur_cs}}}{w['word']}")
            phrase = " ".join(styled)
        else:
            phrase = " ".join(w["word"] for w in group)
        events.append(f"Dialogue: 0,{fmt_time(g_start)},{fmt_time(g_end)},Default,,0,0,0,,{phrase}")

    output_path.write_text(ass_header + "\n".join(events), encoding="utf-8")
    return output_path


def detect_facecam(video_path: Path, start_time: float, duration: float):
    try:
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.set(cv2.CAP_PROP_POS_MSEC, (start_time + 1.0) * 1000)
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces_found = []
        for _ in range(10):
            ret, frame = cap.read()
            if not ret:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            detected = face_cascade.detectMultiScale(gray, 1.3, 5)
            h_frame, w_frame = frame.shape[:2]
            for (x, y, w, h) in detected:
                if w < w_frame * 0.4 and h < h_frame * 0.5:
                    faces_found.append((x, y, w, h))
        cap.release()
        if len(faces_found) >= 3:
            xs = [f[0] for f in faces_found]
            ys = [f[1] for f in faces_found]
            ws = [f[2] for f in faces_found]
            hs = [f[3] for f in faces_found]
            return {"has_cam": True, "x": int(np.mean(xs)), "y": int(np.mean(ys)),
                    "w": int(np.mean(ws)), "h": int(np.mean(hs))}
    except Exception as e:
        print(f"[Facecam] Detection warning: {e}")
    return {"has_cam": False}


def compute_silence_ranges(words: list, clip_start: float, clip_end: float,
                           gap_threshold: float = 0.9, min_gap: float = 0.8,
                           max_ranges: int = 8) -> tuple[list, list]:
    """Finds silent gaps inside the clip. Returns (kept_ranges, time_map) in
    clip-local time. kept_ranges = [(start,end)] to keep, time_map maps source
    clip-local time -> compressed time for subtitle remapping."""
    ws = sorted([w for w in (words or []) if w.get("start") is not None],
                key=lambda w: w["start"])
    ws = [w for w in ws if w["end"] > clip_start and w["start"] < clip_end]
    if not ws:
        return [(0.0, clip_end - clip_start)], None
    local = [{"start": max(0.0, w["start"] - clip_start),
              "end": max(0.1, min(w["end"], clip_end) - clip_start)} for w in ws]
    gaps = []
    prev_end = local[0]["end"]
    for w in local[1:]:
        gap_start, gap_end = prev_end, max(prev_end, w["start"])
        if gap_end - gap_start >= min_gap:
            gaps.append((gap_start, gap_end))
        prev_end = max(prev_end, w["end"])
    if not gaps:
        return [(0.0, clip_end - clip_start)], None
    kept = []
    cursor = 0.0
    for gs, ge in gaps:
        if gs > cursor:
            kept.append((cursor, gs))
        cursor = max(cursor, ge)
    if cursor < clip_end - clip_start:
        kept.append((cursor, clip_end - clip_start))
    kept = kept[:max_ranges + 1]
    time_map = []
    dst = 0.0
    for ks, ke in kept:
        time_map.append((ks, ke, dst))
        dst += ke - ks
    return kept, time_map


def render_clip(
    source_video: Path,
    start_time: float,
    duration: float,
    hook_text: str,
    words: list,
    output_clip_path: Path,
    is_vertical_already: bool = False,
    aspect: str = "9:16",
    resolution: int | str = 720,
    caption_style: str = "karaoke",
    captions_enabled: bool = True,
    hook_banner: bool = True,
    zoom_intensity: int = 1,
    trim_silence: bool = False,
    audio_enhance: bool = True,
):
    """Renders one clip:
      layout: facecam stack / blurred bg / center-crop based on aspect
      overlays: hook banner + animated captions
      effects: zoom punch (zoompan), silence trim (select/aselect)
      audio: loudnorm -14 LUFS
    """
    W, H = target_dims(aspect, resolution)
    work_dir = output_clip_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    src = probe_video(Path(source_video))
    src_aspect = src.get("aspect") or 1.77
    src_w, src_h = src.get("width", 1920), src.get("height", 1080)
    target_aspect = W / H

    # --- silence trimming -------------------------------------------------
    time_map = None
    sil_select = ""
    sil_aselect = ""
    kept_ranges, time_map = [(0.0, duration)], None
    seg_duration = duration
    if trim_silence and words:
        kept_ranges, time_map = compute_silence_ranges(words, start_time, start_time + duration)
        if kept_ranges and len(kept_ranges) >= 2:
            sel = "+".join(f"between(t,{ks:.3f},{ke:.3f})" for ks, ke in kept_ranges)
            sil_select = f"select='{sel}',setpts=N/FRAME_RATE/TB"
            sil_aselect = f"aselect='{sel}',asetpts=N/SR/TB"
            duration = sum(ke - ks for ks, ke in kept_ranges)
            duration = max(0.3, duration)

    # --- caption file -------------------------------------------------------
    has_subs = bool(captions_enabled and words)
    ass_file = None
    ass_escaped = ""
    if has_subs:
        ass_file = work_dir / f"subs_{uuid.uuid4().hex[:6]}.ass"
        generate_ass_subtitles(words, start_time, ass_file, play_w=W, play_h=H,
                               style=caption_style or "karaoke", time_map=time_map)
        ass_escaped = str(ass_file).replace("\\", "/").replace(":", "\\:")

    # --- hook banner ---------------------------------------------------------
    hook_img = None
    hook_escaped = ""
    hook_h = int(W * 200 / 1080)
    if hook_banner:
        hook_img = work_dir / f"hook_{uuid.uuid4().hex[:6]}.png"
        create_hook_image(hook_text, hook_img, width=W, height=hook_h)

    facecam = ({"has_cam": False} if is_vertical_already
               else detect_facecam(source_video, start_time, duration))

    # --- base layout ----------------------------------------------------------
    vertical_source = src_aspect < 0.75
    if aspect in ("9:16", "4:5"):
        if is_vertical_already or (vertical_source and aspect == "9:16"):
            base = (f"[v0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2[base]")
        elif facecam["has_cam"]:
            cx, cy, cw, ch = facecam["x"], facecam["y"], facecam["w"], facecam["h"]
            cam_pad_w = int(cw * 2.8)
            cam_pad_h = int(ch * 2.5)
            crop_x = max(0, cx - int(cw * 0.9))
            crop_y = max(0, cy - int(ch * 0.7))
            top_h = int(H * 0.34)
            bot_h = H - top_h
            base = (
                f"[v0]split=2[vcam][vgame];"
                f"[vcam]crop={cam_pad_w}:{cam_pad_h}:{crop_x}:{crop_y},"
                f"scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}[camtop];"
                f"[vgame]scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h}[gamebot];"
                f"[camtop][gamebot]vstack=inputs=2[base]")
        else:
            # blurred background + fitted middle video
            fg_h = min(H - 2, int(W * src_h / max(1, src_w)))
            fg_h = max(2, fg_h)
            base = (
                f"[v0]split=2[bg][fg];"
                f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bgblur];"
                f"[fg]scale={W}:{fg_h}[fgscaled];"
                f"[bgblur][fgscaled]overlay=0:(H-fg_h)/2[base]")
            base = base.replace("(H-fg_h)/2", str((H - fg_h) // 2)).replace("fg_h", str(fg_h))
    elif aspect in ("1:1", "16:9"):
        if abs(src_aspect - target_aspect) < 0.2:
            base = f"[v0]scale={W}:{H}[base]"
        else:
            # center crop to fill (professional full-bleed reframe)
            base = (f"[v0]scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H}[base]")
    else:
        base = f"[v0]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2[base]"

    # --- zoom punch -------------------------------------------------------------
    zoom_label = "base"
    if zoom_intensity and int(zoom_intensity) > 0:
        intensity = min(2, max(1, int(zoom_intensity)))
        max_zoom = 1.0 + 0.045 * intensity  # 1.045 .. 1.09
        zexpr = f"1+{max_zoom - 1:.3f}*exp(-on/10)"
        zoompan = (
            f"fps=30,zoompan=z='{zexpr}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}")
        base += f";[{zoom_label}]{zoompan}[zoomed]"
        zoom_label = "zoomed"

    # --- overlays ----------------------------------------------------------------
    cur = zoom_label
    if hook_img:
        hook_y = int(H * 0.10) if aspect == "9:16" else int(H * 0.06)
        base += f";[1:v]scale={W}:{hook_h}[hook];[{cur}][hook]overlay=0:{hook_y}[vhook]"
        cur = "vhook"
    if has_subs:
        base += f";[{cur}]ass='{ass_escaped}'[vout]"
        cur = "vout"

    final_v = cur
    graph = (
        (f"[0:v]{sil_select}[v0]" if sil_select else "[0:v]null[v0]")
        + ";" + base
    )

    has_audio = probe_has_audio(source_video)
    audio_map = None
    if has_audio:
        if sil_aselect:
            graph += ";[0:a]" + sil_aselect + "[a0]"
            src_audio_label = "[a0]"
        else:
            src_audio_label = "[0:a]"
        if audio_enhance:
            graph += f";{src_audio_label}loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
            audio_map = "[aout]"
        else:
            graph += f";{src_audio_label}anull[aout]"
            audio_map = "[aout]"

    cmd = [
        "ffmpeg", "-y", "-threads", "0",
        "-ss", str(start_time), "-t", str(max(0.5, seg_duration)),
        "-i", str(source_video),
    ]
    if hook_img:
        cmd += ["-i", str(hook_img)]
    cmd += [
        "-filter_complex", graph,
        "-map", f"[{final_v}]",
    ]
    if audio_map:
        cmd += ["-map", audio_map]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-maxrate", "6M" if str(resolution) == "1080" else "3M",
        "-bufsize", "12M" if str(resolution) == "1080" else "6M",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(max(0.3, duration)),
        str(output_clip_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800)
    if res.returncode != 0:
        err_tail = "\n".join((res.stderr or "").strip().splitlines()[-8:]).strip()
        raise RuntimeError(f"ffmpeg render failed (exit {res.returncode}): {err_tail or 'unknown error'}")

    thumb_path = work_dir / f"{output_clip_path.stem}_thumb.jpg"
    thumb_cmd = ["ffmpeg", "-y", "-ss", "1.0", "-i", str(output_clip_path),
                 "-vframes", "1", "-q:v", "2",
                 "-vf", f"scale={W}:-2", str(thumb_path)]
    subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)

    for tmp in (hook_img, ass_file):
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
    return output_clip_path, thumb_path
