"""
App version endpoints: GET /app/latest (public) for the "Update available" prompt, and
PUT /admin/app-version so a release only needs: copy the APK + bump the version.
"""
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth.routes import require_admin
from backend.database import get_db
from backend.system.app_config import get_values, set_values

router = APIRouter(tags=["App"])

KEYS = ("latest_version_code", "latest_version_name", "latest_apk_notes", "min_supported_version_code",
        "notify_test_builds")


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def public_base_url(request: Request) -> str:
    """The URL the phone used (works behind cloudflared / nginx); PUBLIC_BASE_URL overrides."""
    env = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}"


def _latest(db: Session, request: Request) -> dict:
    v = get_values(db, KEYS)
    return {
        "version_code": _int(v["latest_version_code"]),
        "version_name": v["latest_version_name"] or "",
        "apk_url": f"{public_base_url(request)}/apk",
        "notes": v["latest_apk_notes"] or "",
        "min_supported_version_code": _int(v["min_supported_version_code"], 1),
        # the app sets force = its versionCode < min_supported_version_code
        "force": False,
        "notify_test_builds": (v["notify_test_builds"] or "").strip().lower() == "true",
    }


@router.get("/app/latest")
def app_latest(request: Request, db: Session = Depends(get_db)):
    """No auth: the newest app build and the oldest build still allowed."""
    return _latest(db, request)


class AppVersionUpdate(BaseModel):
    version_code: Optional[int] = None
    version_name: Optional[str] = None
    notes: Optional[str] = None
    min_supported_version_code: Optional[int] = None
    notify_test_builds: Optional[bool] = None


@router.put("/admin/app-version")
def put_app_version(body: AppVersionUpdate, request: Request, admin: dict = Depends(require_admin),
                    db: Session = Depends(get_db)):
    values = {}
    if body.version_code is not None:
        if body.version_code < 1:
            raise HTTPException(status_code=400, detail="version_code must be >= 1")
        values["latest_version_code"] = body.version_code
    if body.version_name is not None:
        if not body.version_name.strip():
            raise HTTPException(status_code=400, detail="version_name cannot be empty")
        values["latest_version_name"] = body.version_name.strip()
    if body.notes is not None:
        values["latest_apk_notes"] = body.notes.strip()[:1000]
    if body.min_supported_version_code is not None:
        values["min_supported_version_code"] = max(1, body.min_supported_version_code)
    if body.notify_test_builds is not None:
        values["notify_test_builds"] = "true" if body.notify_test_builds else "false"
    if not values:
        raise HTTPException(status_code=400, detail="Nothing to update")
    set_values(db, values)
    db.commit()
    return _latest(db, request)
