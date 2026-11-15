import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .config import OUTPUT_DIR, UPLOADS_DIR, get_settings
from .storage import (
    create_job, update_job, add_clip, update_clip, 
    get_pending_scheduled_clips, get_clip
)
from .video_processor import probe_video, download_video, render_clip
from .transcriber import extract_audio, transcribe_audio
from .gemini_director import pick_stream_moments, polish_short_video
from .posters.instagram import post_to_instagram
from .posters.tiktok import post_to_tiktok
from .posters.buffer import post_to_buffer

_scheduler_running = False

def start_background_scheduler():
    global _scheduler_running
    if _scheduler_running:
        return
    _scheduler_running = True
    
    def loop():
        while True:
            try:
                check_and_post_scheduled_clips()
            except Exception as e:
                print(f"[Scheduler] Loop error: {e}")
            time.sleep(30)
            
    t = threading.Thread(target=loop, daemon=True)
    t.start()

def check_and_post_scheduled_clips():
    pending = get_pending_scheduled_clips()
    for clip in pending:
        publish_clip(clip["id"])

def publish_clip(clip_id: str):
    clip = get_clip(clip_id)
    if not clip:
        return
    
    video_path = Path(clip["file_path"])
    if not video_path.exists():
        update_clip(clip_id, status="failed", error="Video file missing")
        return
        
    settings = get_settings()
    ig_res = {"success": True, "mode": "skipped"}
    tiktok_res = {"success": True, "mode": "skipped"}
    buffer_res = {"success": False}
    
    # If Buffer is configured, post to Buffer (handles both IG & TikTok)
    if settings.get("buffer_access_token"):
        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://clipy-0o5j.onrender.com"
        video_url = f"{base_url}/media/{video_path.name}"
        buffer_res = post_to_buffer(video_url, clip["caption"], clip["hashtags"])

    if settings.get("auto_post_instagram", True):
        ig_res = post_to_instagram(video_path, clip["caption"], clip["hashtags"])
        
    if settings.get("auto_post_tiktok", True):
        tiktok_res = post_to_tiktok(video_path, clip["caption"], clip["hashtags"])
        
    update_clip(
        clip_id,
        status="posted",
        ig_status="posted" if ig_res.get("success") else "failed",
        ig_post_url=ig_res.get("url", ""),
        tiktok_status="posted" if tiktok_res.get("success") else "failed",
        tiktok_post_url=tiktok_res.get("url", "")
    )

def process_job_async(job_id: str, source_type: str, source_url: str = "", file_path: str = "", num_clips: int = 3, interval_hours: float = 3.0):
    def worker():
        try:
            update_job(job_id, status="processing")
            job_dir = OUTPUT_DIR / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            
            # Step 1: Ingest Video
            if source_type == "url":
                video_file = download_video(source_url, job_dir)
            else:
                video_file = Path(file_path)
                
            info = probe_video(video_file)
            dur = info["duration"]
            is_vertical = info["aspect"] < 0.7 # e.g. 9:16 is ~0.56
            is_short = dur <= 150.0 # Under 2.5 minutes is treated as a Short
            
            update_job(job_id, duration=dur, is_short=1 if is_short else 0)
            
            # Step 2: Extract & Transcribe Audio
            audio_path = job_dir / "audio.wav"
            extract_audio(video_file, audio_path)
            transcript_data = transcribe_audio(audio_path)
            
            settings = get_settings()
            now = datetime.now()
            
            if is_short:
                # Polishing 1 short video
                polish = polish_short_video(transcript_data["text"], dur)
                clip_id = f"clip_{uuid.uuid4().hex[:8]}"
                out_clip = job_dir / f"{clip_id}.mp4"
                
                render_clip(
                    source_video=video_file,
                    start_time=0.0,
                    duration=dur,
                    hook_text=polish.get("hook_text", "Wait for it! 😱"),
                    words=transcript_data["words"],
                    output_clip_path=out_clip,
                    is_vertical_already=is_vertical
                )
                
                # Immediate post schedule
                sched_time = now.isoformat()
                add_clip({
                    "id": clip_id,
                    "job_id": job_id,
                    "title": "Short Video",
                    "hook_text": polish.get("hook_text", ""),
                    "caption": polish.get("caption", ""),
                    "hashtags": polish.get("hashtags", []),
                    "duration": dur,
                    "start_time": 0.0,
                    "end_time": dur,
                    "file_path": str(out_clip),
                    "thumbnail_path": str(job_dir / f"{clip_id}_thumb.jpg"),
                    "score": polish.get("score", 90),
                    "scheduled_time": sched_time,
                    "status": "ready"
                })
            else:
                # Multi-moment extraction from longer video/stream
                moments = pick_stream_moments(transcript_data["text"], dur, count=num_clips)
                for i, m in enumerate(moments):
                    clip_id = f"clip_{uuid.uuid4().hex[:8]}"
                    out_clip = job_dir / f"{clip_id}.mp4"
                    c_dur = m["end"] - m["start"]
                    
                    render_clip(
                        source_video=video_file,
                        start_time=m["start"],
                        duration=c_dur,
                        hook_text=m.get("hook_text", "UNBELIEVABLE Moment! 💀"),
                        words=transcript_data["words"],
                        output_clip_path=out_clip,
                        is_vertical_already=is_vertical
                    )
                    
                    # Schedule: 1st clip now, subsequent clips staggered by interval_hours
                    sched_time = (now + timedelta(hours=i * interval_hours)).isoformat()
                    add_clip({
                        "id": clip_id,
                        "job_id": job_id,
                        "title": m.get("title", f"Clip {i+1}"),
                        "hook_text": m.get("hook_text", ""),
                        "caption": m.get("caption", ""),
                        "hashtags": m.get("hashtags", []),
                        "duration": c_dur,
                        "start_time": m["start"],
                        "end_time": m["end"],
                        "file_path": str(out_clip),
                        "thumbnail_path": str(job_dir / f"{clip_id}_thumb.jpg"),
                        "score": m.get("score", 88),
                        "scheduled_time": sched_time,
                        "status": "scheduled" if i > 0 else "ready"
                    })
                    
            update_job(job_id, status="completed", completed_at=datetime.now().isoformat())
            
        except Exception as e:
            print(f"[Worker] Job {job_id} failed: {e}")
            update_job(job_id, status="failed", error=str(e))
            
    t = threading.Thread(target=worker, daemon=True)
    t.start()

