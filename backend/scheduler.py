"""Background processing pipeline.

Architecture:
  * Jobs are persisted rows (queued -> processing -> completed/failed/cancelled)
  * A single dispatcher thread picks queued jobs one at a time (Render free
    instances have ~512MB RAM - parallel renders would OOM-crash and kill the
    "disconnects while processing" bug again)
  * Cooperative cancellation between stages
  * Startup recovery: interrupted jobs are retried or failed with a clear error
  * Scheduled publishing loop (per-user accounts, never fakes success)
"""
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import OUTPUT_DIR
from .database import db
from .filestore import delete_file, file_exists, prune_db_mirror, save_from_stream, save_file
from .gemini_director import build_editing_plan, pick_stream_moments, polish_short_video
from .storage import (
    add_clip, get_clip, get_due_posts, get_job, get_queued_jobs, is_cancel_requested,
    list_clips, request_cancel, schedule_post, update_clip, update_job, update_project,
    update_scheduled,
)
from .transcriber import extract_audio, transcribe_audio
from .usersettings import get_prefs, get_secret
from .video_processor import download_video, probe_video, render_clip

_worker_started = False
_current_job = {"id": None}
MAX_CONCURRENT = 1


class JobCancelled(Exception):
    pass


def _check_cancel(job_id: str):
    if is_cancel_requested(job_id):
        raise JobCancelled(job_id)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def start_background_scheduler():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def dispatcher():
        # Give the web process a moment, then recover any interrupted jobs.
        time.sleep(3)
        try:
            recover_interrupted_jobs()
        except Exception as e:
            print(f"[Worker] Recovery error: {e}")
        while True:
            try:
                if _active_worker_count() < MAX_CONCURRENT:
                    queued = get_queued_jobs(1)
                    if queued:
                        job = queued[0]
                        t = threading.Thread(target=_run_job, args=(job["id"],), daemon=True)
                        t.start()
                        time.sleep(1)  # let it register as active before next pick
                    else:
                        time.sleep(3)
                else:
                    time.sleep(3)
            except Exception as e:
                print(f"[Worker] Dispatch error: {e}")
                time.sleep(5)

    def publisher():
        while True:
            try:
                check_and_publish_due_posts()
            except Exception as e:
                print(f"[Publisher] Loop error: {e}")
            time.sleep(30)

    threading.Thread(target=dispatcher, daemon=True).start()
    threading.Thread(target=publisher, daemon=True).start()


def _active_worker_count() -> int:
    return 1 if _current_job["id"] else 0


def recover_interrupted_jobs():
    """Jobs still queued/processing after a restart never hang forever."""
    rows = db.query("SELECT * FROM jobs WHERE status IN ('queued','processing')")
    for job in rows:
        if _current_job["id"] == job["id"]:
            continue
        project_id = job.get("project_id")
        project = db.one("SELECT * FROM projects WHERE id = ?", (project_id,)) if project_id else None
        src = (project or {}).get("source_file") or ""
        has_src = bool(src) and file_exists(src)
        attempts = int(job.get("attempts") or 0)
        if has_src and attempts < 2:
            update_job(job["id"], status="queued", step="Re-queued after restart",
                       progress=2, cancel_requested=0, attempts=attempts + 1)
            update_project(project_id, status="queued") if project_id else None
            print(f"[Worker] Re-queued interrupted job {job['id']}")
        else:
            reason = ("Source video was lost during a server restart - please re-upload it."
                      if not has_src else "Job interrupted repeatedly by server restarts.")
            update_job(job["id"], status="failed", step="Interrupted",
                       error=f"Interrupted by a server restart. {reason}")
            update_project(project_id, status="failed", error=reason) if project_id else None
            print(f"[Worker] Failed interrupted job {job['id']}")


# ---------------------------------------------------------------------------
# Main job pipeline
# ---------------------------------------------------------------------------
def _run_job(job_id: str):
    _current_job["id"] = job_id
    job = get_job(job_id)
    try:
        if job and job.get("kind") == "reference":
            _process_reference_job(job_id)
        else:
            process_job(job_id)
    except JobCancelled:
        db.execute("DELETE FROM clips WHERE job_id = ?", (job_id,))
        update_job(job_id, status="cancelled", step="Cancelled by user", progress=0,
                   error="Cancelled by user")
        project = db.one("SELECT id FROM projects WHERE last_job_id = ?", (job_id,))
        if project:
            update_project(project["id"], status="cancelled", error="Cancelled by user")
    except Exception as e:
        import traceback
        traceback.print_exc()
        msg = str(e)[:800]
        update_job(job_id, status="failed", step="Failed", error=msg)
        project = db.one("SELECT id FROM projects WHERE last_job_id = ?", (job_id,))
        if project:
            update_project(project["id"], status="failed", error=msg)
    finally:
        _current_job["id"] = None
        try:
            prune_db_mirror()
        except Exception:
            pass


