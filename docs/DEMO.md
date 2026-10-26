# Local demo

A browser page that shows the appointment flow end to end on a fake clinic: a patient
registers, chats (English or Hindi), books, confirms, reschedules, cancels, hits an emergency
and opts out, while the doctors' day, the funnel and the staff tasks update beside the chat.

It runs on your laptop only: `DEMO_MODE=true`, bound to `127.0.0.1`, against the hosted dev
Supabase project's **demo clinic** (fake data). No Sarvam, no WhatsApp, no calls, no tunnel.

## Real vs simulated

| Part | In the demo |
|---|---|
| Conversation engine (`salesbot_core.flow`) | **Real**: the same engine WhatsApp and voice will use |
| Bookings, holds, cancels, reschedules, confirmations | **Real**: `salesbot_api.booking` on Postgres (exclusion constraint, events, audit) |
| Funnel numbers | **Real**: counted from the append-only `events` table |
| Staff tasks | **Real** rows in `staff_tasks`. **No alert reaches a person yet** (alert channel is an open decision) |
| Opt-out | **Real**: saved (and a `consent_declined` event written) before the reply says so |
| Opening hours answer | **Real**: from the doctors' hours in the database |
| Registration | **Simulated form**: a signed request built by the page's server, parsed by the real intake adapter, stored with `source = simulation` |
| WhatsApp messages | **Simulated**: shown on the page with "WhatsApp would send this (simulated)"; nothing is sent |
| Phone calls | **Simulated / not part of the demo**: no Sarvam call is made |
| Address, parking, fee, insurance answers | **DEMO text** from the clinic pack (fictional; only used when `DEMO_MODE` is on) |
| Clinic, doctors, patients | **Fake**: Demo Clinic, Dr. Priya Kulkarni (demo), Dr. Arjun Rao (demo); patients Asha, Ravi, Meera with `+9100000000xx` numbers |
| Hindi text | **DRAFT** (not reviewed by a native speaker); every Hindi reply is tagged DRAFT |

## Run it

From Git Bash in the repo root:

```bash
make demo-reset   # delete and re-seed the demo clinic (only it)
make demo         # seeds if needed, starts the API on 127.0.0.1:8000, prints the URL
```

Open **http://127.0.0.1:8000/demo**. Stop with Ctrl+C.

- `make demo-seed` seeds without deleting (idempotent).
- All three targets set `DEMO_MODE=true` for their own process only; `.env` keeps it off.
- They refuse to run unless `APP_ENV` is `local` (or empty) and the database looks like a dev
  database: any patient outside the demo clinic that was not simulated stops them.
- `.env` needs `SUPABASE_DB_URL` and `INTAKE_HMAC_SECRET` (the Register button signs its
  request with it).

## Page controls

- **Register patient (simulated)**: registers Asha through the real intake code; shows the
  welcome message. A second press says she is already registered (no second welcome).
- **Chat box**: type as the patient. Tap an option button to choose it, or type its number.
- **Chat as**: switch to Ravi (Hindi, confirmed appointment tomorrow) or Meera (English, next
  free time booked). Each patient's conversation is kept separately.
- **Another patient takes this slot**: books the first time currently offered in the chat, as
  other demo patients, through the real booking code (once per doctor free at that time, so the
  time is really gone). Then pick that time in the chat to see it refused.
- **Reset demo**: deletes and re-seeds the demo clinic only; clears the chat.
- **Doctors' day** (‹ ›): both doctors' appointments for a day, in time order (IST): time,
  patient, status, language. Cancelled and moved appointments are struck through.
- **Funnel**: registered, booked, confirmed, rescheduled, cancelled (from `events`).
- **Staff tasks**: urgent ones in red, newest first.
- The page refreshes the right side every 2 seconds.

## 6-minute live script

Before you start: `make demo-reset`, then `make demo`, page open, zoom so the banner fits.

