"""Voice endpoints without a database: auth, validation, failure paths, Sarvam request shapes.
Database-backed behaviour (booking, idempotency, concurrency, costs) is in test_voice_db.py."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from salesbot_api.settings import Settings, get_settings
from salesbot_api.voice import store, tools
from salesbot_api.voice.sarvam import (
    DryRunOutboundAdapter,
    OutboundRequest,
    SarvamNotConfiguredError,
    SarvamOutboundAdapter,
)
from salesbot_api.voice.security import mask_phone, normalize_phone
from salesbot_core.voice import COMMON_FIELDS, VOICE_TOOLS

TOOL_SECRET = "tool-secret-for-tests-0123456789abcdef"
HOOK_SECRET = "hook-secret-for-tests-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOOL_SECRET}"}
BODY = {"clinic": "demo", "interaction_id": "20300107/abc", "caller_phone": "+919000000001"}


@pytest.fixture
def voice_settings() -> Settings:
    return Settings(_env_file=None, voice_tool_secret=TOOL_SECRET, voice_webhook_secret=HOOK_SECRET)


@pytest.fixture
def api(client, voice_settings):
    from salesbot_api.main import app

    app.dependency_overrides[get_settings] = lambda: voice_settings
    return client


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOOL_SECRET},
        {"Authorization": f"Bearer {TOOL_SECRET}x"},
    ],
)
def test_tools_reject_bad_auth_with_401(api, headers):
    r = api.post("/api/v1/voice/tools/identify_patient", json=BODY, headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_hooks_need_auth_too(api):
    assert api.post("/api/v1/voice/hooks/on-start", json=BODY).status_code == 401
    assert api.post("/api/v1/voice/hooks/on-end", json=BODY).status_code == 401


def test_tools_are_off_until_a_secret_is_set(client):
    r = client.post("/api/v1/voice/tools/identify_patient", json=BODY, headers=AUTH)
    assert r.status_code == 503


def test_good_auth_without_a_database_is_503(api):
    r = api.post("/api/v1/voice/tools/identify_patient", json=BODY, headers=AUTH)
    assert r.status_code == 503
    assert "database" in r.json()["detail"]


def test_ip_allowlist(api, voice_settings):
    voice_settings.voice_ip_allowlist = "4.213.167.70, 35.154.84.150"
    r = api.post("/api/v1/voice/tools/identify_patient", json=BODY, headers=AUTH)
    assert r.status_code == 401  # the test client is not on the list


def test_webhook_token(api):
    payload = {"app_id": "a", "interaction_id": "20300107/abc", "duration": 10}
    assert api.post("/api/v1/voice/webhooks/wrong/inbound", json=payload).status_code == 401
    r = api.post(f"/api/v1/voice/webhooks/{HOOK_SECRET}/inbound", json=payload)
    assert r.status_code == 503  # authenticated, then no database


def test_validation_errors_name_fields_and_never_echo_values(api):
    from salesbot_api.api_v1 import get_conn
    from salesbot_api.main import app

    app.dependency_overrides[get_conn] = lambda: iter([None])
    body = {**BODY, "patient_id": "not-a-uuid", "hold_id": "+919876543210"}
    r = api.post("/api/v1/voice/tools/book", json=body, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_request" and r.json()["ok"] is False
    assert "body.hold_id" in r.json()["fields"]
    assert "9876543210" not in r.text and "not-a-uuid" not in r.text


# ---------------------------------------------------------------------------
# The request models match the tool specs the agent generator publishes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", VOICE_TOOLS, ids=lambda s: s.name)
def test_request_models_match_the_published_tool_specs(spec):
    model, _ = tools.TOOLS[spec.name]
    published = {f.name for f in (*COMMON_FIELDS, *spec.fields)}
    assert set(model.model_fields) == published
    required = {name for name, f in model.model_fields.items() if f.is_required()}
    assert required <= {f.name for f in (*COMMON_FIELDS, *spec.fields) if f.required}


def test_unset_sarvam_variables_count_as_missing():
    req = tools.AvailabilityIn.model_validate(
        {**BODY, "patient_id": "", "date": "@date", "part_of_day": " "}
    )
    assert req.patient_id is None and req.date is None and req.part_of_day is None


# ---------------------------------------------------------------------------
# Failure paths speak a safe line in the call's language (store replaced by fakes)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_store(monkeypatch):
    business = store.Business(
        id=uuid.uuid4(),
        name="Demo Clinic",
        slug="demo",
        pack_id="clinic",
        timezone="Asia/Kolkata",
        default_language="en",
        allowed_languages=["en", "hi"],
        emergency_numbers=["112", "108"],
        contact_phone="+912000000000",
    )
    calls = {"finished": [], "cleared": [], "events": []}
    monkeypatch.setattr(store, "business_by_slug", lambda conn, slug: business)
    monkeypatch.setattr(store, "ensure_call", lambda *a, **k: (uuid.uuid4(), False))
    monkeypatch.setattr(store, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(store, "begin_tool_call", lambda *a, **k: (1, None))
    monkeypatch.setattr(store, "finish_tool_call", lambda *a, **k: calls["finished"].append(a))
    monkeypatch.setattr(store, "clear_tool_call", lambda *a, **k: calls["cleared"].append(a))
    monkeypatch.setattr(store, "call_event", lambda *a, **k: calls["events"].append(k))
    return calls


NOW = datetime(2030, 1, 7, 2, 30, tzinfo=UTC)


def test_unexpected_error_gives_a_safe_line_and_no_internals(fake_store, caplog):
    def boom(ctx, req):
        raise RuntimeError("relation public.secret_table does not exist +919000000001")

    req = tools.PatientIn.model_validate({**BODY, "language": "Hindi"})
    result = tools.run_tool("identify_patient", req, None, Settings(_env_file=None), NOW, boom)
    assert (result.ok, result.code, result.language) == (False, "internal_error", "hi")
    assert result.next_step == "create_staff_task"
    assert "secret_table" not in result.model_dump_json()
    assert fake_store["cleared"], "a failed call must be cleared so a retry runs again"
    assert "9000000001" not in caplog.text and "secret_table" not in caplog.text


def test_unsupported_language_falls_back_to_the_clinic_default(fake_store):
    req = tools.PatientIn.model_validate({**BODY, "language": "Marathi"})  # clinic: en, hi
    result = tools.run_tool(
        "book", req, None, Settings(_env_file=None), NOW, lambda ctx, r: tools._patient(ctx, None)
    )
    assert (result.ok, result.code, result.language) == (False, "patient_required", "en")
    assert result.spoken_text and result.next_step == "identify_patient"


def test_a_repeated_call_returns_the_stored_result(fake_store, monkeypatch):
    stored = tools.ToolResult(ok=True, code="ok", spoken_text="stored", language="en")
    monkeypatch.setattr(store, "begin_tool_call", lambda *a, **k: (None, stored.model_dump()))

    def must_not_run(ctx, req):
        raise AssertionError("handler ran twice")

    req = tools.PatientIn.model_validate(BODY)
    result = tools.run_tool("book", req, None, Settings(_env_file=None), NOW, must_not_run)
    assert result.spoken_text == "stored"


def test_request_hash_ignores_key_order():
    a = tools.PatientIn.model_validate({**BODY, "patient_id": str(uuid.UUID(int=1))})
    b = tools.PatientIn.model_validate(dict(reversed(list(a.model_dump(mode="json").items()))))
    assert tools.request_hash(a) == tools.request_hash(b)


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "e164"),
    [
        ("+919000000001", "+919000000001"),
        ("919000000001", "+919000000001"),
        ("09000000001", "+919000000001"),
        ("9000000001", "+919000000001"),
        ("+91 90000 00001", "+919000000001"),
        ("12345", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, e164):
    assert normalize_phone(raw) == e164


def test_mask_phone_keeps_only_the_last_four_digits():
    assert mask_phone("+919876543210") == "+91******3210"


# ---------------------------------------------------------------------------
# Sarvam outbound request (nothing is sent)
# ---------------------------------------------------------------------------

REQUEST = OutboundRequest(
    to_number="+919000000001",
    agent_variables={"patient_id": "p"},
    webhook_url="https://api.example/api/v1/voice/webhooks/x/outbound",
    metadata={"purpose": "confirmation_call"},
    initial_language="Hindi",
)


def test_dry_run_adapter_never_touches_the_network(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(httpx, "post", no_network)
    adapter = DryRunOutboundAdapter()
    assert adapter.place(REQUEST).startswith("dry-run-")
    assert not adapter.places_real_calls and adapter.placed == [REQUEST]


def test_sarvam_adapter_refuses_without_settings():
    with pytest.raises(SarvamNotConfiguredError):
        SarvamOutboundAdapter(Settings(_env_file=None, sarvam_voice_api_key="k"))


def test_sarvam_adapter_sends_the_documented_request(monkeypatch):
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"attempt_id": "att-1"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        _env_file=None,
        sarvam_voice_api_key="key",
        sarvam_org_id="org",
        sarvam_workspace_id="ws",
        sarvam_app_id="app",
        sarvam_app_version=3,
        sarvam_connection_id="conn",
        sarvam_agent_phone_number="+918000000000",
    )
    assert SarvamOutboundAdapter(settings).place(REQUEST) == "att-1"
    assert sent["url"] == (
        "https://apps.sarvam.ai/api/outbounds/v1/orgs/org/workspaces/ws/outbounds"
    )
    assert sent["headers"] == {"X-API-Key": "key"}
    assert sent["json"] == {
        "app_config": {
            "app_id": "app",
            "app_version": 3,
            "connection_config": {"connection_id": "conn", "agent_phone_number": "+918000000000"},
            "agent_variables": {"patient_id": "p"},
            "app_overrides": {"initial_language_name": "Hindi"},
        },
        "user_config": {"user_phone_number": "+919000000001"},
        "webhook_config": {
            "url": "https://api.example/api/v1/voice/webhooks/x/outbound",
            "metadata": {"purpose": "confirmation_call"},
        },
    }


def test_spend_cap_defaults(voice_settings):
    assert voice_settings.spend_session_cap_inr == Decimal(100)
    assert voice_settings.spend_daily_cap_inr == Decimal(500)
    assert voice_settings.outbound_calls_enabled is False


def test_today_is_reported_in_india():
    from datetime import UTC, datetime

    today = tools._today(datetime(2026, 10, 26, 13, 50, tzinfo=UTC))
    assert today == {
        "today_weekday": "Monday",
        "today_date": "2026-10-26",
        "time_now": "19:20",
        "timezone": "Asia/Kolkata (IST)",
    }
