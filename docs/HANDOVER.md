# Handover

## 1. What this is

Anvia is an appointment receptionist for small clinics (English and Hindi; Marathi switched
off for now). It books, moves, confirms and cancels appointments, answers simple
questions and creates tasks for the clinic team. Calls will run on Sarvam Voice Agents; all
business rules live in our API and Postgres. Background: [README](../README.md),
[ARCHITECTURE](ARCHITECTURE.md).

## 2. Where the code is

Three branches. Each one contains everything in the one above it:

| Branch | Commit | What it holds |
|---|---|---|
| `main` | `a9292aa` | Project scaffold only (PR #1) |
| `feature/anvia-phase1` | `79ec231` | The 7-stage build so far: scheduling, booking engine, intake webhook (stage A), conversation engine and languages (B), voice backend for Sarvam, cost tracking, spend guard (E) |
| `demo/smallest-demo` | latest commit (this file is on it) | All of the above plus the engine on the database, fixes and the demo page |

**Start new work from `demo/smallest-demo`.** It contains everything. Stage work that should not
carry the demo can go on `feature/anvia-phase1`. PRs #2, #3 and #4 were closed without merging
on 26 Oct 2026 (the owner's decision): work continues on branches, and nothing goes to `main`
without agreement. The old branches `feature/anvia-stage-b`, `feature/anvia-sarvam` and
`chore/scaffold` were deleted the same day; their commits are all in the branches above. CI runs
on every push to `feature/**`, `demo/**` and `chore/**`.

## 3. Done

- Booking engine in Postgres: doctors' hours, holds, no double booking (database constraint),
  reschedule and cancel rules, audit log, funnel events, row-level security per clinic.
- Signed intake webhook for new patients ([INTAKE_CONTRACT](INTAKE_CONTRACT.md)).
- Conversation flow in English and Hindi, running on the database: book, move, confirm,
  cancel, FAQs, emergencies, opt-out, "talk to the clinic".
- Voice backend: tool endpoints, start/end hooks, webhooks. **Tested, but never run live.**
- Cost tracking per call and a spend guard ([COSTS](COSTS.md)).
- Demo page at `/demo` on a fake clinic ([DEMO](DEMO.md)).
- Tests (unit and database) and CI with a local Supabase.

Stage-by-stage evidence: [STATUS](STATUS.md). Decisions and open questions:
[DECISIONS](DECISIONS.md).

## 4. Simulated or not working

- Registration form: simulated (the demo signs a fake request; no real form is connected).
- WhatsApp: not built (Meta account blocked; provider undecided). Messages are only shown.
- Phone calls and the voice agent: no agent created in Sarvam, no number, never called.
- Reminders and confirmation requests: jobs are queued, but no worker sends them.
- Google Calendar: mock only.
- Doctor dashboard behind a login: not built (the demo's day view has no login, localhost only).
- Staff alerts: tasks are saved, nobody is notified.
- Marathi language detection: known gaps; Marathi is off for the demo clinic.
- "after 6", "before 10", "later in the week": not understood ([DEMO](DEMO.md#known-limitations)).
- Hindi, Marathi and the emergency wording are DRAFT (not reviewed).

## 5. Next steps

Credit costs are from [COSTS](COSTS.md) (1 credit = ₹1; balance about 1,00,100 on 24 Oct 2026).

| Step | What is needed to finish it | Internal access | External access or decision | Owner | Blocked by |
|---|---|---|---|---|---|
| WhatsApp live test | Unlock the Meta account; real number, display name, templates; build the adapter and webhook | repo, `.env` (`WHATSAPP_*`) | Meta Business account unlocked | Monica (Meta), developer (code) | Meta account lock; WhatsApp provider decision |
| Sarvam setup | `SARVAM_VOICE_API_KEY` (Voice Agents dashboard → Settings → API Key), `SARVAM_ORG_ID` and `SARVAM_WORKSPACE_ID` (Settings → Organisation / Workspace → General) into `.env`. Costs nothing | `.env` | Sarvam account | Monica | Nothing |
| Recording retention | Set a period in Settings → Workspace → Data retention (until then Sarvam keeps recordings forever) | none | Sarvam workspace **Owner** only; the period is a DPDP decision | Monica | Retention decision |
| Create the agent | Follow [SARVAM_AGENT](SARVAM_AGENT.md) §2–8; put `VOICE_TOOL_SECRET` into Sarvam Secrets; write `SARVAM_APP_ID` and `SARVAM_APP_VERSION` into `.env`. Creating is free; each simulated test run costs ₹2 (the 10 test cases: ₹20, reserve with `make voice-ops ARGS='test-runs 10'`) | `.env`, repo | Sarvam workspace member | developer (with Monica's go to spend) | Sarvam setup; retention set |
| First voice test call | Public HTTPS URL for the API (`PUBLIC_BASE_URL`: tunnel for the test, later the container host); webhook URL uses `VOICE_WEBHOOK_SECRET`. About ₹3.50 per minute (₹3.90 with Sarvam telephony); team phones only | `.env`, laptop | Approval to run a tunnel | developer, Monica approves | Agent created; tunnel approval; consent wording (team phones until agreed) |
| Number KYC and rental | KYC in Deploy → Phone Numbers (Individual or Corporate + email; documents and duration UNKNOWN); ₹59 per number per 30 days, auto-renews from the wallet, released with no grace period if credits run short. Then `SARVAM_CONNECTION_ID`, `SARVAM_AGENT_PHONE_NUMBER` | `.env` | KYC as account holder | Monica | Owner's decision to rent |
| Outbound call compliance | TRAI/DLT registration, number declaration, DND rules ([DECISIONS](DECISIONS.md#telecom-rules-for-outbound-calls-trai--dlt)); `OUTBOUND_CALLS_ENABLED` stays false until then | none | Sarvam, legal adviser | Monica + legal adviser (UNKNOWN who) | Legal answer |
| Reminders worker | Process that runs queued jobs (welcome, confirmation, reminder) | repo, Supabase | Needs a sending channel | developer | WhatsApp or voice live |
| Doctor dashboard with login | Day view in `apps/admin` behind Supabase Auth and row-level security | repo, Supabase dashboard | Vercel project link (needs approval) | developer | Vercel linking approval |
| Google Calendar | OAuth app, `GOOGLE_OAUTH_*`, adapter | repo | Google Cloud project | developer, Monica (Google account) | Owner's go |
| Consent wording on the form | Text covering messages and call recording in en/hi/mr ([INTAKE_CONTRACT](INTAKE_CONTRACT.md)) | none | Legal sign-off; form owner | Monica + legal adviser | Legal review |
| Emergency wording review | Clinical sign-off of the emergency line in every language | none | A doctor | Monica (arranges), clinic doctor | UNKNOWN reviewer |
| Hindi and Marathi review | Native review of all DRAFT text; list with `make console ARGS='--list-drafts'` | repo | Native speakers | Monica (arranges) | UNKNOWN reviewers |
| Container host | Always-on HTTPS host near Mumbai for the API image | repo | Hosting account and cost | Monica decides | Decision |
| WhatsApp provider | Cloud API directly or a BSP | none | Meta / BSP | Monica decides | Meta account lock |
| Staff alert channel | How emergencies and "talk to the clinic" reach a person | repo | Decision (WhatsApp, SMS, call transfer) | Monica decides, developer builds | Decision; required before any pilot |
| Supabase Pro | Upgrade before any pilot (no pausing, backups) | Supabase dashboard | Paid plan | Monica | Pilot date |

## 6. Run it

```bash
make setup        # dependencies and git hooks
make test         # all tests + admin build (database tests skip without a database)
make lint
make demo-reset   # fresh fake demo clinic (only it)
make demo         # http://127.0.0.1:8000/demo
```

How to present the demo: [DEMO](DEMO.md). On Windows run `make` from Git Bash.

## 7. Environment

`.env.example` in the repo root (and `apps/admin/.env.example`) lists every variable name,
empty. Copy it to `.env` and fill it in. Where the values come from (never commit them):

- `SUPABASE_*`: Supabase dashboard of the dev project (Project Settings → API / Database).
- `INTAKE_HMAC_SECRET`, `VOICE_TOOL_SECRET`, `VOICE_WEBHOOK_SECRET`: random strings you
  generate (32+ characters); the intake one is shared with the registration form's owner.
- `SARVAM_*`: Sarvam dashboard ([SARVAM_AGENT](SARVAM_AGENT.md) §1).
- `WHATSAPP_*`: Meta Business; `GOOGLE_OAUTH_*`: Google Cloud; `HUBSPOT_*`: HubSpot private app.

The demo needs only `SUPABASE_DB_URL` and `INTAKE_HMAC_SECRET` (the Register button signs its
request); `make demo` turns `DEMO_MODE` on by itself.

## 8. House rules

- Secrets only in `.env` and the password manager. Never in code, commits, logs or chat.
- No real patient data anywhere in dev, tests, fixtures or screenshots.
- Demo mode stays on localhost: no tunnel, no shared host.
- Never merge to `main` without agreement. Work on a branch; ask before pushing.
- Sarvam credits, number rental, KYC and outbound calls need the owner's go
  ([CLAUDE.md](../CLAUDE.md), "Do not decide without asking").
