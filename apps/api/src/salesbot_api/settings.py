"""Server settings, read from env / the repo-root .env. Empty values count as unset."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: Literal["local", "staging", "production"] = "local"
    secret_key: SecretStr | None = None
    log_level: str = "INFO"
    # Local demo only (docs/DEMO.md): mounts the unauthenticated /demo routes and allows
    # `make demo-reset`. Never on with a tunnel or on a shared host.
    demo_mode: bool = False

    # Supabase. The service role key bypasses RLS: server-side only.
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: SecretStr | None = None
    supabase_db_url: SecretStr | None = None
    supabase_jwt_secret: SecretStr | None = None

    # Client intake (docs/INTAKE_CONTRACT.md). Shared with the owner of the registration form.
    intake_adapter: Literal["hmac_webhook"] = "hmac_webhook"
    intake_hmac_secret: SecretStr | None = None

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_oauth_redirect_uri: str | None = None

    whatsapp_provider: str = "mock"
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None

    hubspot_enabled: bool = False
    hubspot_access_token: SecretStr | None = None

    # Sarvam Voice Agents (docs/SARVAM_AGENT.md). The agent calls our API tools with
    # VOICE_TOOL_SECRET as a bearer token (stored in Sarvam's Secrets). Sarvam's call-completion
    # webhooks cannot send headers, so their URL carries VOICE_WEBHOOK_SECRET instead.
    voice_tool_secret: SecretStr | None = None
    voice_webhook_secret: SecretStr | None = None
    # Optional comma-separated source IPs allowed to call the voice endpoints. Empty = any.
    voice_ip_allowlist: str | None = None
    sarvam_voice_api_key: SecretStr | None = None
    sarvam_org_id: str | None = None
    sarvam_workspace_id: str | None = None
    sarvam_app_id: str | None = None
    sarvam_app_version: int | None = None
    sarvam_connection_id: str | None = None
    sarvam_agent_phone_number: str | None = None
    # Real outbound calls stay off until the owner says so. Off = dry run: log and record what
    # would be called, never dial.
    outbound_calls_enabled: bool = False
    # Public HTTPS base of this API, for the webhook URL sent with outbound calls.
    public_base_url: str | None = None

    # Spend guard for the shared Sarvam credit wallet (docs/COSTS.md). Outbound calls and test
    # runs we start are blocked once a cap would be passed. A session is one API process
    # unless SPEND_SESSION_ID names it.
    spend_session_cap_inr: Decimal = Decimal("100")
    spend_daily_cap_inr: Decimal = Decimal("500")
    spend_session_id: str | None = None

    call_max_seconds: int = 600
    default_business_usage_cap_minutes: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
