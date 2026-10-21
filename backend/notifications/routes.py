"""
ISF Recorder — Notification API Routes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Allows admins to manually trigger daily WhatsApp summaries or test notifications.
"""
from fastapi import APIRouter, Depends, BackgroundTasks, Query
from backend.auth.routes import require_admin
from backend.notifications.daily_summary import generate_and_send_daily_summary

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.post("/trigger-daily-summary")
async def trigger_daily_summary(
    background_tasks: BackgroundTasks,
    admin: dict = Depends(require_admin)
):
    """Admin-only: Manually trigger the daily WhatsApp summary digest for their organization."""
    org_id = admin["org_id"]
    background_tasks.add_task(generate_and_send_daily_summary, org_id)
    return {"status": "dispatched", "message": "Daily summary generation queued"}


@router.post("/trigger-health-report")
async def trigger_health_report(dry_run: bool = Query(True), admin: dict = Depends(require_admin)):
    """Admin: today's health message. dry_run=true (default) returns the text without sending."""
    from backend.notifications.health_report import send_health_report
    return await send_health_report(dry_run=dry_run)
