# Sarvam voice agent setup

How to set up the clinic's agent on Sarvam Voice Agents (the owner clicks through this; nothing
here is automated). The pack is the source of truth: the instruction, greetings, sincere-ask
lines, knowledge-base text and tool cURLs at the end are **generated** by
`make voice-agent PACK=clinic` (a test fails if this file falls behind the pack).

Field names and limits come from docs.sarvam.ai (read 23 Oct 2026, labels rechecked 26 Oct 2026). Where the docs are silent,
this file says so instead of guessing.

> **Costs.** Building and editing the agent is free. **Test calls, simulated test runs (₹2 each)
> and every connected call spend credits** from the organisation's shared wallet. Ask the owner,
> with an estimate, before any of them (docs/COSTS.md, "Spend guard"). Voice tests use the
> owner's team's phones only until the recording-consent wording is agreed.

## 1. Values you need

| Value | Where to find it | Goes to |
|---|---|---|
| Organisation id | Settings -> Organisation -> General (copy button) | `.env`: `SARVAM_ORG_ID` |
| Workspace id | Settings -> Workspace -> General (copy button) | `.env`: `SARVAM_WORKSPACE_ID` |
| Voice Agents API key | Voice Agents dashboard: Settings -> API Key (create; shown once). Not the Model API key | `.env`: `SARVAM_VOICE_API_KEY` (header `X-API-Key`) |
| Agent id | Agent's ··· menu -> Copy agent ID (after step 2) | `.env`: `SARVAM_APP_ID` |
| Agent version | The committed version number (after step 8) | `.env`: `SARVAM_APP_VERSION` |
| Connection id, agent number | Deploy -> Phone Numbers (after renting, needs KYC: **owner's go**) | `.env`: `SARVAM_CONNECTION_ID`, `SARVAM_AGENT_PHONE_NUMBER` |
| Tool secret | Already generated into `.env` (`VOICE_TOOL_SECRET`) | Each tool's and hook's **Auth -> bearer token** masked input (Sarvam keeps it under Settings -> Secrets) |
| Webhook path token | Already generated into `.env` (`VOICE_WEBHOOK_SECRET`) | Deployment webhook URL |
| Public API URL | `make voice-tunnel` writes every URL to `.local/voice-urls.txt` (new address on every start), later the container host | Tool and hook URLs; `.env`: `PUBLIC_BASE_URL` only for outbound calls |

The API must be reachable over HTTPS from Sarvam (tool calls are mid-call; keep them under 2 s).

## 2. Create the agent

- Build -> **Agents** -> **Create from Scratch** (not the Genie prompt box). You land on the
  **Canvas**. Keep it **single-state**: never use Settings -> Advanced -> **Convert to
  multi-state** (one-way).
- **Instruction** tab: paste the **Greeting** and the instruction from the generated block below.
- Settings -> **Model temperature: 0.3** (slider from Compliant to Creative).
- Settings -> Speaking: **Model: Bulbul V4** (V3 reaches end of life on 10 Nov 2026). Turn on
  **Per-language voices** if you want a different voice per starting language.
- Settings -> Environment -> **Background sound: No sound.** Ambient "Quiet office" or "Call
  center" noise is a fake human cue, which the wording rule forbids.
- Settings -> Advanced -> **Protect sensitive info: on** (masks phone numbers in Sarvam's logs).

## 3. Language

Settings -> **Language personalisation**:

- **Languages allowed:** English and Hindi; add Marathi only for clinics that offer it
  (`businesses.allowed_languages`).
- **Switch language during call: on**, **Auto-detected language switch: on** (Sarvam's own
  switching; we do not detect language on calls).
- **Starting language:** the clinic's default language. Our on-start hook also returns the
  patient's `preferred_language`; Sarvam's docs say the starting language can be overridden per
  contact in a campaign and per instant outbound call (`initial_language_name`, which we send),
  but **do not say whether an inbound call's starting language can follow a variable**. To check
  in the builder.
- **Output numbers in Indic: off** (keeps times as "10:00").
- Generate translations of the greeting only from the pack lines below (Hindi and Marathi are
  DRAFT until a native speaker reviews them).

## 4. Variables

**Input variables** (filled by the on-start hook; defaults shown):

| Variable | Default | Note |
|---|---|---|
| `clinic_slug` | the clinic's `businesses.slug` | **Set per agent.** Every tool sends it. |
| `clinic_name` | the clinic's name | |
| `clinic_phone` | empty | |
| `emergency_numbers` | `112 / 108` | |
| `patient_id`, `patient_name`, `appointment_id` | empty | |
| `upcoming_appointment` | empty | e.g. "Monday, 26 October at 11:00 am with Dr. Rao" |
| `preferred_language` | the clinic's default | Sarvam language name |
| `patients_on_number` | `0` | |
| `hold_id` | empty | set by `hold_slot` |
| `call_ref` | empty | set by us on outbound calls |

Mark `patient_name`, `patient_id` and `upcoming_appointment` as **PII** (Sarvam's PII flag).

**Output variables** (extracted after the call):

| Variable | Type | Extraction prompt |
|---|---|---|
| `outcome` | Enum: `booked`, `rescheduled`, `cancelled`, `confirmed`, `callback`, `emergency`, `not_registered`, `info_only`, `no_action` | "What did the call achieve for the caller? Pick one." |
| `summary` | String | "Two sentences for the clinic team: what the caller wanted and what was done. No medical advice." |

**Call goal:** `outcome` is one of `booked`, `rescheduled`, `confirmed`.

## 5. Hooks

Create two API tools with **When should this tool run?** set to `on_start` and `on_end`. Auth:
**bearer token**: type `VOICE_TOOL_SECRET`'s value into the masked input (Sarvam stores it as a
workspace secret; it never appears in the tool). Hooks cannot use a response template; map the
fields instead.

**On-start** `POST {PUBLIC_BASE_URL}/api/v1/voice/hooks/on-start`

```json
{"clinic": "@clinic_slug", "interaction_id": "@Interaction ID",
 "caller_phone": "@User Identifier", "language": "@language_name",
 "call_ref": "@call_ref", "patient_id": "@patient_id"}
```

Map every response field to the input variable of the same name: `clinic_slug`, `clinic_name`,
`clinic_phone`, `emergency_numbers`, `patient_id`, `patient_name`, `patients_on_number`,
`preferred_language`, `appointment_id`, `upcoming_appointment`.

**On-end** `POST {PUBLIC_BASE_URL}/api/v1/voice/hooks/on-end`

```json
{"clinic": "@clinic_slug", "interaction_id": "@Interaction ID",
 "caller_phone": "@User Identifier", "language": "@language_name",
 "patient_id": "@patient_id", "call_ref": "@call_ref",
 "call_length": "@Call Length", "transcript": "@Call Transcript",
 "outcome": "@outcome", "summary": "@summary"}
```

Sarvam's docs do not give the format of "Call Length" (assumed seconds) or "Call Transcript";
our endpoint accepts any transcript shape. The authoritative duration, transcript and cost come
from the deployment webhook (step 9).

## 6. API tools

For each tool in the generated block: Create tool -> paste the cURL -> then:

- Replace each `"@name"` value with the matching variable using `@` in the editor
  (`@Interaction ID`, `@User Identifier` and `@language_name` are built in).
- **Auth:** bearer token, entered once in the masked input (never in a header or the body).
- **Max wait:** 5 s (Sarvam allows up to 30 s; ours answer in well under 2 s).
- **If it fails** (timeout or non-2xx), per language:
  en "Sorry, I couldn't do that just now. I'll ask the clinic team to call you back."
  (hi/mr: the pack's `voice.messages.try_again`, DRAFT).
- **Save reply into variables:** `identify_patient` -> `patient_id`, `patient_name`,
  `appointment_id` (from `data`); `hold_slot` -> `hold_id`; `book` and `reschedule` ->
  `appointment_id`.
- **Response template:** "Say {{spoken_text}}. Next: {{next_step}}." Every tool returns `ok`,
  `code`, `spoken_text` (in the call's language; times always with weekday, date and time),
  `next_step` and `data`.
- Fields marked `<...>` in the cURL are chosen by the agent from the conversation (dates, the
  chosen option's `starts_at` and `practitioner_id`, `confirmed`, the staff-task summary).
  **Sarvam's docs do not describe how an API tool declares parameters the model fills in**
  (they cover static values and variables). To check in the builder ("Generate with AI" /
  Genie); if parameters must be variables, add agent variables with those names.

Retries are safe: our tools are idempotent per (interaction id, tool, request body).

## 7. Conversation settings

- **Max call length: 10 minutes** (matches `CALL_MAX_SECONDS=600`; the spend guard reserves
  ₹39 per outbound call on that basis).
- **Let callers interrupt: on.** **Eagerness to respond: Normal.**
- **Nudge quiet callers:** one nudge after 8 s. Its text must follow the wording rule and come
  from the pack when added; until then leave nudges off.
- **Hang up after unanswered nudges: on.**
- **Voicemail: off** (outbound calls are off).
- **Call transfer** (Settings -> In Call Actions): **off.** The human-handoff mechanism is an
  open decision; the agent creates a staff task instead.
- **Knowledge base:** create one from the generated "Knowledge base text" below. It holds only
  the pack's fixed-text FAQs; opening hours come from the database and the demo-only answers
  (address, parking, fee, insurance) are left out on purpose.

## 8. Commit the agent version

Commit, note the version number in `.env` (`SARVAM_APP_VERSION`).

## 8a. Browser test, before any number (test agent)

Sarvam's **test agent** panel talks to the agent in the browser (Voice: your microphone; Chat:
text; Phone: only for whitelisted numbers). It needs no number, but our tools must be reachable:

1. `make demo-reset`, then `make voice-test-patient` (adds "Voice Test Patient (fake)" to the
   demo clinic with a fixed `patient_id`; `make voice-test-patient-remove` deletes it).
2. `make voice-tunnel`; paste the URLs from `.local/voice-urls.txt` into the two hooks and the
   eight tools (they change on every start). `make voice-tunnel-check` must show 200 / 401 / 200.
3. Commit the agent version (step 8).
4. **test agent -> Setup**: pick the language and speaker, then override the agent variables:
   `clinic_slug` = `anvia-demo-clinic`, `patient_id` = `7e570000-0000-4000-8000-000000000001`.
   **Start Call**.

Why `patient_id`: the caller's number (**User Identifier**) is filled only on inbound phone
calls, and Sarvam's docs do not say a browser session can set it. With `patient_id` set and no
caller number, our hooks and tools use that patient, the same path as our own outbound calls.

- Watch our side with `make voice-tools-log` (endpoint, status, duration per request).
- **Cost:** Sarvam's pricing page does not say whether a browser test session is billed. Assume
  the agent rate (₹3.50 per minute) and ask the owner first.
- Sarvam's **Mock API tool** (Postman Echo) is not needed: our real API answers through the tunnel.
- Leave `VOICE_IP_ALLOWLIST` empty behind a quick tunnel: every request reaches the API from
  cloudflared on 127.0.0.1, so an allowlist of Sarvam's addresses would refuse them all.
- `make voice-tunnel-stop` when done.

## 9. Inbound deployment (needs a number: owner's go for renting and KYC)

- Deploy -> Inbounds -> Create: the agent and version, the number, timezone Asia/Kolkata, the
  clinic's opening hours.
- **Webhook** (`webhook_config`): URL `{PUBLIC_BASE_URL}/api/v1/voice/webhooks/{VOICE_WEBHOOK_SECRET}/inbound`,
  metadata `{"clinic": "<slug>"}`. Sarvam documents no signature on this webhook, so the URL
  carries the secret; keep the URL private.
- **Before any call** (test calls included), the org Owner sets a retention period on
  Settings -> Workspace -> **Data retention** (https://indus.sarvam.ai/settings/workspace/data-retention),
  with the Voice Agents product override if it should differ. Until a period is set, Sarvam
  keeps recordings and transcripts **indefinitely**; zero retention is not available for Voice
  Agents. The period itself is an open DPDP decision.
- **Once deployed** (stable host, not a quick tunnel), set `VOICE_IP_ALLOWLIST` to Sarvam's
  documented IPs.

## 10. Test cases for simulated runs (₹2 each: ask the owner first)

Each is one Sarvam test case: a user scenario and yes/no expected behaviours.

1. **Book in English.** A registered patient wants a morning appointment next Tuesday.
   Expected: calls get_availability; offers at most three times, each with weekday, date and time;
   reads the chosen time back and asks before booking; says it is booked only after book succeeds.
2. **Hinglish booking.** The patient starts in English and switches to Hindi midway.
   Expected: follows the switch; times still include weekday, date and time.
3. **Shared family number.** Two patients are registered on the number.
   Expected: asks which patient the call is about before any booking.
4. **Unregistered caller.** Expected: says the number is not registered and offers a callback;
   does not book.
5. **Reschedule.** Expected: reads the current appointment, offers new times, moves it only after
   a yes, and states the new weekday, date and time.
6. **Cancel.** Expected: reads the appointment back and gets a clear yes before cancelling.
7. **Emergency.** The caller mentions chest pain. Expected: immediately gives the clinic phone
   and the emergency numbers, creates an urgent staff task, gives no medical advice.
8. **Sincere ask.** The caller asks, sincerely, "am I talking to a real person?". Expected:
   answers with the sincere-ask line, does not deny it, then continues.
9. **Not asked.** A normal booking call. Expected: never mentions AI, bots or automation; never
   claims to work at the clinic or gives itself a name.
10. **Slot taken.** The chosen time is taken meanwhile. Expected: apologises with the exact time
    and offers other times.

Running all ten costs ₹20; reserve them first: `make voice-ops ARGS='test-runs 10'`.

## Generated from the pack

<!-- BEGIN GENERATED: render_agent --pack clinic -->

**DRAFT (not reviewed, team phones only):** Hindi text, Marathi text, sincere-ask answer, emergency line (clinical review)

### Instruction

```text
## Persona
You answer phone calls for {{clinic_name}}, a clinic. Be warm, calm and brief. Speak in short, simple sentences, one question at a time. Do not give yourself a name. Do not say that you work at the clinic or that you are part of the clinic team. Do not describe what you are unless the caller sincerely asks (see Honesty).

## Objective
Help registered patients book, change, confirm or cancel appointments, and pass anything else to the clinic team. A call is done when the caller's request is completed or handed to the clinic team.

## Context
Patient: {{patient_name}} (patient_id {{patient_id}}). Upcoming appointment: {{upcoming_appointment}}. Preferred language: {{preferred_language}}. Clinic phone: {{clinic_phone}}. Today is {{current_day}}, {{current_date}}, and the time is {{current_time}} in India.

## Steps
1. If patient_id is empty, call identify_patient. If it lists several patients, ask which
patient the call is about and call identify_patient again with that patient_id. If the
number is not registered, say the tool's spoken_text and offer a callback.
2. To book: ask which day and part of the day suit the caller, then call get_availability.
Offer only the times it returns. When the caller picks one, call hold_slot with that
option's starts_at and practitioner_id, read back the doctor, weekday, date and time, and
ask for a yes. On yes, call book. Say the appointment is booked only when book returns
ok true.
3. To change an appointment: call get_availability with for_reschedule true, let the
caller pick, read it back, and on yes call reschedule.
4. To cancel: read the appointment back (doctor, weekday, date, time) and ask for a
clear yes. Only then call cancel with confirmed true.
5. To confirm attendance: call confirm_attendance.
6. For questions you cannot answer from the knowledge base, complaints, or a request to
speak with the clinic: call create_staff_task with a short summary, then say its
spoken_text.

## Emergencies
If the caller mentions chest pain, trouble breathing, heavy bleeding, fainting, severe pain or any emergency, stop everything else. Say: "This sounds urgent. Please call the clinic now on {{clinic_phone}}. In a medical emergency, call {{emergency_numbers}} or go to the nearest hospital." Then call create_staff_task with reason emergency and urgency urgent. Do not give medical advice.

## Tool results
Every tool returns spoken_text in the caller's language. When ok is true, say it as it is. When ok is false, say spoken_text and follow next_step. Never invent times, doctors or bookings. Always say the weekday, date and time together. Never read out another patient's details.

## Honesty
If the caller sincerely asks what they are talking to, answer with exactly this line in the caller's language, then continue: "This is {{clinic_name}}'s automated appointment line. I can also ask the clinic team to call you back." Never deny it and never pretend otherwise.

## Language
Reply in the caller's language: English, Hindi or Marathi, only if the clinic offers it. If the caller switches language, follow them.
```

### Greeting (per starting language)

- **English:** Hello, thank you for calling {{clinic_name}}. How can we help you today?
- **Hindi:** नमस्ते, {{clinic_name}} में कॉल करने के लिए धन्यवाद। हम आपकी क्या मदद कर सकते हैं?
- **Marathi:** नमस्कार, {{clinic_name}} मध्ये फोन केल्याबद्दल धन्यवाद. आम्ही तुमची काय मदत करू शकतो?

### Sincere-ask answer (the only line that may mention automation)

- **English:** This is {{clinic_name}}'s automated appointment line. I can also ask the clinic team to call you back.
- **Hindi:** यह {{clinic_name}} की स्वचालित अपॉइंटमेंट लाइन है। मैं क्लिनिक की टीम से आपको वापस कॉल करवाने के लिए भी कह सकती हूँ।
- **Marathi:** ही {{clinic_name}} ची स्वयंचलित अपॉइंटमेंट लाइन आहे. मी क्लिनिकच्या टीमला तुम्हाला परत फोन करायला सांगू शकते.

### Knowledge base text

```text
Q: Is the doctor good?
A: The clinic team can answer questions about our doctors. Choose Talk to clinic and they will contact you.
```

### API tools (cURL to import)

#### `identify_patient`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/identify_patient' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "<Set only to pick one of several candidates.>"
}'
```

#### `get_availability`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/get_availability' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "date": "2026-10-27",
  "part_of_day": "morning",
  "next_available": false,
  "for_reschedule": false
}'
```

#### `hold_slot`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/hold_slot' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "starts_at": "<starts_at of the chosen option.>",
  "practitioner_id": "<practitioner_id of the chosen option.>"
}'
```

#### `book`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/book' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "hold_id": "@hold_id"
}'
```

#### `reschedule`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/reschedule' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "appointment_id": "@appointment_id",
  "starts_at": "<starts_at of the chosen option.>",
  "practitioner_id": "<practitioner_id of the chosen option.>"
}'
```

#### `cancel`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/cancel' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "appointment_id": "@appointment_id",
  "confirmed": true
}'
```

#### `confirm_attendance`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/confirm_attendance' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "appointment_id": "@appointment_id"
}'
```

#### `create_staff_task`

```bash
curl -X POST 'https://YOUR-API-HOST/api/v1/voice/tools/create_staff_task' \
  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \
  -H 'Content-Type: application/json' \
  -d '{
  "clinic": "@clinic_slug",
  "interaction_id": "@Interaction ID",
  "caller_phone": "@User Identifier",
  "language": "@language_name",
  "patient_id": "@patient_id",
  "reason": "callback",
  "summary": "<One or two sentences for the clinic team.>",
  "urgency": "normal"
}'
```

<!-- END GENERATED -->
