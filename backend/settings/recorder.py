"""
ISF Recorder — Recorder configuration, admin lists and default report templates (contract §3, §9)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET  /settings/recorder-config              (any user) modes, role label, lists, template per mode
PUT  /settings/recorder-config              (admin)    report_templates / role_label / default_mode
GET  /settings/lists/{list_type}            (admin)    balgruh | coach_name
POST /settings/lists/{list_type}            (admin)    {name}
PUT  /settings/lists/{list_type}/{id}       (admin)    {name?, is_active?, sort_order?}
DELETE /settings/lists/{list_type}/{id}     (admin)    sets inactive

organization.settings.recorder is written key-by-key inside one UPDATE (jsonb_set / ||), never by
reading the whole settings blob and writing it back, so concurrent writers cannot undo each other.
"""
import json
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.identity import invalidate_org_cache, is_super
from backend.auth.routes import get_current_user, require_admin
from backend.database import SessionLocal, get_db

logger = logging.getLogger("isf_recorder.settings.recorder")
router = APIRouter(prefix="/settings", tags=["Recorder Settings"])

MODES = ["schedule", "meeting"]
LIST_TYPES = ("balgruh", "coach_name")
COACH_TEMPLATE_TITLE = "Report template for Coach"
MEETING_TEMPLATE_TITLE = "Meeting minutes"

from backend.settings.report_prompts import (OBSERVATIONS_OLD_LINE, OBSERVATIONS_NEW_LINE,  # noqa: E402
                                         COACH_REPORT_PROMPT, MEETING_MINUTES_PROMPT,  # noqa: E402
                                             MEETING_MINUTES_PROMPT_V1)

DEFAULT_RECORDER = {"role_label": "Coach", "modes": MODES, "default_mode": "schedule", "report_templates": {}}


# ─── Settings storage ────────────────────────────────────────────────────────

def get_recorder_settings(db: Session, org_id: str) -> dict:
    raw = db.execute(text("SELECT settings -> 'recorder' FROM isf_recorder.organization WHERE id = :id"),
                     {"id": org_id}).scalar()
    if isinstance(raw, str):
        raw = json.loads(raw)
    rec = dict(DEFAULT_RECORDER)
    if isinstance(raw, dict):
        rec.update({k: v for k, v in raw.items() if v is not None})
    if not isinstance(rec.get("report_templates"), dict):
        rec["report_templates"] = {}
    modes = [m for m in (rec.get("modes") or []) if m in MODES] or list(MODES)
    rec["modes"] = modes
    if rec.get("default_mode") not in modes:
        rec["default_mode"] = modes[0]
    return rec


def update_recorder_settings(db: Session, org_id: str, patch: Optional[dict] = None,
                             report_templates: Optional[dict] = None) -> None:
    """One atomic UPDATE: merges `patch` into settings.recorder and `report_templates` into its sub-object."""
    db.execute(text("""
        UPDATE isf_recorder.organization
        SET settings = jsonb_set(
            COALESCE(settings, '{}'::jsonb), '{recorder}',
            (COALESCE(settings -> 'recorder', '{}'::jsonb) || CAST(:patch AS jsonb))
              || jsonb_build_object('report_templates',
                     COALESCE(settings -> 'recorder' -> 'report_templates', '{}'::jsonb) || CAST(:rt AS jsonb)),
            true)
        WHERE id = :org
    """), {"org": org_id, "patch": json.dumps(patch or {}), "rt": json.dumps(report_templates or {})})


def template_for_mode(db: Session, org_id: str, mode: Optional[str]):
    """The org's report template row (id, title, prompt) for a mode, or None. Round 3: Schedule and
    Meeting have their own template (default Coach Report / Meeting minutes); recordings from the old
    app (mode NULL) use the Schedule template."""
    key = mode if mode in MODES else "schedule"
    slots = get_recorder_settings(db, org_id).get("report_templates") or {}
    tid = slots.get(key)
    row = None
    if tid:
        try:
            row = db.execute(text("""
                SELECT id, title, prompt FROM isf_recorder.chat_template WHERE id = CAST(:id AS uuid) AND org_id = :org
            """), {"id": str(tid), "org": org_id}).fetchone()
        except Exception:
            db.rollback()
    if row is None:
        # mapped template deleted (or never set): fall back to the mode's default template
        row = db.execute(text("""
            SELECT id, title, prompt FROM isf_recorder.chat_template WHERE org_id = :org AND title = :t
            ORDER BY created_at LIMIT 1
        """), {"org": org_id, "t": MEETING_TEMPLATE_TITLE if key == "meeting" else COACH_TEMPLATE_TITLE}).fetchone()
    return row


