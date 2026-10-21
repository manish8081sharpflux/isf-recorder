"""
ISF Recorder — Jomi web app (21 Oct 2026, phase 1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The browser version of Jomi for reading: login (phone + WhatsApp OTP), Previous recordings with filters, report
(read / edit / finalize / copy / share link / Hindi), audio, transcript, Director's POV (admins). It is plain
HTML + JS in backend/webapp/static, served at /web, and uses the same API and rules as the phone app.
Recording stays in the phone app.

A browser <audio> element cannot send the Authorization header, so:
POST /web/audio-link/{id}   (logged in, same access rule as /recordings/{id}/audio) → a link valid for 1 hour
GET  /web/audio/{id}?uid&exp&sig                                                  → streams the audio
"""
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.identity import load_live_user
from backend.auth.routes import get_current_user
from backend.calls.access import call_access
from backend.config import JWT_SECRET
from backend.database import get_db

router = APIRouter(prefix="/web", tags=["Web app"])

AUDIO_LINK_SECONDS = 3600


def _sign(recording_id: str, user_id: str, exp: int) -> str:
    msg = f"audio|{recording_id}|{user_id}|{exp}".encode()
    return hmac.new((JWT_SECRET or "").encode(), msg, hashlib.sha256).hexdigest()


@router.post("/audio-link/{recording_id}")
def audio_link(recording_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="Server is missing JWT_SECRET")
    call_access(db, recording_id, user, write=False)
    try:
        rec = db.execute(text("SELECT user_id FROM isf_recorder.recording WHERE id = CAST(:id AS uuid) AND is_deleted = FALSE"),
                         {"id": recording_id}).fetchone()
    except Exception:
        db.rollback()
        rec = None
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    if user.get("role") != "admin" and str(rec.user_id) != user["user_id"]:
        raise HTTPException(status_code=403, detail="Permission denied")
    exp = int(time.time()) + AUDIO_LINK_SECONDS
    sig = _sign(recording_id, user["user_id"], exp)
    return {"path": f"/web/audio/{recording_id}?uid={user['user_id']}&exp={exp}&sig={sig}", "expires_in": AUDIO_LINK_SECONDS}


@router.get("/audio/{recording_id}")
def signed_audio(recording_id: str, request: Request, uid: str = Query(...), exp: int = Query(...), sig: str = Query(...),
                 db: Session = Depends(get_db)):
    if exp < time.time() or not JWT_SECRET or not hmac.compare_digest(sig, _sign(recording_id, uid, exp)):
        raise HTTPException(status_code=403, detail="This audio link has expired. Reload the page.")
    user = load_live_user(uid)
    if not user:
        raise HTTPException(status_code=403, detail="Account not active")
    from backend.recordings.routes import stream_audio
    return stream_audio(recording_id, request, current_user=user, db=db)
