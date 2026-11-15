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
    """Downloads a video or stream using yt-dlp with parallel fragments."""
    output_template = str(output_dir / "%(id)s.%(ext)s")
    cmd = [
        "python", "-m", "yt_dlp",
        "--no-playlist",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo[height<=1080]+bestaudio/best",
        "--merge-output-format", "mp4",
        "--concurrent-fragments", "5",
        "--print", "title",
        "-o", output_template,
        url
    ]
    title = ""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        out_lines = (res.stdout or "").strip().split("\n")
        if out_lines and out_lines[0].strip():
            title = out_lines[0].strip()
    except Exception as e:
        print(f"[yt-dlp] Fast download error, falling back: {e}")
        subprocess.run(["python", "-m", "yt_dlp", "--no-playlist", "-o", output_template, url], check=True)
    
    # Find the downloaded file
    mp4s = list(output_dir.glob("*.mp4"))
    if not mp4s:
        # Fallback any video file
        files = [f for f in output_dir.iterdir() if f.suffix.lower() in [".mp4", ".mkv", ".webm"]]
        if files:
            return files[0], title
        raise RuntimeError("Download completed but no video file found.")
    return mp4s[0], title

def create_hook_image(hook_text: str, output_path: Path, width=1080, height=220):
    """Creates a viral bold hook banner with Pillow."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Rounded background card
    card_margin_x = 40
    card_margin_y = 20
    card_rect = [card_margin_x, card_margin_y, width - card_margin_x, height - card_margin_y]
    draw.rounded_rectangle(card_rect, radius=28, fill=(15, 15, 20, 235), outline=(255, 215, 0, 240), width=4)
    
    # Font setup
    font_size = 46
    try:
        font = ImageFont.truetype("arialbd.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
        
    words = hook_text.split()
    lines = []
    curr = []
    for w in words:
        curr.append(w)
        test_line = " ".join(curr)
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) > (width - card_margin_x * 2 - 60):
            curr.pop()
            if curr:
                lines.append(" ".join(curr))
            curr = [w]
    if curr:
        lines.append(" ".join(curr))
        
    total_text_h = len(lines) * (font_size + 10)
    start_y = (height - total_text_h) / 2
    
    for i, line in enumerate(lines):
        line_bbox = draw.textbbox((0, 0), line, font=font)
        line_w = line_bbox[2] - line_bbox[0]
        start_x = (width - line_w) / 2
        curr_x = start_x
        y = start_y + i * (font_size + 10)
        
        # Word by word rendering with highlight for CAPITAL words
        for word in line.split():
            w_bbox = draw.textbbox((0, 0), word + " ", font=font)
            w_width = w_bbox[2] - w_bbox[0]
            
            # If word is ALL CAPS (and len > 1) -> Highlight Yellow
            if word.isupper() and len(word) > 1 and word.isalnum():
                color = (255, 235, 59, 255) # Bright Yellow
            else:
                color = (255, 255, 255, 255) # White
                
            draw.text((curr_x, y), word + " ", font=font, fill=color)
            curr_x += w_width
            
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
    create_hook_image(hook_text, hook_img, width=1080, height=220)
    
    ass_file = work_dir / f"subs_{uuid.uuid4().hex[:6]}.ass"
    generate_ass_subtitles(words, start_time, ass_file)
    
    # Escape ass path for ffmpeg filtergraph on Windows
    ass_escaped = str(ass_file).replace("\\", "/").replace(":", "\\:")
    hook_escaped = str(hook_img).replace("\\", "/").replace(":", "\\:")
    
    facecam = detect_facecam(source_video, start_time, duration) if not is_vertical_already else {"has_cam": False}
    
    if is_vertical_already:
        # Already 9:16, just scale to 1080x1920 and overlay hook + subs
        filtergraph = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2[base];"
            f"[1:v]scale=1080:220[hook];"
            f"[base][hook]overlay=0:120[vwithhook];"
            f"[vwithhook]ass='{ass_escaped}'[vout]"
        )
    elif facecam["has_cam"]:
        # Facecam on top 35%, gameplay bottom 65%
        # Crop cam box (expanded around face)
        cx, cy, cw, ch = facecam["x"], facecam["y"], facecam["w"], facecam["h"]
        cam_pad_w = int(cw * 2.8)
        cam_pad_h = int(ch * 2.5)
        crop_x = max(0, cx - int(cw * 0.9))
        crop_y = max(0, cy - int(ch * 0.7))
        
        filtergraph = (
            f"[0:v]split=2[vcam][vgame];"
            f"[vcam]crop={cam_pad_w}:{cam_pad_h}:{crop_x}:{crop_y},scale=1080:680:force_original_aspect_ratio=increase,crop=1080:680[camtop];"
            f"[vgame]scale=1080:1240:force_original_aspect_ratio=increase,crop=1080:1240[gamebot];"
            f"[camtop][gamebot]vstack=inputs=2[stacked];"
            f"[1:v]scale=1080:220[hook];"
            f"[stacked][hook]overlay=0:570[vwithhook];"
            f"[vwithhook]ass='{ass_escaped}'[vout]"
        )
    else:
        # Fit 16:9 gameplay in middle with blurred background on top & bottom
        filtergraph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bgblur];"
            f"[fg]scale=1080:608[fgscaled];"
            f"[bgblur][fgscaled]overlay=0:(1920-608)/2[composed];"
            f"[1:v]scale=1080:220[hook];"
            f"[composed][hook]overlay=0:480[vwithhook];"
            f"[vwithhook]ass='{ass_escaped}'[vout]"
        )
        
    cmd = [
        "ffmpeg", "-y",
        "-threads", "0",
        "-ss", str(start_time),
        "-t", str(duration),
        "-i", str(source_video),
        "-i", str(hook_img),
        "-filter_complex", filtergraph,
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_clip_path)
    ]
    
    subprocess.run(cmd, check=True)
    
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

