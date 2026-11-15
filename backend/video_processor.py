import os
import subprocess
import json
import uuid
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import cv2
import numpy as np

from .config import UPLOADS_DIR, OUTPUT_DIR, get_settings
from .transcriber import extract_audio, transcribe_audio
from .gemini_director import pick_stream_moments, polish_short_video

def probe_video(video_path: Path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        stream = data.get("streams", [{}])[0]
        fmt = data.get("format", {})
        width = int(stream.get("width", 1920))
        height = int(stream.get("height", 1080))
        dur_str = stream.get("duration") or fmt.get("duration") or "0"
        duration = float(dur_str)
        return {"width": width, "height": height, "duration": duration, "aspect": width / max(1, height)}
    except Exception as e:
        print(f"[Probe] Error: {e}")
        return {"width": 1920, "height": 1080, "duration": 0, "aspect": 1.77}

def download_video(url: str, output_dir: Path):
    """Downloads a video or stream using yt-dlp with multi-client fallbacks for datacenter IPs."""
    import yt_dlp
    
    settings = get_settings()
    output_template = str(output_dir / "%(id)s.%(ext)s")
    base_opts = {
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
    }

    is_youtube = any(d in url.lower() for d in ("youtube.com", "youtu.be"))
    is_tiktok = "tiktok.com" in url.lower()

    # Check for cookies file
    cookie_candidates = [
        output_dir.parent.parent / "cookies.txt",
        Path("cookies.txt"),
        Path("data/cookies.txt"),
    ]
    for cf in cookie_candidates:
        if cf.is_file() and cf.stat().st_size > 0:
            base_opts['cookiefile'] = str(cf)
            break

    # If user provided raw YouTube cookies in settings
    yt_cookies_text = (settings.get("youtube_cookies") or "").strip()
    if yt_cookies_text and is_youtube:
        c_path = output_dir / "yt_cookies.txt"
        c_path.write_text(yt_cookies_text, encoding="utf-8")
        base_opts['cookiefile'] = str(c_path)

    # TikTok often blocks anonymous/datacenter IPs - attach the user's session cookie when available
    try:
        session_id = (settings.get("tiktok_session_id") or "").strip()
        if session_id and is_tiktok:
            import http.cookiejar
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name="sessionid", value=session_id,
                port=None, port_specified=False,
                domain=".tiktok.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True,
                secure=True, expires=None, discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            ))
            base_opts['cookiejar'] = jar
    except Exception as e:
        print(f"[yt-dlp] Cookie setup skipped: {e}")

    def _short_err(e) -> str:
        msg = " ".join(str(e).split())  # collapse newlines/indentation
        if "sign in to confirm" in msg.lower():
            return "YouTube requested bot verification. Try another video, or upload the MP4 directly."
        return msg[:300] or type(e).__name__

    errors = []
    title = ""

    # Strategy 1: Mobile clients (android, ios, web) with best quality up to 1080p
    strategy_1 = {
        **base_opts,
        'format': 'bestvideo[height<=1080]+bestaudio/bestvideo*+bestaudio/best[height<=1080]/best',
        'merge_output_format': 'mp4',
    }
    if is_youtube:
        strategy_1['extractor_args'] = {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['configs', 'webpage'],
            }
        }

    # Strategy 2: Embedded player / TV clients with generic format fallback
    strategy_2 = {
        **base_opts,
        'format': 'bestvideo*+bestaudio/best',
        'merge_output_format': 'mp4',
    }
    if is_youtube:
        strategy_2['extractor_args'] = {
            'youtube': {
                'player_client': ['tv_embedded', 'web_embedded', 'android', 'ios'],
            }
        }

    # Strategy 3: Any available stream without strict filters
    strategy_3 = {
        **base_opts,
        'format': 'bestvideo*+bestaudio/best/all',
    }

    for strat in [strategy_1, strategy_2, strategy_3]:
        try:
            with yt_dlp.YoutubeDL(strat) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    title = info.get("title", "")
            # Check if any video file was created
            for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.m4a"):
                files = sorted(output_dir.glob(pattern))
                if files:
                    return files[0], title
        except Exception as e:
            errors.append(_short_err(e))
            print(f"[yt-dlp] Strategy error: {errors[-1]}")

    # Final check for any downloaded file
    for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.m4a"):
        files = sorted(output_dir.glob(pattern))
        if files:
            return files[0], title

    detail = errors[-1] if errors else "yt-dlp returned no video file (unsupported or private link?)"
    raise RuntimeError(f"Download failed: {detail}")

FONTS_DIR = Path(__file__).resolve().parent / "fonts"

