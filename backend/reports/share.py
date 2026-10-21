"""
ISF Recorder — Report share links (21 Oct 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WhatsApp cuts a long message, so the app's Share sends a short message with a link instead of the
whole report.

POST /recordings/{id}/report/share-link {lang?}  (owner or admin) → {token, url}
GET  /r/{token}?lang=hi                          (no login) → the full report as a web page

- One link per recording, made on the first Share and reused; it never expires.
- Anyone with the link can read it (the token is 32 random bytes, so it cannot be guessed).
- The page always shows the CURRENT report (later edits show up); deleting the recording deletes the link.
- Calls (source = 'call') never get a link: they are private to their owner.
- The page has a "Save as PDF" button (the browser's print → Save as PDF) so it can be read offline.
"""
import html
import json
import logging
import re
import secrets
from datetime import timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.routes import get_current_user
from backend.database import get_db
from backend.reports import service
from backend.reports.routes import _recording
from backend.system.routes import public_base_url

logger = logging.getLogger("isf_recorder.reports.share")
router = APIRouter(tags=["Report share links"])

IST = timezone(timedelta(hours=5, minutes=30))


class ShareLinkRequest(BaseModel):
    lang: Optional[str] = None      # "hi" = open the Hindi version


def share_url(base: str, token: str, lang: Optional[str]) -> str:
    url = f"{base}/r/{token}"
    return f"{url}?lang={lang}" if lang in service.TRANSLATION_LANGUAGES else url


