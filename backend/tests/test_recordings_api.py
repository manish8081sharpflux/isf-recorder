"""Recordings API additions (contract §4.1, 4.6-4.8, 4.11)."""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import make_user, delete_user, wav_bytes

IST = timezone(timedelta(hours=5, minutes=30))


def _sid():
    return f"rec_test_{uuid.uuid4().hex}"


def test_start_schedule_requires_balgruh_and_coach(client, staff):
    r = client.post("/recordings/start", json={"session_id": _sid(), "mode": "schedule", "balgruh_name": "Mohor Boys"},
                    headers=staff["headers"])
    assert r.status_code == 400
    sid = _sid()
    r = client.post("/recordings/start", json={
        "session_id": sid, "mode": "schedule", "balgruh_name": "Mohor Boys", "assistant_coach": "Rizwana",
        "participants": [{"user_id": None, "name": "Guest"}]}, headers=staff["headers"])
    assert r.status_code == 200 and r.json()["status"] == "created"
    detail = client.get(f"/recordings/{r.json()['recording_id']}", headers=staff["headers"]).json()
    assert detail["title"] == "Mohor Boys · Rizwana" and detail["mode"] == "schedule"
    assert detail["assistant_coach"] == "Rizwana" and detail["participants"] == [{"user_id": None, "name": "Guest"}]
    assert detail["report"] is None and detail["report_status"] is None
    # legacy start (v1.2.0) still works and stores no mode
    r = client.post("/recordings/start", json={"session_id": _sid(), "mic_source": "wired"}, headers=staff["headers"])
    assert r.status_code == 200 and r.json()["status"] == "created"


def test_list_filters_and_new_fields(client, staff):
    now = datetime.now(timezone.utc)
    ids = {}
    with SessionLocal() as s:
        for key, started, mode in (("today", now, "meeting"), ("old", now - timedelta(days=10), "schedule"),
                                   ("legacy", now - timedelta(days=1), None)):
            ids[key] = str(s.execute(text("""
                INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode,
                    balgruh_name, duration_seconds, voice_seconds, title)
                VALUES (:u, :o, :sid, 'completed', :st, :mode, :bg, 2040, 2000, 'Weekly review') RETURNING id
            """), {"u": staff["id"], "o": staff["org_id"], "sid": _sid(), "st": started, "mode": mode,
                   "bg": "Mohor Boys" if mode == "schedule" else None}).scalar())
        s.commit()
    today_ist = now.astimezone(IST).strftime("%Y-%m-%d")
    r = client.get("/recordings/", params={"date_from": today_ist, "date_to": today_ist}, headers=staff["headers"])
    got = {i["id"] for i in r.json()["items"]}
    assert ids["today"] in got and ids["old"] not in got and ids["legacy"] not in got
    r = client.get("/recordings/", params={"mode": "schedule"}, headers=staff["headers"])
    items = {i["id"]: i for i in r.json()["items"]}
    assert set(items) == {ids["old"], ids["legacy"]}  # old-app recordings (no mode) count as Schedule
    r = client.get("/recordings/", params={"mode": "meeting"}, headers=staff["headers"])
    assert {i["id"] for i in r.json()["items"]} == {ids["today"]}
    item = items[ids["old"]]
    assert item["balgruh_name"] == "Mohor Boys" and item["duration_formatted"] == "34 min"
    assert item["voice_seconds"] == 2000 and item["report_status"] is None
    d = (now - timedelta(days=10)).astimezone(IST)
    assert item["date_formatted"] == f"{d.day} {d.strftime('%b %Y')}"
    for key in ("user_name", "estimated_cost_formatted", "status", "started_at_formatted"):
        assert key in item  # legacy fields kept
    assert client.get("/recordings/", params={"date_from": "07-10-2026"}, headers=staff["headers"]).status_code == 400