def template_mapped_modes(db: Session, org_id: str, template_id: str) -> list:
    """Modes whose report uses this template (deleting it is refused while mapped)."""
    slots = get_recorder_settings(db, org_id).get("report_templates") or {}
    return [m for m in MODES if str(slots.get(m) or "") == str(template_id)]


def restore_default_templates(db: Session, org_id: str) -> dict:
    """Re-creates 'Report template for Coach' / 'Meeting minutes' if missing (never touches existing
    ones) and points empty or dangling report slots at them. Returns {"created": [...], "mapped": {...}}."""
    created, ids = [], {}
    for title, prompt, icon, order in ((COACH_TEMPLATE_TITLE, COACH_REPORT_PROMPT, "🎓", 0),
                                       (MEETING_TEMPLATE_TITLE, MEETING_MINUTES_PROMPT, "🗒️", 1)):
        row = db.execute(text("""
            SELECT id FROM isf_recorder.chat_template WHERE org_id = :o AND title = :t ORDER BY created_at LIMIT 1
        """), {"o": org_id, "t": title}).fetchone()
        if not row:
            row = db.execute(text("""
                INSERT INTO isf_recorder.chat_template (org_id, title, prompt, icon, sort_order, is_active)
                VALUES (:o, :t, :p, :i, :s, TRUE) RETURNING id
            """), {"o": org_id, "t": title, "p": prompt, "i": icon, "s": order}).fetchone()
            created.append(title)
        ids[title] = str(row.id)
    slots = get_recorder_settings(db, org_id).get("report_templates") or {}
    existing = {str(r.id) for r in db.execute(text("SELECT id FROM isf_recorder.chat_template WHERE org_id = :o"),
                                               {"o": org_id}).fetchall()}
    fill = {}
    for mode, title in (("schedule", COACH_TEMPLATE_TITLE), ("meeting", MEETING_TEMPLATE_TITLE)):
        if str(slots.get(mode) or "") not in existing:
            fill[mode] = ids[title]
    if fill:
        update_recorder_settings(db, org_id, {}, fill)
    db.commit()
    return {"created": created, "mapped": fill}


# ─── Startup seed (§3.4) ─────────────────────────────────────────────────────

