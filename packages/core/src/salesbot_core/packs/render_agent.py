"""Render the Sarvam voice agent's setup from a pack, so the pack stays the source of truth.

    uv run python -m salesbot_core.packs.render_agent --pack clinic [--api-base https://...]

Prints Markdown: the instruction, the greeting and sincere-ask line per language, the
knowledge-base text from the pack's FAQs, and every API tool as a cURL command to import in
Sarvam's tool builder. docs/SARVAM_AGENT.md embeds this output for the clinic pack; a test
checks it is current. Values that differ per call or per clinic stay Sarvam agent variables
({{clinic_name}}, {{patient_name}}, ...), filled by our on-start hook.

The wording scan (salesbot_core.text.wording) runs over everything rendered here; only the
sincere-ask lines are allowlisted.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from salesbot_core.packs import Pack, load_all_packs
from salesbot_core.packs.schema import Language
from salesbot_core.voice import (
    COMMON_FIELDS,
    SARVAM_LANGUAGE_NAMES,
    VOICE_TOOLS,
    ToolSpec,
    check_voice_pack,
)

# Pack placeholders that become Sarvam agent variables.
AGENT_VARIABLES: dict[str, str] = {
    "business_name": "clinic_name",
    "clinic_phone": "clinic_phone",
    "emergency_numbers": "emergency_numbers",
    "patient_name": "patient_name",
    "patient_id": "patient_id",
    "upcoming_appointment": "upcoming_appointment",
    "preferred_language": "preferred_language",
    # Sarvam's own read-only variables
    "current_day": "current_day",
    "current_date": "current_date",
    "current_time": "current_time",
}
DEFAULT_API_BASE = "https://YOUR-API-HOST"
BEGIN = "<!-- BEGIN GENERATED: render_agent --pack {pack} -->"
END = "<!-- END GENERATED -->"


def _var(name: str) -> str:
    return "{{" + name + "}}"


_VARS = {k: _var(v) for k, v in AGENT_VARIABLES.items()}


@dataclass
class RenderedAgent:
    pack_id: str
    instruction: str
    greetings: dict[Language, str]
    sincere_ask: dict[Language, str]
    knowledge_base: str
    curl: dict[str, str]
    draft: list[str] = field(default_factory=list)

    def texts_to_scan(self) -> list[str]:
        return [self.instruction, *self.greetings.values(), self.knowledge_base]

    def markdown(self) -> str:
        out = [BEGIN.format(pack=self.pack_id), ""]
        if self.draft:
            out += ["**DRAFT (not reviewed, team phones only):** " + ", ".join(self.draft), ""]
        out += ["### Instruction", "", "```text", self.instruction, "```", ""]
        out += ["### Greeting (per starting language)", ""]
        for lang, text in self.greetings.items():
            out.append(f"- **{SARVAM_LANGUAGE_NAMES[lang]}:** {text}")
        out += ["", "### Sincere-ask answer (the only line that may mention automation)", ""]
        for lang, text in self.sincere_ask.items():
            out.append(f"- **{SARVAM_LANGUAGE_NAMES[lang]}:** {text}")
        out += ["", "### Knowledge base text", "", "```text", self.knowledge_base, "```", ""]
        out += ["### API tools (cURL to import)", ""]
        for name, cmd in self.curl.items():
            out += [f"#### `{name}`", "", "```bash", cmd, "```", ""]
        out.append(END)
        return "\n".join(out) + "\n"


def _curl(spec: ToolSpec, api_base: str) -> str:
    body: dict[str, object] = {}
    for f in (*COMMON_FIELDS, *spec.fields):
        kind, _, name = f.source.partition(":")
        if kind == "model":
            body[f.name] = f.example if f.example is not None else f"<{f.description}>"
        else:
            # Replace with the matching variable using @ in Sarvam's tool editor.
            body[f.name] = f"@{name}"
    payload = json.dumps(body, ensure_ascii=False, indent=2)
    return (
        f"curl -X POST '{api_base}/api/v1/voice/tools/{spec.name}' \\\n"
        "  -H 'Authorization: Bearer <VOICE_TOOL_SECRET from Sarvam Secrets>' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        f"  -d '{payload}'"
    )


def render(pack: Pack, *, api_base: str = DEFAULT_API_BASE) -> RenderedAgent:
    check_voice_pack(pack)
    if pack.voice is None or pack.conversation is None:
        raise ValueError(f"pack {pack.id!r} needs voice and conversation sections")
    emergency = pack.conversation.messages["emergency"]["en"].format(**_VARS)
    sincere = {lang: text.format(**_VARS) for lang, text in pack.voice.sincere_ask_answer.items()}
    values = {**_VARS, "emergency_line": emergency, "sincere_ask_answer": sincere["en"]}
    sections = [
        f"## {title}\n{body.format(**values)}" for title, body in pack.voice.instruction.items()
    ]
    # Fixed text only: demo FAQs are fictional, and opening hours come from the database.
    faqs = [
        f"Q: {faq.question['en']}\nA: {faq.answer['en'].format(**_VARS)}"
        for faq in pack.faqs
        if faq.source == "text" and not faq.demo
    ]
    draft = [f"{SARVAM_LANGUAGE_NAMES[lang]} text" for lang in pack.draft_languages]
    if pack.voice.sincere_ask_draft:
        draft.append("sincere-ask answer")
    if pack.is_draft("en", "emergency"):
        draft.append("emergency line (clinical review)")
    return RenderedAgent(
        pack_id=pack.id,
        instruction="\n\n".join(sections),
        greetings={lang: text.format(**_VARS) for lang, text in pack.greeting.items()},
        sincere_ask=sincere,
        knowledge_base="\n\n".join(faqs) or "(no FAQs in this pack)",
        curl={spec.name: _curl(spec, api_base) for spec in VOICE_TOOLS},
        draft=draft,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pack", required=True)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args(argv)
    pack = load_all_packs()[args.pack]
    print(render(pack, api_base=args.api_base).markdown(), end="")


if __name__ == "__main__":
    main()
