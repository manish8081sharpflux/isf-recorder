"""
ISF Recorder — Live Forex Sync Service (USD -> INR)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches and caches real live USD to INR exchange rates from public internet feeds.
Provides automatic daily/hourly synchronization and graceful fallback.
"""
import time
import json
import logging
import urllib.request
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal

logger = logging.getLogger("isf_recorder.forex")

# In-memory cache
_cached_rate: float = 96.0
_last_fetch_time: float = 0.0
CACHE_TTL_SECONDS: float = 3600.0  # 1 hour freshness


def _fetch_from_internet() -> Optional[float]:
    """Queries live internet exchange rate feeds."""
    headers = {"User-Agent": "ISFRecorder/2.0 (ForexSync)"}

    # 1. Primary: open.er-api.com
    try:
        req = urllib.request.Request("https://open.er-api.com/v6/latest/USD", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            inr = data.get("rates", {}).get("INR")
            if inr and float(inr) > 0:
                logger.info(f"Live USD->INR rate fetched from open.er-api: {float(inr):.4f}")
                return float(inr)
    except Exception as e:
        logger.warning(f"Primary forex feed open.er-api failed: {e}")

    # 2. Secondary fallback: exchangerate-api.com
    try:
        req = urllib.request.Request("https://api.exchangerate-api.com/v4/latest/USD", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            inr = data.get("rates", {}).get("INR")
            if inr and float(inr) > 0:
                logger.info(f"Live USD->INR rate fetched from exchangerate-api: {float(inr):.4f}")
                return float(inr)
    except Exception as e2:
        logger.warning(f"Secondary forex feed exchangerate-api failed: {e2}")

    return None


def get_live_usd_to_inr_rate(db: Optional[Session] = None, force_refresh: bool = False) -> float:
    """
    Returns current USD to INR exchange rate.
    Uses in-memory cached rate if within 1 hour, otherwise refreshes from internet.
    Persists updated rate to isf_recorder.app_config.
    """
    global _cached_rate, _last_fetch_time

    now = time.time()
    if not force_refresh and (now - _last_fetch_time < CACHE_TTL_SECONDS) and _cached_rate > 0:
        return _cached_rate

    # Try fetching fresh internet rate
    live_rate = _fetch_from_internet()
    if live_rate is not None and live_rate > 0:
        _cached_rate = live_rate
        _last_fetch_time = now

        # Persist to database
        close_db_after = False
        db_sess = db
        if db_sess is None:
            db_sess = SessionLocal()
            close_db_after = True

        try:
            db_sess.execute(
                text("""
                    INSERT INTO isf_recorder.app_config (key, value, description, updated_at)
                    VALUES ('usd_to_inr_exchange_rate', :val, 'Live USD to INR exchange rate for Gemini cost calculation', NOW())
                    ON CONFLICT (key) DO UPDATE
                    SET value = :val, updated_at = NOW()
                """),
                {"val": f"{live_rate:.4f}"}
            )
            db_sess.commit()
        except Exception as dbe:
            logger.warning(f"Failed to persist live forex rate to DB: {dbe}")
        finally:
            if close_db_after:
                db_sess.close()

        return _cached_rate

    # If internet failed, read last known rate from database
    close_db_after = False
    db_sess = db
    if db_sess is None:
        db_sess = SessionLocal()
        close_db_after = True

    try:
        row = db_sess.execute(
            text("SELECT value FROM isf_recorder.app_config WHERE key = 'usd_to_inr_exchange_rate'")
        ).fetchone()
        if row and row[0]:
            _cached_rate = float(row[0])
            _last_fetch_time = now
            return _cached_rate
    except Exception as dbe2:
        logger.warning(f"Failed to read fallback forex rate from DB: {dbe2}")
    finally:
        if close_db_after:
            db_sess.close()

    # Ultimate fallback
    return _cached_rate if _cached_rate > 0 else 96.0
