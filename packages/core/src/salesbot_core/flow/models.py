"""Data the flow engine takes and returns. Plain, JSON-serializable, channel-agnostic.

A channel adapter (WhatsApp, voice, the dev console) turns an incoming message into `Inbound`,
calls the engine, and renders each `Reply`: text plus options. WhatsApp shows options as
buttons or a list, voice reads them out, the console numbers them.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from salesbot_core.events import EventType
from salesbot_core.packs.schema import Language


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Option(_Model):
    """A choice offered to the patient. `id` comes back in Inbound.choice when tapped."""

    id: str
    label: str


class Inbound(_Model):
    text: str = ""
    # Option id when the patient tapped a button or list row.
    choice: str | None = None
    received_at: datetime


class Reply(_Model):
    # Pack message key, kept for analytics and for the wording scan.
    key: str
    text: str
    language: Language
    options: list[Option] = Field(default_factory=list)
    # True = not reviewed; must never reach a real patient (salesbot_core.flow.drafts).
    draft: bool = False
    # Text a live channel sends instead while this reply is DRAFT (emergencies: numbers only,
    # no wording). None = a DRAFT reply blocks the turn.
    fallback: str | None = None


class StaffTask(_Model):
    """Work for clinic staff in the admin (the alert channel is an open decision)."""

    kind: str  # "emergency" | "human_requested" | "not_understood" | "no_openings" | "opt_out"
    reason: str
    urgent: bool = False


class FlowEvent(_Model):
    """A conversation-level funnel event. Booking events (booking_created, ...) are written by
    the booking engine itself, never here, so nothing is counted twice."""

    type: EventType
    properties: dict[str, Any] = Field(default_factory=dict)


class Step(StrEnum):
    IDLE = "idle"
    WELCOME = "welcome"  # asked "book your first visit?"
    PICK_DAY = "pick_day"
    PICK_TIME = "pick_time"
    CONFIRM_SLOT = "confirm_slot"  # a slot is held; asked "shall I book it?"
    OFFER_EARLIEST = "offer_earliest"  # nothing in the lookahead window; offered the first opening
    CONFIRM_CANCEL = "confirm_cancel"
    CONFIRM_APPOINTMENT = "confirm_appointment"  # after a confirmation request or reminder
    CONFIRM_SECOND = "confirm_second"  # has an appointment; asked "book another as well?"


class Purpose(StrEnum):
    BOOK = "book"
    RESCHEDULE = "reschedule"


class Hold(_Model):
    id: UUID
    practitioner_id: UUID
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime


class ConversationState(_Model):
    """Everything the engine needs between messages. Stored per conversation (Stage C)."""

    language: Language
    step: Step = Step.IDLE
    purpose: Purpose | None = None
    # The appointment being rescheduled, cancelled or confirmed.
    appointment_id: UUID | None = None
    # Reschedules keep the original doctor.
    practitioner_id: UUID | None = None
    day: date | None = None
    days_from: date | None = None
    times_offset: int = 0
    offered: list[Option] = Field(default_factory=list)
    hold: Hold | None = None
    earliest: datetime | None = None
    # Consecutive messages the engine could not understand.
    misses: int = 0


class Turn(_Model):
    state: ConversationState
    replies: list[Reply] = Field(default_factory=list)
    events: list[FlowEvent] = Field(default_factory=list)
    tasks: list[StaffTask] = Field(default_factory=list)
    # Language of the inbound message for events.language: "en"/"hi"/"mr", "other", or None.
    inbound_language: str | None = None
    # The patient asked to stop messages. The channel adapter records the opt-out.
    opt_out: bool = False
