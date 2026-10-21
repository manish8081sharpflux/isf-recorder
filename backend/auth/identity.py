"""
ISF Recorder — Live user identity (contract §2.1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The JWT only proves who the caller is. Role, active/archived state, phone and name are re-read from
the database (60 s in-process cache, invalidated by the users routes), so archiving a user or
changing a role takes effect within a minute without waiting for the 90-day token to expire.

Super admins: phones whose last 10 digits are in SUPER_ADMIN_PHONES. They are always admins.
"""
import threading
import time
from typing import Dict, Optional, Tuple

from sqlalchemy import text

from backend.config import SUPER_ADMIN_PHONES

USER_CACHE_TTL = 60.0
ORG_CACHE_TTL = 60.0

_user_cache: Dict[str, Tuple[float, Optional[dict]]] = {}
_org_label_cache: Dict[str, Tuple[float, str]] = {}
_lock = threading.Lock()


def last10(phone: Optional[str]) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:]


def is_super_admin_phone(phone: Optional[str]) -> bool:
    """Permanent (bootstrap) super admins from SUPER_ADMIN_PHONES: they can never lose the flag."""
    return bool(phone) and last10(phone) in SUPER_ADMIN_PHONES


def is_super(row) -> bool:
    """DB flag user.is_super_admin (migration 006) OR the permanent bootstrap list."""
    return bool(getattr(row, "is_super_admin", False)) or is_super_admin_phone(getattr(row, "phone", None))


def mask_phone(phone: Optional[str]) -> str:
    d = last10(phone)
    return f"******{d[-4:]}" if d else ""


def invalidate_user_cache(user_id: Optional[str] = None) -> None:
    with _lock:
        if user_id:
            _user_cache.pop(str(user_id), None)
        else:
            _user_cache.clear()


def invalidate_org_cache(org_id: Optional[str] = None) -> None:
    with _lock:
        if org_id:
            _org_label_cache.pop(str(org_id), None)
        else:
            _org_label_cache.clear()


def org_role_label(org_id: Optional[str]) -> str:
    """Display label for the 'staff' role (organization.settings.recorder.role_label, default 'Coach')."""
    if not org_id:
        return "Coach"
    now = time.time()
    with _lock:
        hit = _org_label_cache.get(str(org_id))
        if hit and hit[0] > now:
            return hit[1]
    label = "Coach"
    try:
        from backend.database import SessionLocal
        with SessionLocal() as db:
            value = db.execute(text("""
                SELECT settings -> 'recorder' ->> 'role_label' FROM isf_recorder.organization WHERE id = :id
            """), {"id": str(org_id)}).scalar()
            if value and str(value).strip():
                label = str(value).strip()
    except Exception:
        pass
    with _lock:
        _org_label_cache[str(org_id)] = (now + ORG_CACHE_TTL, label)
    return label


def role_label(role: Optional[str], org_id: Optional[str] = None) -> str:
    return "Admin" if role == "admin" else org_role_label(org_id)


def load_live_user(user_id: str) -> Optional[dict]:
    """Current DB state of a user, or None if missing / inactive / archived. Cached 60 s."""
    key = str(user_id)
    now = time.time()
    with _lock:
        hit = _user_cache.get(key)
        if hit and hit[0] > now:
            return dict(hit[1]) if hit[1] else None

    from backend.database import SessionLocal
    data = None
    try:
        with SessionLocal() as db:
            row = db.execute(text("""
                SELECT id, org_id, name, phone, role, is_active, archived_at, is_super_admin
                FROM isf_recorder."user" WHERE id = CAST(:id AS uuid)
            """), {"id": key}).fetchone()
    except Exception:
        row = None  # malformed id in an old/forged token
    if row and row.is_active and row.archived_at is None:
        sup = is_super(row)
        data = {
            "user_id": str(row.id),
            "org_id": str(row.org_id),
            "role": "admin" if sup else row.role,
            "phone": row.phone,
            "name": row.name,
            "is_super_admin": sup,
        }
    with _lock:
        _user_cache[key] = (now + USER_CACHE_TTL, data)
    return dict(data) if data else None
