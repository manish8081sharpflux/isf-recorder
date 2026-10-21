"""Small helpers for isf_recorder.app_config (key/value settings)."""
from typing import Dict, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


def get_values(db: Session, keys: Iterable[str]) -> Dict[str, Optional[str]]:
    keys = list(keys)
    rows = db.execute(text("SELECT key, value FROM isf_recorder.app_config WHERE key = ANY(:k)"), {"k": keys}).fetchall()
    found = {r.key: r.value for r in rows}
    return {k: found.get(k) for k in keys}


def set_values(db: Session, values: Dict[str, object]) -> None:
    for k, v in values.items():
        db.execute(text("""
            INSERT INTO isf_recorder.app_config (key, value, updated_at) VALUES (:k, :v, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """), {"k": k, "v": "" if v is None else str(v)})
