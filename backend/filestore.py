"""Persistent file storage for Clipy.

Videos and thumbnails are stored on the local disk for speed, AND mirrored
into the `files` database table when the deployment has no persistent disk
(Render free tier). On restart the disk cache is cold, but files are restored
from the database on first access.

Persistence mode (env CLIPY_PERSIST_FILES):
  auto  - (default) mirror to DB when DATABASE_URL / Render is detected
  disk  - local disk only (PC usage)
  db    - always mirror to DB
  off   - disk only, never DB

Files above CLIPY_MAX_PG_FILE_MB (default 64 MB) are never mirrored into the
database - they stay disk-only with a warning (uploads are usually transient).
"""
import os
import shutil
from pathlib import Path

from .config import BASE_DIR, DATA_DIR
from .database import db, IS_PG

PERSIST_MODE = (os.getenv("CLIPY_PERSIST_FILES") or "auto").lower()
if PERSIST_MODE == "auto":
    PERSIST_MODE = "db" if (IS_PG or os.getenv("RENDER")) else "disk"

MAX_PG_FILE = int(os.getenv("CLIPY_MAX_PG_FILE_MB", "64")) * 1024 * 1024

DB_ENABLED = PERSIST_MODE in ("db",)
MIME_DEFAULTS = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png", ".wav": "audio/wav"}


def rel_key(path: Path | str) -> str:
    """Stable key for a file: path relative to BASE_DIR (survives deploys)."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _abs(key: str) -> Path:
    return BASE_DIR / key


def save_file(path: Path | str, data: bytes, user_id: int = 0, mime: str = "") -> int:
    """Writes bytes to disk (and DB mirror when enabled). Returns size."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    size = len(data)
    if DB_ENABLED and size <= MAX_PG_FILE:
        key = rel_key(p)
        if not mime:
            mime = MIME_DEFAULTS.get(p.suffix.lower(), "application/octet-stream")
        db.execute("DELETE FROM files WHERE path = ?", (key,))
        db.execute(
            "INSERT INTO files (path, user_id, mime, size, data, created_at) VALUES (?,?,?,?,?,?)",
            (key, user_id, mime, size, data, db.now()),
        )
    return size


def save_from_stream(path: Path | str, stream, user_id: int = 0, mime: str = "",
                     max_bytes: int | None = None) -> dict:
    """Streams an upload to disk chunk-by-chunk (low memory), then mirrors to DB
    if small enough. Returns {'path', 'size', 'truncated', 'mirrored'}."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    truncated = False
    with open(p, "wb") as f:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            if max_bytes and size + len(chunk) > max_bytes:
                truncated = True
                break
            f.write(chunk)
            size += len(chunk)
    mirrored = False
    if DB_ENABLED and not truncated and size <= MAX_PG_FILE:
        try:
            data = p.read_bytes()
            key = rel_key(p)
            if not mime:
                mime = MIME_DEFAULTS.get(p.suffix.lower(), "application/octet-stream")
            db.execute("DELETE FROM files WHERE path = ?", (key,))
            db.execute(
                "INSERT INTO files (path, user_id, mime, size, data, created_at) VALUES (?,?,?,?,?,?)",
                (key, user_id, mime, size, data, db.now()),
            )
            mirrored = True
        except Exception:
            mirrored = False
    return {"path": str(p), "size": size, "truncated": truncated, "mirrored": mirrored}


def open_file(path: Path | str, user_id: int | None = None) -> Path | None:
    """Returns a local, playable file path, restoring from the DB mirror when
    the disk copy is gone (e.g. after a deploy restart). None = truly missing."""
    p = Path(path)
    if p.exists() and p.stat().st_size > 0:
        return p
    key = rel_key(p)
    row = db.one("SELECT data, size, mime FROM files WHERE path = ?", (key,))
    if not row or row.get("data") in (None, b""):
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = row["data"]
        if isinstance(data, memoryview):
            data = data.tobytes()
        p.write_bytes(data)
        return p
    except Exception:
        return None


def delete_file(path: Path | str) -> bool:
    """Removes a file from disk and DB mirror. Never raises."""
    removed = False
    p = Path(path)
    try:
        if p.exists():
            p.unlink()
            removed = True
    except Exception:
        pass
    try:
        db.execute("DELETE FROM files WHERE path = ?", (rel_key(p),))
    except Exception:
        pass
    return removed


def file_exists(path: Path | str, user_id: int | None = None) -> bool:
    p = Path(path)
    if p.exists() and p.stat().st_size > 0:
        return True
    return db.one("SELECT path FROM files WHERE path = ?", (rel_key(p),)) is not None


def add_storage(user_id: int, delta: int):
    if not user_id:
        return
    db.execute(
        "UPDATE users SET storage_used = MAX(0, storage_used + ?) WHERE id = ?",
        (delta, user_id),
    )


def db_storage_bytes() -> int:
    return int(db.scalar("SELECT COALESCE(SUM(size), 0) FROM files") or 0)


def db_file_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM files") or 0)


def prune_db_mirror(max_files: int | None = None):
    """Evicts the oldest DB-mirrored files when approaching a storage budget.
    Files stay referenced by projects; clips whose mirror is pruned fall back
    to 'missing file' state with re-render options. Returns bytes freed."""
    budget_mb = int(os.getenv("CLIPY_MAX_PG_TOTAL_MB", "900"))
    total = db_storage_bytes()
    budget = budget_mb * 1024 * 1024
    freed = 0
    if total <= budget:
        return freed
    rows = db.query("SELECT path, size FROM files ORDER BY created_at ASC")
    for r in rows:
        if total - freed <= budget:
            break
        db.execute("DELETE FROM files WHERE path = ?", (r["path"],))
        freed += int(r["size"] or 0)
    return freed
