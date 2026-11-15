import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .config import OUTPUT_DIR, UPLOADS_DIR, get_settings
from .storage import (
    create_job, update_job, add_clip, update_clip, 
    get_pending_scheduled_clips, get_clip, is_cancel_requested,
    delete_clips_for_job
)
from .video_processor import probe_video, download_video, render_clip
from .transcriber import extract_audio, transcribe_audio
from .gemini_director import pick_stream_moments, polish_short_video
from .posters.instagram import post_to_instagram
from .posters.tiktok import post_to_tiktok
from .posters.buffer import post_to_buffer
from .posters.youtube import post_to_youtube
from .oauth import get_base_url
from .maintenance import run_cleanup, delete_job_source

_scheduler_running = False

class JobCancelled(Exception):
    pass

def _check_cancel(job_id: str):
    """Cooperative cancellation point between processing stages."""
    if is_cancel_requested(job_id):
        raise JobCancelled(job_id)

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
    settings = get_settings()
    if settings.get("review_mode"):
        # User wants to approve each post manually - hold everything.
        return
    pending = get_pending_scheduled_clips()
    for clip in pending:
        publish_clip(clip["id"])

def publish_clip(clip_id: str):
    clip = get_clip(clip_id)
    if not clip or clip.get("status") == "posted":
        return
    
    video_path = Path(clip["file_path"])
    if not video_path.exists():
        update_clip(clip_id, status="failed", error="Video file missing")
        return
        
    settings = get_settings()
    ig_res = {"success": True, "mode": "skipped"}
    tiktok_res = {"success": True, "mode": "skipped"}
    youtube_res = {"success": True, "mode": "skipped"}
    buffer_res = {"success": False}
    
    attempted = 0
    succeeded = 0
    errors = []

    # Merge user's default hashtag preset into every caption
    default_tags = [t for t in (settings.get("default_hashtags") or "").replace(",", " ").split() if t]
    clip_tags = clip.get("hashtags") if isinstance(clip.get("hashtags"), list) else []
    final_hashtags = clip_tags + [t for t in default_tags if t not in clip_tags]

    # If Buffer is configured, post to Buffer (handles IG, TikTok & optionally YouTube)
    if settings.get("buffer_access_token"):
        attempted += 1
        video_url = f"{get_base_url()}/media/{video_path.name}"
        exclude = [] if settings.get("buffer_post_youtube", True) else ["youtube"]
        buffer_res = post_to_buffer(video_url, clip["caption"], final_hashtags, exclude_services=exclude)
        if buffer_res.get("success"):
            succeeded += 1
        else:
            errors.append(f"Buffer: {buffer_res.get('error', 'failed')}")

    if settings.get("auto_post_instagram", True):
        attempted += 1
        ig_res = post_to_instagram(video_path, clip["caption"], final_hashtags)
        if ig_res.get("success"):
            succeeded += 1
        else:
            errors.append(f"Instagram: {ig_res.get('error', 'failed')}")
        
    if settings.get("auto_post_tiktok", True):
        attempted += 1
        tiktok_res = post_to_tiktok(video_path, clip["caption"], final_hashtags)
        if tiktok_res.get("success"):
            succeeded += 1
        else:
            errors.append(f"TikTok: {tiktok_res.get('error', 'failed')}")

    if settings.get("auto_post_youtube") and (settings.get("youtube_access_token") or settings.get("youtube_refresh_token")):
        attempted += 1
        youtube_res = post_to_youtube(
            video_path, clip["caption"], final_hashtags,
            title=clip.get("hook_text") or clip.get("title") or "Clip"
        )
        if youtube_res.get("success"):
            succeeded += 1
        else:
            errors.append(f"YouTube: {youtube_res.get('error', 'failed')}")

    if attempted == 0:
        # Nothing is configured to auto-post - leave the clip ready for manual sharing.
        update_clip(clip_id, status="ready", error="No platform connected for auto-posting")
        return

    if succeeded == 0:
        update_clip(
            clip_id,
            status="failed",
            error=" | ".join(errors)[:500],
            ig_status="posted" if ig_res.get("success") and ig_res.get("mode") != "skipped" else ("skipped" if ig_res.get("mode") == "skipped" else "failed"),
            tiktok_status="posted" if tiktok_res.get("success") and tiktok_res.get("mode") != "skipped" else ("skipped" if tiktok_res.get("mode") == "skipped" else "failed"),
            youtube_status="posted" if youtube_res.get("success") and youtube_res.get("mode") != "skipped" else ("skipped" if youtube_res.get("mode") == "skipped" else "failed"),
        )
        return

    update_clip(
        clip_id,
        status="posted",
        error="",
        ig_status="posted" if ig_res.get("success") and ig_res.get("mode") != "skipped" else ("skipped" if ig_res.get("mode") == "skipped" else "failed"),
        ig_post_url=ig_res.get("url", ""),
        tiktok_status="posted" if tiktok_res.get("success") and tiktok_res.get("mode") != "skipped" else ("skipped" if tiktok_res.get("mode") == "skipped" else "failed"),
        tiktok_post_url=tiktok_res.get("url", ""),
        youtube_status="posted" if youtube_res.get("success") and youtube_res.get("mode") != "skipped" else ("skipped" if youtube_res.get("mode") == "skipped" else "failed"),
        youtube_post_url=youtube_res.get("url", ""),
    )

