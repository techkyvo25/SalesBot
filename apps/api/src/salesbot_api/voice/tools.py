"""API tools and lifecycle hooks the Sarvam voice agent calls (docs/SARVAM_AGENT.md).

    POST /api/v1/voice/tools/<tool>     identify_patient, get_availability, hold_slot, book,
                                         reschedule, cancel, confirm_attendance,
                                         create_staff_task
    POST /api/v1/voice/hooks/on-start   agent variables before the agent speaks
    POST /api/v1/voice/hooks/on-end     what happened, when the call ends

Every tool answers 200 with a ToolResult: `ok`, a stable `code`, `spoken_text` in the call's
language (from the pack's voice.messages; any line about a time has the weekday, date and
time in the clinic's timezone), `next_step` for the agent, and structured `data`. Failures
use the same shape with a safe line and never expose internals. Tool calls are idempotent per
(interaction_id, tool, request hash): a retried call returns the stored result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dtime
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from salesbot_api import booking
from salesbot_api.api_v1 import get_conn
from salesbot_api.records import Actor
from salesbot_api.settings import Settings, get_settings
from salesbot_api.voice import store
from salesbot_api.voice.security import mask_phone, normalize_phone, require_tool_auth
from salesbot_core.costs import call_cost
from salesbot_core.events import Channel, EventType
from salesbot_core.packs import Pack, load_all_packs
from salesbot_core.packs.schema import Language
from salesbot_core.scheduling import Scope, Slot, VisitType
from salesbot_core.voice import SARVAM_LANGUAGE_NAMES, Spoken, check_voice_pack, language_code
from salesbot_core.voice import say as pack_say
from salesbot_core.voice import when as spoken_when

log = logging.getLogger("salesbot.voice")
router = APIRouter(
    prefix="/api/v1/voice", tags=["voice"], dependencies=[Depends(require_tool_auth)]
)

ConnDep = Annotated[psycopg.Connection, Depends(get_conn)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
MAX_OPTIONS = 3
# Part of the day in the clinic's local time.
PARTS: dict[str, tuple[dtime, dtime]] = {
    "morning": (dtime(5), dtime(12)),
    "afternoon": (dtime(12), dtime(16)),
    "evening": (dtime(16), dtime(23, 59)),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


NowDep = Annotated[datetime, Depends(utcnow)]
Day = date  # a request field is called `date`


def _blank_to_none(value: Any) -> Any:
    # Unset Sarvam variables arrive as "" (or an unresolved "@name"); treat both as missing.
    if isinstance(value, str) and (not value.strip() or value.strip().startswith("@")):
        return None
    return value


Opt = BeforeValidator(_blank_to_none)


# ---------------------------------------------------------------------------
# Request and response models
# ---------------------------------------------------------------------------


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    clinic: str = Field(min_length=1, max_length=100)
    interaction_id: str = Field(min_length=1, max_length=200)
    caller_phone: Annotated[str | None, Opt] = None
    language: Annotated[str | None, Opt] = None


class IdentifyIn(ToolRequest):
    patient_id: Annotated[UUID | None, Opt] = None


class PatientIn(ToolRequest):
    patient_id: Annotated[UUID | None, Opt] = None


class AvailabilityIn(PatientIn):
    date: Annotated[Day | None, Opt] = None
    part_of_day: Annotated[Literal["morning", "afternoon", "evening"] | None, Opt] = None
    next_available: bool = False
    for_reschedule: bool = False


class HoldIn(PatientIn):
    starts_at: AwareDatetime
    practitioner_id: UUID


class BookIn(PatientIn):
    hold_id: UUID


class RescheduleIn(PatientIn):
    appointment_id: UUID
    starts_at: AwareDatetime
    practitioner_id: Annotated[UUID | None, Opt] = None


class CancelIn(PatientIn):
    appointment_id: UUID
    confirmed: bool = False


class ConfirmIn(PatientIn):
    appointment_id: UUID


TaskReason = Literal[
    "callback", "emergency", "question", "reschedule", "cancel", "complaint", "other"
]


class StaffTaskIn(PatientIn):
    reason: TaskReason
    summary: str = Field(min_length=1, max_length=2000)
    urgency: Literal["normal", "urgent"] = "normal"


class ToolResult(BaseModel):
    ok: bool
    code: str
    spoken_text: str
    language: Language
    next_step: str | None = None
    # True when spoken_text is DRAFT (unreviewed); voice tests use the team's phones only.
    draft: bool = False
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


class ToolFailureError(Exception):
    """An expected failure, spoken to the caller with a safe line from the pack."""

    def __init__(self, code: str, key: str, next_step: str | None = None, **values: Any) -> None:
        super().__init__(code)
        self.code, self.key, self.next_step, self.values = code, key, next_step, values


@dataclass
class Ctx:
    conn: psycopg.Connection
    settings: Settings
    business: store.Business
    pack: Pack
    language: Language
    call_id: UUID
    interaction_id: str
    caller_phone: str | None
    now: datetime

    @property
    def actor(self) -> Actor:
        return Actor(
            type="client", channel=Channel.VOICE, language=self.language, call_id=self.call_id
        )

    def say(self, key: str, **values: Any) -> Spoken:
        return pack_say(self.pack, key, self.language, **values)

    def result(
        self,
        key: str,
        *,
        code: str = "ok",
        ok: bool = True,
        next_step: str | None = None,
        data: dict[str, Any] | None = None,
        prefix: Spoken | None = None,
        **values: Any,
    ) -> ToolResult:
        spoken = self.say(key, **values)
        text = f"{prefix.text} {spoken.text}" if prefix else spoken.text
        return ToolResult(
            ok=ok,
            code=code,
            spoken_text=text,
            language=spoken.language,
            next_step=next_step,
            draft=spoken.draft or bool(prefix and prefix.draft),
            data=data or {},
        )

    def when(self, starts_at: datetime) -> dict[str, str]:
        return spoken_when(starts_at, self.business.timezone, self.language)

    def doctor(self, practitioner_id: UUID) -> str:
        names = store.practitioner_names(
            self.conn, self.business.id, [practitioner_id], self.language
        )
        return names.get(practitioner_id, "")

    def slot_values(self, appt: dict[str, Any]) -> dict[str, str]:
        return {"doctor": self.doctor(appt["practitioner_id"]), **self.when(appt["starts_at"])}


def resolve_language(
    requested: str | None, preferred: str | None, business: store.Business
) -> Language:
    for candidate in (language_code(requested), language_code(preferred)):
        if candidate and candidate in business.allowed_languages:
            return candidate
    return business.default_language


def _pack_for(business: store.Business) -> Pack:
    pack = load_all_packs().get(business.pack_id)
    if pack is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "clinic pack not found")
    check_voice_pack(pack)
    return pack


def _business(conn: psycopg.Connection, slug: str) -> store.Business:
    business = store.business_by_slug(conn, slug)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown clinic")
    return business


def request_hash(req: BaseModel) -> str:
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_tool(
    tool: str,
    req: ToolRequest,
    conn: psycopg.Connection,
    settings: Settings,
    now: datetime,
    handler: Callable[[Ctx, Any], ToolResult],
) -> ToolResult:
    business = _business(conn, req.clinic)
    pack = _pack_for(business)
    caller = normalize_phone(req.caller_phone)
    call_id, created = store.ensure_call(
        conn, business.id, req.interaction_id, caller_phone=caller, now=now
    )
    patient_id = getattr(req, "patient_id", None)
    client = store.get_client(conn, business.id, patient_id) if patient_id else None
    language = resolve_language(
        req.language, client["preferred_language"] if client else None, business
    )
    ctx = Ctx(conn, settings, business, pack, language, call_id, req.interaction_id, caller, now)
    if created:
        store.call_event(
            conn,
            business.id,
            EventType.CALL_RECEIVED,
            call_id=call_id,
            interaction_id=req.interaction_id,
            now=now,
            language=language,
        )

    row_id, stored = store.begin_tool_call(
        conn,
        business.id,
        call_id=call_id,
        interaction_id=req.interaction_id,
        tool=tool,
        request_hash=request_hash(req),
        request=req.model_dump(mode="json"),
    )
    if row_id is None:
        if stored is not None:
            return ToolResult.model_validate(stored)
        return ctx.result("try_again", ok=False, code="in_progress", next_step="wait_and_retry")

    started = time.monotonic()
    try:
        result = handler(ctx, req)
    except ToolFailureError as fail:
        result = ctx.result(
            fail.key, ok=False, code=fail.code, next_step=fail.next_step, **fail.values
        )
    except Exception as exc:
        # Never leak internals to the caller or personal data to logs.
        # Type and code location only: exception messages can carry personal data.
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        log.error(
            "voice tool %s failed (%s at %s:%s)",
            tool, type(exc).__name__, frame.filename.rsplit("salesbot", 1)[-1], frame.lineno,
        )  # fmt: skip
        store.clear_tool_call(conn, row_id)
        return ctx.result(
            "try_again", ok=False, code="internal_error", next_step="create_staff_task"
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    store.finish_tool_call(
        conn, row_id, result.model_dump(mode="json"), ok=result.ok, duration_ms=duration_ms
    )
    store.call_event(
        conn,
        business.id,
        EventType.VOICE_TOOL_CALLED,
        call_id=call_id,
        interaction_id=req.interaction_id,
        now=now,
        language=result.language,
        client_id=client["id"] if client else None,
        tool=tool,
        ok=result.ok,
        code=result.code,
        duration_ms=duration_ms,
    )
    log.info("voice tool %s -> %s in %d ms", tool, result.code, duration_ms)
    return result


def _patient(ctx: Ctx, patient_id: UUID | None) -> dict[str, Any]:
    """The patient for this call. On inbound calls (caller number known) the patient must be
    registered on that number, so one caller cannot act on another patient's record."""
    if patient_id is None:
        raise ToolFailureError("patient_required", "try_again", "identify_patient")
    client = store.get_client(ctx.conn, ctx.business.id, patient_id)
    if client is None:
        raise ToolFailureError("unknown_patient", "try_again", "identify_patient")
    if ctx.caller_phone and ctx.caller_phone not in (client["phone"], client["caregiver_phone"]):
        raise ToolFailureError("unknown_patient", "try_again", "identify_patient")
    store.link_call_client(ctx.conn, ctx.call_id, client)
    return client


