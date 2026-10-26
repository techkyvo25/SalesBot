"""Ready-to-paste Sarvam setup for the 8 API tools and 2 hooks, with the tunnel address filled
in and the secret left out (docs/SARVAM_AGENT.md sections 5 and 6). Written by
`make voice-tunnel` to .local/voice-tools.txt, because the address changes on every start.
"""

from __future__ import annotations

import json

from salesbot_core.packs.render_agent import tool_body
from salesbot_core.voice import VOICE_TOOLS

AUTH = (
    "Auth: Bearer token. Type the value of VOICE_TOOL_SECRET (your .env line "
    "VOICE_TOOL_SECRET=...) into the masked input. Never in a header or the body."
)
IF_IT_FAILS = (
    "If it fails (en): Sorry, I couldn't do that just now. I'll ask the clinic team to call "
    "you back."
)
# Hook bodies (section 5): fixed, not model-chosen.
ON_START = {
    "clinic": "@clinic_slug",
    "interaction_id": "@Interaction ID",
    "caller_phone": "@User Identifier",
    "language": "@language_name",
    "call_ref": "@call_ref",
    "patient_id": "@patient_id",
}
ON_END = {
    "clinic": "@clinic_slug",
    "interaction_id": "@Interaction ID",
    "caller_phone": "@User Identifier",
    "language": "@language_name",
    "patient_id": "@patient_id",
    "call_ref": "@call_ref",
    "call_length": "@Call Length",
    "transcript": "@Call Transcript",
    "outcome": "@outcome",
    "summary": "@summary",
}
ON_START_SAVES = (
    "clinic_slug, clinic_name, clinic_phone, emergency_numbers, patient_id, patient_name, "
    "patients_on_number, preferred_language, appointment_id, upcoming_appointment"
)


def _json(body: dict[str, object]) -> str:
    return json.dumps(body, ensure_ascii=False, indent=2)


def paste_text(base: str) -> str:
    api = f"{base.rstrip('/')}/api/v1/voice"
    out = [
        "SARVAM SETUP: 8 API tools + 2 hooks (docs/SARVAM_AGENT.md sections 5 and 6)",
        f"Tunnel address: {base}  (changes on every `make voice-tunnel`: paste again)",
        'In each body, replace every "@name" value with that variable using @ in the editor.',
        "Values in <...> are chosen by the agent from the conversation.",
        "",
    ]
    hooks = (
        (
            "on_start",
            "on-start",
            ON_START,
            f"Map response fields to the variables: {ON_START_SAVES}",
        ),
        ("on_end", "on-end", ON_END, "No mapping (the reply is ignored)."),
    )
    for when, path, body, after in hooks:
        out += [
            "=" * 72,
            f"HOOK  {path}",
            f"When should this tool run?: {when}",
            f"Method: POST   URL: {api}/hooks/{path}",
            AUTH,
            "Body (JSON):",
            _json(body),
            after,
            "",
        ]
    for spec in VOICE_TOOLS:
        saves = ", ".join(spec.saves) if spec.saves else "(none)"
        out += [
            "=" * 72,
            f"TOOL  {spec.name}",
            "When should this tool run?: run (During conversation)",
            f"Name: {spec.name}",
            f"Description: {spec.description}",
            f"Method: POST   URL: {api}/tools/{spec.name}",
            AUTH,
            "Body (JSON):",
            _json(tool_body(spec)),
            "Response template: Say {{spoken_text}}. Next: {{next_step}}.",
            f"Save reply into variables (fields of data): {saves}",
            "Max wait: 5 s",
            IF_IT_FAILS,
            "",
        ]
    return "\n".join(out)