def _process_reference_job(job_id: str):
    """Analyzes the reference video of a project (kind='reference')."""
    from .reference import ReferenceError, analyze
    job = get_job(job_id)
    if not job:
        return
    project_id = job.get("project_id")
    project = db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        update_job(job_id, status="failed", error="Project no longer exists")
        return
    user_id = int(job.get("user_id") or 0)
    update_job(job_id, status="processing", step="Retrieving reference video...", progress=15,
               started_at=db.now())
    update_project(project_id, status="analyzing_reference")

    work_dir = OUTPUT_DIR / f"ref_{project_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    local_file = None
    if project.get("reference_file"):
        from .filestore import open_file
        local_file = open_file(project["reference_file"], user_id)
        if not local_file and not project.get("reference_url"):
            raise RuntimeError(
                "The uploaded reference file is missing on the server. Upload it again.")
    res = analyze(user_id, url=project.get("reference_url") or "",
                  local_file=Path(local_file) if local_file else None,
                  work_dir=work_dir)

    if project.get("reference_url") and res.get("video_path"):
        update_project(project_id, reference_file=res["video_path"])

    if not res["ok"]:
        update_job(job_id, status="failed", step="Analysis failed",
                   error=res.get("error", "unknown"))
        update_project(project_id, status="draft", error=res.get("error", "")[:300])
        return

    profile = res["profile"]
    update_project(project_id,
                   reference_profile=json.dumps(profile, ensure_ascii=False),
                   status="draft", error=None)
    note = "" if profile.get("source") == "gemini" else " (heuristic - check API key)"
    update_job(job_id, status="completed",
               step=f"Reference analyzed: {res.get('platform') or 'video'}{note}",
               progress=100, completed_at=db.now())
    update_project(project_id, status="ready_to_edit")


