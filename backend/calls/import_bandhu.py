"""
One-time copy of OLD GU Bandhu call recordings (flipboard.call_recordings) into Jomi Calls (14 Oct 2026).

User decisions: copy only WORK calls (contacts below), all into ONE account (phone --owner-phone), private to
that person. Bandhu's own transcript + summary + tasks/updates/issues are reused: no new AI cost.
Bandhu data and files are only READ (files are copied, never moved or deleted).

Run on the server, from C:\\GroundUp\\isf-recorder:
    python -m backend.calls.import_bandhu                      # dry run: counts only, changes nothing
    python -m backend.calls.import_bandhu --apply              # copy (safe to run again: external_id 'bandhu:<id>')
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import timezone, timedelta
from pathlib import Path

from sqlalchemy import text

from backend.config import AUDIO_STORAGE_ROOT
from backend.database import SessionLocal

IST = timezone(timedelta(hours=5, minutes=30))
WORK_CONTACTS = {
    "bhavnesh desai sir", "groundup owner", "owner", "yadav sir", "yadhav sir", "yadav initiative",
    "roshan cg", "sandhya singh", "sagar sir sharp flux technologies", "sagar hatikat (devoloper)",
    "asmita mam", "asmita mam hkm", "pralay groundup", "gayatri groundup personal", "hemant kanchan",
    "vishal pipraiya pagdandi",
}


def _contact(row) -> str:
    name = (row.contact_name or row.employee_name or "").strip()
    return re.sub(r"^call recording\s+", "", name, flags=re.I).strip()


def _as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _report(row) -> str:
    """Bandhu's results in the Jomi 'Call summary' layout (Summary / Tasks / Updates / Issues)."""
    tasks = [t for t in _as_list(row.extracted_tasks) if isinstance(t, dict) and t.get("text")]
    updates = [u for u in _as_list(row.extracted_updates) if isinstance(u, dict) and u.get("text")]
    issues = [i for i in _as_list(row.extracted_issues) if isinstance(i, dict) and i.get("text")]
    def task_line(t):
        parts = [p for p in (t.get("assigned_to"), t["text"], t.get("deadline")) if p]
        return "- " + " · ".join(str(p) for p in parts)
    out = ["**Summary**", f"- {row.ai_summary.strip()}" if (row.ai_summary or "").strip() else "None", "",
           "**Tasks**", *([task_line(t) for t in tasks] or ["None"]), "",
           "**Updates**", *([f"- {u['text']}" + (f" ({u['by']})" if u.get("by") else "") for u in updates] or ["None"]), "",
           "**Issues**", *([f"- {i['text']}" for i in issues] or ["None"])]
    return "\n".join(out)


