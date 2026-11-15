import os
from datetime import datetime, timedelta
from pathlib import Path

from .config import get_settings, UPLOADS_DIR
from .storage import get_all_clips, get_job, delete_clip, delete_clips_for_job

def _remove_file(path: str):
    if path and Path(path).exists():
        try:
            Path(path).unlink()
        except Exception:
            pass

def run_cleanup():
    """
    Applies the user's storage rules:
      - keep_last_clips: keep only the N newest clips
      - max_clip_age_days: delete clips older than X days
    Scheduled clips are never touched (they still need to be posted).
    Returns number of clips removed.
    """
    s = get_settings()
    keep_n = int(s.get("keep_last_clips") or 0)
    max_age = int(s.get("max_clip_age_days") or 0)
    if not keep_n and not max_age:
        return 0

    clips = get_all_clips()
    removable = set()

    if keep_n:
        active = [c for c in clips if c["status"] not in ("scheduled",)]
        # newest first is the default ordering from get_all_clips()
        for old in active[keep_n:]:
            removable.add(old["id"])

    if max_age:
        cutoff = datetime.now() - timedelta(days=max_age)
        for c in clips:
            if c["status"] == "scheduled":
                continue
            try:
                if datetime.fromisoformat(str(c["created_at"])) < cutoff:
                    removable.add(c["id"])
            except Exception:
                pass

    removed = 0
    for clip_id in list(removable):
        if delete_clip(clip_id):
            removed += 1
    if removed:
        print(f"[Cleanup] Removed {removed} old clip(s)")
    return removed

def delete_job_source(job_id: str):
    """Deletes the raw uploaded/downloaded source video of a completed job (clips are kept)."""
    job = get_job(job_id)
    if not job:
        return
    _remove_file(job.get("source_file"))
