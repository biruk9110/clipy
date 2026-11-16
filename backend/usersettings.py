"""Per-user settings: editing preferences (JSON) + encrypted secrets.

Replaces the old global settings.json - every preference now belongs to the
authenticated user and persists in the database (survives restarts/deploys).
Secrets (Gemini key, Buffer token) are encrypted at rest and never returned
to the frontend in plaintext.
"""
import json

from .crypto import decrypt, encrypt, mask
from .database import db

DEFAULT_PREFS = {
    "gemini_model": "",                      # optional override (e.g. gemini-2.5-flash)
    "default_aspect_ratio": "9:16",
    "default_resolution": "720",             # 720 | 1080
    "default_num_clips": 3,
    "default_clip_duration": 45,             # seconds per clip target
    "caption_style": "karaoke",              # karaoke | bold | minimal | none
    "captions_enabled": True,
    "editing_intensity": "balanced",         # light | balanced | aggressive
    "trim_silence": True,
    "hook_banner": True,
    "audio_enhance": True,
    "auto_publish": False,
    "auto_post_platforms": {"tiktok": False, "instagram": False, "youtube": False},
    "schedule_interval_hours": 3,
    "review_mode": False,
    "default_hashtags": "",
    "notify_enabled": False,
    "keep_last_clips": 0,
    "max_clip_age_days": 0,
    "delete_source_after": False,
    "whisper_model": "tiny",
    "onboarding_done": False,
}

SECRET_NAMES = ("gemini_api_key", "buffer_access_token")


def get_prefs(user_id: int) -> dict:
    row = db.one("SELECT prefs FROM user_settings WHERE user_id = ?", (user_id,))
    try:
        saved = json.loads(row["prefs"]) if row and row["prefs"] else {}
    except Exception:
        saved = {}
    merged = DEFAULT_PREFS.copy()
    merged.update({k: v for k, v in saved.items() if k in merged})
    # onboarding flag also mirrored on users row
    u = db.one("SELECT onboarding_done FROM users WHERE id = ?", (user_id,))
    if u:
        merged["onboarding_done"] = bool(u.get("onboarding_done"))
    return merged


def save_prefs(user_id: int, patch: dict) -> dict:
    current = get_prefs(user_id)
    clean = {k: v for k, v in patch.items() if k in current}
    # Type safety for known toggles
    for k in ("captions_enabled", "trim_silence", "hook_banner", "audio_enhance",
              "auto_publish", "review_mode", "notify_enabled", "delete_source_after",
              "onboarding_done"):
        if k in clean:
            clean[k] = bool(clean[k])
    for k in ("default_num_clips", "default_clip_duration", "schedule_interval_hours",
              "keep_last_clips", "max_clip_age_days"):
        if k in clean:
            try:
                clean[k] = max(0, int(clean[k]))
            except Exception:
                clean[k] = current.get(k)
    if "default_aspect_ratio" in clean and clean["default_aspect_ratio"] not in ("9:16", "16:9", "1:1", "4:5"):
        clean["default_aspect_ratio"] = "9:16"
    if "default_resolution" in clean and str(clean["default_resolution"]) not in ("720", "1080"):
        clean["default_resolution"] = "720"
    if "editing_intensity" in clean and clean["editing_intensity"] not in ("light", "balanced", "aggressive"):
        clean["editing_intensity"] = "balanced"
    if "caption_style" in clean and clean["caption_style"] not in ("karaoke", "bold", "minimal", "none"):
        clean["caption_style"] = "karaoke"
    if "auto_post_platforms" in clean:
        p = clean["auto_post_platforms"]
        if isinstance(p, dict):
            clean["auto_post_platforms"] = {
                plat: bool(p.get(plat)) for plat in ("tiktok", "instagram", "youtube")}
        else:
            clean.pop("auto_post_platforms")
    current.update(clean)
    _upsert_settings(user_id, prefs=json.dumps(current))
    if "onboarding_done" in clean:
        db.execute("UPDATE users SET onboarding_done = ? WHERE id = ?",
                   (1 if clean["onboarding_done"] else 0, user_id))
    return current


def _upsert_settings(user_id: int, prefs: str | None = None, secrets_json: str | None = None):
    from .database import IS_PG
    now = db.now()
    row = db.one("SELECT user_id FROM user_settings WHERE user_id = ?", (user_id,))
    if not row:
        db.execute(
            "INSERT INTO user_settings (user_id, prefs, secrets, updated_at) VALUES (?,?,?,?)",
            (user_id, prefs or "{}", secrets_json or "{}", now))
        return
    if prefs is not None:
        db.execute("UPDATE user_settings SET prefs = ?, updated_at = ? WHERE user_id = ?",
                   (prefs, now, user_id))
    if secrets_json is not None:
        db.execute("UPDATE user_settings SET secrets = ?, updated_at = ? WHERE user_id = ?",
                   (secrets_json, now, user_id))


def _get_secrets_row(user_id: int) -> dict:
    row = db.one("SELECT secrets FROM user_settings WHERE user_id = ?", (user_id,))
    try:
        return json.loads(row["secrets"]) if row and row["secrets"] else {}
    except Exception:
        return {}


def set_secret(user_id: int, name: str, value: str):
    """Encrypts and stores a secret. Empty value removes it."""
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret: {name}")
    data = _get_secrets_row(user_id)
    if value:
        data[name] = encrypt(value)
    else:
        data.pop(name, None)
    _upsert_settings(user_id, secrets_json=json.dumps(data))


def get_secret(user_id: int, name: str) -> str:
    data = _get_secrets_row(user_id)
    return decrypt(data.get(name, ""))


def remove_secret(user_id: int, name: str):
    data = _get_secrets_row(user_id)
    data.pop(name, None)
    _upsert_settings(user_id, secrets_json=json.dumps(data))


def secret_status(user_id: int) -> dict:
    """Safe summary for the frontend - masked indicator only, never plaintext."""
    data = _get_secrets_row(user_id)
    out = {}
    for name in SECRET_NAMES:
        stored = data.get(name, "")
        raw = decrypt(stored) if stored else ""
        out[name] = {"set": bool(raw), "masked": mask(raw)}
    return out
