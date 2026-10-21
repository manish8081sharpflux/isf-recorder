"""
Daily admin health message (WhatsApp, super admins only, 21:05 IST): what happened today and whether
the server is healthy. collect_health_stats() reads the DB; build_health_text() is a pure formatter.
"""
import logging
import shutil
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal

logger = logging.getLogger("isf_recorder.health_report")

IST = timezone(timedelta(hours=5, minutes=30))
LOW_DISK_GB = 15.0
STUCK_MINUTES = 30


def collect_health_stats(db: Session, day: Optional[datetime] = None) -> Dict:
    from backend.config import AUDIO_STORAGE_ROOT
    from backend.system.app_config import get_values
    day = (day or datetime.now(IST)).astimezone(IST)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    p = {"s": start, "e": end}
    rec = db.execute(text("""
        SELECT COUNT(*) AS started,
               COUNT(*) FILTER (WHERE status = 'completed') AS completed,
               COUNT(*) FILTER (WHERE status = 'error') AS errors,
               COALESCE(SUM(duration_seconds), 0) AS seconds
        FROM isf_recorder.recording WHERE started_at >= :s AND started_at < :e
    """), p).fetchone()
    stuck = db.execute(text(f"""
        SELECT COUNT(*) FROM isf_recorder.recording
        WHERE status = 'processing'
          AND COALESCE(finish_requested_at, finished_at, started_at) < NOW() - INTERVAL '{STUCK_MINUTES} minutes'
    """)).scalar() or 0
    unfinished = db.execute(text("""
        SELECT COUNT(*) FROM isf_recorder.recording r
        WHERE r.status = 'recording' AND r.finish_requested_at IS NULL
          AND NOT (r.last_heartbeat_at IS NOT NULL AND r.last_heartbeat_at > NOW() - INTERVAL '3 minutes')
    """)).scalar() or 0
    cost_rows = db.execute(text("""
        SELECT operation_type, COALESCE(SUM(total_cost_inr), 0) AS inr, COUNT(*) AS calls
        FROM isf_recorder.gemini_usage_log WHERE created_at >= :s AND created_at < :e
        GROUP BY operation_type ORDER BY 2 DESC
    """), p).fetchall()
    try:
        from backend.transcription.chunked_stt import RESCUE_MODEL
    except Exception:
        RESCUE_MODEL = ""
    rescues = None
    if RESCUE_MODEL:
        rescues = db.execute(text("""
            SELECT COUNT(DISTINCT (recording_id, start_sec)) FROM isf_recorder.transcription_chunk
            WHERE model = :m AND updated_at >= :s AND updated_at < :e
        """), {**p, "m": RESCUE_MODEL}).scalar() or 0
        rescue_inr = float(db.execute(text("""
            SELECT COALESCE(SUM(total_cost_inr), 0) FROM isf_recorder.gemini_usage_log
            WHERE gemini_model = :m AND operation_type = 'TRANSCRIPTION' AND created_at >= :s AND created_at < :e
        """), {**p, "m": RESCUE_MODEL}).scalar() or 0)
    else:
        rescue_inr = 0.0
    # Failed AI calls cost nothing but say something went wrong (e.g. a missing API key)
    failed = db.execute(text("""
        SELECT gemini_model, LEFT(COALESCE(error_message, ''), 80) AS err, COUNT(*) AS n
        FROM isf_recorder.gemini_usage_log
        WHERE created_at >= :s AND created_at < :e AND COALESCE(api_status, 'SUCCESS') <> 'SUCCESS'
        GROUP BY 1, 2 ORDER BY 3 DESC
    """), p).fetchall()
    versions = [r[0] for r in db.execute(text("""
        SELECT DISTINCT device_info ->> 'app_version' FROM isf_recorder.recording
        WHERE started_at >= :s AND started_at < :e AND device_info ->> 'app_version' IS NOT NULL
        ORDER BY 1
    """), p).fetchall()]
    try:
        free_gb = shutil.disk_usage(AUDIO_STORAGE_ROOT).free / 1024 ** 3
    except Exception:
        free_gb = None
    backup = get_values(db, ("last_backup_at", "last_backup_ok", "last_backup_bytes"))
    return {
        "date": start, "started": rec.started, "completed": rec.completed, "errors": rec.errors,
        "stuck": stuck, "unfinished": unfinished, "audio_hours": float(rec.seconds or 0) / 3600.0,
        "cost": [(r.operation_type, float(r.inr or 0), int(r.calls)) for r in cost_rows],
        "rescues": rescues, "rescue_inr": rescue_inr, "free_gb": free_gb, "versions": versions,
        "failed": [(r.gemini_model, r.err, int(r.n)) for r in failed],
        "backup_at": backup["last_backup_at"] or None, "backup_ok": backup["last_backup_ok"],
        "backup_bytes": backup["last_backup_bytes"],
    }


