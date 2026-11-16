"""Encrypts secrets (API keys, OAuth tokens) at rest.

Key resolution order:
  1. SECRET_KEY env var (recommended for production - set it in Render)
  2. Auto-generated key persisted in the app's own database (bootstrap)
"""
import base64
import os

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _load_key() -> bytes:
    env = (os.getenv("SECRET_KEY") or "").strip()
    if env:
        # Accept any passphrase: derive a stable 32-byte Fernet key from it.
        digest = __import__("hashlib").sha256(env.encode()).digest()
        return base64.urlsafe_b64encode(digest)
    # Bootstrap: generate once, persist via the database layer (app_meta).
    from .database import db, IS_PG
    row = db.one("SELECT value FROM app_meta WHERE key = ?", ("fernet_key",))
    if row and row.get("value"):
        return row["value"].encode()
    key = Fernet.generate_key()
    try:
        db.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING"
            if IS_PG else
            "INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)",
            ("fernet_key", key.decode()),
        )
    except Exception:
        pass
    return key


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(value: str) -> str:
    """Encrypts a secret for storage. Empty values stay empty."""
    if not value:
        return ""
    try:
        return _get().encrypt(str(value).encode()).decode()
    except Exception:
        return ""


def decrypt(value: str) -> str:
    """Decrypts a stored secret. Empty or undecryptable values return empty."""
    if not value:
        return ""
    try:
        return _get().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        return ""


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return value[:4] + "•••••" + value[-3:]


def is_masked(value: str) -> bool:
    """True when the client echoed a masked value back (contains our bullet separator)."""
    return isinstance(value, str) and "•••••" in value
