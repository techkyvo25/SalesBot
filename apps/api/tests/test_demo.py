"""The local demo (docs/DEMO.md): mounting rules, localhost only, page wording, and the demo
API against the database (tests marked by the `conn` fixture skip without one)."""

import re
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from salesbot_api.conversation import Conversation, open_conversation
from salesbot_api.demo import routes, seed
from salesbot_api.main import app as main_app
from salesbot_api.settings import Settings, get_settings
from salesbot_api.voice import store
from salesbot_core.flow import Inbound
from salesbot_core.text.wording import forbidden_terms

REPO = Path(__file__).resolve().parents[3]
STATIC = REPO / "apps/api/src/salesbot_api/demo/static"
WEEKDAY = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), \d{1,2} \w+")
CLOCK = re.compile(r"\d{1,2}:\d{2} (am|pm)")
SECRET = "demo-test-secret-at-least-32-characters!!"


def demo_settings(**extra) -> Settings:
    return Settings(_env_file=None, demo_mode=True, intake_hmac_secret=SECRET, **extra)


def demo_client(settings: Settings, **kwargs) -> TestClient:
    app = FastAPI()
    assert routes.mount(app, settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, **kwargs)


# -- mounting and binding -------------------------------------------------------------------


def test_demo_routes_are_not_mounted_by_default():
    app = FastAPI()
    assert not routes.mount(app, Settings(_env_file=None))
    with TestClient(app) as c:
        assert c.get("/demo").status_code == 404
        assert c.post("/demo/api/reset").status_code == 404


def test_main_app_has_no_demo_routes_without_demo_mode():
    with TestClient(main_app) as c:
        for path in ("/demo", "/demo/api/funnel", "/demo/api/day", "/demo/api/tasks"):
            assert c.get(path).status_code == 404


def test_demo_mode_needs_a_local_app_env():
    app = FastAPI()
    assert not routes.mount(app, Settings(_env_file=None, demo_mode=True, app_env="staging"))


def test_demo_page_is_served_with_demo_mode():
    with demo_client(demo_settings()) as c:
        res = c.get("/demo")
        assert res.status_code == 200
        assert "DEMO MODE: fake data." in res.text
        assert c.get("/demo/demo.js").status_code == 200


def test_demo_refuses_other_machines():
    with demo_client(demo_settings(), client=("10.1.2.3", 50000)) as c:
        assert c.get("/demo").status_code == 403
        assert c.post("/demo/api/reset").status_code == 403


def test_make_demo_binds_localhost_only():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("\ndemo:", 1)[1].split("\n\n", 1)[0]
    assert "--host 127.0.0.1" in target
    assert "0.0.0.0" not in makefile  # noqa: S104
    assert "DEMO_MODE=true" in target


def test_seed_refuses_without_demo_mode_or_outside_local():
    with pytest.raises(seed.RefusedError):
        seed.check_safe(None, Settings(_env_file=None))  # type: ignore[arg-type]
    with pytest.raises(seed.RefusedError):
        seed.check_safe(None, Settings(_env_file=None, demo_mode=True, app_env="production"))  # type: ignore[arg-type]


# -- page text ------------------------------------------------------------------------------


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        for name, value in attrs:
            if name in ("placeholder", "title", "aria-label") and value:
                self.parts.append(value)

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def page_texts() -> list[str]:
    parser = _Text()
    parser.feed((STATIC / "demo.html").read_text(encoding="utf-8"))
    script = (STATIC / "demo.js").read_text(encoding="utf-8")
    strings = re.findall(r'"([^"\n]*)"', script)
    return parser.parts + strings


def test_page_text_passes_the_wording_scan():
    hits = [(t, forbidden_terms(t)) for t in page_texts() if forbidden_terms(t)]
    assert hits == []


