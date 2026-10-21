"""
ISF Recorder — Learned names (21 Oct 2026, admins)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET    /names?balgruh=Mohor Girls   names Jomi learned for a Balgruha ('' = everywhere), with corrected spellings
DELETE /names/{id}                  forget one (it is no longer sent to the AI or used to correct spellings)
Nobody has to use this: it is only for removing a mistake.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.routes import require_admin
from backend.database import get_db

router = APIRouter(prefix="/names", tags=["Name memory"])


@router.get("")
def list_names(balgruh: Optional[str] = Query(None), admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT id, balgruh_name, name, wrong_spellings, heard_count, corrected_count, updated_at
        FROM isf_recorder.name_memory
        WHERE org_id = CAST(:o AS uuid) AND NOT is_deleted AND (CAST(:b AS text) IS NULL OR balgruh_name = :b)
        ORDER BY corrected_count DESC, heard_count DESC, name
        LIMIT 500
    """), {"o": admin["org_id"], "b": balgruh.strip() if balgruh is not None else None}).fetchall()
    return {"items": [{
        "id": r.id, "balgruh": r.balgruh_name, "name": r.name,
        "wrong_spellings": r.wrong_spellings if isinstance(r.wrong_spellings, list) else json.loads(r.wrong_spellings or "[]"),
        "heard_count": r.heard_count, "corrected_count": r.corrected_count,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]}


@router.delete("/{name_id}")
def forget_name(name_id: int, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.execute(text("""
        UPDATE isf_recorder.name_memory SET is_deleted = TRUE, wrong_spellings = '[]'::jsonb, updated_at = NOW()
        WHERE id = :id AND org_id = CAST(:o AS uuid) RETURNING id
    """), {"id": name_id, "o": admin["org_id"]}).fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Name not found")
    return {"status": "forgotten", "id": name_id}
