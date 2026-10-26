"""Voice operations from the terminal (dev). Uses SUPABASE_DB_URL from .env.

    uv run --package salesbot-api python -m salesbot_api.tools.voice_ops spend
    uv run --package salesbot-api python -m salesbot_api.tools.voice_ops test-runs 5
    uv run --package salesbot-api python -m salesbot_api.tools.voice_ops outbound \\
        --clinic demo --appointment <uuid> --purpose confirmation_call
    uv run --package salesbot-api python -m salesbot_api.tools.voice_ops actuals
    uv run --package salesbot-api python -m salesbot_api.tools.voice_ops key-check

- spend      today's and this session's spend against the caps.
- test-runs  reserve N simulated test runs (Rs 2 each) against the caps BEFORE you start them
             in Sarvam's dashboard. Refuses with a clear error when a cap would be passed.
             It reserves only; it never starts a run.
- outbound   request an outbound call. A dry run unless outbound calls are enabled (they are
             not: docs/DECISIONS.md). Prints why a call is blocked.
- actuals    the "Actuals" table for docs/COSTS.md, from stored calls.
- key-check  read-only check of SARVAM_VOICE_API_KEY, SARVAM_ORG_ID and SARVAM_WORKSPACE_ID:
             lists the workspace's deployments (not billed). Prints the status and a count only.

Never prints secrets or patient details (phone numbers are masked).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import httpx

from salesbot_api import db
from salesbot_api.settings import Settings
from salesbot_api.voice import outbound, sarvam, spend, store
from salesbot_core.costs import SpendCapReachedError, SpendKind, load_rates


def _spend(settings: Settings) -> None:
    now = datetime.now(UTC)
    with db.connect(settings, autocommit=True) as conn:
        s = spend.status(conn, settings, now)
    print(f"session {s.session_id}: Rs {s.session_used} of Rs {s.session_cap}")
    print(f"day {s.day} (IST): Rs {s.day_used} of Rs {s.day_cap}")


def _test_runs(settings: Settings, count: int) -> int:
    estimate = load_rates().voice.simulated_test_run * count
    with db.connect(settings, autocommit=True) as conn:
        try:
            spend.reserve(
                conn,
                settings,
                kind=SpendKind.TEST_RUN,
                estimate=estimate,
                now=datetime.now(UTC),
                note=f"{count} simulated test runs",
            )
        except SpendCapReachedError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 2
    print(f"Reserved Rs {estimate} for {count} simulated test runs. You may start them now.")
    return 0


def _outbound(settings: Settings, clinic: str, appointment: str, purpose: str) -> int:
    with db.connect(settings, autocommit=True) as conn:
        business = store.business_by_slug(conn, clinic)
        if business is None:
            print("unknown clinic", file=sys.stderr)
            return 1
        try:
            result = outbound.request_call(
                conn,
                settings,
                business_id=business.id,
                appointment_id=appointment,  # type: ignore[arg-type]
                purpose=purpose,
                now=datetime.now(UTC),  # type: ignore[arg-type]
            )
        except SpendCapReachedError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 2
    print(f"{result.status}" + (f": {result.reason}" if result.reason else ""))
    return 0 if result.status != "blocked" else 1


def _actuals(settings: Settings) -> None:
    with db.connect(settings, autocommit=True) as conn:
        rows = conn.execute(
            """
            select to_char(date_trunc('month', started_at at time zone 'Asia/Kolkata'),
                           'YYYY-MM') as month,
                   count(*) filter (where status = 'completed') as calls,
                   coalesce(sum(agent_minutes), 0) as minutes,
                   coalesce(sum(telephony_pulses), 0) as pulses,
                   coalesce(sum(cost_inr_estimate), 0) as cost
            from public.calls where telephony_provider = 'sarvam' and not dry_run
            group by 1 order by 1
            """
        ).fetchall()
        confirmed = {
            r["month"]: r["n"]
            for r in conn.execute(
                """
                select to_char(date_trunc('month', occurred_at at time zone 'Asia/Kolkata'),
                               'YYYY-MM') as month, count(*) as n
                from public.events where type = 'appointment_confirmed' group by 1
                """
            ).fetchall()
        }
    print(
        "| Month | Connected calls | Agent minutes | Telephony pulses | Voice cost (Rs) "
        "| Rs per call | Confirmed appointments | Rs per confirmed appointment |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        cost, calls, conf = Decimal(r["cost"]), r["calls"], confirmed.get(r["month"], 0)
        per_call = f"{cost / calls:.2f}" if calls else "-"
        per_conf = f"{cost / conf:.2f}" if conf else "-"
        print(
            f"| {r['month']} | {calls} | {r['minutes']} | {r['pulses']} | {cost:.2f} "
            f"| {per_call} | {conf} | {per_conf} |"
        )
    if not rows:
        print("| (no calls yet) | | | | | | | |")


def _key_check(settings: Settings) -> int:
    """GET .../deployments?limit=1 with the Voice Agents key. Read-only, never prints the key."""
    missing = [
        name
        for name, value in (
            ("SARVAM_VOICE_API_KEY", settings.sarvam_voice_api_key),
            ("SARVAM_ORG_ID", settings.sarvam_org_id),
            ("SARVAM_WORKSPACE_ID", settings.sarvam_workspace_id),
        )
        if not value
    ]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    key = settings.sarvam_voice_api_key
    if key is None:
        return 2
    url = (
        f"{sarvam.BASE}/app-authoring/v1/orgs/{quote(settings.sarvam_org_id or '')}"
        f"/workspaces/{quote(settings.sarvam_workspace_id or '')}/deployments"
    )
    try:
        r = httpx.get(
            url,
            params={"limit": 1},
            headers={"X-API-Key": key.get_secret_value()},
            timeout=sarvam.TIMEOUT,
        )
    except httpx.HTTPError as exc:
        print(f"key check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"key check: HTTP {r.status_code} (401/403 = wrong key, 404/422 = wrong ids)")
        return 1
    print(f"key check: HTTP 200, deployments in the workspace: {r.json().get('total', '?')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("spend")
    runs = sub.add_parser("test-runs")
    runs.add_argument("count", type=int)
    call = sub.add_parser("outbound")
    call.add_argument("--clinic", required=True)
    call.add_argument("--appointment", required=True)
    call.add_argument(
        "--purpose", choices=["confirmation_call", "reminder_call"], default="confirmation_call"
    )
    sub.add_parser("actuals")
    sub.add_parser("key-check")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "spend":
        _spend(settings)
    elif args.command == "test-runs":
        return _test_runs(settings, args.count)
    elif args.command == "outbound":
        return _outbound(settings, args.clinic, args.appointment, args.purpose)
    elif args.command == "key-check":
        return _key_check(settings)
    else:
        _actuals(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