def _duration(path: str) -> int:
    try:
        from backend.recordings.chunk_assembler import find_binary
        res = subprocess.run([find_binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
        return int(float(res.stdout.strip() or 0))
    except Exception:
        return 0


def run(apply: bool, owner_phone: str) -> None:
    with SessionLocal() as db:
        owner = db.execute(text("""
            SELECT id, org_id, name FROM isf_recorder."user" WHERE RIGHT(phone, 10) = :p ORDER BY created_at LIMIT 1
        """), {"p": owner_phone[-10:]}).fetchone()
        if not owner:
            raise SystemExit(f"No Jomi user with phone …{owner_phone[-10:]}")
        slug = db.execute(text("SELECT slug FROM isf_recorder.organization WHERE id = :o"), {"o": owner.org_id}).scalar() or "isf"
        rows = db.execute(text("""
            SELECT id, employee_name, contact_name, phone_number, file_path, filename, transcript, ai_summary,
                   extracted_tasks, extracted_updates, extracted_issues, status, call_timestamp, uploaded_at,
                   call_type, call_source
            FROM flipboard.call_recordings ORDER BY COALESCE(call_timestamp, uploaded_at)
        """)).fetchall()
        work = [r for r in rows if _contact(r).lower() in WORK_CONTACTS]
        has_calls_schema = db.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'isf_recorder' AND table_name = 'recording' AND column_name = 'external_id'
        """)).fetchone() is not None
        if apply and not has_calls_schema:
            raise SystemExit("Migration 011_calls.sql is not applied yet")
        print(f"Bandhu calls: {len(rows)} | work calls: {len(work)} | owner: {owner.name} (…{owner_phone[-4:]})"
              f" | {'APPLY' if apply else 'DRY RUN'}")
        per, copied, skipped, missing = {}, 0, 0, 0
        for r in work:
            per[_contact(r)] = per.get(_contact(r), 0) + 1
            ext_id = f"bandhu:{r.id}"
            if has_calls_schema and db.execute(text("SELECT 1 FROM isf_recorder.recording WHERE external_id = :e"),
                                               {"e": ext_id}).fetchone():
                skipped += 1
                continue
            src = os.path.normpath(r.file_path or "")
            if not src or not os.path.exists(src):
                missing += 1
                continue
            if not apply:
                copied += 1
                continue
            when = r.call_timestamp or r.uploaded_at
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=IST)            # Bandhu stored phone-local (IST) times
            day = (when or r.uploaded_at).astimezone(IST).strftime("%Y-%m-%d")
            dst_dir = Path(AUDIO_STORAGE_ROOT) / slug / day
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = str(dst_dir / f"bandhu_{str(r.id)[:12]}{Path(src).suffix.lower() or '.m4a'}")
            shutil.copy2(src, dst)
            contact = _contact(r)
            rid = db.execute(text("""
                INSERT INTO isf_recorder.recording
                (user_id, org_id, session_id, title, status, mic_source, sample_rate, duration_seconds, audio_file_path,
                 file_size_bytes, started_at, finished_at, pipeline_stage, finish_source, recorded_by_name, transcript,
                 overview, stt_engine, source, contact_name, contact_phone, call_type, call_app, external_id)
                VALUES (:u, :o, :sid, :title, 'completed', 'call', 16000, :dur, :path, :size, :when, :when, 'done',
                        'bandhu_import', :name, :tr, :ov, 'bandhu', 'call', :contact, :phone, 'unknown', :app, :ext)
                RETURNING id
            """), {"u": owner.id, "o": owner.org_id, "sid": f"bandhu_{str(r.id)[:12]}", "title": f"Call with {contact}",
                   "dur": _duration(dst), "path": dst, "size": os.path.getsize(dst), "when": when, "name": owner.name,
                   "tr": r.transcript, "ov": r.ai_summary, "contact": contact, "phone": r.phone_number,
                   "app": r.call_source or "samsung", "ext": ext_id}).scalar()
            if (r.transcript or "").strip() or (r.ai_summary or "").strip():
                db.execute(text("""
                    INSERT INTO isf_recorder.recording_report (recording_id, template_title, content, status, generated_at, updated_at)
                    VALUES (:r, 'Call summary (from Bandhu)', :c, 'draft', NOW(), NOW())
                """), {"r": rid, "c": _report(r)})
            for t in _as_list(r.extracted_tasks):
                if isinstance(t, dict) and t.get("text"):
                    db.execute(text("""
                        INSERT INTO isf_recorder.call_task (recording_id, user_id, text, assigned_to, due_text, origin)
                        VALUES (:r, :u, :t, :a, :d, 'ai')
                    """), {"r": rid, "u": owner.id, "t": str(t["text"])[:500], "a": t.get("assigned_to"),
                           "d": t.get("deadline")})
            db.commit()
            copied += 1
        for name, n in sorted(per.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {name}")
        print(f"{'copied' if apply else 'would copy'}: {copied} | already copied: {skipped} | audio file missing: {missing}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--owner-phone", default="9684019697")
    a = ap.parse_args()
    run(a.apply, a.owner_phone)
