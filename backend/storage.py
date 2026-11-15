import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path
from .config import DATA_DIR

DB_PATH = DATA_DIR / "clipy.db"

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        title TEXT,
        source_type TEXT, -- url / upload
        source_url TEXT,
        source_file TEXT,
        duration REAL,
        is_short BOOLEAN DEFAULT 0,
        status TEXT, -- pending, processing, completed, failed
        step TEXT DEFAULT 'Starting...',
        progress INTEGER DEFAULT 0,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    )
    """)
    
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN step TEXT DEFAULT 'Starting...'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN progress INTEGER DEFAULT 0")
    except Exception:
        pass
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clips (
        id TEXT PRIMARY KEY,
        job_id TEXT,
        title TEXT,
        hook_text TEXT,
        caption TEXT,
        hashtags TEXT,
        duration REAL,
        start_time REAL,
        end_time REAL,
        file_path TEXT,
        thumbnail_path TEXT,
        score INTEGER,
        scheduled_time TIMESTAMP,
        status TEXT, -- ready, scheduled, posted, failed
        tiktok_status TEXT DEFAULT 'pending', -- pending, posted, failed, skipped
        tiktok_post_url TEXT,
        ig_status TEXT DEFAULT 'pending', -- pending, posted, failed, skipped
        ig_post_url TEXT,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(job_id) REFERENCES jobs(id)
    )
    """)
    conn.commit()
    conn.close()

init_db()

def create_job(job_id: str, title: str, source_type: str, source_url: str = "", source_file: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs (id, title, source_type, source_url, source_file, status, step, progress) VALUES (?, ?, ?, ?, ?, 'pending', '📥 Queued, preparing to download...', 5)",
        (job_id, title, source_type, source_url, source_file)
    )
    conn.commit()
    conn.close()

def update_job(job_id: str, **kwargs):
    conn = get_db()
    sets = [f"{k} = ?" for k in kwargs.keys()]
    values = list(kwargs.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()
    conn.close()

def get_job(job_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_jobs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_clip(clip_data: dict):
    conn = get_db()
    hashtags_str = json.dumps(clip_data.get("hashtags", [])) if isinstance(clip_data.get("hashtags"), list) else clip_data.get("hashtags", "[]")
    conn.execute("""
    INSERT INTO clips (
        id, job_id, title, hook_text, caption, hashtags, duration, start_time, end_time,
        file_path, thumbnail_path, score, scheduled_time, status, tiktok_status, ig_status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        clip_data["id"], clip_data["job_id"], clip_data.get("title", ""), clip_data.get("hook_text", ""),
        clip_data.get("caption", ""), hashtags_str, clip_data.get("duration", 0),
        clip_data.get("start_time", 0), clip_data.get("end_time", 0), clip_data.get("file_path", ""),
        clip_data.get("thumbnail_path", ""), clip_data.get("score", 85), clip_data.get("scheduled_time"),
        clip_data.get("status", "ready"), clip_data.get("tiktok_status", "pending"), clip_data.get("ig_status", "pending")
    ))
    conn.commit()
    conn.close()

def update_clip(clip_id: str, **kwargs):
    conn = get_db()
    if "hashtags" in kwargs and isinstance(kwargs["hashtags"], list):
        kwargs["hashtags"] = json.dumps(kwargs["hashtags"])
    sets = [f"{k} = ?" for k in kwargs.keys()]
    values = list(kwargs.values()) + [clip_id]
    conn.execute(f"UPDATE clips SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()
    conn.close()

def get_all_clips():
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, j.title as job_title, j.source_type 
        FROM clips c 
        LEFT JOIN jobs j ON c.job_id = j.id 
        ORDER BY c.created_at DESC
    """).fetchall()
    conn.close()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["hashtags"] = json.loads(item["hashtags"])
        except Exception:
            item["hashtags"] = []
        out.append(item)
    return out

def get_clip(clip_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    try:
        item["hashtags"] = json.loads(item["hashtags"])
    except Exception:
        item["hashtags"] = []
    return item

def get_pending_scheduled_clips():
    conn = get_db()
    now_iso = datetime.now().isoformat()
    rows = conn.execute("""
        SELECT * FROM clips 
        WHERE status = 'scheduled' 
        AND scheduled_time IS NOT NULL 
        AND scheduled_time <= ?
    """, (now_iso,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["hashtags"] = json.loads(item["hashtags"])
        except Exception:
            item["hashtags"] = []
        out.append(item)
    return out