@router.post("/recordings/{recording_id}/report/share-link")
def create_share_link(recording_id: str, request: Request, body: ShareLinkRequest = ShareLinkRequest(),
                      user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    rec = _recording(db, recording_id, user)
    source = db.execute(text("SELECT source FROM isf_recorder.recording WHERE id = :id"), {"id": str(rec.id)}).scalar()
    if source == "call":
        # Same answer as for any recording the user may not share: calls are private.
        raise HTTPException(status_code=404, detail="Recording not found")
    has_report = db.execute(text("""
        SELECT 1 FROM isf_recorder.recording_report
        WHERE recording_id = :id AND status <> 'generating' AND LENGTH(TRIM(content)) > 0
    """), {"id": str(rec.id)}).scalar()
    if not has_report:
        raise HTTPException(status_code=409, detail="The report is not ready yet")
    token = db.execute(text("SELECT token FROM isf_recorder.report_share WHERE recording_id = :id"),
                       {"id": str(rec.id)}).scalar()
    if not token:
        db.execute(text("""
            INSERT INTO isf_recorder.report_share (token, recording_id, created_by)
            VALUES (:t, :id, CAST(:u AS uuid)) ON CONFLICT (recording_id) DO NOTHING
        """), {"t": secrets.token_urlsafe(32), "id": str(rec.id), "u": user["user_id"]})
        db.commit()
        token = db.execute(text("SELECT token FROM isf_recorder.report_share WHERE recording_id = :id"),
                           {"id": str(rec.id)}).scalar()
    lang = (body.lang or "").strip().lower() or None
    return {"token": token, "url": share_url(public_base_url(request), token, lang)}


# ─── Public page ──────────────────────────────────────────────────────────────

def _inline(t: str) -> str:
    """Escaped text with **bold**, __bold__ and *italic* / _italic_."""
    x = html.escape(t, quote=False)
    x = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", x)
    x = re.sub(r"__(.+?)__", r"<strong>\1</strong>", x)
    x = re.sub(r"(?<![*\w])\*(?![*\s])(.+?)(?<![*\s])\*(?![*\w])", r"<em>\1</em>", x)
    x = re.sub(r"(?<![_\w])_(?![_\s])(.+?)(?<![_\s])_(?![_\w])", r"<em>\1</em>", x)
    return x


def markdown_to_html(md: str) -> str:
    """The Markdown the report AI writes (headings, bullets, numbered points, bold, ---, simple tables)."""
    out, para, table = [], [], []
    bullet = re.compile(r"^([-*+•])\s+(.*)$")
    numbered = re.compile(r"^(\d+)[.)]\s+(.*)$")

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()
        if table:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table
                    if not re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", r.strip())]
            if rows:
                head, body = rows[0], rows[1:]
                out.append("<div class='tbl'><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                           + "</tr></thead><tbody>"
                           + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                           + "</tbody></table></div>")
            table.clear()

    for raw in (md or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        t = line.strip()
        indent = min((len(line) - len(line.lstrip())) // 2, 4)
        if t.startswith("|") and t.count("|") >= 2:
            if para:
                flush()
            table.append(t)
            continue
        if table:
            flush()
        if not t:
            flush()
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", t):
            flush()
            out.append("<hr>")
        elif t.startswith("#"):
            flush()
            level = min(len(t) - len(t.lstrip("#")) + 1, 4)
            out.append(f"<h{level}>{_inline(t.lstrip('#').strip().replace('**', ''))}</h{level}>")
        elif len(t) > 2 and t.startswith("*") and t.endswith("*") and not t.startswith(("**", "* ")) \
                or (t.startswith("**") and t.endswith("**") and t.count("**") == 2 and len(t) > 4):
            flush()
            out.append(f"<h4>{_inline(t.strip('*').strip())}</h4>")      # *1. Heading* / **Heading**
        elif bullet.match(t):
            flush()
            out.append(f"<div class='li' style='margin-left:{indent * 18}px'><span class='dot'>•</span>"
                       f"<span>{_inline(bullet.match(t).group(2))}</span></div>")
        elif numbered.match(t):
            flush()
            m = numbered.match(t)
            out.append(f"<div class='li' style='margin-left:{indent * 18}px'><span class='dot'>{m.group(1)}.</span>"
                       f"<span>{_inline(m.group(2))}</span></div>")
        else:
            para.append(t)
    flush()
    return "\n".join(out)


def _names(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return [str(p.get("name")).strip() for p in (v or []) if isinstance(p, dict) and (p.get("name") or "").strip()]


def _hindi(db: Session, recording_id: str, content: str, translations) -> Optional[str]:
    """The saved Hindi version of the current report; made (once, then saved) if the report changed."""
    h = service.source_hash(content)
    cached = (translations or {}).get("hi") or {}
    if cached.get("source_hash") == h and (cached.get("text") or "").strip():
        return cached["text"]
    try:
        translated = service.translate(recording_id, "hi", content)
    except Exception as e:
        logger.warning(f"Share page: Hindi translation failed for {recording_id}: {e}")
        return None
    if not translated.strip():
        return None
    db.execute(text("""
        UPDATE isf_recorder.recording_report
        SET translations = COALESCE(translations, '{}'::jsonb) || jsonb_build_object('hi', CAST(:val AS jsonb))
        WHERE recording_id = :id
    """), {"id": recording_id, "val": json.dumps({"text": translated, "source_hash": h})})
    db.commit()
    return translated


_CSS = """
:root{--bg:#f6f7f5;--card:#fff;--ink:#1d2320;--muted:#66706b;--line:#e3e7e4;--accent:#1f8a70}
@media (prefers-color-scheme:dark){:root{--bg:#121614;--card:#1b201e;--ink:#e8ece9;--muted:#9aa49f;--line:#2c3330;--accent:#4cc19f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Devanagari",sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.brand{color:var(--accent);font-weight:700;font-size:13px;letter-spacing:.04em;text-transform:uppercase}
h1{font-size:22px;line-height:1.3;margin:4px 0 8px}
.meta{color:var(--muted);font-size:14px;margin:2px 0}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0 4px}
.btn{display:inline-block;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:20px;
     padding:7px 16px;font:inherit;font-size:14px;font-weight:600;text-decoration:none;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent)}
.note{color:var(--muted);font-size:13px;margin:8px 0 0}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
h2,h3,h4{line-height:1.35;margin:20px 0 6px}
h2{font-size:20px}h3{font-size:18px}h4{font-size:16px}
p{margin:6px 0 10px}
.li{display:flex;gap:8px;margin:4px 0}
.dot{color:var(--accent);flex:0 0 auto;min-width:12px}
.tbl{overflow-x:auto;margin:10px 0}
table{border-collapse:collapse;font-size:14px;min-width:100%}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
footer{color:var(--muted);font-size:12px;text-align:center;margin:16px 0 24px}
@media print{
  :root{--bg:#fff;--card:#fff;--ink:#000;--muted:#444;--line:#ccc;--accent:#1f8a70}
  .bar,.note.screen,footer{display:none}
  main{max-width:none;padding:0}.card{border:0;padding:0}
  h2,h3,h4{break-after:avoid}.li,tr{break-inside:avoid}
}
"""


def _page(title: str, body: str, lang: str = "en", status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<meta name='robots' content='noindex,nofollow'><meta name='referrer' content='no-referrer'>"
           f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
           f"<body><main>{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status, headers={
        "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


def _message_page(text_: str, status: int) -> HTMLResponse:
    return _page("Jomi report", f"<div class='card'><div class='brand'>Jomi report</div><p>{html.escape(text_)}</p></div>",
                 status=status)


@router.get("/r/{token}", include_in_schema=False)
def view_shared_report(token: str, lang: Optional[str] = Query(None), db: Session = Depends(get_db)):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token or ""):
        return _message_page("This link is not valid.", 404)
    row = db.execute(text("""
        SELECT r.id, r.title, r.mode, r.balgruh_name, r.assistant_coach, r.assistant_coaches, r.participants,
               r.started_at, r.created_at, r.source, r.is_deleted, COALESCE(u.name, r.recorded_by_name) AS recorded_by,
               rr.content, rr.status, COALESCE(rr.translations, '{}'::jsonb) AS translations
        FROM isf_recorder.report_share s
        JOIN isf_recorder.recording r ON r.id = s.recording_id
        LEFT JOIN isf_recorder."user" u ON u.id = r.user_id
        LEFT JOIN isf_recorder.recording_report rr ON rr.recording_id = r.id
        WHERE s.token = :t
    """), {"t": token}).fetchone()
    if not row or row.is_deleted or row.source == "call":
        return _message_page("This report is not available any more.", 404)
    content = (row.content or "").strip()
    if not content or row.status == "generating":
        return _message_page("The report is being prepared. Please open the link again in a few minutes.", 200)
    db.execute(text("UPDATE isf_recorder.report_share SET open_count = open_count + 1, last_opened_at = NOW() WHERE token = :t"),
               {"t": token})
    db.commit()

    lang = "hi" if (lang or "").strip().lower() == "hi" else "en"
    shown, note = content, ""
    if lang == "hi":
        hindi = _hindi(db, str(row.id), content, row.translations)
        if hindi:
            shown = hindi
        else:
            lang, note = "en", "The Hindi version could not be made right now, so the English report is shown."

    hi = lang == "hi"
    title = (row.title or "").replace("\\n", " ").strip() or "Report"
    when = row.started_at or row.created_at
    meta = []
    date_line = " · ".join(x for x in [
        when.astimezone(IST).strftime("%d %b %Y, %I:%M %p").lstrip("0").replace(" 0", " ") if when else "",
        {"schedule": "Schedule", "meeting": "Meeting"}.get((row.mode or "").lower(), "")] if x)
    if date_line:
        meta.append(f"📅 {date_line}")
    if row.balgruh_name:
        meta.append(f"🏠 {'बालगृह' if hi else 'Balgruha'}: {row.balgruh_name}")
    if row.recorded_by:
        meta.append(f"🎙️ {'रिकॉर्ड किया' if hi else 'Recorded by'}: {row.recorded_by}")
    if (row.mode or "") == "meeting":
        people = _names(row.participants)
        if people:
            meta.append(f"👥 {'शामिल लोग' if hi else 'Participants'}: {', '.join(people)}")
    else:
        coaches = _names(row.assistant_coaches) or ([row.assistant_coach] if row.assistant_coach else [])
        if coaches:
            meta.append(f"👥 {'सहायक कोच' if hi else 'Assistant coaches'}: {', '.join(coaches)}")

    other = ("English", f"?lang=en") if hi else ("हिंदी", "?lang=hi")
    body = (
        "<div class='card'>"
        "<div class='brand'>Jomi report</div>"
        f"<h1>{html.escape(title)}</h1>"
        + "".join(f"<div class='meta'>{html.escape(m)}</div>" for m in meta)
        + "<div class='bar'>"
        f"<button class='btn' onclick='window.print()'>⬇ {'PDF सेव करें' if hi else 'Save as PDF'}</button>"
        f"<a class='btn ghost' href='{other[1]}'>{other[0]}</a>"
        "</div>"
        f"<p class='note screen'>{'बिना इंटरनेट के पढ़ने के लिए: PDF सेव करें चुनें, फिर “Save as PDF”.' if hi else 'To read it without internet: tap Save as PDF, then choose “Save as PDF”.'}</p>"
        + (f"<p class='note'>{html.escape(note)}</p>" if note else "")
        + "<hr>"
        + markdown_to_html(shown)
        + "</div><footer>Shared from Jomi · Initiative Sewa Foundation</footer>"
    )
    return _page(title, body, lang=lang)
