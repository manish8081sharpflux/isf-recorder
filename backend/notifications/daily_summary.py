"""
ISF Recorder — Daily WhatsApp Summary Notification
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Collects all recordings, summaries, and action items from the day,
formats an executive digest, and sends it via WhatsApp to administrators.
Scheduled to run daily at 9:00 PM IST (configurable).
"""
import logging
from datetime import datetime, date
from typing import Optional, List
import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.config import (
    META_API_VERSION,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_ACCESS_TOKEN,
)

logger = logging.getLogger("isf_recorder.daily_summary")


async def generate_and_send_daily_summary(org_id: Optional[str] = None):
    """
    Builds the daily summary for the given org (or all active orgs) and dispatches via WhatsApp.
    """
    db: Session = SessionLocal()
    try:
        orgs = db.execute(
            text("SELECT id, name, slug FROM isf_recorder.organization WHERE is_active = TRUE")
        ).fetchall()

        for org in orgs:
            if org_id and str(org.id) != str(org_id):
                continue
            await _send_org_summary(db, str(org.id), org.name)
    except Exception as e:
        logger.exception(f"Error during daily summary run: {e}")
    finally:
        db.close()


async def _send_org_summary(db: Session, org_id: str, org_name: str):
    """Generates and sends summary for a specific organization."""
    today = date.today()

    # 1. Fetch today's completed recordings
    recordings = db.execute(
        text("""
            SELECT r.id, r.title, r.duration_seconds, r.overview, r.decisions,
                   COALESCE(u.name, r.recorded_by_name, 'Unknown') as user_name
            FROM isf_recorder.recording r
            LEFT JOIN isf_recorder.user u ON r.user_id = u.id
            WHERE r.org_id = :org_id
              AND r.is_deleted = FALSE
              AND r.created_at::date = :today
            ORDER BY r.created_at ASC
        """),
        {"org_id": org_id, "today": today}
    ).fetchall()

    if not recordings:
        logger.info(f"No recordings today for org {org_name}. Skipping daily summary.")
        return

    # 2. Fetch today's action items
    action_items = db.execute(
        text("""
            SELECT description, assigned_to, urgency
            FROM isf_recorder.action_item
            WHERE org_id = :org_id
              AND created_at::date = :today
            ORDER BY
              CASE urgency
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'normal' THEN 3
                ELSE 4
              END ASC
        """),
        {"org_id": org_id, "today": today}
    ).fetchall()

    # 3. Find admins in this org to send to
    admins = db.execute(
        text("""
            SELECT phone, name FROM isf_recorder.user
            WHERE org_id = :org_id AND role = 'admin' AND is_active = TRUE AND archived_at IS NULL
        """),
        {"org_id": org_id}
    ).fetchall()

    if not admins:
        logger.warning(f"No active admins found for org {org_name} to send summary.")
        return

    # 4. Format digest message
    total_recs = len(recordings)
    total_seconds = sum(r.duration_seconds or 0 for r in recordings)
    minutes = total_seconds // 60

    lines = [
        f"📊 *{org_name} — Daily Audio & Meeting Digest*",
        f"📅 Date: {today.strftime('%d %b %Y')}",
        f"🎙️ Recordings Today: *{total_recs}* ({minutes} mins total)",
        "",
        "📝 *Summary of Conversations:*"
    ]

    for idx, r in enumerate(recordings, 1):
        title = r.title or "Untitled Session"
        lines.append(f"{idx}. *{title}* (by {r.user_name})")
        if r.overview:
            short_overview = r.overview[:180] + ("..." if len(r.overview) > 180 else "")
            lines.append(f"   _{short_overview}_")

    if action_items:
        lines.append("")
        lines.append("⚡ *Action Items Captured:*")
        for item in action_items[:8]:  # Top 8 items
            urgency_icon = "🔴" if item.urgency in ("high", "critical") else "🔹"
            assigned = f" (@{item.assigned_to})" if item.assigned_to else ""
            lines.append(f"{urgency_icon} {item.description}{assigned}")

    message_text = "\n".join(lines)

    # 5. Dispatch via WhatsApp to each admin
    async with httpx.AsyncClient(timeout=15) as client:
        for admin in admins:
            if await send_whatsapp_text(client, admin.phone, message_text):
                logger.info(f"Sent daily summary to admin {admin.name}")


async def send_whatsapp_text(client: httpx.AsyncClient, phone: str, body: str) -> bool:
    """Free-form WhatsApp text through the Meta Cloud API (shared by the digest and the health report)."""
    url = f"https://graph.facebook.com/{META_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    clean_phone = (phone or "").replace("+", "").replace(" ", "").replace("-", "")
    payload = {"messaging_product": "whatsapp", "to": clean_phone, "type": "text", "text": {"body": body}}
    try:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return True
        logger.warning(f"WhatsApp send to ...{clean_phone[-4:]} failed: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        logger.error(f"WhatsApp dispatch exception for ...{clean_phone[-4:]}: {e}")
    return False
