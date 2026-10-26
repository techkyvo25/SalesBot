"""Industry pack schema.

A pack is pure configuration (YAML in packs/). The engine reads it and never contains
industry-specific logic. Adding a new industry should mean adding a YAML file, not code.
"""

from __future__ import annotations

import re
from string import Formatter
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Language = Literal["en", "hi", "mr"]


def _require_english(value: dict[Language, str]) -> dict[Language, str]:
    if "en" not in value:
        raise ValueError("localized text must include an 'en' fallback")
    return value


# Text in one or more languages. English is required as the fallback.
LocalizedText = Annotated[dict[Language, str], AfterValidator(_require_english)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntakeQuestion(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    prompt: LocalizedText
    type: Literal["text", "phone", "date", "choice", "service"] = "text"
    required: bool = True
    choices: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _choices_for_choice(self) -> IntakeQuestion:
        if self.type == "choice" and not self.choices:
            raise ValueError(f"intake question '{self.key}' is type 'choice' but has no choices")
        return self


class Service(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: LocalizedText
    duration_minutes: int = Field(gt=0, le=480)
    # Indicative price for revenue estimates in the funnel (INR). Optional.
    price_inr: int | None = Field(default=None, ge=0)


class FAQ(_Strict):
    """A common question the conversation answers, then it asks its current question again.

    `answer` may use the engine's placeholders; with `source: opening_hours` it gets {hours},
    rendered from the doctors' working hours in the database. `demo: true` marks fictional
    text for the local demo (docs/DEMO.md): the engine uses it only when FlowContext.demo is on.
    """

    id: str = Field(default="", pattern=r"^([a-z][a-z0-9_]*)?$")
    question: LocalizedText
    answer: LocalizedText
    # Phrases in any supported language (inputs, never shown). Same matching as intents.
    keywords: list[str] = Field(default_factory=list)
    source: Literal["text", "opening_hours"] = "text"
    # Offer "Talk to clinic" with the answer.
    offer_human: bool = False
    # English answer for the voice agent's knowledge base when the text answer does not fit a
    # call (for example it names a button). None = use answer["en"].
    voice_answer: str | None = None
    demo: bool = False


class EmergencyConfig(_Strict):
    # Lower-cased phrases in any supported language. Matching is done by the engine.
    triggers: list[str] = Field(default_factory=list)
    action: Literal["handoff", "callback", "advise"] = "handoff"
    message: LocalizedText


Intent = Literal[
    "book",
    "reschedule",
    "cancel",
    "confirm",
    "yes",
    "no",
    "human",
    "menu",
    "more",
    "stop",
    "thanks",
]


class SincereAsk(_Strict):
    """A patient sincerely asking whether they are talking to a person or a machine.

    `answer` is the one short truthful line, and the only patient-facing text allowed to mention
    automation (wording policy, docs/DECISIONS.md). It is DRAFT in every language until reviewed.
    """

    # Phrases in any supported language (inputs, never shown to patients).
    triggers: list[str] = Field(min_length=1)
    answer: LocalizedText
    draft: bool = True


class Conversation(_Strict):
    """Text for the channel-agnostic flow engine (salesbot_core.flow).

    Every patient-facing sentence comes from `messages`, with {placeholders} filled by the
    engine. The engine checks that every message it needs is present.
    """

    messages: dict[str, LocalizedText]
    # Phrases that signal an intent, in any supported language (inputs, never shown).
    # A trailing * matches any word with that prefix: "बदल*" matches "बदलायची".
    intents: dict[Intent, list[str]]
    sincere_ask: SincereAsk
    # Sent by live channels instead of the emergency message while that is DRAFT. Numbers and
    # symbols only, no wording, so it is the same in every language and needs no review.
    emergency_fallback: str

    @field_validator("emergency_fallback")
    @classmethod
    def _numbers_only(cls, value: str) -> str:
        names = {name for _, name, _, _ in Formatter().parse(value) if name}
        if not {"clinic_phone", "emergency_numbers"} <= names:
            raise ValueError("emergency_fallback needs {clinic_phone} and {emergency_numbers}")
        if any(ch.isalpha() for ch in re.sub(r"\{[a-z_]+\}", "", value)):
            raise ValueError("emergency_fallback must not contain words, only placeholders")
        return value


class VoiceText(_Strict):
    """Text for the Sarvam voice agent (salesbot_core.voice, docs/SARVAM_AGENT.md).

    `instruction` is the agent's instruction in English, as ordered sections (title -> body).
    Placeholders are filled by salesbot_core.packs.render_agent. `messages` are the spoken lines
    our API tools return as `spoken_text`. `sincere_ask_answer` is the one short truthful line
    for a sincere "am I talking to a machine?"; it is DRAFT until reviewed and is the only voice
    text allowed to mention automation.
    """

    instruction: dict[str, str] = Field(min_length=1)
    # Literal mode (`render_agent --literal`): sections replaced when the clinic's name, phone
    # and emergency numbers are written in instead of agent variables. Same titles only.
    literal: dict[str, str] = Field(default_factory=dict)
    messages: dict[str, LocalizedText]
    sincere_ask_answer: LocalizedText
    sincere_ask_draft: bool = True

    @model_validator(mode="after")
    def _literal_sections_exist(self) -> VoiceText:
        unknown = set(self.literal) - set(self.instruction)
        if unknown:
            raise ValueError(f"voice.literal names unknown sections: {sorted(unknown)}")
        return self


class Pack(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    version: int = Field(ge=1)
    languages: list[Language] = Field(min_length=1)
    greeting: LocalizedText
    intake_questions: list[IntakeQuestion] = Field(min_length=1)
    services: list[Service] = Field(min_length=1)
    faqs: list[FAQ] = Field(default_factory=list)
    emergency: EmergencyConfig | None = None
    # Pack-specific CRM stages, in order. The generic funnel events live in salesbot_core.events.
    lead_stages: list[str] = Field(min_length=2)
    conversation: Conversation | None = None
    voice: VoiceText | None = None
    # DRAFT text is never sent to real patients (salesbot_core.flow.drafts). Whole languages
    # awaiting native review, and individual conversation messages awaiting review.
    draft_languages: list[Language] = Field(default_factory=list)
    draft_messages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Pack:
        for label, items in (
            ("intake question key", [q.key for q in self.intake_questions]),
            ("faq id", [f.id for f in self.faqs if f.id]),
            ("service id", [s.id for s in self.services]),
            ("lead stage", self.lead_stages),
        ):
            dupes = {x for x in items if items.count(x) > 1}
            if dupes:
                raise ValueError(f"duplicate {label}: {sorted(dupes)}")
        known = set(self.conversation.messages) if self.conversation else set()
        known |= {f"voice.{k}" for k in self.voice.messages} if self.voice else set()
        if self.conversation is not None and any(not f.id for f in self.faqs):
            raise ValueError("faqs need an id when the pack has a conversation section")
        unknown = set(self.draft_messages) - known
        if unknown:
            raise ValueError(f"draft_messages names unknown messages: {sorted(unknown)}")
        return self

    def is_draft(self, language: Language, message_key: str | None = None) -> bool:
        if language in self.draft_languages:
            return True
        return message_key is not None and message_key in self.draft_messages

    def service(self, service_id: str) -> Service:
        for s in self.services:
            if s.id == service_id:
                return s
        raise KeyError(service_id)
