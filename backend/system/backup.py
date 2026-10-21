"""
Nightly backup of the isf_recorder schema (scheduler job, 02:30 server time).

pg_dump -n isf_recorder -Fc -> BACKUP_DIR\\isf_recorder_YYYYMMDD.dump (written to a .tmp first),
verified (size > 0 and `pg_restore --list` succeeds), newest BACKUP_KEEP_DAYS kept. The result is
stored in app_config (last_backup_at / last_backup_ok / last_backup_bytes). Never raises.
"""
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from backend.database import SessionLocal
from backend.system.app_config import set_values

logger = logging.getLogger("isf_recorder.backup")

DEFAULT_PG_DUMP = r"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"
DEFAULT_BACKUP_DIR = r"C:\GroundUp\backups\isf_recorder"
NAME_RE = re.compile(r"^isf_recorder_(\d{8})\.dump$")


def _settings() -> Dict:
    pg_dump = os.getenv("PG_DUMP_PATH", DEFAULT_PG_DUMP)
    return {
        "dir": Path(os.getenv("BACKUP_DIR", DEFAULT_BACKUP_DIR)),
        "keep": max(1, int(os.getenv("BACKUP_KEEP_DAYS", "14") or 14)),
        "pg_dump": pg_dump,
        "pg_restore": os.getenv("PG_RESTORE_PATH") or str(Path(pg_dump).with_name(
            Path(pg_dump).name.replace("pg_dump", "pg_restore"))),
    }


def _db_url() -> str:
    from backend.config import DATABASE_URL
    # libpq does not understand SQLAlchemy driver suffixes such as postgresql+psycopg2://
    return re.sub(r"^postgresql\+\w+://", "postgresql://", DATABASE_URL)


def prune(backup_dir: Path, keep: int) -> int:
    dumps = sorted((p for p in backup_dir.glob("isf_recorder_*.dump") if NAME_RE.match(p.name)),
                   key=lambda p: p.name, reverse=True)
    removed = 0
    for old in dumps[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Could not remove old backup {old}: {e}")
    return removed


def run_backup(now: Optional[datetime] = None, record: bool = True) -> Dict:
    """Returns {"ok", "path", "bytes", "error"?, "pruned"}; also records the result in app_config."""
    now = now or datetime.now()
    cfg = _settings()
    result = {"ok": False, "path": None, "bytes": 0, "pruned": 0}
    try:
        cfg["dir"].mkdir(parents=True, exist_ok=True)
        final = cfg["dir"] / f"isf_recorder_{now.strftime('%Y%m%d')}.dump"
        tmp = final.with_suffix(".dump.tmp")
        result["path"] = str(final)
        dump = subprocess.run([cfg["pg_dump"], "-n", "isf_recorder", "-Fc", "-f", str(tmp), "-d", _db_url()],
                              capture_output=True, text=True, timeout=3600)
        if dump.returncode != 0:
            raise RuntimeError(f"pg_dump exit {dump.returncode}: {(dump.stderr or '').strip()[:300]}")
        size = tmp.stat().st_size if tmp.exists() else 0
        if size <= 0:
            raise RuntimeError("pg_dump produced an empty file")
        check = subprocess.run([cfg["pg_restore"], "--list", str(tmp)], capture_output=True, text=True, timeout=600)
        if check.returncode != 0:
            raise RuntimeError(f"pg_restore --list failed: {(check.stderr or '').strip()[:300]}")
        os.replace(tmp, final)
        result.update(ok=True, bytes=size)
        result["pruned"] = prune(cfg["dir"], cfg["keep"])
        logger.info(f"Backup ok: {final} ({size} bytes, {result['pruned']} old removed)")
    except Exception as e:
        result["error"] = str(e)[:300]
        logger.error(f"Nightly backup FAILED: {result['error']}")
        try:
            tmp_path = Path(result["path"]).with_suffix(".dump.tmp") if result["path"] else None
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
    if record:
        try:
            with SessionLocal() as db:
                set_values(db, {"last_backup_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "last_backup_ok": "true" if result["ok"] else "false",
                                "last_backup_bytes": result["bytes"]})
                db.commit()
        except Exception as e:
            logger.error(f"Could not record the backup result: {e}")
    return result


def run_backup_job() -> None:
    """Scheduler entry point: never lets a failure escape."""
    try:
        run_backup()
    except Exception as e:  # pragma: no cover - run_backup already catches everything
        logger.exception(f"Backup job crashed: {e}")
