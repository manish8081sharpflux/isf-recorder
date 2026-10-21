"""Report share links: Share sends a link to a public page with the full report (WhatsApp cuts long text)."""
import uuid

from sqlalchemy import text

from backend.database import SessionLocal
from backend.reports import service
from backend.reports.share import markdown_to_html
from backend.tests.conftest import make_user, delete_user

REPORT = "**General Overview**\n\n* Reading <practice>\n    * Asmita read well\n\n1. Coins - 10\n\n---\n\n| Name | Coins |\n|---|---|\n| Asmita | 10 |"


def _recording(user, source="recording", report=REPORT, status="draft"):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, balgruh_name,
                assistant_coaches, title, source, recorded_by_name)
            VALUES (:u, :o, :sid, 'completed', NOW(), 'schedule', 'Mohor Boys',
                    '[{"user_id": null, "name": "Rizwana"}]'::jsonb, 'Mohor Boys · Rizwana', :src, :n) RETURNING id
        """), {"u": user["id"], "o": user["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "src": source,
               "n": user["name"]}).scalar())
        if report is not None:
            s.execute(text("""INSERT INTO isf_recorder.recording_report (recording_id, content, status)
                              VALUES (:id, :c, :st)"""), {"id": rid, "c": report, "st": status})
        s.commit()
    return rid


def test_link_is_made_once_and_page_shows_the_full_report(client, staff):
    rid = _recording(staff)
    r1 = client.post(f"/recordings/{rid}/report/share-link", json={}, headers=staff["headers"])
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/recordings/{rid}/report/share-link", json={"lang": "hi"}, headers=staff["headers"]).json()
    assert r1.json()["token"] == r2["token"] and len(r2["token"]) >= 40
    assert r2["url"].endswith(f"/r/{r2['token']}?lang=hi") and "?" not in r1.json()["url"]

    page = client.get(f"/r/{r2['token']}")          # no login
    assert page.status_code == 200
    assert "noindex" in page.headers["x-robots-tag"]
    body = page.text
    assert "Mohor Boys · Rizwana" in body and "Rizwana" in body and "Save as PDF" in body
    assert "<strong>General Overview</strong>" in body or "<h4>General Overview</h4>" in body
    assert "Reading &lt;practice&gt;" in body and "<practice>" not in body          # escaped
    assert "<td>Asmita</td>" in body

    # later edits show up on the same link
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording_report SET content = 'Edited report' WHERE recording_id = :id"), {"id": rid})
        s.commit()
    assert "Edited report" in client.get(f"/r/{r2['token']}").text
    with SessionLocal() as s:
        assert s.execute(text("SELECT open_count FROM isf_recorder.report_share WHERE recording_id = :id"), {"id": rid}).scalar() == 2


def test_hindi_link_uses_the_saved_translation(client, staff, monkeypatch):
    rid = _recording(staff)
    url = client.post(f"/recordings/{rid}/report/share-link", json={"lang": "hi"}, headers=staff["headers"]).json()["url"]
    calls = []
    monkeypatch.setattr(service, "translate", lambda rec_id, lang, content: calls.append(lang) or "**सामान्य जानकारी**")
    path = url.split("://", 1)[1].split("/", 1)[1]
    path = "/" + path[path.index("r/"):]
    for _ in range(2):
        page = client.get(path)
        assert "सामान्य जानकारी" in page.text and "PDF सेव करें" in page.text and "lang='hi'" in page.text
    assert calls == ["hi"]                 # translated once, then saved


def test_hindi_failure_falls_back_to_english(client, staff, monkeypatch):
    rid = _recording(staff)
    token = client.post(f"/recordings/{rid}/report/share-link", json={}, headers=staff["headers"]).json()["token"]

    def boom(*a, **k):
        raise RuntimeError("AI down")
    monkeypatch.setattr(service, "translate", boom)
    page = client.get(f"/r/{token}?lang=hi")
    assert page.status_code == 200 and "General Overview" in page.text and "could not be made" in page.text


def test_who_may_make_a_link(client, staff, admin):
    rid = _recording(staff)
    other = make_user("staff")
    try:
        assert client.post(f"/recordings/{rid}/report/share-link", json={}, headers=other["headers"]).status_code == 403
    finally:
        delete_user(other["id"])
    assert client.post(f"/recordings/{rid}/report/share-link", json={}, headers=admin["headers"]).status_code == 200
    assert client.post(f"/recordings/{rid}/report/share-link", json={}).status_code in (401, 403)


def test_calls_and_unready_reports_get_no_link(client, staff):
    call = _recording(staff, source="call")
    assert client.post(f"/recordings/{call}/report/share-link", json={}, headers=staff["headers"]).status_code == 404
    pending = _recording(staff, report="", status="generating")
    assert client.post(f"/recordings/{pending}/report/share-link", json={}, headers=staff["headers"]).status_code == 409
    none = _recording(staff, report=None)
    assert client.post(f"/recordings/{none}/report/share-link", json={}, headers=staff["headers"]).status_code == 409


def test_deleted_recording_or_bad_token(client, staff):
    rid = _recording(staff)
    token = client.post(f"/recordings/{rid}/report/share-link", json={}, headers=staff["headers"]).json()["token"]
    with SessionLocal() as s:
        s.execute(text("DELETE FROM isf_recorder.recording WHERE id = :id"), {"id": rid})
        s.commit()
    assert client.get(f"/r/{token}").status_code == 404
    assert client.get("/r/short").status_code == 404
    assert client.get("/r/" + "x" * 43).status_code == 404


def test_markdown_rendering():
    h = markdown_to_html("# Title\n*1. Heading*\n- a **b** *c*\n\nline one\nline two\n<script>alert(1)</script>")
    assert "<h2>Title</h2>" in h and "<h4>1. Heading</h4>" in h
    assert "<strong>b</strong>" in h and "<em>c</em>" in h
    assert "line one<br>line two" in h and "<script>" not in h
