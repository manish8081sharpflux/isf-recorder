"""App version endpoint, nightly backup (mocked subprocess) and the daily health message."""
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text

from backend.database import SessionLocal
from backend.system import backup
from backend.system.app_config import get_values, set_values


# ─── /app/latest ────────────────────────────────────────────────────────────

def test_app_latest_public_and_admin_update(client, admin, staff):
    with SessionLocal() as s:
        before = get_values(s, ("latest_version_code", "latest_version_name", "latest_apk_notes",
                                "min_supported_version_code", "notify_test_builds"))
    try:
        r = client.get("/app/latest", headers={"host": "jomi.example.org", "x-forwarded-proto": "https"})
        assert r.status_code == 200 and r.json()["apk_url"] == "https://jomi.example.org/apk"
        assert client.put("/admin/app-version", json={"version_code": 9}, headers=staff["headers"]).status_code == 403
        r = client.put("/admin/app-version", json={"version_code": 9, "version_name": "2.0.9", "notes": "Faster reports",
                                                   "min_supported_version_code": 4, "notify_test_builds": True},
                       headers=admin["headers"])
        assert r.status_code == 200
        j = client.get("/app/latest").json()
        assert (j["version_code"], j["version_name"], j["notes"], j["min_supported_version_code"], j["notify_test_builds"]) == \
            (9, "2.0.9", "Faster reports", 4, True)
        assert client.put("/admin/app-version", json={}, headers=admin["headers"]).status_code == 400
    finally:
        with SessionLocal() as s:
            set_values(s, {k: v for k, v in before.items() if v is not None})
            s.commit()


# ─── backup ─────────────────────────────────────────────────────────────────

def _fake_run(dump_bytes: bytes = b"PGDMP-fake", dump_rc: int = 0, list_rc: int = 0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if "--list" in cmd:
            return SimpleNamespace(returncode=list_rc, stdout="", stderr="bad archive" if list_rc else "")
        out = cmd[cmd.index("-f") + 1]
        if dump_rc == 0:
            with open(out, "wb") as f:
                f.write(dump_bytes)
        return SimpleNamespace(returncode=dump_rc, stdout="", stderr="connection refused" if dump_rc else "")
    return run


def _recorded():
    with SessionLocal() as s:
        return get_values(s, ("last_backup_ok", "last_backup_bytes", "last_backup_at"))


def test_backup_ok_verifies_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "3")
    for d in ("20261001", "20261002", "20261003", "20261004"):
        (tmp_path / f"isf_recorder_{d}.dump").write_bytes(b"old")
    (tmp_path / "other.txt").write_text("keep me")
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls=calls))
    res = backup.run_backup(now=datetime(2026, 10, 12, 2, 30))
    assert res["ok"] and res["bytes"] == len(b"PGDMP-fake") and res["pruned"] == 2
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["isf_recorder_20261003.dump", "isf_recorder_20261004.dump", "isf_recorder_20261012.dump", "other.txt"]
    dump_cmd = calls[0]
    assert dump_cmd[1:4] == ["-n", "isf_recorder", "-Fc"] and "--list" in calls[1]
    assert _recorded()["last_backup_ok"] == "true" and _recorded()["last_backup_bytes"] == str(len(b"PGDMP-fake"))


def test_backup_failures_are_recorded_never_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    for kwargs, why in ((dict(dump_rc=1), "pg_dump exit"), (dict(dump_bytes=b""), "empty"),
                        (dict(list_rc=1), "pg_restore --list")):
        monkeypatch.setattr(subprocess, "run", _fake_run(**kwargs))
        res = backup.run_backup(now=datetime(2026, 10, 12))
        assert res["ok"] is False and why in res["error"]
        assert _recorded()["last_backup_ok"] == "false"
        assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())
        assert not (tmp_path / "isf_recorder_20261012.dump").exists()

    def boom(*a, **k):
        raise FileNotFoundError("pg_dump.exe not found")
    monkeypatch.setattr(subprocess, "run", boom)
    backup.run_backup_job()  # must not raise


# ─── health message ─────────────────────────────────────────────────────────

def test_health_text_builder():
    from backend.notifications.health_report import build_health_text
    s = {"date": datetime(2026, 10, 12), "started": 7, "completed": 5, "errors": 1, "stuck": 1, "unfinished": 2,
         "audio_hours": 6.25, "cost": [("TRANSCRIPTION", 41.2, 30), ("REPORT", 3.1, 5), ("CHAT", 0.2, 3)],
         "rescues": 2, "free_gb": 12.4, "versions": ["2.0.2", "2.0.3"],
         "backup_at": "2026-10-11T21:00:05+00:00", "backup_ok": "true", "backup_bytes": "13083025"}
    t = build_health_text(s)
    assert "12 Oct 2026" in t and "7 started · 5 done · 1 errors · ⚠️ 1 stuck" in t
    assert "6.2 h" in t or "6.3 h" in t
    assert "AI cost: ₹4" in t and "Transcription ₹41" in t and "Chat" not in t   # < ₹0.5 rows hidden
    assert "Rescue windows: 2" in t and "12 GB ⚠️ LOW" in t and "✅ ok, 12.5 MB" in t
    assert "Unfinished sessions: 2" in t and "2.0.2, 2.0.3" in t
    assert len(t.splitlines()) <= 11
    quiet = build_health_text({**s, "stuck": 0, "unfinished": 0, "rescues": 0, "free_gb": 200, "versions": [],
                               "cost": [], "backup_ok": "false"})
    assert "stuck" not in quiet and "LOW" not in quiet and "❌ FAILED" in quiet and "AI cost: ₹0 (none)" in quiet
    assert "Failed AI calls" not in quiet
    # rescue cost + failed calls (13 Oct: 36 OpenAI calls before the key was set)
    t2 = build_health_text({**s, "rescues": 109, "rescue_inr": 142.42,
                            "failed": [("openai-gpt-4o-transcribe-diarize", "OPENAI_API_KEY is not set on the server", 36)]})
    assert "Rescue windows: 109 (₹142)" in t2
    assert "⚠️ Failed AI calls: 36 (no cost) — openai-gpt-4o-transcribe-diarize: OPENAI_API_KEY is not set" in t2
    assert len(t2.splitlines()) <= 12


def test_health_report_dry_run_endpoint_never_sends(client, admin, monkeypatch):
    import backend.notifications.daily_summary as ds

    async def no_send(*a, **k):
        raise AssertionError("dry run must not send")
    monkeypatch.setattr(ds, "send_whatsapp_text", no_send)
    r = client.post("/notifications/trigger-health-report?dry_run=true", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["sent"] == 0 and r.json()["text"].startswith("🩺 Jomi health")


def test_apk_download_name_has_version(monkeypatch):
    from backend import main
    from backend.database import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as db:
        old = db.execute(text("SELECT value FROM isf_recorder.app_config WHERE key='latest_version_name'")).scalar()
        db.execute(text("UPDATE isf_recorder.app_config SET value='9.9.9' WHERE key='latest_version_name'")); db.commit()
    try:
        assert main._apk_file_name() == "ISFRecorder-9.9.9.apk"
    finally:
        with SessionLocal() as db:
            db.execute(text("UPDATE isf_recorder.app_config SET value=:v WHERE key='latest_version_name'"), {"v": old}); db.commit()
