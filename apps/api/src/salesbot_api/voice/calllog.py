"""One line per request to the voice endpoints, for the first voice tests (`make voice-tools-log`).

Each line: UTC time, endpoint name, HTTP status, duration. Never the request or response body,
never a phone number, never the webhook path token. On only when VOICE_CALL_LOG names a file
(`make voice-tunnel` sets it to .local/voice-tools.log).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response

PREFIX = "/api/v1/voice/"


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


def line(name: str, status: int, duration_ms: int, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} {name} {status} {duration_ms}ms\n"


def _append(target: Path, text: str) -> None:
    with target.open("a", encoding="utf-8") as f:
        f.write(text)


def install(app: FastAPI, log_file: str) -> None:
    target = Path(log_file)
    target.parent.mkdir(parents=True, exist_ok=True)

    @app.middleware("http")
    async def voice_call_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        name = endpoint_name(request.url.path)
        if name is None:
            return await call_next(request)
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            _append(target, line(name, status, int((time.monotonic() - started) * 1000)))