def process_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return
    user_id = int(job.get("user_id") or 0)
    project = db.one("SELECT * FROM projects WHERE id = ?", (job.get("project_id"),))
    prefs = get_prefs(user_id) if user_id else {}

    update_job(job_id, status="processing", step="Preparing video...", progress=8,
               started_at=db.now(), cancel_requested=0)
    if project:
        update_project(project["id"], status="processing", error=None)

    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. Ingest source -------------------------------------------------
    source_file = (project or {}).get("source_file") or ""
    source_url = (project or {}).get("source_url") or ""
    video_file = None
    if (project or {}).get("source_type") == "upload" and source_file:
        if not file_exists(source_file, user_id):
            raise RuntimeError(
                "The uploaded source video is missing (it may have been cleaned up). "
                "Open the project and re-upload the video.")
        video_file = Path(source_file)
    elif source_url:
        update_job(job_id, step="Downloading source video...", progress=14)
        video_file, real_title = download_video(source_url, job_dir)
        if real_title and project:
            update_project(project["id"], name=real_title[:120])
    if not video_file or not Path(video_file).exists():
        raise RuntimeError("No source video available for this job.")

    _check_cancel(job_id)
    info = probe_video(video_file)
    dur = info["duration"] or 0
    is_vertical = info["aspect"] < 0.75
    if project:
        update_project(project["id"], source_duration=dur,
                       source_width=info["width"], source_height=info["height"])

    # ---- 2. Transcribe ------------------------------------------------------
    update_job(job_id, step="Transcribing speech (Whisper)...", progress=30)
    audio_path = job_dir / "audio.wav"
    extract_audio(video_file, audio_path)
    transcript_data = transcribe_audio(audio_path, prefs.get("whisper_model", "tiny"))

    _check_cancel(job_id)

    # ---- 3. Plan (reference-aware) ------------------------------------------
    reference_profile = (project or {}).get("reference_profile") or ""
    job_kind = job.get("kind") or "clips"
    aspect = job.get("aspect_ratio") or prefs.get("default_aspect_ratio", "9:16")
    resolution = job.get("resolution") or prefs.get("default_resolution", "720")
    caption_style = job.get("caption_style") or prefs.get("caption_style", "karaoke")
    intensity = job.get("editing_intensity") or prefs.get("editing_intensity", "balanced")
    trim_silence = bool(job.get("trim_silence")) or bool(prefs.get("trim_silence"))
    num_clips = int(job.get("num_clips") or prefs.get("default_num_clips", 3))
    clip_duration = int(job.get("clip_duration") or prefs.get("default_clip_duration", 45))
    interval_hours = float(job.get("interval_hours") or prefs.get("schedule_interval_hours", 3))

    plan = None
    # Single-clip polish (keeping whole video as 1 clip) only applies when user specifically asks for 1 clip
    # and the video is short (<= 90s). If user asks for 2+ clips on TikTok/Shorts, divide into distinct parts!
    single_short_mode = (num_clips == 1 and dur <= 90.0)

    if reference_profile:
        update_job(job_id, step="Applying reference editing style (AI)...", progress=45)
        clip_target = max(10, min(60, int(dur / max(1, num_clips)))) if dur < 180 else clip_duration
        min_d = max(8, int(clip_target * 0.7))
        max_d = min(int(dur), max(min_d + 10, int(clip_target * 1.35)))
        if single_short_mode:
            plan = {"clips": [{
                "start": 0.0, "end": dur, "hook_text": "", "caption": "", "hashtags": [],
                "score": 0, "zoom_intensity": 1, "transition": "hard_cut", "sfx_hint": "",
                "silence_gaps": []}], "global": {}}
        else:
            plan = build_editing_plan(
                transcript_data["text"], dur, transcript_data.get("language", ""),
                num_clips, min_d, max_d, trim_silence, user_id=user_id, project=project)
        if project:
            update_project(project["id"], editing_plan=json.dumps(plan, ensure_ascii=False))
    elif single_short_mode:
        update_job(job_id, step="AI is writing hook & caption...", progress=45)
        polish = polish_short_video(transcript_data["text"], dur, user_id=user_id, project=project)
        plan = {"clips": [{"start": 0.0, "end": dur, "hook_text": polish.get("hook_text", ""),
                           "caption": polish.get("caption", ""), "hashtags": polish.get("hashtags", []),
                           "score": polish.get("score", 85), "zoom_intensity": 1,
                           "transition": "hard_cut", "sfx_hint": "", "silence_gaps": []}],
                "global": {}}
    else:
        update_job(job_id, step="AI is searching for the best moments...", progress=45)
        clip_target = max(10, min(60, int(dur / max(1, num_clips)))) if dur < 180 else clip_duration
        min_d = max(8, int(clip_target * 0.7))
        max_d = min(int(dur), max(min_d + 10, int(clip_target * 1.35)))
        moments = pick_stream_moments(transcript_data["text"], dur, count=num_clips,
                                      min_d=min_d, max_d=max_d, user_id=user_id, project=project)
        plan = {"clips": moments, "global": {}}

    _check_cancel(job_id)
    clips_plan = plan.get("clips") or []
    if not clips_plan:
        raise RuntimeError("The AI could not find any moments to edit. Try a longer video.")

    # ---- 4. Render -----------------------------------------------------------
    now = datetime.now()
    created = []
    total = len(clips_plan)
    for i, m in enumerate(clips_plan):
        _check_cancel(job_id)
        start = max(0.0, float(m.get("start") or 0))
        end = min(dur if dur > 0 else m.get("end", start + 30), float(m.get("end") or (start + 30)))
        if end - start < 3:
            end = start + min(30, max(3, dur - start))
        c_dur = dur if single_short_mode else max(3.0, end - start)

        update_job(job_id, step=f"Rendering clip {i + 1} of {total}...",
                   progress=int(50 + (i / total) * 45))
        clip_id = db.new_id("clip")
        out_clip = job_dir / f"{clip_id}.mp4"
        try:
            render_clip(
                source_video=video_file,
                start_time=start,
                duration=c_dur,
                hook_text=m.get("hook_text") or f"Part {i + 1} — Watch till the END! 😱",
                words=transcript_data["words"],
                output_clip_path=out_clip,
                is_vertical_already=is_vertical,
                aspect=aspect,
                resolution=resolution,
                caption_style=caption_style if prefs.get("captions_enabled", True) else "none",
                captions_enabled=bool(prefs.get("captions_enabled", True)),
                hook_banner=bool(prefs.get("hook_banner", True)),
                zoom_intensity=int(m.get("zoom_intensity") or (1 if intensity != "light" else 0)),
                trim_silence=trim_silence and (dur > 90),
                audio_enhance=bool(prefs.get("audio_enhance", True)),
            )
        except RuntimeError as e:
            # A single clip failing shouldn't kill the whole job
            print(f"[Worker] Clip {i + 1} render failed: {e}")
            if not created:
                raise
            continue

        # Mirror rendered file into persistent storage
        out_size = 0
        try:
            data = out_clip.read_bytes()
            out_size = len(data)
            save_file(str(out_clip), data, user_id=user_id)
        except Exception as e:
            print(f"[Worker] Persist warning for {clip_id}: {e}")

        thumb = job_dir / f"{clip_id}_thumb.jpg"
        thumb_size = 0
        if thumb.exists():
            try:
                tdata = thumb.read_bytes()
                thumb_size = len(tdata)
                save_file(str(thumb), tdata, user_id=user_id)
            except Exception:
                pass

        sched_time = (now + timedelta(hours=i * interval_hours)).isoformat(timespec="seconds")
        add_clip({
            "id": clip_id, "user_id": user_id, "job_id": job_id,
            "project_id": (project or {}).get("id"),
            "title": m.get("title") or m.get("hook_text") or f"Clip {i + 1}",
            "hook_text": m.get("hook_text", ""),
            "caption": m.get("caption", ""),
            "hashtags": m.get("hashtags", []),
            "duration": round(float(out_clip_probe_duration(out_clip) or c_dur), 2),
            "start_time": start, "end_time": end,
            "file_path": str(out_clip),
            "thumbnail_path": str(thumb) if thumb.exists() else "",
            "width": 720 if str(resolution) == "720" else 1080,
            "height": 1280 if (str(resolution) == "720" and aspect == "9:16") else 0,
            "aspect_ratio": aspect,
            "score": m.get("score", 85),
            "scheduled_time": sched_time,
            "status": "ready",
        })
        created.append(clip_id)
        if prefs.get("auto_publish") and i == 0:
            try:
                schedule_post(user_id, clip_id, "all", sched_time)
            except Exception:
                pass

    # ---- 5. Done ---------------------------------------------------------------
    if not created:
        raise RuntimeError("No clips could be rendered from this video.")

    update_job(job_id, status="completed",
               step=f"Done! {len(created)} clip(s) ready", progress=100,
               completed_at=db.now())
    if project:
        update_project(project["id"], status="completed", last_job_id=job_id, error=None)

    # Optional source cleanup (free disk space)
    try:
        prefs_delete = prefs.get("delete_source_after")
        if prefs_delete and (project or {}).get("source_type") == "upload" and source_file:
            delete_file(source_file)
            update_project(project["id"], source_file="")
            db.execute("UPDATE projects SET source_file_missing = 1 WHERE id = ?", (project["id"],))
    except Exception:
        pass