def test_status_route_not_shadowed_and_scoped(client, staff, admin):
    sid = _sid()
    rid = client.post("/recordings/start", json={"session_id": sid}, headers=staff["headers"]).json()["recording_id"]
    r = client.get("/recordings/status", params={"ids": f"{rid},{uuid.uuid4()}"}, headers=staff["headers"])
    assert r.status_code == 200
    assert r.json()["items"] == [{"id": rid, "status": "recording", "pipeline_stage": None, "report_status": None,
                                  "error_message": None, "title": None, "duration_seconds": 0,
                                  "silence_skipped_count": 0, "silence_skipped_seconds": 0}]
    other = make_user("staff")
    try:
        assert client.get("/recordings/status", params={"ids": rid}, headers=other["headers"]).json()["items"] == []
    finally:
        delete_user(other["id"])
    assert len(client.get("/recordings/status", params={"ids": rid}, headers=admin["headers"]).json()["items"]) == 1
    too_many = ",".join(str(uuid.uuid4()) for _ in range(51))
    assert client.get("/recordings/status", params={"ids": too_many}, headers=staff["headers"]).status_code == 400
    assert client.get("/recordings/status", params={"ids": "nope"}, headers=staff["headers"]).status_code == 400


def test_erase_removes_rows_and_files_keeps_cost(client, staff, monkeypatch):
    import backend.recordings.lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "schedule_transcription_job", lambda *a, **k: True)
    sid = _sid()
    rid = client.post("/recordings/start", json={"session_id": sid}, headers=staff["headers"]).json()["recording_id"]
    for i in range(2):
        client.post(f"/recordings/{sid}/chunk", data={"chunk_index": str(i)},
                    files={"chunk_file": ("c.wav", wav_bytes(1.5, 2000, 300), "audio/wav")}, headers=staff["headers"])
    client.post(f"/recordings/{sid}/finish", json={"total_chunks": 2}, headers=staff["headers"])
    deadline = time.time() + 30
    while time.time() < deadline:
        with SessionLocal() as s:
            row = s.execute(text("SELECT pipeline_stage, audio_file_path FROM isf_recorder.recording WHERE id = :id"),
                            {"id": rid}).fetchone()
        if row.pipeline_stage == "queued":
            break
        time.sleep(0.2)
    m4a = row.audio_file_path
    chunk_dir = os.path.join(os.path.dirname(m4a), sid)
    assert os.path.exists(m4a) and os.path.isdir(chunk_dir)
    with SessionLocal() as s:  # simulate a finished transcription with cost, segments, report and chat
        s.execute(text("UPDATE isf_recorder.recording SET status = 'completed', pipeline_stage = 'done' WHERE id = :id"), {"id": rid})
        run = s.execute(text("""INSERT INTO isf_recorder.transcription_run (recording_id, model, status)
                                VALUES (:id, 'gemini-2.5-flash', 'completed') RETURNING id"""), {"id": rid}).scalar()
        s.execute(text("UPDATE isf_recorder.recording SET active_run_id = :run WHERE id = :id"), {"id": rid, "run": run})
        s.execute(text("""INSERT INTO isf_recorder.gemini_usage_log (recording_id, run_id, operation_type, gemini_model,
                          request_timestamp, total_cost_inr, gemini_request_id)
                          VALUES (:id, :run, 'TRANSCRIPTION', 'gemini-2.5-flash', NOW(), 1.25, :req)"""),
                  {"id": rid, "run": run, "req": f"test_{rid}"})
        s.execute(text("""INSERT INTO isf_recorder.transcript_segment (recording_id, text) VALUES (:id, 'hello')"""), {"id": rid})
        s.execute(text("""INSERT INTO isf_recorder.recording_report (recording_id, content, status, run_id)
                          VALUES (:id, 'report', 'draft', :run)"""), {"id": rid, "run": run})
        s.execute(text("""INSERT INTO isf_recorder.transcription_chunk (run_id, recording_id, idx, start_sec, end_sec)
                          VALUES (:run, :id, 0, 0, 3)"""), {"id": rid, "run": run})
        s.execute(text("""INSERT INTO isf_recorder.chat_conversation (user_id, org_id, recording_id, mode)
                          VALUES (:u, :o, :id, 'session')"""), {"id": rid, "u": staff["id"], "o": staff["org_id"]})
        s.commit()

    other = make_user("staff")
    try:
        assert client.delete(f"/recordings/{rid}", headers=other["headers"]).status_code == 403
    finally:
        delete_user(other["id"])
    r = client.delete(f"/recordings/{rid}", headers=staff["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "erased" and body["files_removed"] >= 3  # m4a + 2 chunk WAVs
    assert not os.path.exists(m4a) and not os.path.exists(chunk_dir)
    with SessionLocal() as s:
        for table in ("recording", "transcript_segment", "transcription_run", "recording_report",
                      "transcription_chunk", "audio_chunk", "chat_conversation"):
            col = "id" if table == "recording" else "recording_id"
            assert s.execute(text(f"SELECT COUNT(*) FROM isf_recorder.{table} WHERE {col} = :id"), {"id": rid}).scalar() == 0
        usage = s.execute(text("SELECT recording_id, org_id, total_cost_inr FROM isf_recorder.gemini_usage_log WHERE gemini_request_id = :r"),
                          {"r": f"test_{rid}"}).fetchone()
        assert usage.recording_id is None and str(usage.org_id) == staff["org_id"] and float(usage.total_cost_inr) == 1.25
        tomb = s.execute(text("SELECT recording_id, deleted_by FROM isf_recorder.deleted_session WHERE session_id = :sid"),
                         {"sid": sid}).fetchone()
        assert str(tomb.recording_id) == rid and str(tomb.deleted_by) == staff["id"]
        s.execute(text("DELETE FROM isf_recorder.gemini_usage_log WHERE gemini_request_id = :r"), {"r": f"test_{rid}"})
        s.commit()
    late = client.post(f"/recordings/{sid}/chunk", data={"chunk_index": "2"},
                       files={"chunk_file": ("c.wav", wav_bytes(1.0), "audio/wav")}, headers=staff["headers"])
    assert late.json()["status"] == "discarded"
    assert client.get(f"/recordings/{rid}", headers=staff["headers"]).status_code == 404


def test_erase_refused_while_processing(client, staff):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at)
            VALUES (:u, :o, :sid, 'processing', NOW()) RETURNING id
        """), {"u": staff["id"], "o": staff["org_id"], "sid": _sid()}).scalar())
        s.commit()
    assert client.delete(f"/recordings/{rid}", headers=staff["headers"]).status_code == 409


def test_audio_supports_range_requests(client, staff, tmp_path):
    """ExoPlayer seeks with Range; Starlette 0.38's FileResponse ignores it, so the route answers 206 itself."""
    sid = _sid()
    rid = client.post("/recordings/start", json={"session_id": sid}, headers=staff["headers"]).json()["recording_id"]
    f = tmp_path / "a.m4a"
    payload = bytes(range(256)) * 40  # 10240 bytes
    f.write_bytes(payload)
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording SET audio_file_path = :p WHERE id = :id"), {"p": str(f), "id": rid})
        s.commit()
    h = staff["headers"]
    r = client.get(f"/recordings/{rid}/audio", headers=h)
    assert r.status_code == 200 and r.content == payload and r.headers["accept-ranges"] == "bytes"
    r = client.get(f"/recordings/{rid}/audio", headers={**h, "Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == payload[100:200]
    assert r.headers["content-range"] == "bytes 100-199/10240"
    r = client.get(f"/recordings/{rid}/audio", headers={**h, "Range": "bytes=10000-"})
    assert r.status_code == 206 and r.content == payload[10000:]
    r = client.get(f"/recordings/{rid}/audio", headers={**h, "Range": "bytes=-40"})
    assert r.status_code == 206 and r.content == payload[-40:]
    r = client.get(f"/recordings/{rid}/audio", headers={**h, "Range": "bytes=20000-"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */10240"


def test_audio_not_ready_vs_not_available(client, staff):
    """Right after Stop the m4a is still being stitched -> 404 (app retries); missing file -> 410 wording."""
    sid = _sid()
    rid = client.post("/recordings/start", json={"session_id": sid}, headers=staff["headers"]).json()["recording_id"]
    h = staff["headers"]
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording SET status = 'uploading', pipeline_stage = 'stitching' WHERE id = :id"), {"id": rid})
        s.commit()
    r = client.get(f"/recordings/{rid}/audio", headers=h)
    assert r.status_code == 404 and "still being saved" in r.json()["detail"]
    with SessionLocal() as s:
        s.execute(text("""UPDATE isf_recorder.recording SET status = 'completed', pipeline_stage = 'done',
                          audio_file_path = 'Z:/missing/x.m4a' WHERE id = :id"""), {"id": rid})
        s.commit()
    r = client.get(f"/recordings/{rid}/audio", headers=h)
    assert r.status_code == 410 and r.json()["detail"].startswith("Audio file is not available for this recording.")
    assert "7-day" not in r.json()["detail"]
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording SET audio_deleted_at = NOW() WHERE id = :id"), {"id": rid})
        s.commit()
    r = client.get(f"/recordings/{rid}/audio", headers=h)
    assert r.status_code == 410 and "old 7-day cleanup" in r.json()["detail"]


def test_finish_stores_silence_gaps_and_old_app_has_none(client, staff, monkeypatch):
    import backend.recordings.lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "start_finalize_thread", lambda *a, **k: None)
    h = staff["headers"]
    sid = _sid()
    rid = client.post("/recordings/start", json={"session_id": sid}, headers=h).json()["recording_id"]
    gaps = [{"at_voice_sec": 95.2, "wall_start_ms": 1700000100000, "seconds": 80},
            {"at_voice_sec": 12.0, "wall_start_ms": 1700000012000, "seconds": 4.5},
            {"at_voice_sec": 20.0, "wall_start_ms": 1, "seconds": 2.0},          # < 3 s: dropped
            {"at_voice_sec": "x", "seconds": 9}]                                 # junk: dropped
    r = client.post(f"/recordings/{sid}/finish", json={"total_chunks": 0, "silence_gaps": gaps}, headers=h)
    assert r.status_code == 200
    d = client.get(f"/recordings/{rid}", headers=h).json()
    assert [g["at_voice_sec"] for g in d["silence_gaps"]] == [12.0, 95.2]
    assert d["silence_skipped_count"] == 2 and d["silence_skipped_seconds"] in (84, 85)
    st = client.get(f"/recordings/status?ids={rid}", headers=h).json()["items"][0]
    assert st["silence_skipped_count"] == 2
    items = client.get("/recordings/", headers=h).json()["items"]
    assert next(i for i in items if i["id"] == rid)["silence_skipped_count"] == 2
    # a repeated finish without gaps keeps them
    client.post(f"/recordings/{sid}/finish", json={"total_chunks": 0}, headers=h)
    assert client.get(f"/recordings/{rid}", headers=h).json()["silence_skipped_count"] == 2
    # old app: no gaps at all
    sid2 = _sid()
    rid2 = client.post("/recordings/start", json={"session_id": sid2}, headers=h).json()["recording_id"]
    client.post(f"/recordings/{sid2}/finish", json={"total_chunks": 0, "duration_seconds": 10}, headers=h)
    d2 = client.get(f"/recordings/{rid2}", headers=h).json()
    assert d2["silence_gaps"] == [] and d2["silence_skipped_count"] == 0 and d2["silence_skipped_seconds"] == 0
