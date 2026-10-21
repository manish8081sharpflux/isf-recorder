"""
ISF Recorder — Retention & Starred Audio Manager
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Star / unstar recordings. The 7-day audio deletion was removed (Jomi v2): audio and chat messages
are kept forever. Raw chunk WAVs are cleaned up by recordings/lifecycle.cleanup_chunk_wavs().
"""
import os
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


logger = logging.getLogger("isf_recorder.retention")


from backend.auth.otp import verify_otp, consume_otp

def toggle_star_recording(
    db: Session,
    recording_id: str,
    user_id: str,
    is_admin: bool = False,
    user_phone: Optional[str] = None,
    otp_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Toggle star status for a recording.
    Staff can only star their own recordings. Admins can star any recording in their org.
    Starring permanently protects audio from 7-day deletion.
    UN-STARring requires entering a WhatsApp OTP to prevent accidental loss of protected audio.
    """
    # Fetch current star status and owner
    query = text("""
        SELECT r.id, r.user_id, r.is_starred, r.audio_deleted_at
        FROM isf_recorder.recording r
        WHERE r.id = :rec_id AND r.is_deleted = FALSE
    """)
    rec = db.execute(query, {"rec_id": recording_id}).fetchone()
    if not rec:
        return {"success": False, "message": "Recording not found"}

    if not is_admin and str(rec.user_id) != str(user_id):
        return {"success": False, "message": "Permission denied: Staff can only star their own recordings"}

    # If currently starred, user is trying to UNSTAR -> require OTP verification
    if rec.is_starred:
        if not otp_code or not otp_code.strip():
            return {
                "success": False,
                "otp_required": True,
                "user_phone": user_phone,
                "message": "Security Check: Entering WhatsApp OTP is required to remove permanent audio protection."
            }

        # Verify OTP
        if not user_phone:
            return {"success": False, "message": "User phone number not found for OTP verification"}

        valid, msg = verify_otp(user_phone, otp_code.strip())
        if not valid:
            return {
                "success": False,
                "otp_required": True,
                "message": msg
            }

        consume_otp(user_phone)

    new_starred = not rec.is_starred
    starred_at = datetime.utcnow() if new_starred else None
    starred_by = user_id if new_starred else None

    db.execute(
        text("""
            UPDATE isf_recorder.recording
            SET is_starred = :is_starred,
                starred_by = :starred_by,
                starred_at = :starred_at
            WHERE id = :id
        """),
        {
            "id": recording_id,
            "is_starred": new_starred,
            "starred_by": starred_by,
            "starred_at": starred_at,
        }
    )
    db.commit()

    action = "starred" if new_starred else "unstarred"
    logger.info(f"Recording {recording_id} was {action} by user {user_id}")
    return {
        "success": True,
        "is_starred": new_starred,
        "action": action,
        "message": f"Recording successfully {action}"
    }
