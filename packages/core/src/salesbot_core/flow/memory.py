"""In-memory scheduling for one client: the dev console and flow tests.

Follows salesbot_api.booking's rules (holds, expiry, practitioner selection, reschedules with
auto or staff approval) on top of the same pure availability engine. It keeps no funnel events:
the database adapter gets those from the booking engine. `log` records what happened, for the
console.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from salesbot_core.flow.models import Hold
from salesbot_core.flow.ports import Appointment, HoldExpired, NotActive, SlotUnavailable
from salesbot_core.packs.schema import Language
from salesbot_core.scheduling import (
    ACTIVE_STATUSES,
    BLOCKING_STATUSES,
    AppointmentStatus,
    Interval,
    PractitionerCalendar,
    SchedulingSettings,
    Scope,
    Slot,
    VisitType,
    WeeklyWindow,
    days_with_openings,
    distinct_times,
    next_opening,
    slots_on_day,
)
from salesbot_core.scheduling.settings import PractitionerSelection, RescheduleApproval


@dataclass
class _Row:
    id: UUID
    practitioner_id: UUID
    client_id: UUID
    visit: VisitType
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    hold_expires_at: datetime | None = None
    rescheduled_from_id: UUID | None = None

    def public(self) -> Appointment:
        return Appointment(
            id=self.id,
            practitioner_id=self.practitioner_id,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            status=self.status,
            visit=self.visit,
        )


@dataclass
class InMemoryScheduling:
    """`calendars` hold weekly hours, breaks and closures; bookings live in `rows`."""

    settings: SchedulingSettings
    calendars: list[PractitionerCalendar]
    names: dict[UUID, dict[Language, str]]
    client_id: UUID = field(default_factory=uuid4)
    rows: dict[UUID, _Row] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    # -- availability -------------------------------------------------------

    def _ids(self, scope: Scope) -> list[UUID]:
        if scope.practitioner_id is not None:
            return [scope.practitioner_id]
        if self.settings.practitioner_selection is PractitionerSelection.DEFAULT_PRACTITIONER:
            # SchedulingSettings guarantees the id is set for this selection.
            return [self.settings.default_practitioner_id]  # type: ignore[list-item]
        return [c.practitioner_id for c in self.calendars]

    def _live(self, row: _Row, now: datetime) -> bool:
        if row.status is AppointmentStatus.HELD:
            return row.hold_expires_at is not None and row.hold_expires_at > now
        return row.status in BLOCKING_STATUSES

    def _calendars(self, scope: Scope, now: datetime) -> list[PractitionerCalendar]:
        wanted = self._ids(scope)
        out = []
        for cal in self.calendars:
            if cal.practitioner_id not in wanted:
                continue
            busy = [
                Interval(start=r.starts_at, end=r.ends_at)
                for r in self.rows.values()
                if r.practitioner_id == cal.practitioner_id and self._live(r, now)
            ]
            out.append(cal.model_copy(update={"busy": [*cal.busy, *busy]}))
        return out

    def _common(self, scope: Scope, now: datetime) -> dict:
        return {
            "visit": scope.visit,
            "now": now,
            "tz": self.tz,
            "min_lead": self.settings.min_lead,
            "step_minutes": self.settings.slot_step_minutes,
        }

    def visit_type(self) -> VisitType:
        seen = any(
            r.client_id == self.client_id
            and r.status not in (AppointmentStatus.HELD, AppointmentStatus.CANCELLED)
            for r in self.rows.values()
        )
        return VisitType.CONSULTATION if seen else VisitType.FIRST_VISIT

    def open_days(
        self, scope: Scope, *, now: datetime, from_day: date | None = None, limit: int | None = None
    ) -> list[date]:
        return days_with_openings(
            self._calendars(scope, now),
            from_day or now.astimezone(self.tz).date(),
            limit=limit or self.settings.days_per_list,
            **self._common(scope, now),
        )

    def open_slots(self, scope: Scope, *, day: date, now: datetime) -> list[Slot]:
        return distinct_times(
            slots_on_day(self._calendars(scope, now), day, **self._common(scope, now))
        )

    def first_opening(self, scope: Scope, *, now: datetime) -> tuple[Slot | None, bool]:
        slot = next_opening(self._calendars(scope, now), **self._common(scope, now))
        if slot is None:
            return None, False
        horizon = datetime.combine(
            now.astimezone(self.tz).date() + timedelta(days=self.settings.lookahead_days),
            datetime.min.time(),
            self.tz,
        )
        return slot, slot.starts_at < horizon

    # -- holds and bookings -------------------------------------------------

    def hold(
        self, scope: Scope, *, starts_at: datetime, now: datetime, reschedule_of: UUID | None = None
    ) -> Hold:
        if reschedule_of is not None:
            original = self.rows.get(reschedule_of)
            if original is None or original.status not in ACTIVE_STATUSES:
                raise NotActive("cannot reschedule this appointment")
            scope = Scope(
                visit=original.visit,
                practitioner_id=scope.practitioner_id or original.practitioner_id,
            )
        day = starts_at.astimezone(self.tz).date()
        for slot in slots_on_day(self._calendars(scope, now), day, **self._common(scope, now)):
            if slot.starts_at != starts_at:
                continue
            row = _Row(
                id=uuid4(),
                practitioner_id=slot.practitioner_id,
                client_id=self.client_id,
                visit=scope.visit,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                status=AppointmentStatus.HELD,
                hold_expires_at=now + self.settings.hold,
                rescheduled_from_id=reschedule_of,
            )
            self.rows[row.id] = row
            self.log.append(f"hold {row.id.hex[:8]} {row.starts_at.isoformat()}")
            return Hold(
                id=row.id,
                practitioner_id=row.practitioner_id,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                expires_at=row.hold_expires_at,
            )
        raise SlotUnavailable("that time is no longer available")

    def release(self, hold_id: UUID) -> None:
        row = self.rows.get(hold_id)
        if row is not None and row.status is AppointmentStatus.HELD:
            del self.rows[hold_id]
            self.log.append(f"release {hold_id.hex[:8]}")

    def book(self, hold_id: UUID, *, now: datetime) -> Appointment:
        row = self.rows.get(hold_id)
        if row is None or row.status is not AppointmentStatus.HELD:
            raise HoldExpired("no such hold")
        if row.hold_expires_at is None or row.hold_expires_at <= now:
            del self.rows[hold_id]
            raise HoldExpired("the hold expired")
        row.hold_expires_at = None
        if row.rescheduled_from_id is None:
            row.status = AppointmentStatus.BOOKED
            self.log.append(f"booking_created {row.id.hex[:8]}")
            return row.public()
        original = self.rows[row.rescheduled_from_id]
        if original.status not in ACTIVE_STATUSES:
            del self.rows[hold_id]
            raise NotActive("original appointment is no longer active")
        if self.settings.reschedule_approval is RescheduleApproval.STAFF:
            row.status = AppointmentStatus.PENDING_APPROVAL
            self.log.append(f"reschedule_requested {row.id.hex[:8]}")
            return row.public()
        original.status = AppointmentStatus.RESCHEDULED
        row.status = AppointmentStatus.BOOKED
        self.log.append(f"appointment_rescheduled {original.id.hex[:8]} -> {row.id.hex[:8]}")
        return row.public()

    def upcoming(self, *, now: datetime) -> Appointment | None:
        active = [
            r
            for r in self.rows.values()
            if r.client_id == self.client_id and r.status in ACTIVE_STATUSES and r.starts_at > now
        ]
        return min(active, key=lambda r: r.starts_at).public() if active else None

    def get(self, appointment_id: UUID) -> Appointment | None:
        row = self.rows.get(appointment_id)
        return row.public() if row else None

    def confirm(self, appointment_id: UUID, *, now: datetime) -> Appointment:
        row = self.rows.get(appointment_id)
        if row is None or row.status not in ACTIVE_STATUSES:
            raise NotActive("cannot confirm this appointment")
        if row.status is not AppointmentStatus.CONFIRMED:
            row.status = AppointmentStatus.CONFIRMED
            self.log.append(f"appointment_confirmed {row.id.hex[:8]}")
        return row.public()

    def cancel(self, appointment_id: UUID, *, now: datetime) -> Appointment:
        row = self.rows.get(appointment_id)
        if row is None or row.status not in ACTIVE_STATUSES:
            raise NotActive("cannot cancel this appointment")
        row.status = AppointmentStatus.CANCELLED
        self.log.append(f"booking_cancelled {row.id.hex[:8]}")
        return row.public()

    def practitioner_name(self, practitioner_id: UUID, language: Language) -> str:
        names = self.names.get(practitioner_id, {})
        return names.get(language) or names.get("en") or next(iter(names.values()), "")

    def weekly_hours(self) -> list[tuple[UUID, list[WeeklyWindow]]]:
        return [(c.practitioner_id, list(c.work)) for c in self.calendars]

    # -- setup helpers (console, tests) -------------------------------------

    def add_booking(self, slot: Slot, visit: VisitType = VisitType.CONSULTATION) -> Appointment:
        row = _Row(
            id=uuid4(),
            practitioner_id=slot.practitioner_id,
            client_id=self.client_id,
            visit=visit,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            status=AppointmentStatus.BOOKED,
        )
        self.rows[row.id] = row
        return row.public()