def seed_recorder_defaults() -> None:
    """Idempotent, per org. The Coach prompt is replaced once (flag templates_v2_seeded), so later
    admin edits are kept; 'Meeting minutes' is added if missing; empty template slots are filled."""
    with SessionLocal() as db:
        orgs = db.execute(text("SELECT id FROM isf_recorder.organization")).fetchall()
        for org in orgs:
            org_id = str(org.id)
            try:
                from backend.chat.gemini_chat import seed_default_templates
                seed_default_templates(db, org_id, None)  # no-op once an org was seeded
                rec = get_recorder_settings(db, org_id)
                flag = bool(rec.get("templates_v2_seeded"))
                coach = db.execute(text("""
                    SELECT id FROM isf_recorder.chat_template WHERE org_id = :o AND title = :t
                    ORDER BY created_at LIMIT 1
                """), {"o": org_id, "t": COACH_TEMPLATE_TITLE}).fetchone()
                if not flag:
                    if coach:
                        db.execute(text("""
                            UPDATE isf_recorder.chat_template SET prompt = :p, updated_at = NOW() WHERE id = :id
                        """), {"p": COACH_REPORT_PROMPT, "id": coach.id})
                    else:
                        coach = db.execute(text("""
                            INSERT INTO isf_recorder.chat_template (org_id, title, prompt, icon, sort_order)
                            VALUES (:o, :t, :p, '🎓', 0) RETURNING id
                        """), {"o": org_id, "t": COACH_TEMPLATE_TITLE, "p": COACH_REPORT_PROMPT}).fetchone()
                meeting = db.execute(text("""
                    SELECT id FROM isf_recorder.chat_template WHERE org_id = :o AND title = :t
                    ORDER BY created_at LIMIT 1
                """), {"o": org_id, "t": MEETING_TEMPLATE_TITLE}).fetchone()
                if not meeting and not flag:
                    meeting = db.execute(text("""
                        INSERT INTO isf_recorder.chat_template (org_id, title, prompt, icon, sort_order)
                        VALUES (:o, :t, :p, '🗒️', 1) RETURNING id
                    """), {"o": org_id, "t": MEETING_TEMPLATE_TITLE, "p": MEETING_MINUTES_PROMPT}).fetchone()
                current = rec.get("report_templates") or {}
                fill = {}
                if coach and not current.get("schedule"):
                    fill["schedule"] = str(coach.id)
                # Round 3 (one time, flag meeting_report_v1): meetings get "Meeting minutes" when the
                # mapping still has both modes on the Coach template (round 2) or no meeting slot.
                # An admin's later choice is never overwritten.
                patch = {} if flag else {"templates_v2_seeded": True}
                # One time (flag observations_v3, approved by the user 2026-10-13): the Observations
                # line becomes the user's own definition + examples in every saved template that still
                # has the old line word for word. A line an admin already rewrote is left alone.
                if not rec.get("observations_v3"):
                    db.execute(text("""
                        UPDATE isf_recorder.chat_template
                        SET prompt = REPLACE(prompt, :old, :new), updated_at = NOW()
                        WHERE org_id = :o AND POSITION(:old IN prompt) > 0
                    """), {"o": org_id, "old": OBSERVATIONS_OLD_LINE, "new": OBSERVATIONS_NEW_LINE})
                    patch["observations_v3"] = True
                if not rec.get("meeting_report_v1"):
                    sched = current.get("schedule") or fill.get("schedule")
                    if meeting and (not current.get("meeting") or current.get("meeting") == sched == (str(coach.id) if coach else None)):
                        fill["meeting"] = str(meeting.id)
                    patch["meeting_report_v1"] = True
                # Round 3 (one time): new Meeting minutes structure, only if the admin never edited the prompt
                if not rec.get("meeting_prompt_v2"):
                    db.execute(text("""
                        UPDATE isf_recorder.chat_template SET prompt = :p, updated_at = NOW()
                        WHERE org_id = :o AND title = :t AND btrim(prompt) = btrim(:old)
                    """), {"p": MEETING_MINUTES_PROMPT, "o": org_id, "t": MEETING_TEMPLATE_TITLE,
                           "old": MEETING_MINUTES_PROMPT_V1})
                    patch["meeting_prompt_v2"] = True
                for key in ("role_label", "modes", "default_mode"):
                    if key not in rec or rec.get(key) is None:
                        patch[key] = DEFAULT_RECORDER[key]
                if patch or fill:
                    update_recorder_settings(db, org_id, patch, fill)
                db.commit()
                if not flag or fill:
                    logger.info(f"Recorder defaults seeded for org {org_id}: {fill or 'templates updated'}")
            except Exception as e:
                db.rollback()
                logger.exception(f"Recorder seed failed for org {org_id}: {e}")


# ─── Recorder config endpoints ───────────────────────────────────────────────

def _list_items(db: Session, org_id: str, list_type: str, include_inactive: bool = False):
    return db.execute(text(f"""
        SELECT id, name, is_active, sort_order, created_at, updated_at
        FROM isf_recorder.recorder_list_item
        WHERE org_id = :o AND list_type = :t {'' if include_inactive else 'AND is_active = TRUE'}
        ORDER BY sort_order, LOWER(name)
    """), {"o": org_id, "t": list_type}).fetchall()


