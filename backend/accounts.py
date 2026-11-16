"""Connected social accounts (per-user), with encrypted tokens and refresh."""
import json
from datetime import datetime

from .crypto import decrypt, encrypt
from .database import db

PLATFORMS = ("tiktok", "instagram", "youtube", "buffer")


def get_accounts(user_id: int) -> list[dict]:
    rows = db.query("SELECT * FROM connected_accounts WHERE user_id = ? ORDER BY platform",
                    (user_id,))
    out = []
    for r in rows:
        acc = dict(r)
        acc["access_token"] = decrypt(acc.pop("access_token_enc", "") or "")
        acc["refresh_token"] = decrypt(acc.pop("refresh_token_enc", "") or "")
        try:
            acc["meta"] = json.loads(acc.get("meta") or "{}")
        except Exception:
            acc["meta"] = {}
        out.append(acc)
    return out


def get_account(user_id: int, platform: str) -> dict | None:
    for acc in get_accounts(user_id):
        if acc["platform"] == platform:
            return acc
    return None


def upsert_account(user_id: int, platform: str, access_token: str = "",
                   refresh_token: str = "", account_id: str = "",
                   display_name: str = "", expires_at: str | None = None,
                   meta: dict | None = None, status: str = "active") -> int:
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform {platform}")
    now = db.now()
    row = db.one("SELECT id FROM connected_accounts WHERE user_id = ? AND platform = ?",
                 (user_id, platform))
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    if row:
        db.execute(
            "UPDATE connected_accounts SET access_token_enc = ?, refresh_token_enc = ?,"
            " account_id = ?, display_name = ?, expires_at = ?, meta = ?, status = ?,"
            " last_connected_at = ? WHERE id = ?",
            (encrypt(access_token), encrypt(refresh_token), account_id, display_name,
             expires_at, meta_json, status, now, row["id"]))
        return int(row["id"])
    return int(db.scalar(
        "INSERT INTO connected_accounts (user_id, platform, account_id, display_name,"
        " access_token_enc, refresh_token_enc, expires_at, meta, status, last_connected_at,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (user_id, platform, account_id, display_name, encrypt(access_token),
         encrypt(refresh_token), expires_at, meta_json, status, now, now)))


def remove_account(user_id: int, platform: str) -> bool:
    return db.execute("DELETE FROM connected_accounts WHERE user_id = ? AND platform = ?",
                      (user_id, platform)) > 0


def token_expired(acc: dict) -> bool:
    exp = acc.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(str(exp)) <= datetime.now()
    except Exception:
        return False


def refresh_account_token(acc: dict) -> dict:
    """Refreshes the access token when expired (OAuth platforms only).
    Buffer tokens and manual tokens have no refresh flow -> pass through."""
    if not acc or not token_expired(acc):
        return acc
    platform = acc["platform"]
    if platform == "youtube":
        from .posters.youtube import refresh_access_token
        rt = acc.get("refresh_token") or ""
        new = refresh_access_token(rt, acc.get("meta", {}).get("client_id", ""),
                                   acc.get("meta", {}).get("client_secret", ""))
        if new:
            db.execute("UPDATE connected_accounts SET access_token_enc = ?, expires_at = NULL"
                       " WHERE id = ?", (encrypt(new), acc["id"]))
            acc["access_token"] = new
            acc["expires_at"] = None
        else:
            db.execute("UPDATE connected_accounts SET status = 'expired' WHERE id = ?",
                       (acc["id"],))
            acc["status"] = "expired"
    elif platform == "instagram":
        from .posters.instagram import refresh_long_lived_token
        new = refresh_long_lived_token(acc.get("access_token") or "")
        if new:
            db.execute("UPDATE connected_accounts SET access_token_enc = ? WHERE id = ?",
                       (encrypt(new), acc["id"]))
            acc["access_token"] = new
    elif platform == "tiktok":
        rt = acc.get("refresh_token") or ""
        if rt:
            from .posters.tiktok import refresh_tiktok_token
            new = refresh_tiktok_token(rt, acc.get("meta", {}))
            if new:
                acc["access_token"] = new
                acc["expires_at"] = None
                db.execute("UPDATE connected_accounts SET access_token_enc = ?, expires_at = NULL"
                           " WHERE id = ?", (encrypt(new), acc["id"]))
            else:
                db.execute("UPDATE connected_accounts SET status = 'expired' WHERE id = ?",
                           (acc["id"],))
                acc["status"] = "expired"
    return acc


def account_status_summary(user_id: int) -> dict:
    out = {}
    for acc in get_accounts(user_id):
        expired = token_expired(acc)
        out[acc["platform"]] = {
            "connected": acc["status"] == "active" and not expired,
            "display_name": acc.get("display_name") or acc.get("platform", "").title(),
            "status": "expired" if expired else acc.get("status", "active"),
            "expires_at": acc.get("expires_at"),
            "last_connected_at": acc.get("last_connected_at"),
        }
    return out
