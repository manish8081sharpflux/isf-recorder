"""Admin "Coach" filter: ONLY the sessions the coach recorded (also under their name); assistant-coach sessions do not count."""
import uuid

from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import make_user, delete_user


def _rec(owner, coaches_json="[]", coach_text=None, recorded_by=None):
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, mode, balgruh_name,
                assistant_coaches, assistant_coach, title, recorded_by_name)
            VALUES (:u, :o, :sid, 'completed', NOW(), 'schedule', 'Mohor Girls', CAST(:cj AS jsonb), :ct, 'Mohor Girls', :rb)
            RETURNING id
        """), {"u": owner["id"], "o": owner["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "cj": coaches_json,
               "ct": coach_text, "rb": recorded_by or owner["name"]}).scalar())
        s.commit()
    return rid


def _ids(client, headers, **q):
    r = client.get("/recordings/", params={"limit": 100, **q}, headers=headers)
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}, r.json()["total"]


def test_coach_filter_shows_only_sessions_the_coach_recorded(client, admin):
    name = f"Asmita {uuid.uuid4().hex[:4]}"
    asmita = make_user("staff", name=name)
    twin = make_user("staff", name=name)          # a second account with the same name (wrong link in the coach list)
    sanket = make_user("staff")
    try:
        own = _rec(asmita)                                                            # made from her account
        as_assistant = _rec(sanket, coaches_json=f'[{{"user_id": null, "name": "{name.lower()}"}}]')
        old_row = _rec(sanket, coach_text=f"Rizwana, {name}")                         # old app text field
        other_account = _rec(twin)                                                    # recorded under her name
        unrelated = _rec(sanket, coaches_json='[{"user_id": null, "name": "Archana"}]')

        got, total = _ids(client, admin["headers"], coach=name, coach_user_id=asmita["id"])
        assert {own, other_account} <= got                                  # recorded by her (any of her accounts)
        assert as_assistant not in got and old_row not in got                 # only present as assistant coach
        assert unrelated not in got and total == len(got)

        # the old behaviour (account only) is still there for anything that sends user_id
        only_account, _ = _ids(client, admin["headers"], user_id=asmita["id"])
        assert only_account == {own}
    finally:
        for u in (asmita, twin, sanket):
            delete_user(u["id"])


def test_coaches_cannot_use_it_to_see_other_recordings(client, staff):
    other = make_user("staff")
    try:
        mine = _rec(staff)
        theirs = _rec(other, coaches_json=f'[{{"user_id": null, "name": "{staff["name"]}"}}]')
        got, _ = _ids(client, staff["headers"], coach=staff["name"])
        assert mine in got and theirs not in got
    finally:
        delete_user(other["id"])
