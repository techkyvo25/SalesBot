"""The first-voice-test tooling: tunnel refusals and URLs, the voice request log, the key check
and the fake patient for Sarvam's browser test."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from salesbot_api.demo import seed
from salesbot_api.settings import Settings, get_settings
from salesbot_api.tools import voice_ops, voice_tunnel
from salesbot_api.voice import calllog
from salesbot_api.voice import tools as voice_tools
from salesbot_core.voice import VOICE_TOOLS

REPO = Path(__file__).resolve().parents[3]
TOOL = "tool-secret-for-tests-only-0123456789abcdef"
HOOK = "webhook-token-for-tests-only-0123456789abc"


def ready(**extra) -> Settings:
    values = {
        "voice_tool_secret": TOOL,
        "voice_webhook_secret": HOOK,
        "supabase_db_url": "postgresql://u:p@127.0.0.1:1/x",
        **extra,
    }
    return Settings(_env_file=None, **values)


# -- refusals -------------------------------------------------------------------------------


def test_tunnel_starts_only_for_local_without_demo_mode():
    assert voice_tunnel.refuse_reason(ready(), {}) is None
    assert "DEMO_MODE" in voice_tunnel.refuse_reason(ready(demo_mode=True), {})
    assert "DEMO_MODE" in voice_tunnel.refuse_reason(ready(), {"DEMO_MODE": "true"})
    assert "APP_ENV" in voice_tunnel.refuse_reason(ready(app_env="production"), {})
    assert "APP_ENV" in voice_tunnel.refuse_reason(ready(app_env="staging"), {})


def test_tunnel_needs_the_secrets_and_the_database():
    reason = voice_tunnel.refuse_reason(Settings(_env_file=None), {})
    assert "VOICE_TOOL_SECRET" in reason and "VOICE_WEBHOOK_SECRET" in reason
    assert "SUPABASE_DB_URL" in reason


def test_tunnel_api_is_local_without_access_log():
    """The access log would print the webhook path token; the API must not listen publicly."""
    source = (REPO / "apps/api/src/salesbot_api/tools/voice_tunnel.py").read_text(encoding="utf-8")
    assert '"--host", "127.0.0.1"' in source
    assert '"--no-access-log"' in source
    assert '"DEMO_MODE": "false"' in source


def test_local_folder_is_gitignored():
    assert ".local/" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()


# -- URLs -----------------------------------------------------------------------------------


def test_voice_urls_cover_tools_hooks_and_webhooks():
    urls = voice_tunnel.voice_urls("https://abc-def.trycloudflare.com/", HOOK)
    api = "https://abc-def.trycloudflare.com/api/v1/voice"
    assert urls["on-start hook"] == f"{api}/hooks/on-start"
    assert urls["on-end hook"] == f"{api}/hooks/on-end"
    for spec in VOICE_TOOLS:
        assert urls[f"tool {spec.name}"] == f"{api}/tools/{spec.name}"
    assert urls["webhook inbound"] == f"{api}/webhooks/{HOOK}/inbound"


def test_masked_urls_hide_the_webhook_token():
    url = voice_tunnel.voice_urls("https://x.trycloudflare.com", HOOK)["webhook inbound"]
    masked = voice_tunnel.mask(url, HOOK)
    assert HOOK not in masked and HOOK[:4] in masked


def test_tunnel_address_is_read_from_cloudflared_output():
    log = (
        "2026-10-26T10:00:00Z INF |  Your quick Tunnel has been created! Visit it at:\n"
        "2026-10-26T10:00:00Z INF |  https://quiet-river-abc123.trycloudflare.com  |\n"
    )
    assert voice_tunnel.find_tunnel_url(log) == "https://quiet-river-abc123.trycloudflare.com"
    assert voice_tunnel.find_tunnel_url("starting...") is None


# -- request log ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/api/v1/voice/tools/book", "tools/book"),
        ("/api/v1/voice/hooks/on-start", "hooks/on-start"),
        (f"/api/v1/voice/webhooks/{HOOK}/inbound", "webhooks/inbound"),
        ("/health", None),
        ("/demo/api/chat", None),
    ],
)
def test_endpoint_names_drop_the_webhook_token(path, name):
    assert calllog.endpoint_name(path) == name


def test_request_log_has_name_status_and_duration_only(tmp_path):
    log = tmp_path / "voice-tools.log"
    app = FastAPI()
    app.include_router(voice_tools.router)
    calllog.install(app, str(log))
    settings = ready()
    app.dependency_overrides[get_settings] = lambda: settings
    body = {"clinic": "demo", "interaction_id": "i-1", "caller_phone": "+919812345678"}
    with TestClient(app) as c:
        assert c.post("/api/v1/voice/tools/identify_patient", json=body).status_code == 401
        c.get("/health")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stamp, name, status, duration = lines[0].split(" ")
    assert name == "tools/identify_patient" and status == "401" and duration.endswith("ms")
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    assert "9812345678" not in lines[0] and TOOL not in lines[0]


def test_request_log_is_off_by_default():
    assert Settings(_env_file=None).voice_call_log is None


# -- key check --------------------------------------------------------------------------------


def test_key_check_needs_the_ids_and_makes_no_call(monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("no Sarvam call without the ids")

    monkeypatch.setattr(voice_ops.httpx, "get", no_network)
    assert voice_ops._key_check(Settings(_env_file=None)) == 2
    err = capsys.readouterr().err
    assert "SARVAM_VOICE_API_KEY" in err and "SARVAM_ORG_ID" in err


def test_key_check_prints_status_and_count_only(monkeypatch, capsys):
    secret = "sarvam-key-for-tests-only"

    class Reply:
        status_code = 200

        def json(self):
            return {"total": 0, "items": []}

    def fake_get(url, params, headers, timeout):
        assert url.endswith("/app-authoring/v1/orgs/org-1/workspaces/ws-1/deployments")
        assert headers == {"X-API-Key": secret} and params == {"limit": 1}
        return Reply()

    monkeypatch.setattr(voice_ops.httpx, "get", fake_get)
    s = Settings(
        _env_file=None,
        sarvam_voice_api_key=secret,
        sarvam_org_id="org-1",
        sarvam_workspace_id="ws-1",
    )
    assert voice_ops._key_check(s) == 0
    out = capsys.readouterr().out
    assert "HTTP 200" in out and secret not in out


# -- fake patient for the browser test --------------------------------------------------------


@pytest.mark.db
def test_voice_test_patient_is_idempotent_and_removable(db_url, conn):
    settings = Settings(_env_file=None, demo_mode=True, supabase_db_url=db_url)
    seed.reset(conn, settings)
    seed.seed(conn, settings, datetime.now(UTC))
    try:
        assert seed.add_voice_test_patient(conn, settings, datetime.now(UTC))
        assert not seed.add_voice_test_patient(conn, settings, datetime.now(UTC))
        row = conn.execute(
            "select * from public.clients where id = %s", (seed.VOICE_TEST_PATIENT_ID,)
        ).fetchone()
        assert row["source"] == "test_call" and row["phone"].startswith("+9100000")
        assert row["business_id"] == seed.find_demo(conn)
        assert seed.remove_voice_test_patient(conn, settings)
        assert not seed.remove_voice_test_patient(conn, settings)
    finally:
        seed.reset(conn, settings)
        seed.seed(conn, settings, datetime.now(UTC))


def test_voice_test_patient_id_is_fixed():
    assert uuid.UUID("7e570000-0000-4000-8000-000000000001") == seed.VOICE_TEST_PATIENT_ID


# -- paste text for Sarvam --------------------------------------------------------------------


def test_paste_text_covers_every_tool_and_hook_without_the_secret():
    from salesbot_api.tools.voice_paste import paste_text

    base = "https://quiet-river-abc123.trycloudflare.com"
    text = paste_text(base)
    api = f"{base}/api/v1/voice"
    assert f"{api}/hooks/on-start" in text and f"{api}/hooks/on-end" in text
    for spec in VOICE_TOOLS:
        assert f"TOOL  {spec.name}" in text and f"{api}/tools/{spec.name}" in text
    assert text.count("Bearer token") == len(VOICE_TOOLS) + 2
    assert "webhooks/" not in text  # the webhook token never goes in this file
    assert TOOL not in text and HOOK not in text
