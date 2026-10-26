"""What the flow engine needs from scheduling, bound to one business and one client.

Two implementations: `memory.InMemoryScheduling` (dev console, tests) and a database adapter
over salesbot_api.booking (Stage C). Method semantics follow salesbot_api.booking exactly:
a hold reserves a slot for settings.hold_minutes; booking a reschedule hold either moves the
appointment at once or, with reschedule_approval = staff, leaves it pending approval while the
original appointment stays.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from salesbot_core.flow.models import Hold
from salesbot_core.packs.schema import Language
from salesbot_core.scheduling import (
    AppointmentStatus,
    SchedulingSettings,
    Scope,
    Slot,
    VisitType,
    WeeklyWindow,
)


class SchedulingError(Exception):
    pass


class SlotUnavailable(SchedulingError):
    """The time is taken, outside working hours, too soon or in the past."""


class HoldExpired(SchedulingError):
    """The hold ran out before the patient said yes."""


class NotActive(SchedulingError):
    """The appointment can no longer be confirmed, cancelled or rescheduled."""


class Appointment(BaseModel):
    id: UUID
    practitioner_id: UUID
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    visit: VisitType


class SchedulingPort(Protocol):
    @property
    def settings(self) -> SchedulingSettings: ...

    def visit_type(self) -> VisitType:
        """First visit until the client has had an appointment, then consultation."""
        ...

    def open_days(
        self, scope: Scope, *, now: datetime, from_day: date | None = None, limit: int | None = None
    ) -> list[date]: ...

    def open_slots(self, scope: Scope, *, day: date, now: datetime) -> list[Slot]: ...

    def first_opening(self, scope: Scope, *, now: datetime) -> tuple[Slot | None, bool]:
        """(earliest slot, whether it falls inside the lookahead window)."""
        ...

    def hold(
        self, scope: Scope, *, starts_at: datetime, now: datetime, reschedule_of: UUID | None = None
    ) -> Hold: ...

    def release(self, hold_id: UUID) -> None: ...

    def book(self, hold_id: UUID, *, now: datetime) -> Appointment:
        """Returns the new appointment: booked, or pending_approval for a staff-approved
        reschedule."""
        ...

    def upcoming(self, *, now: datetime) -> Appointment | None:
        """The client's next active appointment."""
        ...

    def get(self, appointment_id: UUID) -> Appointment | None: ...

    def confirm(self, appointment_id: UUID, *, now: datetime) -> Appointment: ...

    def cancel(self, appointment_id: UUID, *, now: datetime) -> Appointment: ...

    def practitioner_name(self, practitioner_id: UUID, language: Language) -> str: ...

    def weekly_hours(self) -> list[tuple[UUID, list[WeeklyWindow]]]:
        """Each active practitioner's weekly working hours, in a stable order."""
        ...
