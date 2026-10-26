"""Text conversations against the database: the flow engine's SchedulingPort over
salesbot_api.booking, and the records a turn leaves behind (funnel events, staff tasks).

`DbScheduling` follows the same contract as salesbot_core.flow.memory.InMemoryScheduling, but
every hold, booking, confirmation and cancellation goes through the booking engine, so the
database exclusion constraint, events and audit rows apply exactly as for voice and staff.
Bound to one business and one client; every query filters by business_id (this connection
bypasses RLS).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from uuid import UUID

import psycopg

from salesbot_api import booking
from salesbot_api.records import Actor, record_event
from salesbot_api.voice import store
from salesbot_core.events import Channel, EventType, FunnelEvent, LeadSource
from salesbot_core.flow import ConversationState, FlowContext, FlowEngine, Inbound, Turn
from salesbot_core.flow.models import Hold
from salesbot_core.flow.ports import Appointment, HoldExpired, NotActive, SlotUnavailable
from salesbot_core.packs import load_pack_by_id
from salesbot_core.packs.schema import Language
from salesbot_core.scheduling import (
    AppointmentStatus,
    SchedulingSettings,
    Scope,
    Slot,
    VisitType,
)

Row = dict


def _appointment(row: Row) -> Appointment:
    return Appointment(
        id=row["id"],
        practitioner_id=row["practitioner_id"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        status=AppointmentStatus(row["status"]),
        visit=VisitType(row["visit_type"]),
    )


@dataclass
class DbScheduling:
    """SchedulingPort for one client, backed by the booking engine. `Conversation.record`
    keeps `actor.language` on the conversation's language for the booking events."""

    conn: psycopg.Connection
    business_id: UUID
    client_id: UUID
    actor: Actor
    # What happened, for the dev console and the demo page (no personal data).
    log: list[str] = field(default_factory=list)
    _settings: SchedulingSettings | None = None
    _names: dict[tuple[UUID, str], str] = field(default_factory=dict)

    @property
    def settings(self) -> SchedulingSettings:
        if self._settings is None:
            self._settings = booking.load_settings(self.conn, self.business_id)
        return self._settings

    # -- availability -------------------------------------------------------

    def visit_type(self) -> VisitType:
        seen = store.has_past_visits(self.conn, self.business_id, self.client_id)
        return VisitType.CONSULTATION if seen else VisitType.FIRST_VISIT

    def open_days(
        self, scope: Scope, *, now: datetime, from_day: date | None = None, limit: int | None = None
    ) -> list[date]:
        return booking.open_days(
            self.conn, self.business_id, scope=scope, now=now, from_day=from_day, limit=limit
        )

    def open_slots(self, scope: Scope, *, day: date, now: datetime) -> list[Slot]:
        return booking.open_slots(self.conn, self.business_id, day=day, scope=scope, now=now)

    def first_opening(self, scope: Scope, *, now: datetime) -> tuple[Slot | None, bool]:
        return booking.first_opening(self.conn, self.business_id, scope=scope, now=now)

    # -- holds and bookings -------------------------------------------------

    def hold(
        self, scope: Scope, *, starts_at: datetime, now: datetime, reschedule_of: UUID | None = None
    ) -> Hold:
        try:
            row = booking.hold_slot(
                self.conn,
                self.business_id,
                client_id=self.client_id,
                starts_at=starts_at,
                scope=scope,
                actor=self.actor,
                now=now,
                reschedule_of=reschedule_of,
            )
        except booking.SlotUnavailableError as exc:
            self.log.append("slot_unavailable")
            raise SlotUnavailable(str(exc)) from exc
        except (booking.InvalidStateError, booking.NotFoundError) as exc:
            raise NotActive(str(exc)) from exc
        self.log.append(f"hold {row['id'].hex[:8]}")
        return Hold(
            id=row["id"],
            practitioner_id=row["practitioner_id"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            expires_at=row["hold_expires_at"],
        )

    def release(self, hold_id: UUID) -> None:
        booking.release_hold(self.conn, self.business_id, hold_id)
        self.log.append(f"release {hold_id.hex[:8]}")

    def book(self, hold_id: UUID, *, now: datetime) -> Appointment:
        held = store.get_appointment(self.conn, self.business_id, hold_id)
        if held is None or held["client_id"] != self.client_id or held["status"] != "held":
            raise HoldExpired("no such hold")
        try:
            row = booking.book_hold(self.conn, self.business_id, hold_id, actor=self.actor, now=now)
        except booking.HoldExpiredError as exc:
            raise HoldExpired(str(exc)) from exc
        except (booking.InvalidStateError, booking.NotFoundError) as exc:
            raise NotActive(str(exc)) from exc
        self.log.append(f"{row['status']} {row['id'].hex[:8]}")
        return _appointment(row)

    def upcoming(self, *, now: datetime) -> Appointment | None:
        row = store.upcoming_appointment(self.conn, self.business_id, self.client_id, now)
        return _appointment(row) if row else None

    def get(self, appointment_id: UUID) -> Appointment | None:
        row = store.get_appointment(self.conn, self.business_id, appointment_id)
        if row is None or row["client_id"] != self.client_id:
            return None
        return _appointment(row)

    def confirm(self, appointment_id: UUID, *, now: datetime) -> Appointment:
        self._own(appointment_id)
        try:
            row = booking.confirm(
                self.conn, self.business_id, appointment_id, actor=self.actor, now=now
            )
        except (booking.InvalidStateError, booking.NotFoundError) as exc:
            raise NotActive(str(exc)) from exc
        self.log.append(f"confirmed {appointment_id.hex[:8]}")
        return _appointment(row)

    def cancel(self, appointment_id: UUID, *, now: datetime) -> Appointment:
        self._own(appointment_id)
        try:
            row = booking.cancel(
                self.conn, self.business_id, appointment_id, actor=self.actor, now=now
            )
        except (booking.InvalidStateError, booking.NotFoundError) as exc:
            raise NotActive(str(exc)) from exc
        self.log.append(f"cancelled {appointment_id.hex[:8]}")
        return _appointment(row)

    def practitioner_name(self, practitioner_id: UUID, language: Language) -> str:
        key = (practitioner_id, language)
        if key not in self._names:
            names = store.practitioner_names(
                self.conn, self.business_id, [practitioner_id], language
            )
            self._names[key] = names.get(practitioner_id, "")
        return self._names[key]

    def _own(self, appointment_id: UUID) -> None:
        if self.get(appointment_id) is None:
            raise NotActive("appointment not found")


# ---------------------------------------------------------------------------
# A conversation: engine + port + records
# ---------------------------------------------------------------------------

# Engine task kinds -> staff_tasks.kind (the table's CHECK list).
TASK_KINDS = {
    "emergency": "emergency",
    "human_requested": "callback",
    "not_understood": "question",
    "no_openings": "callback",
    "opt_out": "other",
}
# create_staff_task writes these itself; the engine's copies would count twice.
_WRITTEN_WITH_TASK = {EventType.HANDOFF_TASK_CREATED, EventType.EMERGENCY_DETECTED}


@dataclass
class Conversation:
    """One client's text conversation with a clinic, on the database. The caller keeps
    `state` between messages (the demo keeps it in memory; Stage C stores it)."""

    engine: FlowEngine
    port: DbScheduling
    state: ConversationState
    source: LeadSource = LeadSource.SIMULATION

    def handle(self, inbound: Inbound) -> Turn:
        turn = self.engine.handle(self.state, inbound)
        self.record(turn, inbound.received_at)
        return turn

    def welcome(self, now: datetime) -> Turn:
        turn = self.engine.welcome(self.state, now)
        self.record(turn, now)
        return turn

    def confirmation_request(self, appointment_id: UUID, now: datetime) -> Turn:
        turn = self.engine.confirmation_request(self.state, appointment_id, now)
        self.record(turn, now)
        return turn

    def reminder(self, appointment_id: UUID, now: datetime) -> Turn:
        turn = self.engine.reminder(self.state, appointment_id, now)
        self.record(turn, now)
        return turn

    def record(self, turn: Turn, now: datetime) -> None:
        """Write the turn's staff tasks and conversation events, then keep its state.
        Booking events were already written by the booking engine."""
        self.state = turn.state
        conn, business_id = self.port.conn, self.port.business_id
        actor = self.port.actor = replace(self.port.actor, language=turn.state.language)
        for task in turn.tasks:
            store.create_staff_task(
                conn,
                business_id,
                kind=TASK_KINDS.get(task.kind, "other"),
                urgency="urgent" if task.urgent else "normal",
                summary=task.reason,
                actor=actor,
                now=now,
                client_id=self.port.client_id,
            )
            self.port.log.append(f"staff_task {task.kind}{' (urgent)' if task.urgent else ''}")
        for event in turn.events:
            if event.type in _WRITTEN_WITH_TASK and turn.tasks:
                continue
            record_event(
                conn,
                FunnelEvent(
                    business_id=business_id,
                    type=event.type,
                    occurred_at=now,
                    client_id=self.port.client_id,
                    channel=actor.channel,
                    language=turn.inbound_language,
                    source=self.source,
                    properties=event.properties,
                ),
            )


def open_conversation(
    conn: psycopg.Connection,
    business: store.Business,
    client: Row,
    *,
    channel: Channel = Channel.API,
    language: Language | None = None,
) -> Conversation:
    """Engine and port for one client. Raises ValueError if the clinic has no contact phone
    (every emergency reply gives it)."""
    lang: Language = language or client.get("preferred_language") or business.default_language
    port = DbScheduling(
        conn=conn,
        business_id=business.id,
        client_id=client["id"],
        actor=Actor(type="client", channel=channel, language=lang),
    )
    engine = FlowEngine(
        load_pack_by_id(business.pack_id),
        port,
        FlowContext(
            business_name=business.name,
            client_name=client["name"],
            default_language=business.default_language,
            values={"clinic_phone": business.contact_phone or ""},
        ),
    )
    return Conversation(engine=engine, port=port, state=engine.new_state(lang))
