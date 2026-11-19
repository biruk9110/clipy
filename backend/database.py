"""Persistent database layer for Clipy.

Supports two backends selected automatically:
  * PostgreSQL  - when DATABASE_URL is set (production: Render Postgres)
  * SQLite      - local development fallback (data/clipy.db)

All SQL is written with '?' placeholders and translated for psycopg2.
Timestamps are ISO strings; flags are INTEGER 0/1 - both work on both engines.
"""
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .config import BASE_DIR, DATA_DIR

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
# Normalize old-style Heroku/Render URLs (postgres:// -> postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
IS_PG = DATABASE_URL.startswith(("postgresql://", "postgresql+"))


def _new_pg():
    import psycopg2
    import psycopg2.extras
    kwargs = {"dsn": DATABASE_URL}
    if "sslmode" not in DATABASE_URL:
        kwargs["sslmode"] = "require"
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn, cur


DB_PATH = DATA_DIR / "clipy.db"


def _new_sqlite():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=45)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=45000")
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.cursor()
    return conn, cur


class Database:
    """Thin data-access wrapper with placeholder translation."""

    def __init__(self):
        self.DB_PATH = DATA_DIR / "clipy.db"

    @staticmethod
    def _q(sql: str) -> str:
        if IS_PG:
            return sql.replace("?", "%s")
        return sql

    def _open(self):
        return _new_pg() if IS_PG else _new_sqlite()

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        conn, cur = self._open()
        try:
            cur.execute(self._q(sql), params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.commit()
            conn.close()

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        conn, cur = self._open()
        try:
            cur.execute(self._q(sql), params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple | list = ()):
        row = self.one(sql, params)
        if not row:
            return None
        return next(iter(row.values()), None)

    def insert(self, table: str, values: dict) -> int:
        """Insert a row and return the new row's auto-generated id."""
        if not values:
            raise ValueError("insert() requires at least one column value")
        cols = ", ".join(values.keys())
        marks = ", ".join("?" for _ in values)
        if IS_PG:
            return int(self.scalar(
                f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING id",
                tuple(values.values())))
        conn, cur = self._open()
        try:
            cur.execute(self._q(f"INSERT INTO {table} ({cols}) VALUES ({marks})"),
                        tuple(values.values()))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def transaction(self):
        """Context manager exposing (conn, cur) that commits/rolls back."""
        return _Tx(self)

    # ---------- helpers ----------
    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def now() -> str:
        return datetime.now().isoformat(timespec="seconds")


class _Tx:
    def __init__(self, database: Database):
        self.db = database

    def __enter__(self):
        self.conn, self.cur = self.db._open()
        return self.conn, self.cur

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self.conn.close()
        return False


db = Database()

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Portable type placeholders resolved per backend.
if IS_PG:
    T_PK = "SERIAL PRIMARY KEY"
    T_BIGINT = "BIGINT"
    T_BLOB = "BYTEA"
else:
    T_PK = "INTEGER PRIMARY KEY AUTOINCREMENT"
    T_BIGINT = "INTEGER"
    T_BLOB = "BLOB"

SCHEMA = [
    # --- Users -------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS users (
        id {PK},
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        is_admin INTEGER NOT NULL DEFAULT 0,
        onboarding_done INTEGER NOT NULL DEFAULT 0,
        storage_used {BIGINT} NOT NULL DEFAULT 0,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        reset_token TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        user_agent TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS password_resets (
        id {PK},
        user_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    # --- Per-user settings (secrets stored encrypted) -----------------------
    """CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        prefs TEXT NOT NULL DEFAULT '{{}}',
        secrets TEXT NOT NULL DEFAULT '{{}}',
        updated_at TEXT
    )""",
    # --- Connected social accounts -----------------------------------------
    """CREATE TABLE IF NOT EXISTS connected_accounts (
        id {PK},
        user_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        account_id TEXT DEFAULT '',
        display_name TEXT DEFAULT '',
        access_token_enc TEXT DEFAULT '',
        refresh_token_enc TEXT DEFAULT '',
        expires_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        meta TEXT DEFAULT '{{}}',
        last_connected_at TEXT,
        created_at TEXT NOT NULL
    )""",
    # --- Projects (a user video + optional reference + its analysis) --------
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT DEFAULT 'Untitled project',
        status TEXT NOT NULL DEFAULT 'draft',
        source_type TEXT DEFAULT 'upload',
        source_url TEXT DEFAULT '',
        source_file TEXT DEFAULT '',
        source_duration REAL DEFAULT 0,
        source_width INTEGER DEFAULT 0,
        source_height INTEGER DEFAULT 0,
        source_size {BIGINT} DEFAULT 0,
        source_mime TEXT DEFAULT '',
        source_file_missing INTEGER NOT NULL DEFAULT 0,
        reference_url TEXT DEFAULT '',
        reference_file TEXT DEFAULT '',
        reference_platform TEXT DEFAULT '',
        reference_profile TEXT,
        editing_plan TEXT,
        last_job_id TEXT,
        error TEXT,
        pipeline TEXT DEFAULT 'default',
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""",
    # --- Editing jobs (persistent processing pipeline) ----------------------
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL DEFAULT 0,
        project_id TEXT,
        kind TEXT DEFAULT 'clips',
        status TEXT NOT NULL DEFAULT 'queued',
        step TEXT DEFAULT 'Queued',
        progress INTEGER DEFAULT 0,
        num_clips INTEGER DEFAULT 3,
        clip_duration INTEGER DEFAULT 45,
        aspect_ratio TEXT DEFAULT '9:16',
        resolution TEXT DEFAULT '720',
        caption_style TEXT DEFAULT 'karaoke',
        editing_intensity TEXT DEFAULT 'balanced',
        trim_silence INTEGER DEFAULT 0,
        interval_hours REAL DEFAULT 3,
        error TEXT,
        cancel_requested INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0,
        pipeline TEXT DEFAULT 'default',
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT
    )""",
    # --- Generated clips ----------------------------------------------------
    """CREATE TABLE IF NOT EXISTS clips (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL DEFAULT 0,
        job_id TEXT,
        project_id TEXT,
        title TEXT DEFAULT '',
        hook_text TEXT DEFAULT '',
        caption TEXT DEFAULT '',
        hashtags TEXT DEFAULT '[]',
        duration REAL DEFAULT 0,
        start_time REAL DEFAULT 0,
        end_time REAL DEFAULT 0,
        file_path TEXT DEFAULT '',
        thumb_path TEXT DEFAULT '',
        file_size {BIGINT} DEFAULT 0,
        width INTEGER DEFAULT 0,
        height INTEGER DEFAULT 0,
        aspect_ratio TEXT DEFAULT '9:16',
        score INTEGER DEFAULT 85,
        scheduled_time TEXT,
        status TEXT DEFAULT 'ready',
        tiktok_status TEXT DEFAULT 'pending',
        tiktok_post_url TEXT,
        ig_status TEXT DEFAULT 'pending',
        ig_post_url TEXT,
        youtube_status TEXT DEFAULT 'pending',
        youtube_post_url TEXT,
        error TEXT,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""",
    # --- Scheduled posts (publishing queue) ---------------------------------
    """CREATE TABLE IF NOT EXISTS scheduled_posts (
        id {PK},
        user_id INTEGER NOT NULL,
        clip_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result TEXT DEFAULT '',
        post_url TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        published_at TEXT
    )""",
    # --- Persistent file storage (videos survive ephemeral disks) -----------
    """CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL DEFAULT 0,
        mime TEXT DEFAULT '',
        size {BIGINT} DEFAULT 0,
        data {BLOB},
        created_at TEXT NOT NULL
    )""",
    # --- App-level metadata --------------------------------------------------
    """CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )""",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_clips_user ON clips (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_clips_job ON clips (job_id)",
    "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_posts (status, scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_sched_clips ON scheduled_posts (clip_id)",
]

LEGACY_COLUMNS = {
    "projects": [
        ("pipeline", "TEXT DEFAULT 'default'"),
    ],
    "jobs": [
        ("user_id", "INTEGER NOT NULL DEFAULT 0"), ("project_id", "TEXT"),
        ("kind", "TEXT DEFAULT 'clips'"), ("clip_duration", "INTEGER DEFAULT 45"),
        ("aspect_ratio", "TEXT DEFAULT '9:16'"), ("resolution", "TEXT DEFAULT '720'"),
        ("caption_style", "TEXT DEFAULT 'karaoke'"), ("editing_intensity", "TEXT DEFAULT 'balanced'"),
        ("trim_silence", "INTEGER DEFAULT 0"), ("attempts", "INTEGER DEFAULT 0"),
        ("started_at", "TEXT"), ("num_clips", "INTEGER DEFAULT 3"),
        ("interval_hours", "REAL DEFAULT 3"), ("cancel_requested", "INTEGER DEFAULT 0"),
        ("step", "TEXT DEFAULT 'Queued'"), ("progress", "INTEGER DEFAULT 0"),
        ("completed_at", "TEXT"), ("pipeline", "TEXT DEFAULT 'default'"),
    ],
    "clips": [
        ("user_id", "INTEGER NOT NULL DEFAULT 0"), ("project_id", "TEXT"),
        ("file_size", "BIGINT DEFAULT 0" if IS_PG else "INTEGER DEFAULT 0"),
        ("width", "INTEGER DEFAULT 0"), ("height", "INTEGER DEFAULT 0"),
        ("aspect_ratio", "TEXT DEFAULT '9:16'"), ("error", "TEXT"),
        ("thumb_path", "TEXT DEFAULT ''"),
        ("youtube_status", "TEXT DEFAULT 'pending'"), ("youtube_post_url", "TEXT"),
        ("file_missing", "INTEGER DEFAULT 0"), ("buffer_error", "TEXT"),
        ("updated_at", "TEXT"),
    ],
}


def _table_exists(cur, name: str) -> bool:
    if IS_PG:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s", (name,))
    else:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,))
    return cur.fetchone() is not None


def _columns(cur, table: str) -> set:
    if IS_PG:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
        return {r[0] if not isinstance(r, dict) else r["column_name"] for r in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def migrate_legacy_jobs():
    """One-time migration of the old pre-users schema into projects + jobs.

    Old `jobs` rows (no user_id) become projects so existing history survives
    the upgrade instead of disappearing.
    """
    with db.transaction() as (conn, cur):
        cols = _columns(cur, "jobs")
        if "user_id" in cols:
            return  # already migrated
        # Snapshot legacy rows (works for both sqlite Row and PG dicts).
        old_rows = []
        cur.execute("SELECT * FROM jobs")
        desc = [d[0] for d in cur.description]
        for r in cur.fetchall():
            old_rows.append(dict(zip(desc, list(r))))
        cur.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
        conn.commit()
    # Re-create the new-shape tables (CREATE IF NOT EXISTS will now build them)
    init_schema()
    # Map legacy jobs -> projects, and re-insert jobs with the new columns.
    for row in old_rows:
        jid = row.get("id") or db.new_id("job")
        created = row.get("created_at") or db.now()
        project_id = db.new_id("proj")
        db.execute(
            "INSERT INTO projects (id, user_id, name, status, source_type, source_url, source_file,"
            " source_duration, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, 0, row.get("title") or "Imported project", "completed",
             row.get("source_type") or "url", row.get("source_url") or "",
             row.get("source_file") or "", float(row.get("duration") or 0), created, created),
        )
        db.execute(
            "INSERT INTO jobs (id, user_id, project_id, status, step, progress, num_clips,"
            " interval_hours, error, created_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (jid, 0, project_id, row.get("status") or "completed", row.get("step") or "",
             int(row.get("progress") or 100), int(row.get("num_clips") or 3),
             float(row.get("interval_hours") or 3), row.get("error"),
             created, row.get("completed_at")),
        )
        db.execute("UPDATE clips SET user_id = 0, project_id = ? WHERE job_id = ? AND (project_id IS NULL OR project_id = '')",
                   (project_id, jid))
    try:
        db.execute("DROP TABLE IF EXISTS jobs_legacy")
    except Exception:
        pass


def init_schema():
    with db.transaction() as (conn, cur):
        for ddl in SCHEMA:
            cur.execute(db._q(ddl.format(PK=T_PK, BIGINT=T_BIGINT, BLOB=T_BLOB)))
        # Add any missing columns (covers upgrades from older versions).
        for table, columns in LEGACY_COLUMNS.items():
            if not _table_exists(cur, table):
                continue
            existing = _columns(cur, table)
            for col, ddl in columns:
                if col not in existing:
                    ddl_pg = ddl.replace("BIGINT", T_BIGINT)
                    try:
                        if IS_PG:
                            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_pg}")
                        else:
                            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    except Exception:
                        pass
        for idx in INDEXES:
            try:
                cur.execute(db._q(idx))
            except Exception:
                pass  # index may reference columns restored later by migration


def init_db():
    init_schema()
    if not IS_PG:
        migrate_legacy_jobs()
    # Recreate indexes after legacy migration brought columns in line
    with db.transaction() as (conn, cur):
        for idx in INDEXES:
            try:
                cur.execute(db._q(idx))
            except Exception:
                pass
