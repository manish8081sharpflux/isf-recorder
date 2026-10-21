"""
ISF Recorder — User Management Routes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET    /users/                  — List users (any user; non-admins see masked phones)
GET    /users/check             — Duplicate check by name / phone (admin)
POST   /users/                  — Create a user (admin; 409 if the phone exists)
PUT    /users/{id}              — Update name / phone / role / language (admin; role: super admin only)
POST   /users/{id}/archive      — Archive (admin): cannot log in, recordings kept
POST   /users/{id}/unarchive    — Un-archive (admin)
DELETE /users/{id}              — Archive (default) or ?permanent=true real delete (recordings kept)
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.auth.routes import get_current_user, require_admin
from backend.auth.otp import normalize_phone
from backend.auth.identity import (
    invalidate_user_cache, is_super, is_super_admin_phone, last10, mask_phone, role_label,
)

logger = logging.getLogger("isf_recorder.users")
router = APIRouter(prefix="/users", tags=["User Management"])


class CreateUserRequest(BaseModel):
    name: str
    phone: str
    role: str = "staff"  # admin | staff
    preferred_language: str = "hi"


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    preferred_language: Optional[str] = None
    is_active: Optional[bool] = None  # legacy (v1.2.0); false = archive
    is_super_admin: Optional[bool] = None  # super admin only; target must be an admin


def _get_target(db: Session, user_id: str, org_id: str):
    try:
        row = db.execute(text("""
            SELECT id, org_id, name, phone, role, is_active, archived_at, is_super_admin
            FROM isf_recorder.user WHERE id = CAST(:id AS uuid) AND org_id = :org
        """), {"id": user_id, "org": org_id}).fetchone()
    except Exception:
        db.rollback()
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="User not found in your organization")
    return row


def _active_admin_count(db: Session, org_id: str) -> int:
    return db.execute(text("""
        SELECT COUNT(*) FROM isf_recorder.user
        WHERE org_id = :org AND role = 'admin' AND is_active = TRUE AND archived_at IS NULL
    """), {"org": org_id}).scalar() or 0


def _is_last_admin(db: Session, target) -> bool:
    return (target.role == "admin" and target.is_active and target.archived_at is None
            and _active_admin_count(db, str(target.org_id)) <= 1)


def _phone_owner(db: Session, phone: str, exclude_id: Optional[str] = None):
    return db.execute(text("""
        SELECT id, name FROM isf_recorder.user
        WHERE (phone = :phone OR RIGHT(phone, 10) = :l10) AND (CAST(:ex AS uuid) IS NULL OR id <> CAST(:ex AS uuid))
        ORDER BY created_at LIMIT 1
    """), {"phone": phone, "l10": phone[-10:], "ex": exclude_id}).fetchone()


def _audit(db: Session, actor: dict, action: str, target_id: str, meta: dict) -> None:
    db.execute(text("""
        INSERT INTO isf_recorder.audit_log (user_id, org_id, action, target_type, target_id, metadata)
        VALUES (:u, :o, :a, 'user', :t, CAST(:m AS jsonb))
    """), {"u": actor["user_id"], "o": actor["org_id"], "a": action, "t": target_id, "m": json.dumps(meta)})


def _check_removable(db: Session, admin: dict, target) -> None:
    if str(target.id) == str(admin["user_id"]):
        raise HTTPException(status_code=403, detail="You cannot remove your own account")
    if is_super(target):
        raise HTTPException(status_code=403, detail="A super admin cannot be removed")
    if _is_last_admin(db, target):
        raise HTTPException(status_code=409, detail="This is the last active admin")


def _archive(db: Session, admin: dict, target) -> dict:
    _check_removable(db, admin, target)
    db.execute(text("""
        UPDATE isf_recorder.user SET is_active = FALSE, archived_at = COALESCE(archived_at, NOW()) WHERE id = :id
    """), {"id": str(target.id)})
    _audit(db, admin, "user_archived", str(target.id), {"name": target.name})
    db.commit()
    invalidate_user_cache(str(target.id))
    logger.info(f"User archived: {target.name}")
    return {"status": "ok", "archived": True,
            "message": f"User '{target.name}' archived. Their recordings are preserved."}


@router.post("/")
def create_user(body: CreateUserRequest, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    """Create a user in the admin's organization. An existing phone is a 409 (no silent update)."""
    phone = normalize_phone(body.phone)
    if len(phone) < 10:
        raise HTTPException(status_code=400, detail="Please enter a valid phone number (at least 10 digits)")
    if body.role not in ("admin", "staff"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'staff'")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    existing = _phone_owner(db, phone)
    if existing:
        return JSONResponse(status_code=409, content={
            "detail": f"Phone already belongs to {existing.name}", "existing_user_id": str(existing.id)})
    role = "admin" if is_super_admin_phone(phone) else body.role
    if body.role != "staff" and not admin.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Only a super admin can create an admin")

    result = db.execute(text("""
        INSERT INTO isf_recorder.user (org_id, name, phone, role, preferred_language)
        VALUES (:org_id, :name, :phone, :role, :lang)
        RETURNING id
    """), {"org_id": admin["org_id"], "name": name, "phone": phone, "role": role,
           "lang": body.preferred_language}).fetchone()
    _audit(db, admin, "user_created", str(result.id), {"name": name, "role": role})
    db.commit()
    logger.info(f"✅ User created: {name} ({phone[-4:]}) as {role}")
    return {
        "status": "ok",
        "user_id": str(result.id),
        "name": name,
        "phone": phone,
        "role": role,
        "message": f"User '{name}' created. They can now login with OTP on phone {phone}."
    }


@router.get("/")
def list_users(
    include_archived: bool = Query(False),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Members of the organization (directory & filters). Archived users only for admins on request."""
    is_admin = current_user.get("role") == "admin"
    show_archived = include_archived and is_admin
    rows = db.execute(text(f"""
        SELECT id, name, phone, role, preferred_language, is_active, archived_at, last_seen_at, created_at, is_super_admin
        FROM isf_recorder.user
        WHERE org_id = :org_id {'' if show_archived else 'AND archived_at IS NULL'}
        ORDER BY name ASC, created_at ASC
    """), {"org_id": current_user["org_id"]}).fetchall()

    org_id = current_user["org_id"]
    users = []
    for r in rows:
        sup = is_super(r)
        role = "admin" if sup else r.role
        users.append({
            "id": str(r.id),
            "name": r.name,
            "phone": r.phone if is_admin else mask_phone(r.phone),
            "role": role,
            "preferred_language": r.preferred_language,
            "is_active": r.is_active,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "is_archived": r.archived_at is not None,
            "archived_at": r.archived_at.isoformat() if r.archived_at else None,
            "is_super_admin": sup,
            "role_label": role_label(role, org_id),
        })
    return {"users": users, "total": len(users)}


@router.get("/check")
def check_user(
    name: Optional[str] = Query(None),
    phone: Optional[str] = Query(None),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Live duplicate warning for Add Person: matches by last 10 phone digits or trimmed name (any case)."""
    nm = (name or "").strip().lower()
    l10 = last10(phone)
    if not nm and len(l10) < 10:
        return {"matches": []}
    rows = db.execute(text("""
        SELECT id, name, phone, role, archived_at,
               (:l10 <> '' AND RIGHT(phone, 10) = :l10) AS phone_match,
               (:nm <> '' AND LOWER(TRIM(name)) = :nm) AS name_match
        FROM isf_recorder.user
        WHERE org_id = :org
          AND ((:l10 <> '' AND RIGHT(phone, 10) = :l10) OR (:nm <> '' AND LOWER(TRIM(name)) = :nm))
        ORDER BY name
    """), {"org": admin["org_id"], "l10": l10 if len(l10) == 10 else "", "nm": nm}).fetchall()
    return {"matches": [{
        "id": str(r.id), "name": r.name, "phone": r.phone, "role": r.role,
        "is_archived": r.archived_at is not None,
        "match_on": [k for k, v in (("name", r.name_match), ("phone", r.phone_match)) if v],
    } for r in rows]}


@router.put("/{user_id}")
def update_user(user_id: str, body: UpdateUserRequest, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    """Update a user. Role changes: super admin only, and only when the role really changes."""
    target = _get_target(db, user_id, admin["org_id"])
    updates = {}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        updates["name"] = body.name.strip()
    if body.preferred_language is not None:
        updates["preferred_language"] = body.preferred_language
    if body.phone is not None:
        phone = normalize_phone(body.phone)
        if len(phone) < 10:
            raise HTTPException(status_code=400, detail="Please enter a valid phone number (at least 10 digits)")
        if last10(phone) != last10(target.phone):
            owner = _phone_owner(db, phone, exclude_id=str(target.id))
            if owner:
                return JSONResponse(status_code=409, content={
                    "detail": f"Phone already belongs to {owner.name}", "existing_user_id": str(owner.id)})
            if is_super(target) or is_super_admin_phone(phone):
                raise HTTPException(status_code=403, detail="A super admin's phone cannot be changed here")
            updates["phone"] = phone
    if body.role is not None:
        if body.role not in ("admin", "staff"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'staff'")
        current_role = "admin" if is_super(target) else target.role
        if body.role != current_role:  # the v1.2.0 Edit dialog always sends role
            if not admin.get("is_super_admin"):
                raise HTTPException(status_code=403, detail="Only a super admin can change roles")
            if is_super(target):
                raise HTTPException(status_code=409, detail="A super admin always stays admin")
            if body.role == "staff" and _is_last_admin(db, target):
                raise HTTPException(status_code=409, detail="Cannot demote the last active admin")
            updates["role"] = body.role
    if body.is_active is not None and body.is_active != bool(target.is_active and target.archived_at is None):
        if body.is_active:
            updates["is_active"] = True
            updates["archived_at"] = None
        else:
            _check_removable(db, admin, target)
            updates["is_active"] = False

    if body.is_super_admin is not None and body.is_super_admin != is_super(target):
        if not admin.get("is_super_admin"):
            raise HTTPException(status_code=403, detail="Only a super admin can change super admin access")
        if body.is_super_admin:
            if (updates.get("role") or target.role) != "admin":
                raise HTTPException(status_code=409, detail="Only an admin can be made a super admin")
        else:
            if is_super_admin_phone(target.phone):
                raise HTTPException(status_code=409, detail="This super admin is permanent")
            remaining = db.execute(text("""
                SELECT COUNT(*) FROM isf_recorder.user
                WHERE org_id = :org AND is_super_admin = TRUE AND is_active = TRUE AND archived_at IS NULL AND id <> :id
            """), {"org": admin["org_id"], "id": str(target.id)}).scalar() or 0
            if remaining == 0:
                raise HTTPException(status_code=409, detail="Cannot remove the last super admin")
        updates["is_super_admin"] = body.is_super_admin
    if updates.get("role") == "staff" and (updates.get("is_super_admin") or is_super(target))             and updates.get("is_super_admin") is not False:
        raise HTTPException(status_code=409, detail="A super admin always stays admin")

    if not updates:
        if body.model_dump(exclude_none=True):
            return {"status": "ok", "message": "No changes"}
        raise HTTPException(status_code=400, detail="No fields to update")

    sets = []
    params = {"user_id": str(target.id)}
    for k, v in updates.items():
        if k == "is_active" and v is False:
            sets.append("is_active = FALSE")
            sets.append("archived_at = COALESCE(archived_at, NOW())")
        elif k == "archived_at":
            sets.append("archived_at = NULL")
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    db.execute(text(f"UPDATE isf_recorder.user SET {', '.join(sets)} WHERE id = :user_id"), params)
    if "name" in updates:  # an admin gave the person a real name: never ask them on first login
        db.execute(text("""UPDATE isf_recorder.user SET settings = COALESCE(settings, '{}'::jsonb) || '{"name_set": true}'::jsonb
                           WHERE id = :user_id"""), {"user_id": params["user_id"]})
    _audit(db, admin, "user_updated", str(target.id), {k: (v if k != "phone" else "***") for k, v in updates.items()
                                                        if k != "archived_at"})
    db.commit()
    invalidate_user_cache(str(target.id))
    return {"status": "ok", "message": "User updated"}


@router.post("/{user_id}/archive")
def archive_user(user_id: str, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    return _archive(db, admin, _get_target(db, user_id, admin["org_id"]))


@router.post("/{user_id}/unarchive")
def unarchive_user(user_id: str, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    target = _get_target(db, user_id, admin["org_id"])
    db.execute(text("UPDATE isf_recorder.user SET is_active = TRUE, archived_at = NULL WHERE id = :id"),
               {"id": str(target.id)})
    _audit(db, admin, "user_unarchived", str(target.id), {"name": target.name})
    db.commit()
    invalidate_user_cache(str(target.id))
    return {"status": "ok", "archived": False, "message": f"User '{target.name}' can log in again."}


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    permanent: bool = Query(False),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """No query: archive (what v1.2.0 DELETE meant). ?permanent=true: delete the user, keep recordings."""
    target = _get_target(db, user_id, admin["org_id"])
    if not permanent:
        return _archive(db, admin, target)

    _check_removable(db, admin, target)
    kept = db.execute(text("""
        UPDATE isf_recorder.recording SET recorded_by_name = :name WHERE user_id = :id
    """), {"id": str(target.id), "name": target.name}).rowcount
    # FKs (migration 005): recordings/stars/action items/audit/templates -> SET NULL; own chats -> CASCADE
    db.execute(text("DELETE FROM isf_recorder.user WHERE id = :id"), {"id": str(target.id)})
    _audit(db, admin, "user_deleted", str(target.id), {"name": target.name, "recordings_kept": kept})
    db.commit()
    invalidate_user_cache(str(target.id))
    logger.info(f"User permanently deleted: {target.name} ({kept} recordings kept)")
    return {"status": "deleted", "recordings_kept": kept}