def _own_appointment(ctx: Ctx, client: dict[str, Any], appointment_id: UUID) -> dict[str, Any]:
    appt = store.get_appointment(ctx.conn, ctx.business.id, appointment_id)
    if appt is None or appt["client_id"] != client["id"] or not store.is_active(appt):
        raise ToolFailureError("no_upcoming", "no_upcoming", "ask_what_next")
    return appt


def _slot_data(ctx: Ctx, appt: dict[str, Any]) -> dict[str, Any]:
    return {
        "appointment_id": str(appt["id"]),
        "status": appt["status"],
        "starts_at": appt["starts_at"].isoformat(),
        "practitioner_id": str(appt["practitioner_id"]),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


IST = ZoneInfo("Asia/Kolkata")


def _today(now: datetime) -> dict[str, str]:
    """Today in India, for the agent (it has no reliable clock of its own)."""
    local = now.astimezone(IST)
    return {
        "today_weekday": local.strftime("%A"),
        "today_date": local.date().isoformat(),
        "time_now": local.strftime("%H:%M"),
        "timezone": "Asia/Kolkata (IST)",
    }


def _identify(ctx: Ctx, req: IdentifyIn) -> ToolResult:
    if req.patient_id is not None and not ctx.caller_phone:
        # Outbound: we started the call, so the patient is known from our own variables.
        candidates = [c for c in [store.get_client(ctx.conn, ctx.business.id, req.patient_id)] if c]
    elif ctx.caller_phone:
        candidates = store.clients_for_phone(ctx.conn, ctx.business.id, ctx.caller_phone)
    else:
        candidates = []
    if req.patient_id is not None:
        candidates = [c for c in candidates if c["id"] == req.patient_id]
    if not candidates:
        return ctx.result(
            "not_registered",
            ok=False,
            code="not_registered",
            next_step="offer_callback",
            data=_today(ctx.now),
        )
    if len(candidates) > 1:
        return ctx.result(
            "choose_patient",
            code="choose_patient",
            next_step="ask_which_patient",
            names=", ".join(c["name"] for c in candidates),
            data={
                "candidates": [{"patient_id": str(c["id"]), "name": c["name"]} for c in candidates],
                **_today(ctx.now),
            },
        )
    client = candidates[0]
    store.link_call_client(ctx.conn, ctx.call_id, client)
    data: dict[str, Any] = {
        "patient_id": str(client["id"]),
        "patient_name": client["name"],
        "preferred_language": SARVAM_LANGUAGE_NAMES.get(client["preferred_language"], "English"),
        "appointment_id": "",
        **_today(ctx.now),
    }
    upcoming = store.upcoming_appointment(ctx.conn, ctx.business.id, client["id"], ctx.now)
    if upcoming is None:
        return ctx.result("identified", data=data, client_name=client["name"])
    data.update(_slot_data(ctx, upcoming))
    return ctx.result(
        "identified_upcoming", data=data, client_name=client["name"], **ctx.slot_values(upcoming)
    )


def _in_part(slot: Slot, part: str | None, tz: str) -> bool:
    if part is None:
        return True
    start, end = PARTS[part]
    local = slot.starts_at.astimezone(ZoneInfo(tz)).time()
    return start <= local < end


def _availability(ctx: Ctx, req: AvailabilityIn) -> ToolResult:
    client = _patient(ctx, req.patient_id)
    conn, bid = ctx.conn, ctx.business.id
    settings = booking.load_settings(conn, bid)
    tz = settings.timezone
    if req.for_reschedule:
        appt = store.upcoming_appointment(conn, bid, client["id"], ctx.now)
        if appt is None:
            raise ToolFailureError("no_upcoming", "no_upcoming", "ask_what_next")
        scope = Scope(visit=VisitType(appt["visit_type"]), practitioner_id=appt["practitioner_id"])
    else:
        visit = (
            VisitType.CONSULTATION
            if store.has_past_visits(conn, bid, client["id"])
            else VisitType.FIRST_VISIT
        )
        scope = Scope(visit=visit)

    today = ctx.now.astimezone(ZoneInfo(tz)).date()
    wanted = max(req.date, today) if req.date and not req.next_available else None
    prefix: Spoken | None = None
    slots: list[Slot] = []
    if wanted is not None:
        slots = [
            s
            for s in booking.open_slots(conn, bid, day=wanted, scope=scope, now=ctx.now)
            if _in_part(s, req.part_of_day, tz)
        ]
        if not slots:
            prefix = ctx.say("nothing_then")
    if not slots:
        days = booking.open_days(
            conn,
            bid,
            scope=scope,
            now=ctx.now,
            from_day=wanted or today,
            limit=settings.days_per_list,
        )
        for part in [req.part_of_day, None] if req.part_of_day else [None]:
            for day in days:
                slots = [
                    s
                    for s in booking.open_slots(conn, bid, day=day, scope=scope, now=ctx.now)
                    if _in_part(s, part, tz)
                ]
                if slots:
                    break
            if slots:
                break
    if not slots:
        raise ToolFailureError(
            "no_openings", "no_openings", "create_staff_task", days=settings.lookahead_days
        )

    chosen = slots[:MAX_OPTIONS]
    names = store.practitioner_names(conn, bid, [s.practitioner_id for s in chosen], ctx.language)
    phrases = [
        ctx.say("option", doctor=names.get(s.practitioner_id, ""), **ctx.when(s.starts_at)).text
        for s in chosen
    ]
    options = [
        {
            "starts_at": s.starts_at.isoformat(),
            "practitioner_id": str(s.practitioner_id),
            "doctor": names.get(s.practitioner_id, ""),
            "label": phrase,
        }
        for s, phrase in zip(chosen, phrases, strict=True)
    ]
    return ctx.result(
        "options",
        prefix=prefix,
        next_step="ask_which_option",
        data={"options": options},
        options="; ".join(phrases),
    )


def _hold(ctx: Ctx, req: HoldIn) -> ToolResult:
    client = _patient(ctx, req.patient_id)
    visit = (
        VisitType.CONSULTATION
        if store.has_past_visits(ctx.conn, ctx.business.id, client["id"])
        else VisitType.FIRST_VISIT
    )
    try:
        hold = booking.hold_slot(
            ctx.conn,
            ctx.business.id,
            client_id=client["id"],
            starts_at=req.starts_at,
            scope=Scope(visit=visit, practitioner_id=req.practitioner_id),
            actor=ctx.actor,
            now=ctx.now,
        )
    except booking.SlotUnavailableError as exc:
        raise ToolFailureError(
            "slot_unavailable", "slot_taken", "get_availability", **ctx.when(req.starts_at)
        ) from exc
    except booking.NotFoundError as exc:
        raise ToolFailureError("invalid_option", "try_again", "get_availability") from exc
    data = {
        "hold_id": str(hold["id"]),
        "hold_expires_at": hold["hold_expires_at"].isoformat(),
        **_slot_data(ctx, hold),
    }
    return ctx.result("held", next_step="ask_to_book", data=data, **ctx.slot_values(hold))


def _book(ctx: Ctx, req: BookIn) -> ToolResult:
    client = _patient(ctx, req.patient_id)
    hold = store.get_appointment(ctx.conn, ctx.business.id, req.hold_id)
    if hold is None or hold["client_id"] != client["id"]:
        raise ToolFailureError("hold_not_found", "hold_expired", "get_availability")
    if hold["status"] in ("booked", "confirmed"):
        booked = hold  # already booked (a retry with a different body): say so again
    else:
        try:
            booked = booking.book_hold(
                ctx.conn, ctx.business.id, req.hold_id, actor=ctx.actor, now=ctx.now
            )
        except booking.HoldExpiredError as exc:
            raise ToolFailureError("hold_expired", "hold_expired", "get_availability") from exc
        except booking.InvalidStateError as exc:
            raise ToolFailureError("invalid_state", "hold_expired", "get_availability") from exc
    # Confirm only after the commit: book_hold has committed when it returns.
    if booked["status"] == "pending_approval":
        return ctx.result(
            "reschedule_pending",
            code="pending_approval",
            data=_slot_data(ctx, booked),
            **ctx.when(booked["starts_at"]),
        )
    return ctx.result("booked", data=_slot_data(ctx, booked), **ctx.slot_values(booked))


def _reschedule(ctx: Ctx, req: RescheduleIn) -> ToolResult:
    client = _patient(ctx, req.patient_id)
    original = _own_appointment(ctx, client, req.appointment_id)
    try:
        hold = booking.hold_slot(
            ctx.conn,
            ctx.business.id,
            client_id=client["id"],
            starts_at=req.starts_at,
            scope=Scope(practitioner_id=req.practitioner_id),
            actor=ctx.actor,
            now=ctx.now,
            reschedule_of=original["id"],
        )
    except booking.SlotUnavailableError as exc:
        raise ToolFailureError(
            "slot_unavailable", "slot_taken", "get_availability", **ctx.when(req.starts_at)
        ) from exc
    except booking.BookingError as exc:
        raise ToolFailureError(exc.code, "try_again", "create_staff_task") from exc
    try:
        # One transaction moves both rows, links old -> new and stops the old reminders.
        moved = booking.book_hold(
            ctx.conn, ctx.business.id, hold["id"], actor=ctx.actor, now=ctx.now
        )
    except booking.BookingError as exc:
        booking.release_hold(ctx.conn, ctx.business.id, hold["id"])
        raise ToolFailureError(exc.code, "try_again", "create_staff_task") from exc
    if moved["status"] == "pending_approval":
        return ctx.result(
            "reschedule_pending",
            code="pending_approval",
            data=_slot_data(ctx, moved),
            **ctx.when(moved["starts_at"]),
        )
    data = {**_slot_data(ctx, moved), "previous_appointment_id": str(original["id"])}
    return ctx.result("rescheduled", data=data, **ctx.slot_values(moved))


def _cancel(ctx: Ctx, req: CancelIn) -> ToolResult:
    client = _patient(ctx, req.patient_id)
    appt = _own_appointment(ctx, client, req.appointment_id)
    if not req.confirmed:
        return ctx.result(
            "cancel_ask",
            ok=False,
            code="needs_confirmation",
            next_step="ask_confirmation",
            data=_slot_data(ctx, appt),
            **ctx.slot_values(appt),
        )
    try:
        cancelled = booking.cancel(
            ctx.conn,
            ctx.business.id,
            appt["id"],
            actor=ctx.actor,
            now=ctx.now,
            reason="caller asked on a call",
        )
    except booking.InvalidStateError as exc:
        raise ToolFailureError("no_upcoming", "no_upcoming", "ask_what_next") from exc
    return ctx.result("cancelled", data=_slot_data(ctx, cancelled), **ctx.slot_values(cancelled))


def _confirm(ctx: Ctx, req: ConfirmIn) -> ToolResult:
    client = _patient(ctx, req.patient_id)
    appt = _own_appointment(ctx, client, req.appointment_id)
    try:
        confirmed = booking.confirm(
            ctx.conn, ctx.business.id, appt["id"], actor=ctx.actor, now=ctx.now
        )
    except booking.InvalidStateError as exc:
        raise ToolFailureError("no_upcoming", "no_upcoming", "ask_what_next") from exc
    return ctx.result("confirmed", data=_slot_data(ctx, confirmed), **ctx.slot_values(confirmed))


def _staff_task(ctx: Ctx, req: StaffTaskIn) -> ToolResult:
    client = _patient(ctx, req.patient_id) if req.patient_id else None
    urgent = req.reason == "emergency" or req.urgency == "urgent"
    task_id = store.create_staff_task(
        ctx.conn,
        ctx.business.id,
        kind=req.reason,
        urgency="urgent" if urgent else "normal",
        summary=req.summary[:1000],
        actor=ctx.actor,
        now=ctx.now,
        client_id=client["id"] if client else None,
    )
    data = {"task_id": str(task_id)}
    if not urgent:
        return ctx.result("task_created", data=data)
    numbers = " / ".join(ctx.business.emergency_numbers)
    return ctx.result(
        "task_created_urgent",
        code="urgent_task_created",
        data=data,
        emergency_numbers=numbers,
        clinic_phone=ctx.business.contact_phone or numbers,
    )


def _endpoint(
    tool: str, model: type[ToolRequest], handler: Callable[[Ctx, Any], ToolResult]
) -> None:
    def endpoint(req: ToolRequest, conn: ConnDep, settings: SettingsDep, now: NowDep) -> ToolResult:
        return run_tool(tool, req, conn, settings, now, handler)

    # The request model differs per tool; FastAPI reads it from the annotation.
    endpoint.__annotations__["req"] = model
    endpoint.__name__ = f"voice_tool_{tool}"
    router.post(f"/tools/{tool}", response_model=ToolResult)(endpoint)


TOOLS: dict[str, tuple[type[ToolRequest], Callable[[Ctx, Any], ToolResult]]] = {
    "identify_patient": (IdentifyIn, _identify),
    "get_availability": (AvailabilityIn, _availability),
    "hold_slot": (HoldIn, _hold),
    "book": (BookIn, _book),
    "reschedule": (RescheduleIn, _reschedule),
    "cancel": (CancelIn, _cancel),
    "confirm_attendance": (ConfirmIn, _confirm),
    "create_staff_task": (StaffTaskIn, _staff_task),
}
for _name, (_model, _handler) in TOOLS.items():
    _endpoint(_name, _model, _handler)


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class OnStartIn(ToolRequest):
    # Set when we started the call (outbound): our calls.id and the patient.
    call_ref: Annotated[UUID | None, Opt] = None
    patient_id: Annotated[UUID | None, Opt] = None


@router.post("/hooks/on-start")
def on_start(req: OnStartIn, conn: ConnDep, now: NowDep) -> dict[str, Any]:
    """Agent variables before the agent speaks. Map each field to the agent variable of the
    same name in Sarvam's on-start tool."""
    business = _business(conn, req.clinic)
    caller = normalize_phone(req.caller_phone)
    call_id, created = store.ensure_call(
        conn, business.id, req.interaction_id, caller_phone=caller, now=now, call_ref=req.call_ref
    )
    candidates: list[dict[str, Any]] = []
    if req.patient_id is not None:
        found = store.get_client(conn, business.id, req.patient_id)
        candidates = [found] if found else []
    elif caller:
        candidates = store.clients_for_phone(conn, business.id, caller)
    client = candidates[0] if len(candidates) == 1 else None
    language = resolve_language(
        req.language, client["preferred_language"] if client else None, business
    )
    variables: dict[str, Any] = {
        "clinic_slug": business.slug,
        "clinic_name": business.name,
        "clinic_phone": business.contact_phone or "",
        "emergency_numbers": " / ".join(business.emergency_numbers),
        "patient_id": "",
        "patient_name": "",
        "patients_on_number": len(candidates),
        "preferred_language": SARVAM_LANGUAGE_NAMES[language],
        "appointment_id": "",
        "upcoming_appointment": "",
    }
    if client is not None:
        store.link_call_client(conn, call_id, client)
        variables.update(patient_id=str(client["id"]), patient_name=client["name"])
        appt = store.upcoming_appointment(conn, business.id, client["id"], now)
        if appt is not None:
            pack = _pack_for(business)
            ctx = Ctx(
                conn,
                get_settings(),
                business,
                pack,
                language,
                call_id,
                req.interaction_id,
                caller,
                now,
            )
            v = ctx.slot_values(appt)
            variables.update(
                appointment_id=str(appt["id"]),
                upcoming_appointment=pack_say(pack, "option", language, **v).text,
            )
    if created or req.call_ref is not None:
        store.call_event(
            conn,
            business.id,
            EventType.CALL_RECEIVED,
            call_id=call_id,
            interaction_id=req.interaction_id,
            now=now,
            language=language,
            client_id=client["id"] if client else None,
        )
    log.info(
        "voice on-start: caller %s, %d patient(s) on number", mask_phone(caller), len(candidates)
    )
    return variables


class OnEndIn(ToolRequest):
    call_ref: Annotated[UUID | None, Opt] = None
    patient_id: Annotated[UUID | None, Opt] = None
    # Call-context "Call Length" (seconds) and "Call Transcript"; output agent variables.
    call_length: Annotated[float | None, Opt] = None
    transcript: Any = None
    outcome: Annotated[str | None, Opt] = Field(default=None, max_length=100)
    summary: Annotated[str | None, Opt] = Field(default=None, max_length=4000)


@router.post("/hooks/on-end")
def on_end(req: OnEndIn, conn: ConnDep, now: NowDep) -> dict[str, Any]:
    """What happened on the call. Sarvam's call-completion webhook (webhooks.py) later adds
    the authoritative duration, transcript and cost."""
    business = _business(conn, req.clinic)
    caller = normalize_phone(req.caller_phone)
    call_id, _ = store.ensure_call(
        conn, business.id, req.interaction_id, caller_phone=caller, now=now, call_ref=req.call_ref
    )
    language = language_code(req.language)
    client = store.get_client(conn, business.id, req.patient_id) if req.patient_id else None
    seconds = round(req.call_length) if req.call_length else None
    provisional = call_cost(seconds) if seconds else None
    with conn.transaction():
        first_end = conn.execute(
            """
            update public.calls set
              status = case when status = 'in_progress' then 'completed' else status end,
              ended_at = coalesce(ended_at, %s),
              duration_seconds = coalesce(duration_seconds, %s),
              agent_minutes = case when agent_minutes = 0 then %s else agent_minutes end,
              telephony_pulses = case when telephony_pulses = 0 then %s else telephony_pulses end,
              cost_inr_estimate = coalesce(cost_inr_estimate, %s),
              rates_verified_on = coalesce(rates_verified_on, %s),
              outcome = coalesce(%s, outcome),
              summary = coalesce(%s, summary),
              language = coalesce(%s, language),
              transcript = coalesce(transcript, '{}'::jsonb) || %s
            where id = %s
            returning (select count(*) from public.events e
               where e.call_id = calls.id and e.type = 'call_ended') = 0 as first_end
            """,
            (
                now,
                seconds,
                provisional.agent_minutes if provisional else 0,
                provisional.telephony_pulses if provisional else 0,
                provisional.inr if provisional else None,
                provisional.rates_verified_on if provisional else None,
                req.outcome,
                req.summary,
                language,
                Jsonb({"on_end": req.transcript} if req.transcript is not None else {}),
                call_id,
            ),
        ).fetchone()["first_end"]
        if first_end:
            store.call_event(
                conn,
                business.id,
                EventType.CALL_ENDED,
                call_id=call_id,
                interaction_id=req.interaction_id,
                now=now,
                language=language,
                client_id=client["id"] if client else None,
                outcome=req.outcome,
            )
    return {"ok": True}
