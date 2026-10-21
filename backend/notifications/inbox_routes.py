"""
ISF Recorder — Phone notification endpoints (21 Oct 2026, see notifications/inbox.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET  /notifications/inbox?after_id=N   own new notifications (after_id missing → none, only latest_id)
GET  /notifications/settings           own on/off switches (only the ones for the person's role)
PUT  /notifications/settings {prefs}   change them
POST /notifications/announce {message, title?, audience}   super admins: message to all / coaches / admins
"""
import json
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.routes import get_current_user
from backend.database import get_db
from backend.notifications import inbox

router = APIRouter(prefix="/notifications", tags=["Phone notifications"])


@router.get("/inbox")
def get_inbox(after_id: Optional[int] = Query(None, ge=0), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    latest = db.execute(text("SELECT COALESCE(MAX(id), 0) FROM isf_recorder.user_notification WHERE user_id = CAST(:u AS uuid)"),
                        {"u": user["user_id"]}).scalar()
    if after_id is None:
        # A phone that never collected starts from now (old notifications are not shown all at once)
        return {"items": [], "latest_id": int(latest)}
    rows = db.execute(text("""
        SELECT id, kind, title, body, recording_id, created_at FROM isf_recorder.user_notification
        WHERE user_id = CAST(:u AS uuid) AND id > :a AND created_at > NOW() - INTERVAL '2 days'
        ORDER BY id LIMIT 20
    """), {"u": user["user_id"], "a": after_id}).fetchall()
    return {
        "items": [{"id": r.id, "kind": r.kind, "title": r.title, "body": r.body,
                   "recording_id": str(r.recording_id) if r.recording_id else None,
                   "created_at": r.created_at.isoformat()} for r in rows],
        "latest_id": int(rows[-1].id) if rows else max(int(latest), after_id),
    }


def _settings(db: Session, user: dict) -> dict:
    prefs = db.execute(text("SELECT COALESCE(notify_prefs, '{}'::jsonb) FROM isf_recorder.\"user\" WHERE id = CAST(:u AS uuid)"),
                       {"u": user["user_id"]}).scalar()
    on = inbox.prefs_with_defaults(prefs)
    admin = user.get("role") == "admin"
    return {"items": [{"kind": k, "label": label, "on": on[k]}
                      for k, (_, _, label) in inbox.KINDS.items() if inbox.applies(k, admin)],
            "can_announce": bool(user.get("is_super_admin"))}


@router.get("/settings")
def get_settings(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return _settings(db, user)


class SettingsBody(BaseModel):
    prefs: Dict[str, bool]


@router.put("/settings")
def put_settings(body: SettingsBody, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    bad = [k for k in body.prefs if k not in inbox.KINDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown notification: {', '.join(bad)}")
    db.execute(text("""
        UPDATE isf_recorder."user" SET notify_prefs = COALESCE(notify_prefs, '{}'::jsonb) || CAST(:p AS jsonb)
        WHERE id = CAST(:u AS uuid)
    """), {"u": user["user_id"], "p": json.dumps(body.prefs)})
    db.commit()
    return _settings(db, user)


class AnnounceBody(BaseModel):
    message: str
    title: Optional[str] = None
    audience: str = "all"          # all | coaches | admins


@router.post("/announce")
def post_announce(body: AnnounceBody, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.get("is_super_admin"):
        raise HTTPException(status_code=403, detail="Only super admins can send a message to everyone")
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Write a message first")
    if body.audience not in ("all", "coaches", "admins"):
        raise HTTPException(status_code=400, detail="Audience must be all, coaches or admins")
    sent = inbox.announce(db, user, body.message[:1000], body.title, body.audience)
    return {"sent": sent}