@router.get("/recorder-config")
def get_recorder_config(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    org_id = current_user["org_id"]
    rec = get_recorder_settings(db, org_id)
    users = db.execute(text("""
        SELECT id, name, phone, role, is_super_admin FROM isf_recorder.user
        WHERE org_id = :o AND is_active = TRUE AND archived_at IS NULL
        ORDER BY LOWER(name)
    """), {"o": org_id}).fetchall()
    extra = [{"user_id": None, "name": r.name} for r in _list_items(db, org_id, "coach_name")]
    coaches = [{"user_id": str(u.id), "name": u.name} for u in users
               if u.role == "staff" and not is_super(u)]
    templates = {}
    for mode in rec["modes"]:
        row = template_for_mode(db, org_id, mode)
        templates[mode] = {"id": str(row.id), "title": row.title} if row else None
    return {
        "role_label": rec.get("role_label") or "Coach",
        "modes": rec["modes"],
        "default_mode": rec["default_mode"],
        "is_super_admin": bool(current_user.get("is_super_admin")),
        "balgruhs": [{"id": str(r.id), "name": r.name} for r in _list_items(db, org_id, "balgruh")],
        "coaches": coaches + extra,
        "participants": [{"user_id": str(u.id), "name": u.name} for u in users] + extra,
        "report_templates": templates,
        "silence_stop_minutes": silence_stop_minutes(rec),
    }


MAX_SILENCE_STOP_MINUTES = 180


def silence_stop_minutes(rec: dict) -> dict:
    """Per mode: stop the recording after this many minutes without voice (0 = never, the default)."""
    raw = rec.get("silence_stop_minutes") if isinstance(rec.get("silence_stop_minutes"), dict) else {}
    out = {}
    for mode in MODES:
        try:
            out[mode] = max(0, min(MAX_SILENCE_STOP_MINUTES, int(raw.get(mode) or 0)))
        except (TypeError, ValueError):
            out[mode] = 0
    return out


class RecorderConfigUpdate(BaseModel):
    report_templates: Optional[Dict[str, Optional[str]]] = None
    role_label: Optional[str] = None
    default_mode: Optional[str] = None
    silence_stop_minutes: Optional[Dict[str, int]] = None


@router.post("/report-templates/restore-defaults")
def restore_report_templates(admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin: bring back the default report templates if they were deleted (existing ones untouched)."""
    res = restore_default_templates(db, admin["org_id"])
    invalidate_org_cache(admin["org_id"])
    return {**res, "config": get_recorder_config(admin, db)}


@router.put("/recorder-config")
def put_recorder_config(body: RecorderConfigUpdate, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    org_id = admin["org_id"]
    patch, templates = {}, {}
    if body.role_label is not None:
        label = body.role_label.strip()
        if not label or len(label) > 30:
            raise HTTPException(status_code=400, detail="Role label must be 1-30 characters")
        patch["role_label"] = label
    if body.default_mode is not None:
        if body.default_mode not in get_recorder_settings(db, org_id)["modes"]:
            raise HTTPException(status_code=400, detail="Unknown mode")
        patch["default_mode"] = body.default_mode
    if body.silence_stop_minutes is not None:
        current = silence_stop_minutes(get_recorder_settings(db, org_id))
        for mode, minutes in body.silence_stop_minutes.items():
            if mode not in MODES:
                raise HTTPException(status_code=400, detail=f"Unknown mode '{mode}'")
            if minutes is None or minutes < 0 or minutes > MAX_SILENCE_STOP_MINUTES:
                raise HTTPException(status_code=400, detail=f"Silence time must be 0-{MAX_SILENCE_STOP_MINUTES} minutes (0 = off)")
            current[mode] = int(minutes)
        patch["silence_stop_minutes"] = current
    for mode, tid in (body.report_templates or {}).items():
        if mode not in MODES:
            raise HTTPException(status_code=400, detail=f"Unknown mode '{mode}'")
        if tid:
            try:
                ok = db.execute(text("""
                    SELECT 1 FROM isf_recorder.chat_template WHERE id = CAST(:id AS uuid) AND org_id = :o
                """), {"id": tid, "o": org_id}).fetchone()
            except Exception:
                db.rollback()
                ok = None
            if not ok:
                raise HTTPException(status_code=400, detail=f"Template {tid} does not belong to your organization")
        templates[mode] = tid
    update_recorder_settings(db, org_id, patch, templates)
    db.commit()
    invalidate_org_cache(org_id)
    return get_recorder_config(admin, db)


# ─── Lists ───────────────────────────────────────────────────────────────────

class ListItemCreate(BaseModel):
    name: str


class ListItemUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


def _check_type(list_type: str) -> None:
    if list_type not in LIST_TYPES:
        raise HTTPException(status_code=400, detail=f"list_type must be one of {', '.join(LIST_TYPES)}")


def _item_json(r) -> dict:
    return {"id": str(r.id), "name": r.name, "is_active": r.is_active, "sort_order": r.sort_order}


def _duplicate(db: Session, org_id: str, list_type: str, name: str, exclude: Optional[str] = None):
    return db.execute(text("""
        SELECT id, is_active FROM isf_recorder.recorder_list_item
        WHERE org_id = :o AND list_type = :t AND LOWER(name) = LOWER(:n)
          AND (CAST(:ex AS uuid) IS NULL OR id <> CAST(:ex AS uuid))
    """), {"o": org_id, "t": list_type, "n": name, "ex": exclude}).fetchone()


def _dup_response(dup) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": "This name is already in the list" + ("" if dup.is_active else " (disabled — enable it instead)"),
        "existing_id": str(dup.id), "is_active": dup.is_active})


@router.get("/lists/{list_type}")
def get_list(list_type: str, include_inactive: bool = Query(False), admin: dict = Depends(require_admin),
             db: Session = Depends(get_db)):
    _check_type(list_type)
    return {"items": [_item_json(r) for r in _list_items(db, admin["org_id"], list_type, include_inactive)]}


@router.post("/lists/{list_type}")
def add_list_item(list_type: str, body: ListItemCreate, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    _check_type(list_type)
    name = " ".join((body.name or "").split())
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Name must be 1-80 characters")
    dup = _duplicate(db, admin["org_id"], list_type, name)
    if dup:
        return _dup_response(dup)
    row = db.execute(text("""
        INSERT INTO isf_recorder.recorder_list_item (org_id, list_type, name, sort_order, created_by)
        VALUES (:o, :t, :n,
                COALESCE((SELECT MAX(sort_order) + 1 FROM isf_recorder.recorder_list_item WHERE org_id = :o AND list_type = :t), 0),
                :by)
        RETURNING id, name, is_active, sort_order
    """), {"o": admin["org_id"], "t": list_type, "n": name, "by": admin["user_id"]}).fetchone()
    db.commit()
    return _item_json(row)


@router.put("/lists/{list_type}/{item_id}")
def update_list_item(list_type: str, item_id: str, body: ListItemUpdate, admin: dict = Depends(require_admin),
                     db: Session = Depends(get_db)):
    _check_type(list_type)
    sets, params = [], {"id": item_id, "o": admin["org_id"], "t": list_type}
    if body.name is not None:
        name = " ".join(body.name.split())
        if not name or len(name) > 80:
            raise HTTPException(status_code=400, detail="Name must be 1-80 characters")
        dup = _duplicate(db, admin["org_id"], list_type, name, exclude=item_id)
        if dup:
            return _dup_response(dup)
        sets.append("name = :name")
        params["name"] = name
    if body.is_active is not None:
        sets.append("is_active = :active")
        params["active"] = body.is_active
    if body.sort_order is not None:
        sets.append("sort_order = :sort")
        params["sort"] = body.sort_order
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        row = db.execute(text(f"""
            UPDATE isf_recorder.recorder_list_item SET {', '.join(sets)}, updated_at = NOW()
            WHERE id = CAST(:id AS uuid) AND org_id = :o AND list_type = :t
            RETURNING id, name, is_active, sort_order
        """), params).fetchone()
    except Exception:
        db.rollback()
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="List item not found")
    db.commit()
    return _item_json(row)


@router.delete("/lists/{list_type}/{item_id}")
def delete_list_item(list_type: str, item_id: str, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    return update_list_item(list_type, item_id, ListItemUpdate(is_active=False), admin, db)
