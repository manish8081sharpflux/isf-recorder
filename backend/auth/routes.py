"""
ISF Recorder — Auth API Routes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POST /auth/request-otp   — Send OTP to phone via WhatsApp (uses template for guaranteed delivery)
POST /auth/verify-otp    — Verify OTP, return JWT + user profile (auto-provisions verified staff)
GET  /auth/me            — Get current user profile from JWT
"""
import logging
import re
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.auth.jwt_handler import create_token, get_user_from_token
from backend.auth.otp import send_otp_whatsapp, verify_otp, consume_otp, normalize_phone
from backend.auth.identity import load_live_user, is_super_admin_phone, role_label, invalidate_user_cache

logger = logging.getLogger("isf_recorder.auth.routes")
router = APIRouter(prefix="/auth", tags=["Authentication"])


# ─── Request / Response Models ────────────────────────────────────────────────

class OTPRequest(BaseModel):
    phone: str  # Any format: "9421187857", "919421187857", "+91 94211 87857"

class OTPVerify(BaseModel):
    phone: str
    code: str

class TokenResponse(BaseModel):
    token: str
    user: dict

class UserProfile(BaseModel):
    user_id: str
    org_id: str
    org_name: str
    name: str
    phone: str
    role: str
    preferred_language: str


# ─── Auth Dependency ──────────────────────────────────────────────────────────