def out_clip_probe_duration(path: Path) -> float:
    from .video_processor import probe_video
    try:
        return probe_video(path).get("duration") or 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def check_and_publish_due_posts():
    due = get_due_posts()
    for post in due:
        _publish_scheduled_post(post)


def _publish_scheduled_post(post: dict):
    post_id = post["id"]
    clip_id = post["clip_id"]
    user_id = post["user_id"]
    clip = get_clip(clip_id)
    if not clip:
        update_scheduled(post_id, status="failed", error="Clip no longer exists",
                         published_at=db.now())
        return
    update_scheduled(post_id, status="publishing")
    res = publish_clip(clip_id, user_id, platform=post.get("platform") or "all")
    if res.get("ok"):
        update_scheduled(post_id, status="published",
                         result=res.get("message", ""), post_url=res.get("url", ""),
                         published_at=db.now())
    else:
        update_scheduled(post_id, status="failed", error=res.get("error", "publish failed"))


def publish_clip(clip_id: str, user_id: int, platform: str = "all", manual: bool = False) -> dict:
    """Publishes a clip to the user's connected platforms. Never fakes success."""
    clip = get_clip(clip_id, user_id)
    if not clip:
        return {"ok": False, "error": "Clip not found"}
    if clip.get("status") == "posted" and not manual:
        return {"ok": True, "message": "Already published", "url": ""}

    from .filestore import open_file
    video_path = open_file(clip.get("file_path") or "", user_id)
    if not video_path or not video_path.exists():
        update_clip(clip_id, status="failed", error="Video file missing on server")
        return {"ok": False, "error": "Video file missing on server"}

    from .accounts import get_accounts, refresh_account_token
    from .posters.buffer import post_to_buffer
    from .posters.instagram import post_to_instagram
    from .posters.tiktok import post_to_tiktok
    from .posters.youtube import post_to_youtube
    from .oauth import get_base_url

    accounts = {a["platform"]: a for a in get_accounts(user_id)}
    prefs = get_prefs(user_id) if user_id else {}
    hashtags = clip.get("hashtags") if isinstance(clip.get("hashtags"), list) else []
    default_tags = [t for t in (prefs.get("default_hashtags") or "").replace(",", " ").split() if t]
    final_tags = hashtags + [t for t in default_tags if t not in hashtags]
    caption = clip.get("caption") or ""
    title = clip.get("hook_text") or clip.get("title") or "Clip"

    attempted, succeeded, errors, urls = 0, 0, [], {}
    try:
        rel_media = video_path.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
        video_url = f"{get_base_url()}/media/{rel_media}"
    except Exception:
        video_url = f"{get_base_url()}/media/{video_path.parent.name}/{video_path.name}"

    # Buffer first (covers IG/TikTok/YT in one step if connected)
    buffer_acc = accounts.get("buffer")
    if buffer_acc and (platform in ("all", "buffer")):
        attempted += 1
        acc = refresh_account_token(buffer_acc)
        token = (acc.get("access_token") if isinstance(acc, dict) else str(acc or "")) or get_secret(user_id, "buffer_access_token")
        if token:
            res = post_to_buffer(token, video_url, caption, final_tags)
            if res.get("success"):
                succeeded += 1
                urls["buffer"] = res.get("channels", [])
                update_clip(clip_id, ig_status="posted", tiktok_status="posted")
                for svc in res.get("channels", []):
                    if "instagram" in str(svc).lower():
                        update_clip(clip_id, ig_status="posted")
                    if "tiktok" in str(svc).lower():
                        update_clip(clip_id, tiktok_status="posted")
                    if "youtube" in str(svc).lower():
                        update_clip(clip_id, youtube_status="posted")
            else:
                errors.append(f"Buffer: {res.get('error', 'failed')}")
                update_clip(clip_id, buffer_error=res.get("error", "")[:300])
        else:
            errors.append("Buffer: token missing")

    auto_platforms = prefs.get("auto_post_platforms") or {}
    want = {
        "instagram": bool(auto_platforms.get("instagram")) or manual,
        "tiktok": bool(auto_platforms.get("tiktok")) or manual,
        "youtube": bool(auto_platforms.get("youtube")) or manual,
    }
    if platform != "all":
        want = {k: (k == platform) for k in want}

    if want.get("instagram") and accounts.get("instagram") and platform in ("all", "instagram"):
        attempted += 1
        acc = refresh_account_token(accounts["instagram"])
        res = post_to_instagram(acc, video_url, clip["file_path"], caption, final_tags)
        if res.get("success"):
            succeeded += 1
            urls["instagram"] = res.get("url", "")
            update_clip(clip_id, ig_status="posted", ig_post_url=res.get("url", ""))
        else:
            errors.append(f"Instagram: {res.get('error', 'failed')}")
            update_clip(clip_id, ig_status="failed")

    if want.get("tiktok") and accounts.get("tiktok") and platform in ("all", "tiktok"):
        attempted += 1
        acc = refresh_account_token(accounts["tiktok"])
        res = post_to_tiktok(acc, clip["file_path"], caption, final_tags)
        if res.get("success"):
            succeeded += 1
            urls["tiktok"] = res.get("url", "")
            update_clip(clip_id, tiktok_status="posted", tiktok_post_url=res.get("url", ""))
        else:
            errors.append(f"TikTok: {res.get('error', 'failed')}")
            update_clip(clip_id, tiktok_status="failed")

    if want.get("youtube") and accounts.get("youtube") and platform in ("all", "youtube"):
        attempted += 1
        acc = refresh_account_token(accounts["youtube"])
        res = post_to_youtube(acc, clip["file_path"], caption, final_tags, title=title)
        if res.get("success"):
            succeeded += 1
            urls["youtube"] = res.get("url", "")
            update_clip(clip_id, youtube_status="posted", youtube_post_url=res.get("url", ""))
        else:
            errors.append(f"YouTube: {res.get('error', 'failed')}")
            update_clip(clip_id, youtube_status="failed")

    if attempted == 0:
        msg = ("No publishing platform is connected. Connect TikTok, Instagram, "
               "YouTube or Buffer in Connected Accounts first.")
        if manual:
            update_clip(clip_id, status="ready", error=msg)
        return {"ok": False, "error": msg}

    if succeeded == 0:
        msg = " | ".join(errors)[:500]
        update_clip(clip_id, status="failed", error=msg)
        return {"ok": False, "error": msg}

    still_pending = [e for e in errors]
    update_clip(clip_id, status="posted", error=" | ".join(still_pending)[:300])
    return {"ok": True, "message": f"Published to {succeeded} destination(s)",
            "url": next(iter(urls.values()), ""), "urls": urls,
            "warnings": still_pending or None}
