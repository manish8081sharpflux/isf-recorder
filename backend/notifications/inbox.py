"""
ISF Recorder — Phone notifications (21 Oct 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The server saves one row per person per notification (isf_recorder.user_notification); the app collects
new rows every 15 min in the background and on every app open, and shows them as phone notifications.
(No Firebase yet: when a Firebase project exists, notify() is the one place that would also push.)

Who gets what:
  coaches  no_recording        6 PM: "No recording today" (only coaches who recorded in the last 14 days)
           report_edited       an admin edited or finalized their report
  admins   admin_report_ready  every new class / meeting report (off by default)
           admin_daily_summary 8 PM: today's recordings, reports and failures
           admin_failed        a recording or its report failed
           call_shared         a coach shared a call with admins
  everyone announcement        a message typed by a super admin (cannot be switched off)
  everyone upload_stuck        shown by the PHONE itself (pieces not uploaded for 1 h); only its switch is here
Each person can switch the others off (user.notify_prefs). Calls never notify admins unless shared.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.identity import is_super
from backend.database import SessionLocal

logger = logging.getLogger("isf_recorder.notifications.inbox")
IST = timezone(timedelta(hours=5, minutes=30))

COACH, ADMIN, EVERYONE = "coach", "admin", "everyone"
# kind → (who, on by default, label in Settings)
KINDS: Dict[str, tuple] = {
    "no_recording":        (COACH,    True,  "Reminder at 6 PM if I have not recorded today"),
    "report_edited":       (COACH,    True,  "An admin edited or finalized my report"),
    "upload_stuck":        (EVERYONE, True,  "A recording has not uploaded for 1 hour"),
    "admin_report_ready":  (ADMIN,    False, "Every new report from the team"),
    "admin_daily_summary": (ADMIN,    True,  "Daily summary at 8 PM"),
    "admin_failed":        (ADMIN,    True,  "A recording or report failed"),
    "call_shared":         (ADMIN,    True,  "Someone shared a call with me"),
}
ALWAYS_ON = {"announcement"}
KEEP_DAYS = 60


def _is_admin(row) -> bool:
    return row.role == "admin" or is_super(row)


def applies(kind: str, admin: bool) -> bool:
    who = KINDS[kind][0]
    return who == EVERYONE or (who == ADMIN) == admin


def prefs_with_defaults(prefs: Optional[dict]) -> Dict[str, bool]:
    prefs = prefs or {}
    return {k: bool(prefs.get(k, default)) for k, (_, default, _) in KINDS.items()}


def wants(prefs: Optional[dict], kind: str) -> bool:
    if kind in ALWAYS_ON:
        return True
    return prefs_with_defaults(prefs).get(kind, True)


def _people(db: Session, org_id: str, user_ids: Optional[Iterable[str]] = None):
    """Active people of the org (optionally only [user_ids]) with role / super-admin / prefs."""
    sql = """SELECT id, org_id, name, phone, role, is_super_admin, COALESCE(notify_prefs, '{}'::jsonb) AS notify_prefs
             FROM isf_recorder."user"
             WHERE org_id = CAST(:o AS uuid) AND is_active = TRUE AND archived_at IS NULL"""
    params = {"o": str(org_id)}
    if user_ids is not None:
        ids = [str(u) for u in user_ids if u]
        if not ids:
            return []
        sql += " AND id = ANY(CAST(:ids AS uuid[]))"
        params["ids"] = ids
    return db.execute(text(sql), params).fetchall()


def admins_of(db: Session, org_id: str) -> list:
    return [p for p in _people(db, org_id) if _is_admin(p)]


def notify(db: Session, people, kind: str, title: str, body: str = "", recording_id: Optional[str] = None,
           dedupe: Optional[str] = None, created_by: Optional[str] = None) -> int:
    """Saves one notification per person who wants [kind]; the same [dedupe] never twice per person.
    [people] are rows from _people(). Commits. Returns how many were saved."""
    saved = 0
    for p in people:
        if not wants(p.notify_prefs, kind):
            continue
        r = db.execute(text("""
            INSERT INTO isf_recorder.user_notification (org_id, user_id, kind, title, body, recording_id, dedupe_key, created_by)
            VALUES (:o, :u, :k, :t, :b, CAST(:r AS uuid), :d, CAST(:c AS uuid))
            ON CONFLICT (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
            RETURNING id
        """), {"o": str(p.org_id), "u": str(p.id), "k": kind, "t": title[:200], "b": (body or "")[:1000],
               "r": recording_id, "d": dedupe, "c": created_by}).fetchone()
        saved += 1 if r else 0
    db.commit()
    return saved


def notify_safely(fn, *args, **kwargs) -> None:
    """Notifications never break the action that caused them."""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Notification skipped ({getattr(fn, '__name__', fn)}): {e}")


def _title(rec) -> str:
    return ((rec.title or "").replace("\\n", " ").strip() or "Untitled recording")[:120]


# ─── Event hooks (called from the routes) ─────────────────────────────────────

def on_report_changed_by_admin(db: Session, recording_id: str, admin: dict, action: str) -> None:
    """An admin edited / finalized someone else's report → tell the owner (at most one 'edited' per hour)."""
    rec = db.execute(text("""SELECT id, org_id, user_id, title, source FROM isf_recorder.recording
                             WHERE id = CAST(:id AS uuid)"""), {"id": recording_id}).fetchone()
    if not rec or rec.source == "call" or not rec.user_id or str(rec.user_id) == admin["user_id"]:
        return
    who = admin.get("name") or "An admin"
    hour = datetime.now(IST).strftime("%Y%m%d%H")
    if action == "finalized":
        title, dedupe = f"✅ Report finalized: {_title(rec)}", f"finalized:{rec.id}:{hour}"
        body = f"{who} finalized your report."
    else:
        title, dedupe = f"✏️ Report edited: {_title(rec)}", f"edited:{rec.id}:{hour}"
        body = f"{who} made changes to your report."
    notify(db, _people(db, str(rec.org_id), [rec.user_id]), "report_edited", title, body,
           recording_id=str(rec.id), dedupe=dedupe, created_by=admin["user_id"])


def on_call_shared(db: Session, recording_id: str, owner: dict) -> None:
    rec = db.execute(text("""SELECT id, org_id, user_id, title, contact_name FROM isf_recorder.recording
                             WHERE id = CAST(:id AS uuid)"""), {"id": recording_id}).fetchone()
    if not rec:
        return
    admins = [a for a in admins_of(db, str(rec.org_id)) if str(a.id) != str(rec.user_id)]
    who = owner.get("name") or "A coach"
    call = (getattr(rec, "contact_name", None) or _title(rec))
    notify(db, admins, "call_shared", f"📞 {who} shared a call with you", f"Call: {call}",
           recording_id=str(rec.id), dedupe=f"call_shared:{rec.id}", created_by=owner["user_id"])


def announce(db: Session, sender: dict, message: str, title: Optional[str], audience: str) -> int:
    people = _people(db, sender["org_id"])
    if audience == "coaches":
        people = [p for p in people if not _is_admin(p)]
    elif audience == "admins":
        people = [p for p in people if _is_admin(p)]
    title = (title or "").strip() or f"📢 Message from {sender.get('name') or 'Jomi'}"
    return notify(db, people, "announcement", title, message.strip(), created_by=sender["user_id"])


# ─── Background jobs ─────────────────────────────────────────────────────────

def scan_events(db: Optional[Session] = None) -> dict:
    """Every 2 min: new failures and new reports of the last day → admins (each event once)."""
    own = db is None
    db = db or SessionLocal()
    out = {"failed": 0, "report_failed": 0, "ready": 0}
    try:
        admins_by_org: dict = {}

        def admins(org):
            if org not in admins_by_org:
                admins_by_org[org] = admins_of(db, org)
            return admins_by_org[org]

        failed = db.execute(text("""
            SELECT r.id, r.org_id, r.user_id, r.title, r.error_message, COALESCE(u.name, r.recorded_by_name) AS owner
            FROM isf_recorder.recording r LEFT JOIN isf_recorder."user" u ON u.id = r.user_id
            WHERE r.status = 'error' AND r.source = 'recording' AND r.is_deleted = FALSE
              AND COALESCE(r.finished_at, r.started_at, r.created_at) > NOW() - INTERVAL '1 day'
        """)).fetchall()
        for r in failed:
            out["failed"] += notify(db, admins(str(r.org_id)), "admin_failed", f"⚠️ Recording failed: {_title(r)}",
                                    f"{r.owner or 'Coach'} · {(r.error_message or 'Transcription failed').strip()[:150]}",
                                    recording_id=str(r.id), dedupe=f"rec_failed:{r.id}")

        reports = db.execute(text("""
            SELECT r.id, r.org_id, r.user_id, r.title, rr.status, rr.error_message,
                   COALESCE(u.name, r.recorded_by_name) AS owner
            FROM isf_recorder.recording_report rr
            JOIN isf_recorder.recording r ON r.id = rr.recording_id
            LEFT JOIN isf_recorder."user" u ON u.id = r.user_id
            WHERE r.source = 'recording' AND r.is_deleted = FALSE
              AND ((rr.status = 'error' AND rr.updated_at > NOW() - INTERVAL '1 day')
                OR (rr.status IN ('draft', 'final') AND rr.generated_at > NOW() - INTERVAL '1 day'))
        """)).fetchall()
        for r in reports:
            team = [a for a in admins(str(r.org_id)) if str(a.id) != str(r.user_id)]
            if r.status == "error":
                out["report_failed"] += notify(db, team, "admin_failed", f"⚠️ Report failed: {_title(r)}",
                                               f"{r.owner or 'Coach'} · {(r.error_message or '').strip()[:150]}",
                                               recording_id=str(r.id), dedupe=f"report_failed:{r.id}")
            else:
                out["ready"] += notify(db, team, "admin_report_ready", f"📝 New report: {_title(r)}",
                                       f"By {r.owner or 'a coach'}", recording_id=str(r.id), dedupe=f"ready:{r.id}")
        return out
    finally:
        if own:
            db.close()


def _today_start(now: Optional[datetime] = None) -> datetime:
    now = (now or datetime.now(IST)).astimezone(IST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def daily_summary(db: Optional[Session] = None, now: Optional[datetime] = None) -> int:
    """8 PM: one notification per admin with today's numbers (only when something was recorded)."""
    own = db is None
    db = db or SessionLocal()
    try:
        start = _today_start(now)
        day = start.strftime("%Y-%m-%d")
        sent = 0
        for org in db.execute(text("SELECT id FROM isf_recorder.organization WHERE is_active = TRUE")).fetchall():
            rows = db.execute(text("""
                SELECT COALESCE(u.name, r.recorded_by_name, 'Unknown') AS owner, r.status, rr.status AS report_status
                FROM isf_recorder.recording r
                LEFT JOIN isf_recorder."user" u ON u.id = r.user_id
                LEFT JOIN isf_recorder.recording_report rr ON rr.recording_id = r.id
                WHERE r.org_id = :o AND r.source = 'recording' AND r.is_deleted = FALSE
                  AND COALESCE(r.started_at, r.created_at) >= :s
            """), {"o": org.id, "s": start}).fetchall()
            if not rows:
                continue
            ready = sum(1 for r in rows if r.report_status in ("draft", "final"))
            failed = sum(1 for r in rows if r.status == "error" or r.report_status == "error")
            by_person: Dict[str, int] = {}
            for r in rows:
                by_person[r.owner] = by_person.get(r.owner, 0) + 1
            people = ", ".join(f"{n} {c}" for n, c in sorted(by_person.items(), key=lambda x: (-x[1], x[0])))
            parts = [f"{len(rows)} recording{'s' if len(rows) != 1 else ''}", f"{ready} report{'s' if ready != 1 else ''} ready"]
            if failed:
                parts.append(f"{failed} failed")
            sent += notify(db, admins_of(db, str(org.id)), "admin_daily_summary", "📊 Today in Jomi",
                           " · ".join(parts) + f"\n{people}", dedupe=f"daily:{day}")
        return sent
    finally:
        if own:
            db.close()


def no_recording_reminder(db: Optional[Session] = None, now: Optional[datetime] = None) -> int:
    """6 PM: coaches who recorded in the last 14 days but not today."""
    own = db is None
    db = db or SessionLocal()
    try:
        start = _today_start(now)
        day = start.strftime("%Y-%m-%d")
        rows = db.execute(text("""
            SELECT u.id, u.org_id, u.name, u.phone, u.role, u.is_super_admin, COALESCE(u.notify_prefs, '{}'::jsonb) AS notify_prefs
            FROM isf_recorder."user" u
            WHERE u.is_active = TRUE AND u.archived_at IS NULL
              AND EXISTS (SELECT 1 FROM isf_recorder.recording r WHERE r.user_id = u.id AND r.source = 'recording'
                          AND r.is_deleted = FALSE AND COALESCE(r.started_at, r.created_at) >= :since)
              AND NOT EXISTS (SELECT 1 FROM isf_recorder.recording r WHERE r.user_id = u.id AND r.source = 'recording'
                              AND COALESCE(r.started_at, r.created_at) >= :s)
        """), {"since": start - timedelta(days=14), "s": start}).fetchall()
        coaches = [p for p in rows if not _is_admin(p)]
        return notify(db, coaches, "no_recording", "🎙️ No recording today",
                      "You have not recorded a session today. If you had one, open Jomi to check it was saved.",
                      dedupe=f"norec:{day}")
    finally:
        if own:
            db.close()


def cleanup(db: Optional[Session] = None) -> int:
    own = db is None
    db = db or SessionLocal()
    try:
        n = db.execute(text(f"DELETE FROM isf_recorder.user_notification WHERE created_at < NOW() - INTERVAL '{KEEP_DAYS} days'")).rowcount
        db.commit()
        return n
    finally:
        if own:
            db.close()


def run_job(name: str) -> None:
    """Scheduler entry point: one job, errors logged, never raised."""
    fn = {"scan": scan_events, "daily": daily_summary, "norec": no_recording_reminder, "cleanup": cleanup}[name]
    try:
        fn()
    except Exception as e:
        logger.exception(f"Notification job '{name}' failed: {e}")
