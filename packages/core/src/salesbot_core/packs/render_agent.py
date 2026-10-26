"""Render the Sarvam voice agent's setup from a pack, so the pack stays the source of truth.

    uv run python -m salesbot_core.packs.render_agent --pack clinic [--api-base https://...]
    uv run python -m salesbot_core.packs.render_agent --pack clinic --literal \
        --clinic-file apps/api/src/salesbot_api/demo/clinic.yaml

Prints Markdown: the instruction, the greeting and sincere-ask line per language, the
knowledge-base text from the pack's FAQs, and every API tool as a cURL command to import in
Sarvam's tool builder. docs/SARVAM_AGENT.md embeds this output for the clinic pack; a test
checks it is current. Values that differ per call or per clinic stay Sarvam agent variables
({{clinic_name}}, {{patient_name}}, ...), filled by our on-start hook.

Literal mode (`--literal`) writes one clinic's name, phone and emergency numbers into the
text and leaves no {{...}} at all: the pack's `voice.literal` sections replace the ones that
use per-call variables (the agent learns the caller and today's date from identify_patient).

The wording scan (salesbot_core.text.wording) runs over everything rendered here; only the
sincere-ask lines are allowlisted.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


@dataclass(frozen=True)
class ClinicValues:
    """What literal mode writes in instead of agent variables."""

    name: str
    phone: str
    emergency_numbers: list[str] = field(default_factory=lambda: ["112", "108"])

    @classmethod
    def from_file(cls, path: Path) -> ClinicValues:
        """A clinic YAML (the load_clinic format): name, contact_phone, emergency_numbers."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        numbers = data.get("emergency_numbers") or ["112", "108"]
        return cls(data["name"], data["contact_phone"], [str(n) for n in numbers])

    def variables(self) -> dict[str, str]:
        return {
            "business_name": self.name,
            "clinic_phone": self.phone,
            "emergency_numbers": " / ".join(self.emergency_numbers),
        }


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


def render(
    pack: Pack, *, api_base: str = DEFAULT_API_BASE, literal: ClinicValues | None = None
) -> RenderedAgent:
    check_voice_pack(pack)
    if pack.voice is None or pack.conversation is None:
        raise ValueError(f"pack {pack.id!r} needs voice and conversation sections")
    variables = literal.variables() if literal is not None else _VARS
    emergency = pack.conversation.messages["emergency"]["en"].format(**variables)
    sincere = {
        lang: text.format(**variables) for lang, text in pack.voice.sincere_ask_answer.items()
    }
    values = {**variables, "emergency_line": emergency, "sincere_ask_answer": sincere["en"]}
    bodies = dict(pack.voice.instruction)
    if literal is not None:
        bodies |= pack.voice.literal
    sections = [f"## {title}\n{body.format(**values)}" for title, body in bodies.items()]
    # Fixed text only: demo FAQs are fictional, and opening hours come from the database.
    faqs = [
        f"Q: {faq.question['en']}\nA: {(faq.voice_answer or faq.answer['en']).format(**variables)}"
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
        greetings={lang: text.format(**variables) for lang, text in pack.greeting.items()},
        sincere_ask=sincere,
        knowledge_base="\n\n".join(faqs) or "(no FAQs in this pack)",
        curl={spec.name: _curl(spec, api_base) for spec in VOICE_TOOLS},
        draft=draft,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pack", required=True)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--literal", action="store_true", help="no {{variables}}: see above")
    parser.add_argument("--clinic-file", type=Path, help="with --literal: a clinic YAML")
    args = parser.parse_args(argv)
    if args.literal and args.clinic_file is None:
        parser.error("--literal needs --clinic-file")
    pack = load_all_packs()[args.pack]
    literal = ClinicValues.from_file(args.clinic_file) if args.literal else None
    rendered = render(pack, api_base=args.api_base, literal=literal)
    if literal is not None and "{{" in rendered.markdown():
        raise SystemExit("literal output still has {{...}}: add a voice.literal section")
    # Hindi and Marathi lines: a Windows console defaults to a code page that cannot print them.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(rendered.markdown(), end="")


if __name__ == "__main__":
    main()
