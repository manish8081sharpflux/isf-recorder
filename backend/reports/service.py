"""
ISF Recorder — Report generation (contract §5.2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
One editable report per recording (recording_report). After a successful transcription the pipeline
marks the report 'generating' in the same transaction as the transcript, then generate_report()
writes the draft. A report that is final or was edited by a person is never overwritten by a
re-transcription. Every model call is logged with operation_type 'REPORT'.
"""
import logging
import re
import threading
from datetime import timezone, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.transcription.gemini_stt import get_genai_client

logger = logging.getLogger("isf_recorder.reports")

IST = timezone(timedelta(hours=5, minutes=30))
MAX_TRANSCRIPT_CHARS = 400_000
REPORT_MAX_OUTPUT_TOKENS = 8192


# Calls (14 Oct, wording approved by the user): kept in code, not in the admin's template list.
CALL_TEMPLATE_TITLE = "Call summary"
CALL_SUMMARY_PROMPT = """Summarise this phone call recorded on {owner}'s phone with {contact}.
Write in English, as short points. Use only what was said in the call.
Use a person's name only when the call itself shows it (for example someone is addressed by name: "Jay, do this"); otherwise keep "Speaker 1" / "Speaker 2". Never guess which speaker is the phone owner or the contact.

**Summary** — 3 to 5 short points: what the call was about.
**Tasks** — things someone has to do. One per line: who · what · by when (only if a date/day was said).
**Updates** — things already done or in progress.
**Issues** — problems or complaints raised.

If a section has nothing, write "None"."""


class _CallTemplate:
    id = None
    title = CALL_TEMPLATE_TITLE
    prompt = CALL_SUMMARY_PROMPT


def _template_for(db: Session, org_id: str, mode: Optional[str], template_id: Optional[str] = None,
                  source: Optional[str] = None):
    if source == "call" and not template_id:
        return _CallTemplate
    if template_id:
        try:
            return db.execute(text("""
                SELECT id, title, prompt FROM isf_recorder.chat_template WHERE id = CAST(:id AS uuid) AND org_id = :o
            """), {"id": str(template_id), "o": org_id}).fetchone()
        except Exception:
            db.rollback()
            return None
    from backend.settings.recorder import template_for_mode
    return template_for_mode(db, org_id, mode)


def mark_generating_for_pipeline(db: Session, recording_id: str, mode: Optional[str], run_id: Optional[str]) -> bool:
    """Called inside the pipeline's final transaction (no commit here). True = generate after commit."""
    # Round 3: Schedule and Meeting use their own template; old-app recordings (mode NULL) use Schedule
    row = db.execute(text("SELECT CAST(org_id AS text) AS org_id, source FROM isf_recorder.recording WHERE id = :id"),
                     {"id": recording_id}).fetchone()
    org_id = row.org_id if row else None
    tpl = _template_for(db, org_id, mode, source=row.source if row else None)
    if not tpl:
        logger.info(f"No report template configured for mode {mode}; skipping report for {recording_id}")
        return False
    existing = db.execute(text("""
        SELECT status, updated_by FROM isf_recorder.recording_report WHERE recording_id = :id
    """), {"id": recording_id}).fetchone()
    if existing and (existing.status == "final" or existing.updated_by is not None):
        logger.info(f"Report of {recording_id} is final or edited by a person; keeping it")
        return False
    db.execute(text("""
        INSERT INTO isf_recorder.recording_report (recording_id, template_id, template_title, status, run_id, updated_at)
        VALUES (:id, :tid, :title, 'generating', :run, NOW())
        ON CONFLICT (recording_id) DO UPDATE SET
            template_id = EXCLUDED.template_id, template_title = EXCLUDED.template_title, status = 'generating',
            error_message = NULL, run_id = EXCLUDED.run_id, updated_at = NOW()
    """), {"id": recording_id, "tid": str(tpl.id) if tpl.id else None, "title": tpl.title, "run": run_id})
    return True


