"""User authentication: registration, login, persistent DB-backed sessions,
password reset, and per-user isolation helpers."""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from .database import db

SESSION_DAYS = 30
COOKIE_NAME = "clipy_session"
PBKDF2_ITER = 240_000


# ---------------------------------------------------------------------------
# Passwords (PBKDF2-HMAC-SHA256, salted - no extra dependencies)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
EMAIL_DOMAIN = "example"  # only used for validation messaging


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    import re
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", email or ""))


def register(name: str, email: str, password: str) -> dict:
    email = normalize_email(email)
    if not valid_email(email):
        return {"success": False, "error": "Please enter a valid email address."}
    if len(password) < 8:
        return {"success": False, "error": "Password must be at least 8 characters."}
    if not (name or "").strip():
        return {"success": False, "error": "Please enter your name."}
    if db.one("SELECT id FROM users WHERE email = ?", (email,)):
        return {"success": False, "error": "An account with this email already exists."}
    return _register_insert(name, email, password)


def _register_insert(name: str, email: str, password: str) -> dict:
    from .database import IS_PG
    created = db.now()
    if IS_PG:
        user_id = db.scalar(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (?,?,?,?) RETURNING id",
            (name.strip(), email, hash_password(password), created))
    else:
        db.execute(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (?,?,?,?)",
            (name.strip(), email, hash_password(password), created))
        user_id = db.scalar("SELECT id FROM users WHERE email = ?", (email,))
    db.execute(
        "INSERT INTO user_settings (user_id, prefs, secrets, updated_at) VALUES (?,?,?,?)",
        (user_id, "{}", "{}", created))
    return {"success": True, "user_id": user_id}


def authenticate(email: str, password: str) -> dict:
    email = normalize_email(email)
    user = db.one("SELECT * FROM users WHERE email = ?", (email,))
    if not user:
        # Constant-time-ish response: perform a dummy hash to equalize timing.
        verify_password(password, hash_password("dummy"))
        return {"success": False, "error": "Invalid email or password."}
    if user.get("status") != "active":
        return {"success": False, "error": "This account has been disabled."}
    if user.get("locked_until"):
        try:
            if datetime.fromisoformat(user["locked_until"]) > datetime.now():
                return {"success": False, "error": "Too many attempts. Try again in a few minutes."}
        except Exception:
            pass
    if not verify_password(password, user["password_hash"]):
        failed = int(user.get("failed_logins") or 0) + 1
        locked_until = None
        if failed >= 6:
            locked_until = (datetime.now() + timedelta(minutes=10)).isoformat()
        db.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                   (failed, locked_until, user["id"]))
        return {"success": False, "error": "Invalid email or password."}
    db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user["id"],))
    return {"success": True, "user": dict(user)}


def get_user(user_id: int) -> dict | None:
    if not user_id:
        return None
    return db.one("SELECT id, name, email, status, is_admin, onboarding_done, storage_used, created_at"
                  " FROM users WHERE id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Sessions (DB-backed -> survive server restarts and deployments)
# ---------------------------------------------------------------------------
def create_session(user_id: int, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent) VALUES (?,?,?,?,?)",
        (token, user_id, now.isoformat(timespec="seconds"),
         (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"),
         (user_agent or "")[:200]))
    _prune_sessions()
    return token


def _prune_sessions():
    try:
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
    except Exception:
        pass


def session_user(token: str) -> dict | None:
    """Resolves a session cookie token to the logged-in user (or None)."""
    if not token:
        return None
    sess = db.one("SELECT * FROM sessions WHERE token = ?", (token,))
    if not sess:
        return None
    try:
        if datetime.fromisoformat(sess["expires_at"]) < datetime.now():
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    except Exception:
        return None
    user = get_user(sess["user_id"])
    if not user or user.get("status") != "active":
        return None
    # Sliding expiry: refresh once per day of use.
    try:
        if (datetime.now() - datetime.fromisoformat(sess["created_at"])).total_seconds() > 86400:
            db.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                       ((datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), token))
    except Exception:
        pass
    return user


def destroy_session(token: str):
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
def create_reset_token(email: str) -> int | None:
    """Creates a 1-hour reset token for the account (if it exists).
    Returns the raw token via token_key for link building, or None."""
    user = db.one("SELECT id FROM users WHERE email = ?", (normalize_email(email),))
    if not user:
        return None, None
    raw = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    db.execute("DELETE FROM password_resets WHERE user_id = ? AND used = 0", (user["id"],))
    db.execute(
        "INSERT INTO password_resets (user_id, token_hash, expires_at, used, created_at) VALUES (?,?,?,0,?)",
        (user["id"], token_hash, (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"), db.now()))
    return user["id"], raw


def consume_reset_token(raw: str, new_password: str) -> dict:
    if len(new_password or "") < 8:
        return {"success": False, "error": "Password must be at least 8 characters."}
    token_hash = hashlib.sha256((raw or "").encode()).hexdigest()
    row = db.one("SELECT * FROM password_resets WHERE token_hash = ? AND used = 0", (token_hash,))
    if not row:
        return {"success": False, "error": "This reset link is invalid or already used."}
    try:
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return {"success": False, "error": "This reset link has expired. Request a new one."}
    except Exception:
        return {"success": False, "error": "This reset link is invalid."}
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), row["user_id"]))
    db.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (row["id"],))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))  # force re-login everywhere
    return {"success": True}


def change_password(user_id: int, current: str, new_password: str) -> dict:
    user = db.one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    if not user or not verify_password(current or "", user["password_hash"]):
        return {"success": False, "error": "Current password is incorrect."}
    if len(new_password or "") < 8:
        return {"success": False, "error": "New password must be at least 8 characters."}
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))
    return {"success": True}