def process_job_async(job_id: str, source_type: str, source_url: str = "", file_path: str = "", num_clips: int = 3, interval_hours: float = 3.0):
    def worker():
        try:
            update_job(job_id, status="processing", step="📥 Downloading video...", progress=15)
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
            
            _check_cancel(job_id)
            update_job(job_id, duration=dur, is_short=1 if is_short else 0, step="🎙️ Listening & transcribing speech with Whisper...", progress=35)
            
            # Step 2: Extract & Transcribe Audio
            audio_path = job_dir / "audio.wav"
            extract_audio(video_file, audio_path)
            transcript_data = transcribe_audio(audio_path)
            
            _check_cancel(job_id)
            settings = get_settings()
            now = datetime.now()
            
            if is_short:
                # Polishing 1 short video
                update_job(job_id, step="🧠 Gemini AI is analyzing video & generating viral hook title...", progress=55)
                polish = polish_short_video(transcript_data["text"], dur)
                _check_cancel(job_id)
                clip_id = f"clip_{uuid.uuid4().hex[:8]}"
                out_clip = job_dir / f"{clip_id}.mp4"
                
                update_job(job_id, step="🎨 Editing 9:16 layout & burning animated subtitles...", progress=80)
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
                update_job(job_id, step="🧠 Gemini AI is searching for viral moments & writing hooks...", progress=50)
                moments = pick_stream_moments(transcript_data["text"], dur, count=num_clips)
                _check_cancel(job_id)
                for i, m in enumerate(moments):
                    _check_cancel(job_id)
                    clip_id = f"clip_{uuid.uuid4().hex[:8]}"
                    out_clip = job_dir / f"{clip_id}.mp4"
                    c_dur = m["end"] - m["start"]
                    
                    clip_progress = int(50 + ((i + 1) / len(moments)) * 45)
                    update_job(job_id, step=f"✂️ Editing clip {i+1} of {len(moments)}: {m.get('hook_text', '')[:30]}...", progress=clip_progress)
                    
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
                    
            update_job(job_id, status="completed", step="✅ Done! Clips ready to share & post", progress=100, completed_at=datetime.now().isoformat())
            
            # Optional: free disk space by removing the raw source video
            try:
                if settings.get("delete_source_after") and video_file and Path(video_file).exists():
                    Path(video_file).unlink()
            except Exception:
                pass
            run_cleanup()
            
        except JobCancelled:
            # Remove any half-finished output, then mark the job as cancelled
            delete_clips_for_job(job_id)
            update_job(job_id, status="cancelled", step="🚫 Cancelled by user", progress=0, error="Cancelled by user")
        except Exception as e:
            print(f"[Worker] Job {job_id} failed: {e}")
            update_job(job_id, status="failed", error=str(e))
            
    t = threading.Thread(target=worker, daemon=True)
    t.start()