def get_hook_font(size: int):
    """Finds the best bold font for viral hook banners across environments."""
    candidates = [
        FONTS_DIR / "Anton.ttf",
        FONTS_DIR / "Inter.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/impact.ttf"),
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
    """Creates a viral bold hook banner with Pillow."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Rounded background card
    card_margin_x = 44
    card_margin_y = 14
    card_rect = [card_margin_x, card_margin_y, width - card_margin_x, height - card_margin_y]
    draw.rounded_rectangle(card_rect, radius=24, fill=(15, 17, 26, 235), outline=(250, 204, 21, 245), width=4)
    
    # Strip emoji glyphs the banner font has no artwork for (they render as empty boxes)
    hook_text = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D]+",
        "", hook_text or ""
    ).strip() or "Wait for it!"

    words = hook_text.split()
    max_w = (width - card_margin_x * 2) - 60
    
    # Auto-scale font size to fit in 1 or 2 lines
    best_font_size = 52
    best_lines = []
    for test_size in [56, 52, 48, 44, 40, 36]:
        cand_font = get_hook_font(test_size)
        cand_lines = []
        cand_curr = []
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
            
        best_font_size = test_size
        best_lines = cand_lines
        if len(cand_lines) <= 2:
            break
            
    font = get_hook_font(best_font_size)
    lines = best_lines
    line_spacing = 10
    
    # Compute line heights
    line_heights = []
    for line in lines:
        l_bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(max(l_bbox[3] - l_bbox[1], int(best_font_size * 0.85)))
        
    total_text_h = sum(line_heights) + (len(lines) - 1) * line_spacing
    curr_y = max(card_margin_y + 10, (height - total_text_h) / 2)
    
    for i, line in enumerate(lines):
        line_bbox = draw.textbbox((0, 0), line, font=font)
        line_w = line_bbox[2] - line_bbox[0]
        start_x = (width - line_w) / 2
        curr_x = start_x
        
        # Word by word rendering with highlight for UPPERCASE words
        for word in line.split():
            w_bbox = draw.textbbox((0, 0), word + " ", font=font)
            w_width = w_bbox[2] - w_bbox[0]
            
            clean_w = word.strip('.,!?:;"\'()[]')
            if clean_w.isupper() and len(clean_w) > 1 and any(c.isalpha() for c in clean_w):
                color = (250, 204, 21, 255) # Bright Yellow
            else:
                color = (255, 255, 255, 255) # Crisp White
                
            draw.text((curr_x, curr_y), word + " ", font=font, fill=color, stroke_width=3, stroke_fill=(0, 0, 0, 230))
            curr_x += w_width
            
        curr_y += line_heights[i] + line_spacing
            
    img.save(str(output_path), "PNG")
    return output_path

def generate_ass_subtitles(words: list, offset_seconds: float, output_path: Path):
    """Generates animated word-by-word karaoke style ASS subtitles."""
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,3,2,60,60,340,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    
    def fmt_time(t):
        hrs = int(t // 3600)
        mins = int((t % 3600) // 60)
        secs = t % 60
        return f"{hrs:01d}:{mins:02d}:{secs:05.2f}"
    
    # Filter words inside the clip timeframe
    clip_words = []
    for w in words:
        start_rel = w["start"] - offset_seconds
        end_rel = w["end"] - offset_seconds
        if start_rel >= 0:
            clip_words.append({
                "word": w["word"],
                "start": max(0.0, start_rel),
                "end": max(0.1, end_rel)
            })
            
    # Group words into 3-5 word phrases
    chunk_size = 4
    for i in range(0, len(clip_words), chunk_size):
        group = clip_words[i:i + chunk_size]
        if not group:
            continue
        g_start = group[0]["start"]
        g_end = group[-1]["end"]
        
        # Build animated text with karaoke highlight
        styled_words = []
        for w in group:
            dur_cs = int((w["end"] - w["start"]) * 100) # centiseconds
            styled_words.append(f"{{\\k{dur_cs}}}{w['word']}")
        phrase = " ".join(styled_words)
        events.append(f"Dialogue: 0,{fmt_time(g_start)},{fmt_time(g_end)},Default,,0,0,0,,{phrase}")
        
    output_path.write_text(ass_header + "\n".join(events), encoding="utf-8")
    return output_path

def detect_facecam(video_path: Path, start_time: float, duration: float):
    """Detects if there is a facecam overlay (top or corner)."""
    try:
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.set(cv2.CAP_PROP_POS_MSEC, (start_time + 1.0) * 1000)
        
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        faces_found = []
        
        for _ in range(10): # Sample 10 frames
            ret, frame = cap.read()
            if not ret:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            detected = face_cascade.detectMultiScale(gray, 1.3, 5)
            for (x, y, w, h) in detected:
                # If face is reasonably sized for a streamer cam
                h_frame, w_frame = frame.shape[:2]
                if w < w_frame * 0.4 and h < h_frame * 0.5:
                    faces_found.append((x, y, w, h))
        cap.release()
        
        if len(faces_found) >= 3:
            # Average face position
            xs = [f[0] for f in faces_found]
            ys = [f[1] for f in faces_found]
            ws = [f[2] for f in faces_found]
            hs = [f[3] for f in faces_found]
            return {
                "has_cam": True,
                "x": int(np.mean(xs)),
                "y": int(np.mean(ys)),
                "w": int(np.mean(ws)),
                "h": int(np.mean(hs))
            }
    except Exception as e:
        print(f"[Facecam] Detection warning: {e}")
    return {"has_cam": False}

def render_clip(
    source_video: Path,
    start_time: float,
    duration: float,
    hook_text: str,
    words: list,
    output_clip_path: Path,
    is_vertical_already: bool = False
):
    """
    Renders the final 9:16 vertical clip:
    - 1080x1920 layout
    - Facecam on top + gameplay on bottom (if detected) or blurred 16:9 background
    - Hook title bar overlay
    - Word-by-word animated subtitles
    - Loudnorm audio balancing (-14 LUFS)
    """
    work_dir = output_clip_path.parent
    hook_img = work_dir / f"hook_{uuid.uuid4().hex[:6]}.png"
    create_hook_image(hook_text, hook_img, width=1080, height=200)
    
    ass_file = work_dir / f"subs_{uuid.uuid4().hex[:6]}.ass"
    generate_ass_subtitles(words, start_time, ass_file)
    
    # Escape ass path for ffmpeg filtergraph on Windows
    ass_escaped = str(ass_file).replace("\\", "/").replace(":", "\\:")
    hook_escaped = str(hook_img).replace("\\", "/").replace(":", "\\:")
    
    facecam = detect_facecam(source_video, start_time, duration) if not is_vertical_already else {"has_cam": False}
    
    has_subs = bool(words and len(words) > 0)
    
    if is_vertical_already:
        # Already 9:16, just scale to 1080x1920 and overlay hook + subs
        subs_filter = f";[vpre]ass='{ass_escaped}'[vout]" if has_subs else ""
        out_tag = "[vout]" if not has_subs else "[vpre]"
        filtergraph = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2[base];"
            f"[1:v]scale=1080:200[hook];"
            f"[base][hook]overlay=0:140{out_tag}{subs_filter}"
        )
    elif facecam["has_cam"]:
        # Facecam on top 35%, gameplay bottom 65%
        cx, cy, cw, ch = facecam["x"], facecam["y"], facecam["w"], facecam["h"]
        cam_pad_w = int(cw * 2.8)
        cam_pad_h = int(ch * 2.5)
        crop_x = max(0, cx - int(cw * 0.9))
        crop_y = max(0, cy - int(ch * 0.7))
        
        subs_filter = f";[vpre]ass='{ass_escaped}'[vout]" if has_subs else ""
        out_tag = "[vout]" if not has_subs else "[vpre]"
        filtergraph = (
            f"[0:v]split=2[vcam][vgame];"
            f"[vcam]crop={cam_pad_w}:{cam_pad_h}:{crop_x}:{crop_y},scale=1080:680:force_original_aspect_ratio=increase,crop=1080:680[camtop];"
            f"[vgame]scale=1080:1240:force_original_aspect_ratio=increase,crop=1080:1240[gamebot];"
            f"[camtop][gamebot]vstack=inputs=2[stacked];"
            f"[1:v]scale=1080:200[hook];"
            f"[stacked][hook]overlay=0:580{out_tag}{subs_filter}"
        )
    else:
        # Fit 16:9 gameplay in middle with blurred background on top & bottom
        # Top blur is 0..656, middle video is 656..1264, bottom blur is 1264..1920
        # Placing hook banner cleanly in the top third at y=220 (leaving 236px clear space above video)
        subs_filter = f";[vpre]ass='{ass_escaped}'[vout]" if has_subs else ""
        out_tag = "[vout]" if not has_subs else "[vpre]"
        filtergraph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bgblur];"
            f"[fg]scale=1080:608[fgscaled];"
            f"[bgblur][fgscaled]overlay=0:(1920-608)/2[composed];"
            f"[1:v]scale=1080:200[hook];"
            f"[composed][hook]overlay=0:220{out_tag}{subs_filter}"
        )
        
    cmd = [
        "ffmpeg", "-y",
        "-threads", "0",
        "-ss", str(start_time),
        "-t", str(duration),
        "-i", str(source_video),
        "-i", str(hook_img),
        "-filter_complex", filtergraph,
        "-map", "[vout]",          # final label every branch outputs
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_clip_path)
    ]
    
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        # Surface the meaningful ffmpeg error instead of the raw command list
        err_tail = "\n".join((res.stderr or "").strip().splitlines()[-6:]).strip()
        raise RuntimeError(f"ffmpeg render failed (exit {res.returncode}): {err_tail or 'unknown error'}")
    
    # Extract thumbnail
    thumb_path = work_dir / f"{output_clip_path.stem}_thumb.jpg"
    thumb_cmd = [
        "ffmpeg", "-y",
        "-ss", "1.0",
        "-i", str(output_clip_path),
        "-vframes", "1",
        "-q:v", "2",
        str(thumb_path)
    ]
    subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Clean up temp files
    hook_img.unlink(missing_ok=True)
    ass_file.unlink(missing_ok=True)
    
    return output_clip_path, thumb_path