def _size(b) -> str:
    try:
        n = float(b)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def build_health_text(s: Dict) -> str:
    total = sum(c[1] for c in s["cost"])
    by_op = ", ".join(f"{op.title()} ₹{inr:.0f}" for op, inr, _ in s["cost"] if inr >= 0.5) or "none"
    lines = [
        f"🩺 Jomi health — {s['date'].strftime('%d %b %Y')}",
        f"🎙️ Recordings: {s['started']} started · {s['completed']} done · {s['errors']} errors"
        + (f" · ⚠️ {s['stuck']} stuck" if s["stuck"] else ""),
        f"⏱️ Audio: {s['audio_hours']:.1f} h",
        f"💰 AI cost: ₹{total:.0f} ({by_op})",
    ]
    if s.get("rescues"):
        lines.append(f"🛟 Rescue windows: {s['rescues']} (₹{s.get('rescue_inr', 0):.0f})")
    if s.get("failed"):
        n = sum(f[2] for f in s["failed"])
        model, err, _ = s["failed"][0]
        lines.append(f"⚠️ Failed AI calls: {n} (no cost) — {model}: {err or 'error'}")
    if s.get("free_gb") is not None:
        warn = " ⚠️ LOW" if s["free_gb"] < LOW_DISK_GB else ""
        lines.append(f"💾 Free disk (audio): {s['free_gb']:.0f} GB{warn}")
    if s.get("backup_ok") in ("true", "false"):
        when = ""
        if s.get("backup_at"):
            try:
                when = datetime.fromisoformat(s["backup_at"]).astimezone(IST).strftime(" %d %b %H:%M")
            except ValueError:
                when = ""
        status = f"✅ ok, {_size(s.get('backup_bytes'))}" if s["backup_ok"] == "true" else "❌ FAILED"
        lines.append(f"🗄️ Backup{when}: {status}")
    else:
        lines.append("🗄️ Backup: none yet")
    if s.get("unfinished"):
        lines.append(f"⏸️ Unfinished sessions: {s['unfinished']}")
    if s.get("versions"):
        lines.append(f"📱 App versions today: {', '.join(s['versions'])}")
    return "\n".join(lines)


def _super_admin_phones(db: Session) -> List[str]:
    from backend.auth.identity import is_super_admin_phone
    rows = db.execute(text("""
        SELECT phone, is_super_admin FROM isf_recorder.user
        WHERE is_active = TRUE AND archived_at IS NULL AND role = 'admin'
    """)).fetchall()
    return [r.phone for r in rows if r.is_super_admin or is_super_admin_phone(r.phone)]


async def send_health_report(dry_run: bool = False) -> Dict:
    """Builds today's report; sends it to every super admin unless dry_run. Never raises."""
    try:
        with SessionLocal() as db:
            body = build_health_text(collect_health_stats(db))
            phones = _super_admin_phones(db)
        if dry_run:
            return {"sent": 0, "recipients": len(phones), "text": body}
        from backend.notifications.daily_summary import send_whatsapp_text
        sent = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for phone in phones:
                sent += 1 if await send_whatsapp_text(client, phone, body) else 0
        logger.info(f"Health report sent to {sent}/{len(phones)} super admins")
        return {"sent": sent, "recipients": len(phones), "text": body}
    except Exception as e:
        logger.exception(f"Health report failed: {e}")
        return {"sent": 0, "error": str(e)[:300]}