| Time | Do / type | What to point at |
|---|---|---|
| 0:00 | Read the yellow banner aloud | Fake data; registration, WhatsApp and calls are simulated |
| 0:20 | Click **Register patient (simulated)** | Welcome message, "WhatsApp would send this (simulated)"; funnel registered 2 → 3 |
| 0:45 | Type `yes` | Days with free times (from the doctors' real hours) |
| 1:00 | Tap **Tomorrow** | Free times for tomorrow |
| 1:15 | Click **Another patient takes this slot** | A note says who booked it; the day view shows the new booking |
| 1:30 | Tap the **first time** in the list | "Sorry, that time was just taken": the database refused it |
| 1:45 | Type `no, evening instead` | Evening times on the same day: a time beats "no" |
| 2:00 | Tap an evening time, then type `ok` | Booked; message gives weekday, date and time (IST); funnel booked +1; row in the day view |
| 2:30 | Type `what are your opening hours` | Hours from the database, then the menu again; no staff task |
| 2:50 | Type `how much is the consultation fee` | DEMO text (Rs 500) |
| 3:05 | Type `is the doctor good?` | Neutral line, offers the clinic team, makes no claim |
| 3:20 | Type `confirm my appointment` | Confirmed (not a new booking); funnel confirmed +1; status in the day view |
| 3:40 | Type `I want to book another appointment` | It asks first, because Asha already has one; tap **Keep it** |
| 4:00 | Type `change my appointment`, then `day after tomorrow morning`, tap a time, type `yes` | Rescheduled; old row struck through; funnel rescheduled +1 |
| 4:45 | Switch **Chat as** to Ravi, type `namaste` | Hindi reply with the DRAFT tag |
| 5:05 | Type `kal shaam ko appointment chahiye` | Asks before a second booking (Ravi has one) |
| 5:20 | Switch to Asha, type `I have chest pain and can't breathe` | The numbers-only message a live channel sends (112 / 108 / clinic phone); urgent red task; "no alert reaches a person yet" |
| 5:45 | Type `stop` | Opt-out saved before the reply; closing line |

Close by pointing at the funnel and the task panel: every number on the right comes from the
database rows the chat just wrote.

## Recording fallback

If the live demo fails, play a recording made the day before:

1. Run the script above once as a dry run.
2. Press **Win+G** (Windows Game Bar) → **Capture** → record (or **Win+Alt+R** to start/stop).
   Game Bar records one app window: use a browser window, full screen (F11).
3. The video lands in `Videos\Captures`. Check it plays, with the banner readable.
4. Do not record `.env`, a terminal showing settings, or any real patient data.

## Pre-demo checklist

- [ ] Wake the free Supabase project (it pauses after 7 days idle): open the Supabase
      dashboard, un-pause if needed, wait until it is healthy.
- [ ] `make demo-reset` succeeds ("demo clinic ready").
- [ ] Start with `make demo` only (`DEMO_MODE` on for that process only). Not `make dev-api`.
- [ ] No tunnel (ngrok, cloudflared, port forwarding). The page is unauthenticated.
- [ ] Close `.env` and any editor or terminal that shows it.
- [ ] One full dry run of the 6-minute script, then `make demo-reset` again.
- [ ] Browser zoom set so the page fits the projector; notifications off.
- [ ] The recording fallback is on the laptop.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `refused: DEMO_MODE is not true` | Use the make targets (they set it), not plain `uv run` |
| `refused: APP_ENV is ...` | Set `APP_ENV=local` (or leave it empty) in `.env` |
| `refused: ... looks like a production database` | `SUPABASE_DB_URL` points at a database with non-simulated patients. Stop: use the dev project |
| `connection timeout` / page shows "Not available" | The free Supabase project is paused: wake it in the dashboard, then `make demo-reset` |
| `no demo clinic: run make demo-reset` | Run it (or press Reset demo) |
| `INTAKE_HMAC_SECRET is not set` | Add it to `.env` (at least 32 random characters) |
| `register the patient first` | Press Register patient, or chat as Ravi or Meera |
| `no time is on offer in the chat` | Pick a day first so times are listed, then press the button |
| 404 on `/demo` | The server was started without `DEMO_MODE=true`: use `make demo` |
| 403 on `/demo` | You opened it from another machine: the demo answers 127.0.0.1 only |
| Port 8000 in use | Stop the other server (`make dev-api`?) and retry |

## Known limitations

- **Not parsed:** "after 6", "before 10", "later in the week" and similar ranges. A message
  with two different hours ("he is back at 4, so after 6") holds nothing and shows times
  instead; the patient then picks one.
- Hindi text is DRAFT; Marathi is off for the demo clinic (DRAFT as well).
- Chat state lives in server memory: restarting `make demo` forgets conversations (bookings stay
  in the database). Press Reset demo to start clean.
- One presenter at a time; the page is not built for several browsers at once.
- "Another patient takes this slot" may book the time for each doctor free then (so it is gone).
- Staff tasks are rows only; nobody is alerted. Human handoff is an open decision.
- No WhatsApp, voice call, reminder job or Google Calendar runs in the demo.
- Address, parking, fee and insurance answers are DEMO text, not a real clinic's.