def _metadata_block(rec) -> str:
    started = rec.started_at or rec.created_at
    when = started.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST") if started else "Unknown"
    if getattr(rec, "source", None) == "call":
        contact = rec.contact_name or rec.contact_phone or "Unknown"
        call_lines = [f"- Date: {when}", "- Type: Phone call",
                      f"- Call with: {contact}" + (f" ({rec.contact_phone})" if rec.contact_name and rec.contact_phone else ""),
                      f"- Phone owner: {rec.recorded_by or 'Unknown'}"]
        if rec.call_type and rec.call_type != "unknown":
            call_lines.append(f"- Direction: {rec.call_type}")
        return "\n".join(call_lines)
    participants = rec.participants or []
    names = ", ".join(p.get("name", "") for p in participants if isinstance(p, dict) and p.get("name")) or "Not recorded"
    lines = [
        f"- Date: {when}",
        f"- Mode: {(rec.mode or 'not set').capitalize()}",
    ]
    if rec.balgruh_name:
        lines.append(f"- Balgruha: {rec.balgruh_name}")
    coaches = [c.get("name") for c in (rec.assistant_coaches or []) if isinstance(c, dict) and c.get("name")]
    coach_text = ", ".join(coaches) or (rec.assistant_coach or "")
    if coach_text:
        lines.append(f"- Assistant coaches: {coach_text}")
    lines.append(f"- Participants: {names}")
    lines.append(f"- Recorded by: {rec.recorded_by or 'Unknown'}")
    if rec.title:
        lines.append(f"- Title: {rec.title}")
    return "\n".join(lines)


def _transcript_without_timestamps(db: Session, rec) -> str:
    rows = db.execute(text("""
        SELECT speaker, text FROM isf_recorder.transcript_segment WHERE recording_id = :id ORDER BY start_time, created_at
    """), {"id": str(rec.id)}).fetchall()
    if rows:
        body = "\n".join(f"{r.speaker or 'Speaker'}: {r.text}" for r in rows if (r.text or "").strip())
    else:
        body = re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", rec.transcript or "")
    return body[:MAX_TRANSCRIPT_CHARS]


def _load_recording(db: Session, recording_id: str):
    return db.execute(text("""
        SELECT r.id, r.org_id, r.mode, r.title, r.started_at, r.created_at, r.balgruh_name, r.assistant_coach, r.assistant_coaches,
               r.participants, r.transcript, COALESCE(u.name, r.recorded_by_name) AS recorded_by,
               r.source, r.contact_name, r.contact_phone, r.call_type, r.user_id
        FROM isf_recorder.recording r LEFT JOIN isf_recorder."user" u ON u.id = r.user_id
        WHERE r.id = :id
    """), {"id": recording_id}).fetchone()


