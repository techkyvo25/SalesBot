"""One line per request to the voice endpoints, for the first voice tests (`make voice-tools-log`).

Each line: UTC time, endpoint name, HTTP status, duration. Never the request or response body,
never a phone number, never the webhook path token. On only when VOICE_CALL_LOG names a file
(`make voice-tunnel` sets it to .local/voice-tools.log).

With VOICE_CALL_LOG_FIELDS=true (temporary, for debugging a test call) a line also carries the
request fields in FIELDS: dates, times, flags and ids, nothing personal. A value that is not a
short token (letters, digits, `:+._-`) is written as `<other>`, so free text never gets in.
Hook calls show up as `hooks/on-start` and `hooks/on-end` lines.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response

PREFIX = "/api/v1/voice/"
# Non-personal request fields. Never names, phone numbers, transcripts or summaries.
FIELDS = (
    "date", "part_of_day", "next_available", "for_reschedule", "starts_at", "practitioner_id",
    "reason", "urgency",
)  # fmt: skip
_SAFE = re.compile(r"^[A-Za-z0-9:+._-]{1,40}$")


def field_part(body: bytes) -> str:
    """` date=2026-10-27 part_of_day=morning` for the FIELDS present in a JSON body."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return " body=<not json>"
    if not isinstance(data, dict):
        return ""
    parts = []
    for key in FIELDS:
        if key not in data or data[key] in (None, ""):
            continue
        value = data[key]
        text = str(value).lower() if isinstance(value, bool) else str(value)
        parts.append(f"{key}={text if _SAFE.match(text) else '<other>'}")
    return "".join(f" {p}" for p in parts)


def endpoint_name(path: str) -> str | None:
    """`tools/book`, `hooks/on-start`, `webhooks/inbound` (token dropped); None if not voice."""
    if not path.startswith(PREFIX):
        return None
    parts = [p for p in path[len(PREFIX) :].split("/") if p]
    if not parts:
        return "voice"
    if parts[0] == "webhooks":
        # /webhooks/{token}/{kind}: the token is a secret.
        return "webhooks/" + (parts[2] if len(parts) > 2 else "?")
    return "/".join(parts[:2])


def line(
    name: str, status: int, duration_ms: int, now: datetime | None = None, extra: str = ""
) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} {name} {status} {duration_ms}ms{extra}\n"


def _append(target: Path, text: str) -> None:
    with target.open("a", encoding="utf-8") as f:
        f.write(text)


def install(app: FastAPI, log_file: str, *, with_fields: bool = False) -> None:
    target = Path(log_file)
    target.parent.mkdir(parents=True, exist_ok=True)

    @app.middleware("http")
    async def voice_call_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        name = endpoint_name(request.url.path)
        if name is None:
            return await call_next(request)
        extra = field_part(await request.body()) if with_fields and request.method == "POST" else ""
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            _append(target, line(name, status, elapsed, extra=extra))
