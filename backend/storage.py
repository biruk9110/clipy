"""Persistent storage access for projects, jobs, clips, scheduled posts,
history and analytics. Every user-facing function is user-scoped."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from .database import db
from .filestore import delete_file, file_exists


def _json_tags(value):
    try:
        return json.loads(value) if isinstance(value, str) else (value or [])
    except Exception:
        return []


def _norm_clip(row: dict) -> dict:
    if not row:
        return None
    item = dict(row)
    item["hashtags"] = _json_tags(item.get("hashtags"))
    return item


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def create_project(user_id: int, name: str, source_type: str = "upload",
                   source_url: str = "", source_file: str = "", **extra) -> str:
    pid = db.new_id("proj")
    now = db.now()
    fields = {
        "source_duration": float(extra.get("source_duration") or 0),
        "source_width": int(extra.get("source_width") or 0),
        "source_height": int(extra.get("source_height") or 0),
        "source_size": int(extra.get("source_size") or 0),
        "source_mime": extra.get("source_mime") or "",
        "reference_url": extra.get("reference_url") or "",
        "reference_file": extra.get("reference_file") or "",
        "reference_platform": extra.get("reference_platform") or "",
    }
    db.execute(
        "INSERT INTO projects (id, user_id, name, status, source_type, source_url, source_file,"
        " source_duration, source_width, source_height, source_size, source_mime,"
        " reference_url, reference_file, reference_platform, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, user_id, name or "Untitled project", "draft", source_type, source_url,
         source_file, fields["source_duration"], fields["source_width"],
         fields["source_height"], fields["source_size"], fields["source_mime"],
         fields["reference_url"], fields["reference_file"], fields["reference_platform"],
         now, now))
    return pid


def get_project(project_id: str, user_id: int | None = None) -> dict | None:
    row = db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        return None
    return dict(row)


def update_project(project_id: str, **kwargs):
    kwargs["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    db.execute(f"UPDATE projects SET {sets} WHERE id = ?",
               list(kwargs.values()) + [project_id])


def list_projects(user_id: int, limit: int = 50, offset: int = 0,
                  search: str = "", status: str = "") -> list[dict]:
    sql = "SELECT * FROM projects WHERE 1=1"
    params: list = []
    if search:
        sql += " AND (name LIKE ? OR reference_url LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return db.query(sql, params)


def count_projects(user_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM projects WHERE user_id = ?", (user_id,)) or 0)


def delete_project(project_id: str, user_id: int):
    """Deletes the project, its jobs and clips (files included)."""
    jobs = db.query("SELECT id FROM jobs WHERE project_id = ?", (project_id,))
    for j in jobs:
        delete_clips_for_job(j["id"], user_id=user_id, delete_files=True)
        db.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?", (j["id"], user_id))
    proj = get_project(project_id, user_id)
    if proj:
        for key in ("source_file", "reference_file"):
            if proj.get(key):
                delete_file(proj[key])
    db.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def create_job(user_id: int, project_id: str, num_clips: int = 3,
               clip_duration: int = 45, aspect_ratio: str = "9:16",
               resolution: str = "720", caption_style: str = "karaoke",
               editing_intensity: str = "balanced", trim_silence: bool = False,
               interval_hours: float = 3.0, kind: str = "clips",
               pipeline: str = "default") -> str:
    job_id = db.new_id("job")
    db.execute(
        "INSERT INTO jobs (id, user_id, project_id, kind, status, step, progress, num_clips,"
        " clip_duration, aspect_ratio, resolution, caption_style, editing_intensity,"
        " trim_silence, interval_hours, pipeline, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, user_id, project_id, kind, "queued", "Queued for processing", 2,
         num_clips, clip_duration, aspect_ratio, resolution, caption_style,
         editing_intensity, 1 if trim_silence else 0, interval_hours, pipeline or "default", db.now()))
    return job_id


def get_job(job_id: str, user_id: int | None = None) -> dict | None:
    row = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None
    return dict(row)


def update_job(job_id: str, **kwargs):
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", list(kwargs.values()) + [job_id])


def list_jobs(user_id: int, limit: int = 30, offset: int = 0) -> list[dict]:
    return db.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset))


def get_active_jobs(user_id: int) -> list[dict]:
    return db.query(
        "SELECT * FROM jobs WHERE status IN ('queued','processing')"
        " ORDER BY created_at ASC")


def get_queued_jobs(limit: int = 1) -> list[dict]:
    """Worker queue: oldest queued jobs first."""
    return db.query("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?",
                    (limit,))


def request_cancel(job_id: str):
    update_job(job_id, cancel_requested=1)


def is_cancel_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("cancel_requested"))


def delete_job(job_id: str, user_id: int, delete_files: bool = True):
    delete_clips_for_job(job_id, user_id=user_id, delete_files=delete_files)
    db.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id))


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------
def add_clip(data: dict) -> str:
    clip_id = data.get("id") or db.new_id("clip")
    now = db.now()
    hashtags = data.get("hashtags")
    hashtags_str = json.dumps(hashtags, ensure_ascii=False) if isinstance(hashtags, list) else (hashtags or "[]")
    size = 0
    p = data.get("file_path")
    if p and Path(p).exists():
        try:
            size = Path(p).stat().st_size
        except Exception:
            size = 0
    db.execute(
        "INSERT INTO clips (id, user_id, job_id, project_id, title, hook_text, caption, hashtags,"
        " duration, start_time, end_time, file_path, thumb_path, file_size, width, height,"
        " aspect_ratio, score, scheduled_time, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (clip_id, data.get("user_id", 0), data.get("job_id"), data.get("project_id"),
         data.get("title", ""), data.get("hook_text", ""), data.get("caption", ""),
         hashtags_str, data.get("duration", 0), data.get("start_time", 0),
         data.get("end_time", 0), data.get("file_path", ""), data.get("thumbnail_path", ""),
         size, data.get("width", 0), data.get("height", 0),
         data.get("aspect_ratio", "9:16"), data.get("score", 85),
         data.get("scheduled_time"), data.get("status", "ready"), now, now))
    return clip_id


def update_clip(clip_id: str, **kwargs):
    if "hashtags" in kwargs and isinstance(kwargs["hashtags"], list):
        kwargs["hashtags"] = json.dumps(kwargs["hashtags"], ensure_ascii=False)
    kwargs["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    db.execute(f"UPDATE clips SET {sets} WHERE id = ?", list(kwargs.values()) + [clip_id])


def get_clip(clip_id: str, user_id: int | None = None) -> dict | None:
    row = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not row:
        return None
    return _norm_clip(row)


def list_clips(user_id: int, limit: int = 200, offset: int = 0,
               status: str = "", search: str = "") -> list[dict]:
    sql = "SELECT c.*, j.project_id AS job_project FROM clips c LEFT JOIN jobs j ON c.job_id = j.id WHERE 1=1"
    params: list = []
    if status:
        sql += " AND c.status = ?"
        params.append(status)
    if search:
        sql += " AND (c.title LIKE ? OR c.hook_text LIKE ? OR c.caption LIKE ?)"
        params += [f"%{search}%"] * 3
    sql += " ORDER BY c.created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [_norm_clip(r) for r in db.query(sql, params)]


def count_clips(user_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM clips") or 0)


def get_clip_files(clip_id: str, user_id: int | None = None):
    clip = get_clip(clip_id, user_id)
    if not clip:
        return []
    files = []
    for key in ("file_path", "thumb_path"):
        p = clip.get(key)
        if p:
            files.append(p)
    return files


def delete_clip(clip_id: str, user_id: int | None = None) -> bool:
    clip = get_clip(clip_id, user_id)
    if not clip:
        return False
    for key in ("file_path", "thumb_path"):
        if clip.get(key):
            delete_file(clip[key])
    db.execute("DELETE FROM scheduled_posts WHERE clip_id = ?", (clip_id,))
    db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    return True


def delete_clips_for_job(job_id: str, user_id: int | None = None, delete_files: bool = True) -> int:
    rows = db.query("SELECT id, file_path, thumb_path FROM clips WHERE job_id = ?", (job_id,))
    count = 0
    for r in rows:
        if user_id is not None and int(r.get("user_id") or 0) != int(user_id):
            continue
        if delete_files:
            for key in ("file_path", "thumb_path"):
                if r.get(key):
                    delete_file(r[key])
        db.execute("DELETE FROM clips WHERE id = ?", (r["id"],))
        count += 1
    return count


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def get_history(user_id: int, search: str = "", status: str = "",
                days: int = 0, limit: int = 20, offset: int = 0) -> dict:
    """Projects + jobs merged into history items with search/filter/pagination."""
    sql = ("SELECT j.*, p.name AS project_name, p.reference_url, p.reference_platform,"
           " p.source_file AS project_source, p.source_url AS project_source_url,"
           " (SELECT COUNT(*) FROM clips c WHERE c.job_id = j.id) AS clip_count,"
           " (SELECT SUM(c.file_size) FROM clips c WHERE c.job_id = j.id) AS total_size"
           " FROM jobs j LEFT JOIN projects p ON j.project_id = p.id WHERE j.user_id = ?")
    params: list = [user_id]
    if status:
        sql += " AND j.status = ?"
        params.append(status)
    if search:
        sql += " AND (p.name LIKE ? OR j.id LIKE ? OR p.reference_url LIKE ?)"
        params += [f"%{search}%"] * 3
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        sql += " AND j.created_at >= ?"
        params.append(cutoff)
    count_params = [user_id]
    if status:
        count_params.append(status)
    if search:
        count_params += [f"%{search}%"] * 3
    if days:
        count_params.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    total = db.scalar(
        "SELECT COUNT(*) FROM jobs j LEFT JOIN projects p ON j.project_id = p.id WHERE j.user_id = ?"
        + (" AND j.status = ?" if status else "")
        + (" AND (p.name LIKE ? OR j.id LIKE ? OR p.reference_url LIKE ?)" if search else "")
        + (" AND j.created_at >= ?" if days else ""), count_params)
    sql += " ORDER BY j.created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    items = []
    for row in db.query(sql, params):
        item = dict(row)
        item["duration_sec"] = None
        if item.get("started_at") and item.get("completed_at"):
            try:
                item["duration_sec"] = round(
                    (datetime.fromisoformat(item["completed_at"])
                     - datetime.fromisoformat(item["started_at"])).total_seconds())
            except Exception:
                pass
        items.append(item)
    return {"items": items, "total": int(total or 0), "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Scheduled posts
# ---------------------------------------------------------------------------
def schedule_post(user_id: int, clip_id: str, platform: str, scheduled_at: str) -> int:
    res = db.scalar(
        "INSERT INTO scheduled_posts (user_id, clip_id, platform, scheduled_at, status, created_at)"
        " VALUES (?,?,?,?, 'pending', ?) RETURNING id", (user_id, clip_id, platform, scheduled_at, db.now()))
    return res


def list_scheduled(user_id: int, status: str = "", limit: int = 100) -> list[dict]:
    sql = ("SELECT s.*, c.title AS clip_title, c.hook_text, c.file_path, c.status AS clip_status"
           " FROM scheduled_posts s LEFT JOIN clips c ON s.clip_id = c.id WHERE s.user_id = ?")
    params: list = [user_id]
    if status:
        sql += " AND s.status = ?"
        params.append(status)
    sql += " ORDER BY s.scheduled_at ASC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def update_scheduled(post_id: int, **kwargs):
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    db.execute(f"UPDATE scheduled_posts SET {sets} WHERE id = ?", list(kwargs.values()) + [post_id])


def get_due_posts() -> list[dict]:
    return db.query(
        "SELECT * FROM scheduled_posts WHERE status = 'pending' AND scheduled_at <= ? ORDER BY scheduled_at ASC",
        (db.now(),))


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
def get_clips_per_day(user_id: int, days: int = 7) -> list[dict]:
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count FROM clips"
        " WHERE user_id = ? AND substr(created_at, 1, 10) >= ? GROUP BY day ORDER BY day ASC",
        (user_id, since))
    counts = {r["day"]: r["count"] for r in rows}
    out = []
    for i in range(days):
        d = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        out.append({"day": d, "count": counts.get(d, 0)})
    return out


def get_connection_counts(user_id: int) -> dict:
    row = db.one(
        "SELECT SUM(CASE WHEN ig_status = 'posted' THEN 1 ELSE 0 END) AS ig,"
        " SUM(CASE WHEN tiktok_status = 'posted' THEN 1 ELSE 0 END) AS tiktok,"
        " SUM(CASE WHEN youtube_status = 'posted' THEN 1 ELSE 0 END) AS youtube,"
        " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed"
        " FROM clips WHERE user_id = ?", (user_id,))
    d = dict(row) if row else {}
    return {k: (d.get(k) or 0) for k in ("ig", "tiktok", "youtube", "failed")}


def analytics_summary(user_id: int) -> dict:
    clips = db.query("SELECT status, score FROM clips WHERE user_id = ?", (user_id,))
    total = len(clips)
    by = {}
    for c in clips:
        by[c["status"]] = by.get(c["status"], 0) + 1
    scores = [c["score"] for c in clips if c.get("score")]
    posted = by.get("posted", 0) + by.get("published", 0)
    failed = by.get("failed", 0)
    return {
        "total_clips": total,
        "posted": posted,
        "scheduled": by.get("scheduled", 0),
        "ready": by.get("ready", 0),
        "failed": failed,
        "avg_score": round(sum(scores) / len(scores)) if scores else 0,
        "success_rate": round(posted / (posted + failed) * 100) if (posted + failed) else None,
    }
