"""A temporary public address for the first Sarvam voice test (docs/SARVAM_AGENT.md).

    make voice-tunnel          # API on 127.0.0.1 (DEMO_MODE=false) + Cloudflare quick tunnel
    make voice-tunnel-check    # /health 200, tool without token 401, tool with token 200
    make voice-tunnel-stop     # stop both
    make voice-tools-log       # last 20 voice requests (endpoint, status, duration)

Refuses to start with DEMO_MODE=true (the demo routes have no login) or when APP_ENV is not
local. The full URLs, one of which carries VOICE_WEBHOOK_SECRET, go to .local/voice-urls.txt
(gitignored); the terminal shows them masked. Secrets are never printed. Uvicorn's access log
is off, because it would print the webhook path token.

A quick tunnel gets a new address on every start: paste the new URLs into Sarvam each time.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path

from salesbot_api.demo.seed import demo_clinic
from salesbot_api.settings import Settings
from salesbot_core.voice import VOICE_TOOLS

LOCAL = Path(".local")
URLS = LOCAL / "voice-urls.txt"
PIDS = LOCAL / "voice-tunnel.pids"
API_LOG = LOCAL / "api.log"
TUNNEL_LOG = LOCAL / "cloudflared.log"
CALL_LOG = LOCAL / "voice-tools.log"
PORT = int(os.environ.get("VOICE_TUNNEL_PORT", "8010"))
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
INSTALL_HINT = "winget install --id Cloudflare.cloudflared (then open a new terminal)"
TRUE = {"1", "true", "yes", "on"}


# -- pure helpers (tested) ---------------------------------------------------------------


def refuse_reason(settings: Settings, env: Mapping[str, str]) -> str | None:
    """Why the tunnel must not start, or None."""
    if settings.demo_mode or env.get("DEMO_MODE", "").strip().lower() in TRUE:
        return "DEMO_MODE is true: the demo routes have no login and must never be public"
    if settings.app_env != "local":
        return f"APP_ENV is {settings.app_env}: the tunnel is for local tests only"
    missing = [
        name
        for name, value in (
            ("VOICE_TOOL_SECRET", settings.voice_tool_secret),
            ("VOICE_WEBHOOK_SECRET", settings.voice_webhook_secret),
            ("SUPABASE_DB_URL", settings.supabase_db_url),
        )
        if value is None
    ]
    if missing:
        return f"missing in .env: {', '.join(missing)}"
    return None


def voice_urls(base: str, webhook_token: str) -> dict[str, str]:
    """Every URL Sarvam needs, by label."""
    api = f"{base.rstrip('/')}/api/v1/voice"
    urls = {
        "tool base": f"{api}/tools",
        "on-start hook": f"{api}/hooks/on-start",
        "on-end hook": f"{api}/hooks/on-end",
    }
    urls |= {f"tool {t.name}": f"{api}/tools/{t.name}" for t in VOICE_TOOLS}
    urls["webhook inbound"] = f"{api}/webhooks/{webhook_token}/inbound"
    urls["webhook outbound"] = f"{api}/webhooks/{webhook_token}/outbound"
    return urls


def mask(url: str, secret: str) -> str:
    return url.replace(secret, secret[:4] + "…(secret)") if secret else url


def find_tunnel_url(text: str) -> str | None:
    m = TUNNEL_URL.search(text)
    return m.group(0) if m else None


# -- processes ----------------------------------------------------------------------------


def _spawn(cmd: list[str], log: Path, env: dict[str, str]) -> int:
    out = log.open("ab")
    kwargs: dict = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, env=env, **kwargs).pid  # noqa: S603


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 15)


def _get(url: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> int:
    req = urllib.request.Request(  # noqa: S310 (https or loopback only)
        url,
        data=body,
        headers={"User-Agent": "salesbot-voice-check", **(headers or {})},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError):
        return 0


# -- commands -----------------------------------------------------------------------------


def start() -> int:
    settings = Settings()
    reason = refuse_reason(settings, os.environ)
    if reason:
        print(f"refused: {reason}", file=sys.stderr)
        return 2
    if PIDS.exists() and any(_alive(p) for p in json.loads(PIDS.read_text()).values()):
        print("already running: `make voice-tunnel-stop` first", file=sys.stderr)
        return 2
    cloudflared = shutil.which("cloudflared")
    if cloudflared is None:
        print(f"cloudflared not found. Install: {INSTALL_HINT}", file=sys.stderr)
        return 2
    LOCAL.mkdir(exist_ok=True)
    env = {**os.environ, "DEMO_MODE": "false", "VOICE_CALL_LOG": str(CALL_LOG.resolve())}
    api = _spawn(
        [sys.executable, "-m", "uvicorn", "salesbot_api.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--no-access-log"],
        API_LOG,
        env,
    )  # fmt: skip
    pids = {"api": api}
    PIDS.write_text(json.dumps(pids))
    for _ in range(40):
        if _get(f"http://127.0.0.1:{PORT}/health") == 200:
            break
        time.sleep(0.5)
    else:
        print(f"the API did not start: see {API_LOG}", file=sys.stderr)
        stop(quiet=True)
        return 1
    TUNNEL_LOG.write_bytes(b"")
    pids["tunnel"] = _spawn(
        [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
        TUNNEL_LOG,
        env,
    )
    PIDS.write_text(json.dumps(pids))
    base = None
    for _ in range(60):
        base = find_tunnel_url(TUNNEL_LOG.read_text(encoding="utf-8", errors="replace"))
        if base:
            break
        time.sleep(1)
    if base is None:
        print(f"no tunnel address after 60 s: see {TUNNEL_LOG}", file=sys.stderr)
        stop(quiet=True)
        return 1
    assert settings.voice_webhook_secret is not None  # noqa: S101 (checked in refuse_reason)
    token = settings.voice_webhook_secret.get_secret_value()
    urls = voice_urls(base, token)
    URLS.write_text(
        "# Voice test URLs. PRIVATE: the webhook URLs carry VOICE_WEBHOOK_SECRET.\n"
        "# New address on every `make voice-tunnel`: paste again into Sarvam.\n"
        + "".join(f"{label}: {url}\n" for label, url in urls.items()),
        encoding="utf-8",
    )
    print(f"API on 127.0.0.1:{PORT} (DEMO_MODE=false), tunnel up.")
    print(f"Full URLs: {URLS.resolve()}")
    for label in ("tool base", "on-start hook", "on-end hook", "webhook inbound"):
        print(f"  {label}: {mask(urls[label], token)}")
    print(
        "REMINDER: the address changes on every restart. After each `make voice-tunnel`, paste "
        "the new URLs into every Sarvam tool and hook."
    )
    print("Next: make voice-tunnel-check")
    return 0


def stop(quiet: bool = False) -> int:
    if not PIDS.exists():
        if not quiet:
            print("not running")
        return 0
    for pid in json.loads(PIDS.read_text()).values():
        _kill(int(pid))
    PIDS.unlink()
    URLS.unlink(missing_ok=True)  # stale once the tunnel is gone
    if not quiet:
        print("stopped the tunnel and the API")
    return 0


def check() -> int:
    """Status codes only. The tool call identifies nobody (no caller number) and is tagged
    tunnel-check-... in the demo clinic's call log."""
    if not URLS.exists():
        print("not running: `make voice-tunnel` first", file=sys.stderr)
        return 2
    settings = Settings()
    if settings.voice_tool_secret is None:
        print("missing in .env: VOICE_TOOL_SECRET", file=sys.stderr)
        return 2
    text = URLS.read_text(encoding="utf-8").splitlines()
    lines = dict(line.split(": ", 1) for line in text if ": " in line)
    tool = lines["tool identify_patient"].strip()
    base = tool.split("/api/v1/voice/")[0]
    body = json.dumps(
        {"clinic": demo_clinic().slug, "interaction_id": f"tunnel-check-{uuid.uuid4().hex[:12]}"}
    ).encode()
    json_header = {"Content-Type": "application/json"}
    bearer = {
        **json_header,
        "Authorization": f"Bearer {settings.voice_tool_secret.get_secret_value()}",
    }
    results = [
        ("GET /health", _get(f"{base}/health"), 200),
        ("tool without token", _get(tool, json_header, body), 401),
        ("tool with token", _get(tool, bearer, body), 200),
    ]
    for name, got, want in results:
        print(f"{name}: {got} ({'ok' if got == want else f'expected {want}'})")
    return 0 if all(got == want for _, got, want in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quick tunnel for the first voice test")
    parser.add_argument("command", choices=["start", "stop", "check"])
    args = parser.parse_args(argv)
    return {"start": start, "stop": stop, "check": check}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