def test_page_has_the_banner_and_simulation_notes():
    texts = " ".join(page_texts())
    assert (
        "DEMO MODE: fake data. Registration, WhatsApp messages and phone calls are simulated."
        in texts
    )
    assert "WhatsApp would send this (simulated)" in texts
    assert "No alert reaches a person yet" in texts
    # Hindi replies come back with draft = true; the page tags them.
    assert '"tag draft", "DRAFT"' in (STATIC / "demo.js").read_text(encoding="utf-8")


# -- against the database -------------------------------------------------------------------


@pytest.fixture
def demo(db_url):
    settings = demo_settings(supabase_db_url=db_url)
    with demo_client(settings) as c:
        res = c.post("/demo/api/reset")
        assert res.status_code == 200, res.text
        c.session_id = uuid.uuid4().hex  # type: ignore[attr-defined]
        yield c
        c.post("/demo/api/reset")


def chat(c, text="", choice=None, patient="demo-asha"):
    res = c.post(
        "/demo/api/chat",
        json={"session": c.session_id, "patient": patient, "text": text, "choice": choice},
    )
    assert res.status_code == 200, res.text
    return res.json()


def register(c):
    res = c.post("/demo/api/register", json={"session": c.session_id})
    assert res.status_code == 200, res.text
    return res.json()


def first(data, prefix):
    options = [o["id"] for m in data["messages"] for o in m["options"]]
    return next(o for o in options if o.startswith(prefix))


@pytest.mark.db
def test_seed_and_funnel_counts(demo):
    assert demo.get("/demo/api/funnel").json() == {
        "registered": 2,
        "booked": 2,
        "confirmed": 1,
        "rescheduled": 0,
        "cancelled": 0,
    }
    data = register(demo)
    assert data["created"] and data["simulated"]
    assert [m["key"] for m in data["messages"]] == ["welcome"]
    assert data["messages"][0]["outbound"]
    assert demo.get("/demo/api/funnel").json()["registered"] == 3
    again = register(demo)
    assert not again["created"] and again["messages"] == []


@pytest.mark.db
def test_booking_goes_through_the_database(demo, conn):
    register(demo)
    day = first(chat(demo, "book"), "day:")
    slot = first(chat(demo, choice=day), "time:")
    assert [m["key"] for m in chat(demo, choice=slot)["messages"]] == ["confirm_slot"]
    booked = chat(demo, "yes")["messages"][0]
    assert booked["key"] == "booked" and booked["outbound"]
    assert WEEKDAY.search(booked["text"]) and CLOCK.search(booked["text"])
    view = demo.get(f"/demo/api/day?day={day[4:]}").json()
    rows = [r for d in view["doctors"] for r in d["rows"] if r["patient"] == "Asha Demo"]
    assert [r["status"] for r in rows] == ["booked"]
    funnel = demo.get("/demo/api/funnel").json()
    assert funnel["booked"] == 3
    # Cancel: the funnel counts it, the message names the appointment.
    chat(demo, "cancel my appointment")
    cancelled = chat(demo, "yes")["messages"][0]
    assert cancelled["key"] == "cancelled"
    assert WEEKDAY.search(cancelled["text"]) and CLOCK.search(cancelled["text"])
    assert demo.get("/demo/api/funnel").json()["cancelled"] == 1


@pytest.mark.db
def test_a_slot_taken_in_the_database_is_refused(demo, conn):
    register(demo)
    day = first(chat(demo, "book"), "day:")
    times = chat(demo, choice=day)
    slot = first(times, "time:")
    taken = demo.post(
        "/demo/api/other-patient-takes-slot", json={"session": demo.session_id}
    ).json()
    assert taken["booked_by"]
    reply = chat(demo, choice=slot)
    assert reply["messages"][0]["key"] == "slot_lost"
    business = store.business_by_slug(conn, seed.demo_clinic().slug)
    asha = conn.execute(
        "select count(*) as n from public.appointments a join public.clients c "
        "on c.id = a.client_id where a.business_id = %s and c.external_id = 'demo-asha' "
        "and a.starts_at = %s",
        (business.id, datetime.fromisoformat(slot[5:])),
    ).fetchone()["n"]
    assert asha == 0


