"""
The one privacy rule for calls (source = 'call'):
  - the owner may read and change it;
  - an admin may only READ it, and only when the owner switched on "Share with admin";
  - everyone else gets 404 (a call's existence is not revealed).
Class / meeting recordings (source = 'recording') are not affected: their existing checks apply.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session


def is_private_call_for(row, user: dict, write: bool = False) -> bool:
    """True when [row] (needs source, user_id, shared_with_admin) is a call [user] may not see / change."""
    if getattr(row, "source", "recording") != "call":
        return False
    if str(row.user_id) == str(user["user_id"]):
        return False
    if not write and user.get("role") == "admin" and bool(getattr(row, "shared_with_admin", False)):
        return False
    return True


def call_access(db: Session, recording_id: str, user: dict, write: bool = False) -> None:
    """Raise 404 when [recording_id] is someone else's call that [user] may not read (or change)."""
    try:
        row = db.execute(text("""
            SELECT source, user_id, shared_with_admin FROM isf_recorder.recording WHERE id = CAST(:id AS uuid)
        """), {"id": str(recording_id)}).fetchone()
    except Exception:
        db.rollback()
        return          # not a valid id: the endpoint's own lookup answers 404 / 400
    if row is not None and is_private_call_for(row, user, write):
        raise HTTPException(status_code=404, detail="Recording not found")


# SQL piece for lists: hides other people's calls ("r" = isf_recorder.recording alias).
CALL_VISIBILITY_SQL = """(r.source <> 'call' OR r.user_id = CAST(:_viewer AS uuid)
                          OR (:_viewer_admin AND r.shared_with_admin))"""


def visibility_params(user: dict) -> dict:
    return {"_viewer": str(user["user_id"]), "_viewer_admin": user.get("role") == "admin"}
