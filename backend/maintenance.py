"""Storage maintenance: cleanup policies, orphan detection, storage reporting.

Cleanup NEVER deletes files still referenced by active jobs or retained
projects. Database-mirrored files keep metadata alive even when the raw file
is gone - the UI then offers re-upload instead of a broken preview.
"""
import os
from datetime import datetime, timedelta
from pathlib import Path

from .config import OUTPUT_DIR, UPLOADS_DIR
from .database import db
from .filestore import db_file_count, db_storage_bytes, delete_file, rel_key
from .storage import list_clips, update_clip


def run_cleanup(user_id: int | None = None) -> int:
    """Applies the user's retention rules to generated clips.
    Scheduled clips are never touched (they still need to post)."""
    from .usersettings import get_prefs
    if user_id:
        prefs = get_prefs(user_id)
        keep_n = int(prefs.get("keep_last_clips") or 0)
        max_age = int(prefs.get("max_clip_age_days") or 0)
    else:
        return 0

    clips = list_clips(user_id, limit=1000)
    removable = set()
    if keep_n:
        active = [c for c in clips if c["status"] != "scheduled"]
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
    for clip_id in removable:
        clip = next((c for c in clips if c["id"] == clip_id), None)
        if clip:
            for key in ("file_path", "thumb_path"):
                if clip.get(key):
                    delete_file(clip[key])
            db.execute("DELETE FROM scheduled_posts WHERE clip_id = ?", (clip_id,))
            db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
            removed += 1
    return removed


def detect_missing_files(user_id: int) -> list[dict]:
    """Clips whose local file is gone (DB mirror will restore them lazily).
    Only flagged when the mirror is also empty."""
    from .filestore import file_exists
    missing = []
    for clip in list_clips(user_id, limit=1000):
        p = clip.get("file_path")
        if p and not file_exists(p, user_id):
            missing.append({"clip_id": clip["id"], "title": clip.get("title") or clip.get("hook_text")})
    return missing


def orphan_scan() -> list[str]:
    """Output files on disk that no clip row references anymore."""
    referenced = set()
    for sql in ("SELECT file_path AS p FROM clips WHERE file_path != ''",
                "SELECT thumb_path AS p FROM clips WHERE thumb_path != ''",
                "SELECT source_file AS p FROM projects WHERE source_file != ''",
                "SELECT reference_file AS p FROM projects WHERE reference_file != ''"):
        for r in db.query(sql):
            if r.get("p"):
                referenced.add(rel_key(r["p"]))
    orphans = []
    for base in (OUTPUT_DIR, UPLOADS_DIR):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and rel_key(p) not in referenced:
                try:
                    orphans.append(str(p))
                except Exception:
                    pass
    return orphans


def remove_orphans(older_than_hours: int = 24) -> int:
    """Deletes orphaned files older than the threshold (never referenced)."""
    cutoff = datetime.now() - timedelta(hours=older_than_hours)
    removed = 0
    for p in orphan_scan():
        try:
            if datetime.fromtimestamp(Path(p).stat().st_mtime) < cutoff:
                Path(p).unlink()
                removed += 1
        except Exception:
            pass
    return removed


def storage_usage(user_id: int) -> dict:
    clips = list_clips(user_id, limit=1000)
    clips_bytes = sum(int(c.get("file_size") or 0) for c in clips)
    mirrored = db_storage_bytes()
    return {
        "clips_bytes": clips_bytes,
        "clips_count": len(clips),
        "db_mirror_bytes": mirrored,
        "db_mirror_files": db_file_count(),
        "quota_mb": int(os.getenv("CLIPY_MAX_PG_TOTAL_MB", "900")),
    }
