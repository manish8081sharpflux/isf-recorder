"""
ISF Recorder — JWT Authentication Handler
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Create and verify JWT tokens for authenticated API access.
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from jose import jwt, JWTError
from backend.config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRY_DAYS

logger = logging.getLogger("isf_recorder.auth")


def create_token(user_id: str, org_id: str, role: str, phone: str, name: str) -> str:
    """Create a JWT token for an authenticated user."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "org_id": org_id,
        "role": role,
        "phone": phone,
        "name": name,
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRY_DAYS),
        "jti": str(uuid.uuid4()),  # unique token ID
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode a JWT token. Returns claims dict or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug(f"JWT verification failed: {e}")
        return None


def get_user_from_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Extract user info from token.
    Returns dict with: sub (user_id), org_id, role, phone, name
    """
    payload = verify_token(token)
    if not payload:
        return None
    return {
        "user_id": payload.get("sub"),
        "org_id": payload.get("org_id"),
        "role": payload.get("role"),
        "phone": payload.get("phone"),
        "name": payload.get("name"),
    }
