"""
ISF Recorder — Name memory (21 Oct 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The transcription AI misheard names ("Bhavnesh" → "Pavnesh" / "Avnesh"). Without anyone typing lists:

1. Known names: coach list, everyone with a login, the recording's Balgruha, assistant coaches and participants,
   plus names learned for that Balgruha, go into the transcription prompt ("write these names exactly so").
2. Corrections: when someone edits a report and changes a name, the wrong spelling is remembered for that
   Balgruha and replaced automatically (whole words only) in later transcripts and reports.
3. Heard names: after each report, the names in it are counted per Balgruha; a name heard in 2+ recordings
   (or ever corrected) is sent to the AI with the next recordings.

balgruh_name '' = everywhere (meetings, calls). Admins can remove a wrong entry (Settings → Balgruha → Names).
"""
import difflib
import json
import logging
import re
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("isf_recorder.names")

MAX_PROMPT_NAMES = 80
MIN_HEARD_FOR_PROMPT = 2
SIMILAR = 0.75          # spellings this close are the same name (Pavnesh / Bhavnesh = 0.80)
_WORD = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)


def _clean(name: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).strip(" .,:;-*")


def _people(value) -> List[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [p.strip() for p in value.split(",") if p.strip()]
    return [_clean(p.get("name")) for p in (value or []) if isinstance(p, dict) and _clean(p.get("name"))]


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _scopes(balgruh: Optional[str]) -> List[str]:
    b = _clean(balgruh)
    return ["", b] if b else [""]


# ─── 1. Names for the transcription prompt ────────────────────────────────────

def known_names(db: Session, org_id: str, balgruh: Optional[str] = None, extra: Iterable[str] = ()) -> List[str]:
    names: List[str] = [_clean(n) for n in extra if _clean(n)]
    rows = db.execute(text("""
        SELECT name FROM isf_recorder."user" WHERE org_id = CAST(:o AS uuid) AND is_active = TRUE AND archived_at IS NULL
        UNION ALL
        SELECT name FROM isf_recorder.recorder_list_item WHERE org_id = CAST(:o AS uuid) AND list_type = 'coach_name'
    """), {"o": org_id}).fetchall()
    names += [_clean(r.name) for r in rows]
    learned = db.execute(text("""
        SELECT name FROM isf_recorder.name_memory
        WHERE org_id = CAST(:o AS uuid) AND balgruh_name = ANY(:scopes) AND NOT is_deleted
          AND (corrected_count > 0 OR heard_count >= :min_heard)
        ORDER BY corrected_count DESC, heard_count DESC, updated_at DESC
        LIMIT :lim
    """), {"o": org_id, "scopes": _scopes(balgruh), "min_heard": MIN_HEARD_FOR_PROMPT, "lim": MAX_PROMPT_NAMES}).fetchall()
    names += [_clean(r.name) for r in learned]
    seen, out = set(), []
    for n in names:
        if n and not re.fullmatch(r"(staff|coach)\s*\d*", n, re.I) and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out[:MAX_PROMPT_NAMES]


def names_for_recording(db: Session, recording_id: str) -> List[str]:
    rec = db.execute(text("""
        SELECT org_id, balgruh_name, assistant_coach, assistant_coaches, participants, recorded_by_name, contact_name
        FROM isf_recorder.recording WHERE id = CAST(:id AS uuid)
    """), {"id": recording_id}).fetchone()
    if not rec:
        return []
    extra = _people(rec.assistant_coaches) + _people(rec.participants) + _people(rec.assistant_coach or "")
    extra += [rec.recorded_by_name or "", rec.contact_name or ""]
    if rec.balgruh_name:
        extra.append(rec.balgruh_name)
    return known_names(db, str(rec.org_id), rec.balgruh_name, extra)


def prompt_hint(names: List[str]) -> str:
    if not names:
        return ""
    return ("\n- Names that may be spoken in this recording. When you hear one of them (even unclearly), write it with "
            "exactly this spelling; in Devanagari text write the same name in Devanagari: " + ", ".join(names) + ".")


# ─── 2. Corrections ──────────────────────────────────────────────────────────

def fixes(db: Session, org_id: str, balgruh: Optional[str] = None) -> List[Tuple[str, str]]:
    rows = db.execute(text("""
        SELECT name, wrong_spellings FROM isf_recorder.name_memory
        WHERE org_id = CAST(:o AS uuid) AND balgruh_name = ANY(:scopes) AND NOT is_deleted
          AND jsonb_array_length(wrong_spellings) > 0
    """), {"o": org_id, "scopes": _scopes(balgruh)}).fetchall()
    out = []
    for r in rows:
        wrongs = r.wrong_spellings if isinstance(r.wrong_spellings, list) else json.loads(r.wrong_spellings or "[]")
        out += [(w, r.name) for w in wrongs if w and w.lower() != r.name.lower()]
    return sorted(out, key=lambda p: -len(p[0]))    # longer spellings first


def apply_fixes(content: str, pairs: List[Tuple[str, str]]) -> str:
    """Whole words only, any case ("pavnesh", "Pavnesh" → "Bhavnesh"); never inside another word."""
    if not content or not pairs:
        return content
    for wrong, right in pairs:
        content = re.sub(r"(?<![\wऀ-ॿ])" + re.escape(wrong) + r"(?![\wऀ-ॿ])", right, content,
                         flags=re.IGNORECASE)
    return content


def _upsert(db: Session, org_id: str, balgruh: str, name: str, wrong: Optional[str] = None,
            heard: int = 0, corrected: int = 0) -> None:
    db.execute(text("""
        INSERT INTO isf_recorder.name_memory (org_id, balgruh_name, name, wrong_spellings, heard_count, corrected_count)
        VALUES (CAST(:o AS uuid), :b, :n, CAST(:w AS jsonb), :h, :c)
        ON CONFLICT (org_id, balgruh_name, lower(name)) DO UPDATE SET
            heard_count = isf_recorder.name_memory.heard_count + EXCLUDED.heard_count,
            corrected_count = isf_recorder.name_memory.corrected_count + EXCLUDED.corrected_count,
            wrong_spellings = CASE
                WHEN CAST(:wrong_one AS text) IS NULL OR isf_recorder.name_memory.wrong_spellings ? CAST(:wrong_one AS text)
                THEN isf_recorder.name_memory.wrong_spellings
                ELSE isf_recorder.name_memory.wrong_spellings || CAST(:w AS jsonb) END,
            is_deleted = CASE WHEN EXCLUDED.corrected_count > 0 THEN FALSE ELSE isf_recorder.name_memory.is_deleted END,
            updated_at = NOW()
    """), {"o": org_id, "b": balgruh, "n": name, "w": json.dumps([wrong] if wrong else []), "wrong_one": wrong,
           "h": heard, "c": corrected})


def name_corrections(old: str, new: str) -> List[Tuple[str, str]]:
    """Single words changed in an edit that look like a fixed name: similar spelling, the new one capitalised
    (or Devanagari). "Pavnesh" → "Bhavnesh" yes; "good" → "great" no."""
    a, b = _WORD.findall(old or ""), _WORD.findall(new or "")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "replace" or (i2 - i1) != (j2 - j1) or (i2 - i1) > 3:
            continue
        for w, r in zip(a[i1:i2], b[j1:j2]):
            if w.lower() == r.lower() or len(r) < 3 or r.isdigit():
                continue
            name_like = r[0].isupper() or bool(re.match(r"[ऀ-ॿ]", r))
            if name_like and similar(w, r) >= 0.5:
                out.append((w, r))
    return out


def learn_from_edit(db: Session, org_id: str, balgruh: Optional[str], old: str, new: str) -> int:
    pairs = name_corrections(old, new)
    for wrong, right in pairs:
        _upsert(db, org_id, _clean(balgruh), right, wrong=wrong, corrected=1)
    if pairs:
        db.commit()
        logger.info(f"Learned {len(pairs)} name correction(s) for '{_clean(balgruh) or 'everywhere'}': {pairs}")
    return len(pairs)


# ─── 3. Heard names ──────────────────────────────────────────────────────────

def parse_name_list(raw: str) -> List[str]:
    out = []
    for line in (raw or "").splitlines():
        n = _clean(re.sub(r"^[\-\*\d\.\)\s]+", "", line))
        if 2 <= len(n) <= 40 and len(n.split()) <= 3 and not n.lower().startswith(("none", "no name", "here are")):
            out.append(n)
    return out


def learn_heard(db: Session, org_id: str, balgruh: Optional[str], names: List[str]) -> int:
    """Counts each name once per recording. A spelling close to an already known name counts for that name
    (keeps its spelling), so Pavnesh / Bhavnesh do not become two children."""
    b = _clean(balgruh)
    known = db.execute(text("""
        SELECT name, heard_count, corrected_count FROM isf_recorder.name_memory
        WHERE org_id = CAST(:o AS uuid) AND balgruh_name = :b AND NOT is_deleted
    """), {"o": org_id, "b": b}).fetchall()
    done = set()
    for n in names:
        target = n
        best = max(known, key=lambda r: similar(r.name, n), default=None)
        if best is not None and similar(best.name, n) >= SIMILAR:
            target = best.name
        if target.lower() in done:
            continue
        done.add(target.lower())
        _upsert(db, org_id, b, target, heard=1)
    db.commit()
    return len(done)


NAMES_PROMPT = ("List the first names of all people mentioned in this report (children, coaches and others), "
                "spelled exactly as written in it. One name per line, nothing else. If there are none, write NONE.\n\n")


def start_learning_from_report(org_id: str, recording_id: str, balgruh: Optional[str], content: str) -> None:
    import threading
    threading.Thread(target=learn_from_report, args=(org_id, recording_id, balgruh, content), daemon=True,
                     name=f"names-{recording_id[:8]}").start()


def learn_from_report(org_id: str, recording_id: str, balgruh: Optional[str], content: str) -> None:
    """Background, after a report is written: pick the names out of it and count them. Never raises."""
    from backend.database import SessionLocal
    try:
        from backend.reports.service import _call_model
        raw = _call_model(org_id, recording_id, NAMES_PROMPT + (content or "")[:20000], temperature=0.0,
                          operation_type="NAMES")   # its own line in the cost logs
        names = parse_name_list(raw)
        if not names:
            return
        with SessionLocal() as db:
            learn_heard(db, org_id, balgruh, names)
    except Exception as e:
        logger.warning(f"Name learning skipped for {recording_id}: {e}")
