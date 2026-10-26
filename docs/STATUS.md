# Status

Where Anvia Phase 1 stands, stage by stage, with evidence. Updated 2026-10-26 on
`demo/smallest-demo` (stacked on `feature/anvia-sarvam`; PRs #2 to #4 closed unmerged
on 2026-10-26, see docs/HANDOVER.md). The stage plan was paused on
2026-10-25 for the smallest credible demo (docs/DEMO.md).

| Stage | Scope | Status | Evidence |
|---|---|---|---|
| A | Scheduling engine, doctors' hours, holds and bookings in Postgres; client intake (signed webhook) | **Done** | PR #2; `salesbot_api/booking.py`, `intake.py`; `apps/api/tests/test_booking.py`, `test_intake.py`; `supabase/migrations/20261021*`; docs/INTAKE_CONTRACT.md |
| B | Channel-agnostic flow engine (en/hi/mr), text console, wording policy | **Done**, fixes added for the demo | PR #3; `salesbot_core/flow/engine.py`; `packages/core/tests/test_flow.py`, `test_flow_fixes.py`, `test_wording.py` |
| C | WhatsApp channel, flow engine on the database, conversation storage | **Partly done**: engine on the database (`DbScheduling`, `Conversation`) and opt-out persistence done; conversation state is in memory (demo only); WhatsApp **parked** (Meta account blocked; provider is an open decision) | `salesbot_api/conversation.py`; `apps/api/tests/test_demo.py` (booking, opt-out, slot taken, funnel against the database) |
| D | Jobs worker: welcome, confirmation requests, reminders | **Not started**: jobs are queued in `jobs` (welcome on registration), nothing runs them | `intake.py` enqueues `welcome`; no worker |
| E | Voice on Sarvam Voice Agents: tools, hooks, webhooks, spend guard, outbound (dry run) | **Built, not live**: no agent created, no number, outbound off | PR #4; `salesbot_api/voice/`; `apps/api/tests/test_voice_api.py`, `test_voice_db.py`; docs/SARVAM_AGENT.md, docs/COSTS.md |
| F | Google Calendar push; doctor's day view | **Partly**: read-only doctors' day view on the demo page; Google Calendar not started (mock adapter only) | `salesbot_api/demo/routes.py` (`/demo/api/day`); `adapters` mock calendar |
| G | Telephony and outbound calls (number rental, KYC, TRAI/DLT) | **Not started** (owner decisions: number rental, KYC, outbound compliance) | docs/DECISIONS.md (Telephony, Telecom rules) |

## Smallest demo (this branch)

| Item | Status | Evidence |
|---|---|---|
| Engine on the database, seed, console `--db`, `DEMO_MODE` flag; scripted console demo removed | Done | commits on `demo/smallest-demo`; `salesbot_api/demo/seed.py` |
| Fixes: opt-out saved first, confirm intent, FAQ step, time beats "no", acknowledgements, second appointment | Done | `packages/core/tests/test_flow_fixes.py`, `apps/api/tests/test_demo.py` |
| Demo server (`/demo/api/*`), only with `DEMO_MODE=true` + `APP_ENV=local`, localhost only | Done | `salesbot_api/demo/routes.py`; tests: not mounted by default, 403 for other hosts |
| Demo page `/demo` (one HTML + JS, no build) | Done | `salesbot_api/demo/static/`; wording scan over the page text |
| `make demo`, `make demo-seed`, `make demo-reset` (demo clinic only, refuse production-like DBs) | Done | Makefile; `test_reset_touches_only_the_demo_clinic` |
| Not parsed: "after 6", "before 10", "later in the week" | Known limitation | docs/DEMO.md |

## Not in the demo (on purpose)

Sarvam calls, number rental and KYC, WhatsApp, Google Calendar, outbound calls, the admin login,
voice Tier 2, a tunnel. See docs/DECISIONS.md for the open decisions.