def _clean_output(raw: str) -> str:
    out = (raw or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\n([\s\S]*?)\n?```$", out)
    if fence:
        out = fence.group(1).strip()
    return out.replace("\\r\\n", "\n").replace("\\n", "\n")


def _call_model(org_id: str, recording_id: Optional[str], prompt: str, temperature: float = 0.2) -> str:
    from google.genai import types
    from backend.settings.model_resolver import get_intelligence_model
    from backend.transcription.usage_tracker import track_gemini_call
    model = get_intelligence_model(org_id)
    client = get_genai_client()
    config = types.GenerateContentConfig(
        temperature=temperature,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=REPORT_MAX_OUTPUT_TOKENS,
    )
    response = track_gemini_call(
        recording_id=recording_id, operation_type="REPORT", gemini_model=model, org_id=org_id,
        call_fn=lambda: client.models.generate_content(model=model, contents=[prompt], config=config),
    )
    return _clean_output(response.text or "")


def _section_definitions(template_prompt: str) -> str:
    """The template's own section lines (e.g. "*5. Observations* - (...)"), word for word."""
    keep = [line.strip() for line in template_prompt.splitlines()
            if re.match(r"^\*{1,2}\s*\d+\.", line.strip()) or "General Overview" in line]
    return "\n".join(keep)


def build_report_prompt(template_prompt: str, metadata: str, transcript: str) -> str:
    prompt = (f"{template_prompt.strip()}\n\n"
              f"SESSION METADATA:\n{metadata}\n\n"
              f"TRANSCRIPT (speaker: words; timestamps removed):\n\"\"\"\n{transcript}\n\"\"\"\n\n"
              f"Write the report in English as clean Markdown. Use only facts from the transcript and the metadata; "
              f"never invent names, numbers, coins or decisions. Do not include timestamps.")
    # Long transcripts push the template far back: repeat its section definitions (same words, no new
    # rules) right before the model writes, so each point goes in the section its definition describes.
    sections = _section_definitions(template_prompt)
    if sections:
        prompt += ("\n\nBefore writing, re-read the section definitions from the template and put every "
                   "point in the section its definition describes:\n" + sections)
    return prompt


def generate_report(recording_id: str, run_id: Optional[str] = None, template_id: Optional[str] = None) -> bool:
    """Writes the draft for a report already marked 'generating'. Never raises."""
    db = SessionLocal()
    try:
        rec = _load_recording(db, recording_id)
        report = db.execute(text("""
            SELECT template_id, status FROM isf_recorder.recording_report WHERE recording_id = :id
        """), {"id": recording_id}).fetchone()
        if not rec or not report or report.status != "generating":
            return False
        org_id = str(rec.org_id)
        tpl = _template_for(db, org_id, rec.mode, template_id or (str(report.template_id) if report.template_id else None),
                            source=rec.source)
        if not tpl:
            raise RuntimeError("The report template no longer exists. Pick another template and regenerate.")
        transcript = _transcript_without_timestamps(db, rec)
        if not transcript.strip():
            raise RuntimeError("There is no transcript to write a report from.")
        prompt_text = tpl.prompt
        if tpl is _CallTemplate:
            prompt_text = CALL_SUMMARY_PROMPT.format(owner=rec.recorded_by or "the phone owner",
                                                     contact=rec.contact_name or rec.contact_phone or "the other person")
        content = _call_model(org_id, recording_id, build_report_prompt(prompt_text, _metadata_block(rec), transcript))
        if not content.strip():
            raise RuntimeError("The AI returned an empty report. Tap Regenerate.")
        saved = db.execute(text("""
            UPDATE isf_recorder.recording_report
            SET content = :content, status = 'draft', error_message = NULL, generated_at = NOW(), updated_at = NOW(),
                template_id = :tid, template_title = :title, run_id = COALESCE(:run, run_id)
            WHERE recording_id = :id AND status = 'generating'
            RETURNING id
        """), {"id": recording_id, "content": content, "tid": str(tpl.id) if tpl.id else None, "title": tpl.title,
               "run": run_id}).fetchone()
        if saved and rec.source == "call":
            save_call_tasks(db, recording_id, str(rec.user_id), parse_call_tasks(content))
        db.commit()
        logger.info(f"Report draft generated for {recording_id} ({len(content)} chars)" if saved else
                    f"Report of {recording_id} changed while generating; result discarded")
        return bool(saved)
    except Exception as e:
        logger.warning(f"Report generation failed for {recording_id}: {e}")
        try:
            db.rollback()
            db.execute(text("""
                UPDATE isf_recorder.recording_report
                SET status = 'error', error_message = :err, updated_at = NOW()
                WHERE recording_id = :id AND status = 'generating'
            """), {"id": recording_id, "err": str(e)[:500]})
            db.commit()
        except Exception:
            pass
        return False
    finally:
        db.close()


def generate_report_async(recording_id: str, template_id: Optional[str] = None) -> None:
    threading.Thread(target=generate_report, kwargs={"recording_id": recording_id, "template_id": template_id},
                     daemon=True, name=f"report-{recording_id[:8]}").start()


def ai_edit(recording_id: str, instruction: str, content: str) -> str:
    """Proposes an edited report (nothing is saved)."""
    with SessionLocal() as db:
        rec = _load_recording(db, recording_id)
        transcript = _transcript_without_timestamps(db, rec)
        metadata = _metadata_block(rec)
    prompt = (
        "You are editing a report written from a recorded session. Apply the instruction to the CURRENT REPORT and "
        "return the complete updated report as clean Markdown, with no explanations before or after it. Keep "
        "everything the instruction does not ask to change. Use only facts from the transcript and the metadata.\n\n"
        f"INSTRUCTION:\n{instruction.strip()}\n\nCURRENT REPORT:\n\"\"\"\n{content}\n\"\"\"\n\n"
        f"SESSION METADATA:\n{metadata}\n\nTRANSCRIPT:\n\"\"\"\n{transcript}\n\"\"\""
    )
    return _call_model(str(rec.org_id), recording_id, prompt, temperature=0.2)


# ─── Translation (13 Oct 2026) ────────────────────────────────────────────────

TRANSLATION_LANGUAGES = {"hi": "Hindi"}
# Part of the cache key: bump it when the translation prompt changes so saved translations are made again.
TRANSLATION_STYLE = "simple-hindi-1"


def source_hash(content: str) -> str:
    import hashlib
    key = f"{TRANSLATION_STYLE}\n{(content or '').strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def translate(recording_id: str, lang: str, content: str) -> str:
    """The report in Hindi, same Markdown layout (nothing is saved here)."""
    with SessionLocal() as db:
        rec = _load_recording(db, recording_id)
    language = TRANSLATION_LANGUAGES[lang]
    prompt = (
        f"Translate this report from English to {language}. Return only the translated report.\n"
        "- Use simple, everyday spoken Hindi that anyone can understand without a dictionary, the way people "
        "talk at home: not formal, bookish or Sanskrit-heavy Hindi. Short, plain sentences. Keep the meaning exactly.\n"
        "- Prefer the common word: बताया / ज़िक्र किया (not उल्लेख), मतलब (not परिभाषा), बच्चे / लोग (not प्रतिभागी), "
        "मकसद (not उद्देश्य), ज़रूरत (not आवश्यकता), शामिल (not सम्मिलित), शुरू (not प्रारंभ), बाद में (not पश्चात), "
        "बातचीत (not संवाद), जाँच (not मूल्यांकन), हिस्सा लिया (not सहभागिता की), हिम्मत बढ़ाई (not प्रोत्साहित किया), "
        "बहुत (not अत्यधिक), ऊपर बताया गया (not उपरोक्त).\n"
        "- English words that people commonly use in Hindi (session, coach, report, game, team, class) may stay, "
        "written in Devanagari (सेशन, कोच, रिपोर्ट).\n"
        "- Keep the Markdown layout exactly: the same headings, numbering, bold text, bullets and line breaks.\n"
        f"- Write people's names, Balgruha names and story or poem titles in Devanagari as they are pronounced; "
        "do not translate their meaning.\n"
        "- Keep numbers, dates and coin counts exactly as they are, written with the digits 0-9 (never Devanagari digits).\n"
        "- Do not add, remove or summarise anything.\n\n"
        f"REPORT:\n\"\"\"\n{content}\n\"\"\""
    )
    return clean_translation(_call_model(str(rec.org_id), recording_id, prompt, temperature=0.1))


def clean_translation(out: str) -> str:
    """The model sometimes echoes the prompt's wrapper: a (translated) "REPORT:" label and the \"\"\" marks."""
    out = (out or "").replace('"""', "").strip()
    lines = out.splitlines()
    if lines and len(lines[0].strip()) <= 20 and lines[0].strip().endswith((":", "：")):
        out = "\n".join(lines[1:]).strip()        # e.g. "रिपोर्ट:" / "REPORT:"
    return out


# ─── Call tasks (14 Oct) ──────────────────────────────────────────────────────

_SECTION_RE = re.compile(r"^(?:#+\s*)?\*{1,2}\s*([A-Za-z ]+?)\s*\*{1,2}")


def parse_call_tasks(content: str) -> list:
    """The lines under **Tasks** of a call summary -> [{"text", "assigned_to", "due_text"}].
    Each line is "who · what · by when" (the 'by when' part is optional); "None" means no tasks."""
    out, in_tasks = [], False
    for raw in (content or "").splitlines():
        line = raw.strip()
        head = _SECTION_RE.match(line)
        if head:
            in_tasks = head.group(1).strip().lower() == "tasks"
            rest = line[head.end():].strip(" —-:")
            if not in_tasks or not rest or "things someone" in rest.lower():
                continue
            line = rest                       # "**Tasks** — Rizwana · send list" on the same line
        if not in_tasks or not line:
            continue
        item = re.sub(r"^([-*•]|\d+[.)])\s*", "", line).strip()
        if not item or item.lower().rstrip(".") == "none":
            continue
        parts = [p.strip() for p in re.split(r"\s*[·|]\s*", item) if p.strip()]
        if len(parts) >= 3:
            who, what, when = parts[0], " · ".join(parts[1:-1]), parts[-1]
        elif len(parts) == 2:
            who, what, when = parts[0], parts[1], None
        else:
            who, what, when = None, parts[0], None
        if when and when.strip().lower().rstrip(".") in ("none", "-", "n/a", "na", "not said", "not mentioned"):
            when = None
        out.append({"text": what[:500], "assigned_to": who[:120] if who else None,
                    "due_text": when[:120] if when else None})
    return out[:30]


def save_call_tasks(db: Session, recording_id: str, user_id: str, tasks: list) -> int:
    """Adds the AI tasks of a call once; a later regeneration never touches the owner's task list."""
    if not tasks or not user_id or user_id == "None":
        return 0
    already = db.execute(text("""
        SELECT COUNT(*) FROM isf_recorder.call_task WHERE recording_id = :r AND origin = 'ai'
    """), {"r": recording_id}).scalar() or 0
    if already:
        return 0
    for t in tasks:
        db.execute(text("""
            INSERT INTO isf_recorder.call_task (recording_id, user_id, text, assigned_to, due_text, origin)
            VALUES (:r, :u, :t, :a, :d, 'ai')
        """), {"r": recording_id, "u": user_id, "t": t["text"], "a": t.get("assigned_to"), "d": t.get("due_text")})
    return len(tasks)