@pytest.mark.db
def test_emergency_reply_has_the_numbers_only_fallback(demo):
    register(demo)
    data = chat(demo, "I have chest pain and can't breathe")
    message = data["messages"][0]
    assert message["key"] == "emergency"
    for number in ("112", "108", "+910000000000"):
        assert number in message["live_text"]
    assert not re.search(r"[A-Za-z]", message["live_text"])
    tasks = demo.get("/demo/api/tasks").json()
    assert tasks[0]["urgent"] and tasks[0]["kind"] == "emergency"


@pytest.mark.db
def test_hindi_replies_are_draft(demo):
    data = chat(demo, "namaste", patient="demo-ravi")
    assert data["messages"][0]["language"] == "hi"
    assert all(m["draft"] for m in data["messages"])


@pytest.mark.db
def test_opening_hours_come_from_the_database(demo):
    register(demo)
    text = chat(demo, "what are your opening hours")["messages"][0]["text"]
    assert "Dr. Priya Kulkarni (demo): Mon–Sun 9:00 am–1:00 pm, 4:00 pm–9:00 pm" in text
    assert demo.get("/demo/api/tasks").json() == []


@pytest.mark.db
def test_opt_out_is_saved_before_the_reply(demo, conn):
    register(demo)
    data = chat(demo, "stop")
    assert data["opt_out"] and data["messages"][0]["key"] == "opted_out"
    business = store.business_by_slug(conn, seed.demo_clinic().slug)
    client = conn.execute(
        "select * from public.clients where business_id = %s and external_id = 'demo-asha'",
        (business.id,),
    ).fetchone()
    assert client["whatsapp_opt_in"] is False
    event = conn.execute(
        "select count(*) as n from public.events where business_id = %s and client_id = %s "
        "and type = 'consent_declined'",
        (business.id, client["id"]),
    ).fetchone()["n"]
    assert event == 1
    # Messages we start are no longer sent.
    conv = open_conversation(conn, business, client, demo=True)
    assert conv.welcome(datetime.now(UTC)).replies == []


@pytest.mark.db
def test_no_reply_when_the_opt_out_cannot_be_saved(demo, conn, monkeypatch):
    register(demo)
    business = store.business_by_slug(conn, seed.demo_clinic().slug)
    client = conn.execute(
        "select * from public.clients where business_id = %s and external_id = 'demo-asha'",
        (business.id,),
    ).fetchone()
    conv = open_conversation(conn, business, client, demo=True)

    def fail(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(Conversation, "_opt_out", fail)
    with pytest.raises(RuntimeError):
        conv.handle(Inbound(text="stop", received_at=datetime.now(UTC)))
    assert conn.execute(
        "select whatsapp_opt_in from public.clients where id = %s", (client["id"],)
    ).fetchone()["whatsapp_opt_in"]


@pytest.mark.db
def test_reset_touches_only_the_demo_clinic(demo, conn):
    other = uuid.uuid4()
    conn.execute(
        "insert into public.businesses (id, name, slug, pack_id) values (%s, 'Other', %s, "
        "'clinic')",
        (other, f"test-{other.hex[:12]}"),
    )
    try:
        conn.execute(
            "insert into public.clients (business_id, external_id, name, phone, source) "
            "values (%s, 'x-1', 'Other Patient', '+919000000009', 'simulation')",
            (other,),
        )
        assert demo.post("/demo/api/reset").status_code == 200
        kept = conn.execute(
            "select count(*) as n from public.clients where business_id = %s", (other,)
        ).fetchone()["n"]
        assert kept == 1
        # A patient who was not simulated, outside the demo clinic: looks like production.
        conn.execute(
            "insert into public.clients (business_id, external_id, name, phone, source) "
            "values (%s, 'x-2', 'Real Looking', '+919000000008', 'registration_form')",
            (other,),
        )
        res = demo.post("/demo/api/reset")
        assert res.status_code == 409 and "production" in res.json()["detail"]
    finally:
        conn.execute("delete from public.businesses where id = %s", (other,))