def get_current_user(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    """Extract and verify JWT from Authorization header or ?token= query parameter."""
    raw_token = None
    if authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
    elif token:
        raw_token = token.strip()

    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header or token query parameter")
    claims = get_user_from_token(raw_token)
    if not claims or not claims.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Role, active/archived state, phone and name come from the DB (60 s cache), not the token
    user = load_live_user(claims["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="Account archived or removed")
    return user


def require_admin(user: dict = Depends(get_current_user)):
    """Dependency that requires admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/request-otp")
async def request_otp(body: OTPRequest):
    """Send a 6-digit OTP to the user's phone via WhatsApp."""
    clean_phone = normalize_phone(body.phone)
    # Only real Indian mobiles (91 + 10 digits starting 6-9): WhatsApp "accepts" sends to any number,
    # so a short or wrong number used to reach the OTP page without ever getting a code.
    if not re.fullmatch(r"91[6-9]\d{9}", clean_phone):
        raise HTTPException(status_code=400, detail="Please enter a valid 10-digit mobile number.")
    
    success, message = await send_otp_whatsapp(clean_phone)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "ok", "message": message, "phone": clean_phone}


@router.post("/verify-otp", response_model=TokenResponse)
def verify_otp_route(body: OTPVerify, db: Session = Depends(get_db)):
    """
    Verify OTP and return JWT token + user profile.
    If phone exists in isf_recorder.user, uses existing role.
    If not, imports from auth.users or auto-provisions a new staff account.
    """
    phone = normalize_phone(body.phone)
    code = body.code.strip()
    last10 = phone[-10:] if len(phone) >= 10 else phone

    # 1. Verify OTP code
    success, message = verify_otp(phone, code)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    # 2. Look up user by phone or last 10 digits
    result = db.execute(
        text("""
            SELECT u.id, u.org_id, u.name, u.phone, u.role, u.preferred_language, u.is_active, u.archived_at, u.is_super_admin,
                   u.settings, o.name as org_name, o.slug as org_slug
            FROM isf_recorder.user u
            JOIN isf_recorder.organization o ON u.org_id = o.id
            WHERE u.phone = :phone OR RIGHT(u.phone, 10) = :last10
            ORDER BY u.created_at ASC
            LIMIT 1
        """),
        {"phone": phone, "last10": last10},
    ).fetchone()

    # 3. If user does not exist in isf_recorder.user, provision automatically
    if not result:
        # Find default organization (isf or first available)
        org_row = db.execute(
            text("SELECT id, name, slug FROM isf_recorder.organization WHERE slug = 'isf' LIMIT 1")
        ).fetchone()
        if not org_row:
            org_row = db.execute(
                text("SELECT id, name, slug FROM isf_recorder.organization LIMIT 1")
            ).fetchone()

        if not org_row:
            # Create default ISF org if missing
            org_row = db.execute(
                text("""
                    INSERT INTO isf_recorder.organization (name, slug)
                    VALUES ('Initiative Sewa Foundation', 'isf')
                    RETURNING id, name, slug
                """)
            ).fetchone()
            db.commit()

        # Check existing auth.users for profile name & role
        existing_auth_user = None
        try:
            existing_auth_user = db.execute(
                text("SELECT name, role, preferred_language FROM auth.users WHERE RIGHT(phone, 10) = :last10 LIMIT 1"),
                {"last10": last10}
            ).fetchone()
        except Exception:
            db.rollback()

        user_name = existing_auth_user.name if existing_auth_user and existing_auth_user.name else f"Staff {last10[-4:]}"
        # Determine role: super admins (SUPER_ADMIN_PHONES) or an admin in auth.users, otherwise staff
        user_role = "admin" if (is_super_admin_phone(phone) or (existing_auth_user and existing_auth_user.role == "admin")) else "staff"
        lang = existing_auth_user.preferred_language if existing_auth_user and existing_auth_user.preferred_language else "hi"

        # Insert new user into isf_recorder.user
        new_user = db.execute(
            text("""
                INSERT INTO isf_recorder.user (org_id, name, phone, role, preferred_language, settings)
                VALUES (:org_id, :name, :phone, :role, :lang, CAST(:settings AS jsonb))
                ON CONFLICT (phone) DO UPDATE SET last_seen_at = NOW()
                RETURNING id, org_id, name, phone, role, preferred_language, is_active
            """),
            {
                "org_id": org_row.id,
                "name": user_name,
                "phone": phone,
                "role": user_role,
                "lang": lang,
                # self sign-up with a placeholder name: the app asks "What's your name?" after login
                "settings": json.dumps({"name_set": bool(existing_auth_user and existing_auth_user.name)}),
            }
        ).fetchone()
        db.commit()

        # Retrieve newly provisioned user with organization details
        result = db.execute(
            text("""
                SELECT u.id, u.org_id, u.name, u.phone, u.role, u.preferred_language, u.is_active, u.archived_at, u.is_super_admin,
                       u.settings, o.name as org_name, o.slug as org_slug
                FROM isf_recorder.user u
                JOIN isf_recorder.organization o ON u.org_id = o.id
                WHERE u.id = :user_id
            """),
            {"user_id": new_user.id}
        ).fetchone()

    if result.archived_at is not None:
        raise HTTPException(status_code=403, detail="Account archived — ask your admin")
    if not result.is_active:
        raise HTTPException(status_code=403, detail="Your account is deactivated. Contact your admin.")

    is_super = bool(result.is_super_admin) or is_super_admin_phone(result.phone)
    effective_role = "admin" if is_super else result.role
    if is_super and (result.role != "admin" or not result.is_super_admin):
        # Super admins always stay admin (their "view as Coach" switch is client-side only);
        # bootstrap phones (SUPER_ADMIN_PHONES) also get the DB flag
        db.execute(text("UPDATE isf_recorder.user SET role = 'admin', is_super_admin = TRUE WHERE id = :id"),
                   {"id": str(result.id)})
        db.commit()
        invalidate_user_cache(str(result.id))

    # 4. Create JWT token
    token = create_token(
        user_id=str(result.id),
        org_id=str(result.org_id),
        role=effective_role,
        phone=result.phone,
        name=result.name,
    )

    # 5. Safely consume OTP now that token generation succeeded
    consume_otp(phone)

    # 6. Update last_seen_at
    db.execute(
        text("UPDATE isf_recorder.user SET last_seen_at = NOW() WHERE id = :id"),
        {"id": str(result.id)},
    )
    db.commit()

    # 7. Audit log
    db.execute(
        text("""
            INSERT INTO isf_recorder.audit_log (user_id, org_id, action, metadata)
            VALUES (:user_id, :org_id, 'login', :metadata)
        """),
        {
            "user_id": str(result.id),
            "org_id": str(result.org_id),
            "metadata": '{"method": "whatsapp_otp"}',
        },
    )
    db.commit()

    logger.info(f"✅ User logged in: {result.name} ({result.phone[-4:]}) — {effective_role}")

    return {
        "token": token,
        "user": {
            "user_id": str(result.id),
            "org_id": str(result.org_id),
            "org_name": result.org_name,
            "org_slug": result.org_slug,
            "name": result.name,
            "phone": result.phone,
            "role": effective_role,
            "preferred_language": result.preferred_language,
            "is_super_admin": is_super,
            "role_label": role_label(effective_role, str(result.org_id)),
            "needs_name": needs_name(result.name, result.settings),
        },
    }


@router.get("/me")
def get_me(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get current authenticated user's profile."""
    result = db.execute(
        text("""
            SELECT u.id, u.org_id, u.name, u.phone, u.role, u.preferred_language, u.settings,
                   o.name as org_name, o.slug as org_slug
            FROM isf_recorder.user u
            JOIN isf_recorder.organization o ON u.org_id = o.id
            WHERE u.id = :user_id
        """),
        {"user_id": user["user_id"]},
    ).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "user_id": str(result.id),
        "org_id": str(result.org_id),
        "org_name": result.org_name,
        "org_slug": result.org_slug,
        "name": result.name,
        "phone": result.phone,
        "role": user["role"],
        "preferred_language": result.preferred_language,
        "is_super_admin": bool(user.get("is_super_admin")),
        "role_label": role_label(user["role"], str(result.org_id)),
        "needs_name": needs_name(result.name, result.settings),
    }


_PLACEHOLDER_NAME = re.compile(r"^staff\s*\d{4}$", re.IGNORECASE)


def needs_name(name: Optional[str], settings) -> bool:
    """True for self-signed-up users still on the auto name ('Staff 5678'): explicit name_set=false
    flag (new sign-ups) or the placeholder pattern (users created before the flag existed)."""
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    settings = settings if isinstance(settings, dict) else {}
    if settings.get("name_set") is True:
        return False
    clean = " ".join((name or "").split())
    return settings.get("name_set") is False or not clean or bool(_PLACEHOLDER_NAME.match(clean))


class UpdateMeRequest(BaseModel):
    name: str


@router.patch("/me")
def update_me(body: UpdateMeRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Set your own display name (first login asks for it). 2-60 characters."""
    name = " ".join((body.name or "").split())
    if not 2 <= len(name) <= 60:
        raise HTTPException(status_code=400, detail="Please enter your name (2-60 characters)")
    if _PLACEHOLDER_NAME.match(name):
        raise HTTPException(status_code=400, detail="Please enter your real name")
    db.execute(text("""
        UPDATE isf_recorder.user
        SET name = :name, settings = COALESCE(settings, '{}'::jsonb) || '{"name_set": true}'::jsonb
        WHERE id = :id
    """), {"name": name, "id": user["user_id"]})
    db.commit()
    invalidate_user_cache(str(user["user_id"]))
    return get_me(user, db)
