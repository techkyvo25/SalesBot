"""Voice tools, hooks, webhooks, outbound calls and the spend guard against a real Postgres
(Supabase local in CI). Marked `db`.

The clinic fixture (conftest.py): two doctors, Mon-Sat 10:00-13:00 IST with a break at
11:30-12:00, and two patients registered on the same (family) number +919000000001.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from salesbot_api import booking
from salesbot_api.api_v1 import get_conn
from salesbot_api.main import app
from salesbot_api.records import Actor, enqueue_job
from salesbot_api.settings import Settings, get_settings
from salesbot_api.voice import outbound, spend, tools
from salesbot_core.costs import SpendCapReachedError, SpendKind
from salesbot_core.events import Channel

pytestmark = pytest.mark.db

IST = ZoneInfo("Asia/Kolkata")
MONDAY = date(2030, 1, 7)
NOW = datetime.combine(MONDAY, time(8, 0), IST)  # first free time is 10:00
FAMILY = "+919000000001"
SECRET = "tool-secret-for-db-tests-0123456789abcdef"
HOOK = "hook-secret-for-db-tests-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def ist(hh: int, mm: int = 0, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time(hh, mm), IST)


@pytest.fixture
def vsettings(db_url) -> Settings:
    return Settings(
        _env_file=None,
        supabase_db_url=db_url,
        voice_tool_secret=SECRET,
        voice_webhook_secret=HOOK,
        spend_session_id=f"test-{uuid.uuid4().hex[:8]}",
    )


@pytest.fixture
def api(vsettings, db_url):
    def conn():
        with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as c:
            yield c

    app.dependency_overrides[get_settings] = lambda: vsettings
    app.dependency_overrides[get_conn] = conn
    app.dependency_overrides[tools.utcnow] = lambda: NOW
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def slug(conn, clinic) -> str:
    return conn.execute(
        "select slug from public.businesses where id = %s", (clinic.business_id,)
    ).fetchone()["slug"]


def call(api, tool, slug, interaction="20300107/call-1", **body):
    payload = {"clinic": slug, "interaction_id": interaction, "caller_phone": FAMILY, **body}
    r = api.post(f"/api/v1/voice/tools/{tool}", json=payload, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def events(conn, clinic, kind):
    return conn.execute(
        "select * from public.events where business_id = %s and type = %s order by id",
        (clinic.business_id, kind),
    ).fetchall()


def book_10am(api, slug, clinic, interaction="20300107/call-1"):
    held = call(
        api,
        "hold_slot",
        slug,
        interaction,
        patient_id=str(clinic.client_id),
        starts_at=ist(10).isoformat(),
        practitioner_id=str(clinic.dr_a),
    )
    assert held["ok"], held
    return call(
        api,
        "book",
        slug,
        interaction,
        patient_id=str(clinic.client_id),
        hold_id=held["data"]["hold_id"],
    )


# ---------------------------------------------------------------------------
# identify_patient and on-start
# ---------------------------------------------------------------------------


def test_shared_number_lists_candidates(api, slug, clinic):
    r = call(api, "identify_patient", slug)
    assert (r["ok"], r["code"], r["next_step"]) == (True, "choose_patient", "ask_which_patient")
    assert {c["patient_id"] for c in r["data"]["candidates"]} == {
        str(clinic.client_id),
        str(clinic.other_client_id),
    }

    picked = call(api, "identify_patient", slug, patient_id=str(clinic.client_id))
    assert picked["code"] == "ok" and picked["data"]["patient_id"] == str(clinic.client_id)


TODAY = {
    "today_weekday": "Monday",
    "today_date": "2030-01-07",
    "time_now": "08:00",
    "timezone": "Asia/Kolkata (IST)",
}


def test_identify_returns_today_in_india(api, slug, clinic):
    """Every identify_patient answer carries today's weekday, date and time in IST; the spoken
    line stays as before."""
    listed = call(api, "identify_patient", slug)
    picked = call(api, "identify_patient", slug, patient_id=str(clinic.client_id))
    unknown = call(api, "identify_patient", slug, caller_phone="+919111111111")
    for r in (listed, picked, unknown):
        assert {k: r["data"][k] for k in TODAY} == TODAY
        for value in TODAY.values():
            assert value not in r["spoken_text"]


def test_unknown_number_is_not_registered(api, slug):
    r = call(api, "identify_patient", slug, caller_phone="+919111111111")
    assert (r["ok"], r["code"], r["next_step"]) == (False, "not_registered", "offer_callback")


def test_on_start_returns_agent_variables(api, slug, clinic, conn):
    body = {"clinic": slug, "interaction_id": "20300107/start-1", "caller_phone": FAMILY}
    r = api.post("/api/v1/voice/hooks/on-start", json=body, headers=AUTH).json()
    assert r["clinic_name"] == "Test clinic" and r["clinic_slug"] == slug
    assert r["patients_on_number"] == 2 and r["patient_id"] == ""
    assert r["emergency_numbers"] == "112 / 108"
    assert len(events(conn, clinic, "call_received")) == 1


def test_another_callers_patient_is_refused(api, slug, clinic):
    r = call(
        api,
        "get_availability",
        slug,
        caller_phone="+919222222222",
        patient_id=str(clinic.client_id),
    )
    assert (r["ok"], r["code"]) == (False, "unknown_patient")


# ---------------------------------------------------------------------------
# Availability and spoken text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "weekday", "month", "clock"),
    [("English", "Monday", "January", "10:00 am"), ("Hindi", "सोमवार", "जनवरी", "सुबह 10:00")],
)
def test_availability_offers_at_most_three_times_with_weekday_date_and_time(
    api, slug, clinic, language, weekday, month, clock
):
    r = call(
        api,
        "get_availability",
        slug,
        patient_id=str(clinic.client_id),
        date=MONDAY.isoformat(),
        language=language,
    )
    assert r["ok"] and 1 <= len(r["data"]["options"]) <= 3
    first = r["data"]["options"][0]
    assert datetime.fromisoformat(first["starts_at"]) == ist(10)
    for text in (r["spoken_text"], first["label"]):
        assert weekday in text and "7" in text and month in text and clock in text


def test_part_of_day_with_nothing_free_offers_the_next_times(api, slug, clinic):
    r = call(
        api,
        "get_availability",
        slug,
        patient_id=str(clinic.client_id),
        date=MONDAY.isoformat(),
        part_of_day="evening",
    )
    assert r["ok"] and r["spoken_text"].startswith("There is no free time then.")
    assert r["data"]["options"]


# ---------------------------------------------------------------------------
# Booking, idempotency, concurrency
# ---------------------------------------------------------------------------


def test_book_confirms_only_after_commit_and_links_the_call(api, slug, clinic, conn):
    r = book_10am(api, slug, clinic)
    assert (r["ok"], r["code"]) == (True, "ok")
    assert "Monday, 7 January" in r["spoken_text"] and "10:00 am" in r["spoken_text"]
    appt = conn.execute(
        "select a.*, c.provider_call_id from public.appointments a "
        "join public.calls c on c.id = a.call_id where a.id = %s",
        (r["data"]["appointment_id"],),
    ).fetchone()
    assert appt["status"] == "booked" and appt["booked_via"] == "voice"
    assert appt["provider_call_id"] == "20300107/call-1"
    created = events(conn, clinic, "booking_created")
    assert len(created) == 1 and created[0]["channel"] == "voice"
    assert created[0]["call_id"] == appt["call_id"]
    assert {e["properties"]["tool"] for e in events(conn, clinic, "voice_tool_called")} == {
        "hold_slot",
        "book",
    }


def test_a_retried_tool_call_does_not_act_twice(api, slug, clinic, conn):
    held = call(
        api,
        "hold_slot",
        slug,
        patient_id=str(clinic.client_id),
        starts_at=ist(10).isoformat(),
        practitioner_id=str(clinic.dr_a),
    )
    body = {"patient_id": str(clinic.client_id), "hold_id": held["data"]["hold_id"]}
    first = call(api, "book", slug, **body)
    again = call(api, "book", slug, **body)
    assert first == again
    assert len(events(conn, clinic, "booking_created")) == 1
    n = conn.execute(
        "select count(*) as n from public.voice_tool_calls "
        "where business_id = %s and tool = 'book'",
        (clinic.business_id,),
    ).fetchone()["n"]
    assert n == 1


def test_two_calls_holding_the_same_time_exactly_one_wins(vsettings, db_url, slug, clinic):
    def attempt(i: int) -> tools.ToolResult:
        req = tools.HoldIn.model_validate(
            {
                "clinic": slug,
                "interaction_id": f"20300107/race-{i}",
                "caller_phone": FAMILY,
                "patient_id": str(clinic.client_id if i == 0 else clinic.other_client_id),
                "starts_at": ist(10).isoformat(),
                "practitioner_id": str(clinic.dr_a),
            }
        )
        with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as c:
            return tools.run_tool("hold_slot", req, c, vsettings, NOW, tools._hold)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(r.ok for r in results) == [False, True]
    loser = next(r for r in results if not r.ok)
    assert loser.code == "slot_unavailable" and loser.next_step == "get_availability"
    assert "10:00 am" in loser.spoken_text


def test_expired_hold_is_reported_safely(api, slug, clinic, conn):
    held = call(
        api,
        "hold_slot",
        slug,
        patient_id=str(clinic.client_id),
        starts_at=ist(10).isoformat(),
        practitioner_id=str(clinic.dr_a),
    )
    app.dependency_overrides[tools.utcnow] = lambda: NOW + timedelta(minutes=30)
    r = call(api, "book", slug, patient_id=str(clinic.client_id), hold_id=held["data"]["hold_id"])
    assert (r["ok"], r["code"], r["next_step"]) == (False, "hold_expired", "get_availability")


# ---------------------------------------------------------------------------
# Reschedule, cancel, confirm, staff tasks
# ---------------------------------------------------------------------------


def test_reschedule_moves_atomically_and_stops_old_reminders(api, slug, clinic, conn):
    booked = book_10am(api, slug, clinic)
    old_id = booked["data"]["appointment_id"]
    enqueue_job(
        conn,
        business_id=clinic.business_id,
        kind="reminder",
        run_at=ist(9, day=MONDAY),
        appointment_id=old_id,
    )
    # Move to a time the agent was actually offered (the grid follows the visit length).
    offered = call(api, "get_availability", slug, "20300107/call-2",
                   patient_id=str(clinic.client_id), for_reschedule=True)  # fmt: skip
    option = next(
        o for o in offered["data"]["options"] if datetime.fromisoformat(o["starts_at"]) != ist(10)
    )
    new_start = datetime.fromisoformat(option["starts_at"])
    r = call(
        api,
        "reschedule",
        slug,
        "20300107/call-2",
        patient_id=str(clinic.client_id),
        appointment_id=old_id,
        starts_at=option["starts_at"],
        practitioner_id=option["practitioner_id"],
    )
    assert (r["ok"], r["code"]) == (True, "ok")
    assert datetime.fromisoformat(r["data"]["starts_at"]) == new_start
    clock = f"{new_start.astimezone(IST).hour % 12 or 12}:{new_start.astimezone(IST):%M}"
    assert "Monday, 7 January" in r["spoken_text"] and clock in r["spoken_text"]
    old = conn.execute("select * from public.appointments where id = %s", (old_id,)).fetchone()
    assert old["status"] == "rescheduled"
    assert str(old["rescheduled_to_id"]) == r["data"]["appointment_id"]
    queued = conn.execute(
        "select count(*) as n from public.jobs where appointment_id = %s and status = 'queued'",
        (old_id,),
    ).fetchone()["n"]
    assert queued == 0


def test_cancel_needs_an_explicit_yes(api, slug, clinic, conn):
    appt_id = book_10am(api, slug, clinic)["data"]["appointment_id"]
    ask = call(api, "cancel", slug, patient_id=str(clinic.client_id), appointment_id=appt_id)
    assert (ask["ok"], ask["code"]) == (False, "needs_confirmation")
    assert "10:00 am" in ask["spoken_text"]
    status = conn.execute(
        "select status from public.appointments where id = %s", (appt_id,)
    ).fetchone()["status"]
    assert status == "booked"
    done = call(
        api,
        "cancel",
        slug,
        patient_id=str(clinic.client_id),
        appointment_id=appt_id,
        confirmed=True,
    )
    assert done["ok"] and done["data"]["status"] == "cancelled"


def test_confirm_attendance(api, slug, clinic):
    appt_id = book_10am(api, slug, clinic)["data"]["appointment_id"]
    r = call(
        api, "confirm_attendance", slug, patient_id=str(clinic.client_id), appointment_id=appt_id
    )
    assert r["ok"] and r["data"]["status"] == "confirmed"


def test_urgent_staff_task_gives_emergency_numbers(api, slug, clinic, conn):
    r = call(
        api,
        "create_staff_task",
        slug,
        patient_id=str(clinic.client_id),
        reason="emergency",
        summary="Caller reports chest pain.",
    )
    assert r["code"] == "urgent_task_created" and "112 / 108" in r["spoken_text"]
    task = conn.execute(
        "select * from public.staff_tasks where business_id = %s", (clinic.business_id,)
    ).fetchone()
    assert (task["kind"], task["urgency"], task["channel"]) == ("emergency", "urgent", "voice")
    assert events(conn, clinic, "handoff_task_created")
    assert events(conn, clinic, "emergency_detected")


# ---------------------------------------------------------------------------
# End of call: on-end hook and Sarvam's webhook (cost, spend ledger)
# ---------------------------------------------------------------------------


def test_webhook_stores_cost_and_settles_spend_once(api, slug, clinic, conn):
    interaction = f"20300107/{uuid.uuid4().hex}"
    call(api, "identify_patient", slug, interaction, patient_id=str(clinic.client_id))
    payload = {
        "app_id": "app-1",
        "interaction_id": interaction,
        "user_phone_number": FAMILY,
        "agent_phone_number": "+918000000000",
        "duration": 45.2,
        "final_agent_variables": {"outcome": "booked", "summary": "Booked a visit."},
        "interaction_transcript": [{"role": "agent", "en_text": "Hello", "indic_text": None}],
        "metadata": {"clinic": slug},
    }
    for _ in range(2):  # Sarvam may retry a delivery
        r = api.post(f"/api/v1/voice/webhooks/{HOOK}/inbound", json=payload)
        assert r.status_code == 200 and r.json() == {"ok": True}
    row = conn.execute(
        "select * from public.calls where provider_call_id = %s", (interaction,)
    ).fetchone()
    assert (row["status"], row["agent_minutes"], row["telephony_pulses"]) == ("completed", 1, 2)
    assert row["cost_inr_estimate"] == Decimal("3.90")
    assert row["recording_ref"].endswith(interaction) and row["summary"] == "Booked a visit."
    assert row["client_id"] == clinic.client_id and row["consent_recording"] is False
    ledger = conn.execute(
        "select * from public.spend_ledger where interaction_id = %s", (interaction,)
    ).fetchall()
    assert len(ledger) == 1 and ledger[0]["actual_inr"] == Decimal("3.90")
    assert ledger[0]["kind"] == "inbound_call" and ledger[0]["status"] == "settled"
    ended = [e for e in events(conn, clinic, "call_ended") if e["call_id"] == row["id"]]
    assert len(ended) == 1


def test_on_end_hook_records_outcome_once(api, slug, clinic, conn):
    body = {
        "clinic": slug,
        "interaction_id": "20300107/end-1",
        "caller_phone": FAMILY,
        "call_length": 61,
        "outcome": "callback",
        "summary": "Wants a call back.",
    }
    for _ in range(2):
        assert api.post("/api/v1/voice/hooks/on-end", json=body, headers=AUTH).json()["ok"]
    row = conn.execute(
        "select * from public.calls where provider_call_id = '20300107/end-1'"
    ).fetchone()
    assert (row["outcome"], row["agent_minutes"], row["telephony_pulses"]) == ("callback", 2, 3)
    assert len([e for e in events(conn, clinic, "call_ended") if e["call_id"] == row["id"]]) == 1


# ---------------------------------------------------------------------------
# Outbound calls: dry run, opt-in, window, quiet hours, attempts
# ---------------------------------------------------------------------------


@pytest.fixture
def tuesday_appt(conn, clinic):
    staff = Actor(type="staff", channel=Channel.STAFF)
    hold = booking.hold_slot(
        conn,
        clinic.business_id,
        client_id=clinic.client_id,
        starts_at=ist(10, day=MONDAY + timedelta(days=1)),
        scope=booking.Scope(),
        actor=staff,
        now=NOW,
    )
    return booking.book_hold(conn, clinic.business_id, hold["id"], actor=staff, now=NOW)["id"]


def request(conn, vsettings, clinic, appt, at):
    return outbound.request_call(
        conn,
        vsettings,
        business_id=clinic.business_id,
        appointment_id=appt,
        purpose="confirmation_call",
        now=at,
    )


def test_outbound_needs_a_call_opt_in(conn, vsettings, clinic, tuesday_appt):
    r = request(conn, vsettings, clinic, tuesday_appt, ist(10))
    assert (r.status, r.reason) == ("blocked", "no_call_opt_in")


def test_outbound_is_a_dry_run_and_respects_window_and_attempts(
    conn, vsettings, clinic, tuesday_appt
):
    conn.execute("update public.clients set call_opt_in = true where id = %s", (clinic.client_id,))
    vsettings.outbound_calls_enabled = True  # the clinic policy still pins calls off
    assert request(conn, vsettings, clinic, tuesday_appt, ist(8)).reason == "outside_call_window"
    assert request(conn, vsettings, clinic, tuesday_appt, ist(21, 30)).reason in (
        "outside_call_window",
        "quiet_hours",
    )
    first = request(conn, vsettings, clinic, tuesday_appt, ist(10))
    assert first.status == "dry_run" and first.attempt_id.startswith("dry-run-")
    row = conn.execute("select * from public.calls where id = %s", (first.call_id,)).fetchone()
    assert (row["status"], row["dry_run"], row["direction"]) == ("dry_run", True, "outbound")
    assert request(conn, vsettings, clinic, tuesday_appt, ist(10, 30)).reason == "too_soon"
    assert request(conn, vsettings, clinic, tuesday_appt, ist(11, 30)).status == "dry_run"
    assert request(conn, vsettings, clinic, tuesday_appt, ist(12, 30)).status == "dry_run"
    assert request(conn, vsettings, clinic, tuesday_appt, ist(14)).reason == "max_attempts"
    requested = events(conn, clinic, "outbound_call_requested")
    assert len(requested) == 3 and all(e["properties"]["dry_run"] for e in requested)
    spent = conn.execute(
        "select count(*) as n from public.spend_ledger where session_id = %s",
        (vsettings.spend_session_id,),
    ).fetchone()["n"]
    assert spent == 0  # dry runs spend nothing


# ---------------------------------------------------------------------------
# Spend guard
# ---------------------------------------------------------------------------


def test_session_cap_blocks_with_an_event(conn, vsettings, clinic):
    at = datetime(2031, 3, 3, 10, tzinfo=IST)
    for _ in range(2):
        spend.reserve(
            conn,
            vsettings,
            kind=SpendKind.OUTBOUND_CALL,
            estimate=Decimal(39),
            now=at,
            business_id=clinic.business_id,
        )
    with pytest.raises(SpendCapReachedError) as exc:
        spend.reserve(
            conn,
            vsettings,
            kind=SpendKind.OUTBOUND_CALL,
            estimate=Decimal(39),
            now=at,
            business_id=clinic.business_id,
        )
    assert exc.value.scope == "session" and exc.value.used == Decimal(78)
    assert events(conn, clinic, "spend_cap_reached")


def test_released_and_settled_amounts_count_correctly(conn, vsettings):
    at = datetime(2031, 3, 4, 10, tzinfo=IST)
    ledger = spend.reserve(conn, vsettings, kind=SpendKind.TEST_RUN, estimate=Decimal(60), now=at)
    spend.release(conn, ledger)
    s = spend.status(conn, vsettings, at)
    assert s.session_used == 0


def test_daily_cap(conn, db_url):
    at = datetime(2031, 3, 5, 10, tzinfo=IST)
    settings = Settings(
        _env_file=None,
        supabase_db_url=db_url,
        spend_session_id="day-a",
        spend_session_cap_inr=Decimal(1000),
        spend_daily_cap_inr=Decimal(50),
    )
    spend.reserve(conn, settings, kind=SpendKind.TEST_RUN, estimate=Decimal(40), now=at)
    other = settings.model_copy(update={"spend_session_id": "day-b"})
    with pytest.raises(SpendCapReachedError) as exc:
        spend.reserve(conn, other, kind=SpendKind.TEST_RUN, estimate=Decimal(20), now=at)
    assert exc.value.scope == "day"
    # The next IST day starts fresh
    spend.reserve(
        conn, other, kind=SpendKind.TEST_RUN, estimate=Decimal(20), now=at + timedelta(days=1)
    )
