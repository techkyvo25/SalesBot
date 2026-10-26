"""The local demo page and its API (docs/DEMO.md). FAKE data only.

Mounted by `mount` only when DEMO_MODE=true and APP_ENV=local, and every request must come
from this machine (`make demo` binds 127.0.0.1). Nothing here is authenticated: never expose
it through a tunnel or a shared host.

Chat state lives in memory per browser session. Everything else goes through the real code
paths: registration through the signed intake adapter and register_client, bookings, holds,
cancellations and reschedules through salesbot_api.booking (via DbScheduling), staff tasks and
events through Conversation.record. Nothing is sent: WhatsApp messages and calls are simulated.
"""

from __future__ import annotations

import json
import threading
import time as clock
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from salesbot_api import booking, db
from salesbot_api.conversation import Conversation, open_conversation
from salesbot_api.demo import seed
from salesbot_api.intake import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    HmacWebhookIntake,
    IntakeRejectedError,
    register_client,
    sign,
)
from salesbot_api.records import Actor
from salesbot_api.settings import Settings, get_settings
from salesbot_api.voice import store
from salesbot_core.events import Channel, EventType, LeadSource
from salesbot_core.flow import ConversationState, Inbound, Reply, Turn
from salesbot_core.scheduling import Scope
from salesbot_core.text.formatting import long_date, time_label

STATIC = Path(__file__).with_name("static")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
CONSENT_SOURCE = "demo page (simulated registration form, fake patient)"
# Replies a live channel would send as WhatsApp messages we start or that confirm a change.
OUTBOUND_KEYS = {"welcome", "booked", "rescheduled", "confirmed", "cancelled", "confirm_request"}
FUNNEL = {
    "registered": EventType.CLIENT_REGISTERED,
    "booked": EventType.BOOKING_CREATED,
    "confirmed": EventType.APPOINTMENT_CONFIRMED,
    "rescheduled": EventType.APPOINTMENT_RESCHEDULED,
    "cancelled": EventType.BOOKING_CANCELLED,
}


def local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in LOOPBACK:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the demo answers this machine only")


router = APIRouter(prefix="/demo", tags=["demo"], dependencies=[Depends(local_only)])
SettingsDep = Annotated[Settings, Depends(get_settings)]

# session id -> patient external id -> conversation state. One presenter, one process.
_sessions: dict[str, dict[str, ConversationState]] = {}
_lock = threading.Lock()


def mount(app: FastAPI, settings: Settings) -> bool:
    """Add the demo routes when DEMO_MODE=true and APP_ENV=local. Returns whether it did."""
    if not (settings.demo_mode and settings.app_env == "local"):
        return False
    app.include_router(router)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connection(settings: SettingsDep) -> Iterator[psycopg.Connection]:
    try:
        with db.connect(settings, autocommit=True) as conn:
            yield conn
    except db.DatabaseNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


ConnDep = Annotated[psycopg.Connection, Depends(_connection)]


def _business(conn: psycopg.Connection) -> store.Business:
    bid = seed.find_demo(conn)
    business = store.business_by_slug(conn, seed.demo_clinic().slug) if bid else None
    if business is None or business.id != bid:
        raise HTTPException(status.HTTP_409_CONFLICT, "no demo clinic: run `make demo-reset`")
    return business


def _client(conn: psycopg.Connection, business_id: UUID, external_id: str) -> dict | None:
    return conn.execute(
        "select * from public.clients where business_id = %s and external_id = %s",
        (business_id, external_id),
    ).fetchone()


def _conversation(
    conn: psycopg.Connection, business: store.Business, session: str, patient: str
) -> Conversation:
    client = _client(conn, business.id, patient)
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "register the patient first")
    conv = open_conversation(conn, business, client, demo=True)
    saved = _sessions.get(session, {}).get(patient)
    if saved is not None:
        conv.state = saved
    return conv


def _keep(session: str, patient: str, conv: Conversation) -> None:
    _sessions.setdefault(session, {})[patient] = conv.state


def _messages(turn: Turn) -> list[dict[str, Any]]:
    return [_message(r) for r in turn.replies]


def _message(reply: Reply) -> dict[str, Any]:
    return {
        "key": reply.key,
        "text": reply.text,
        "language": reply.language,
        "draft": reply.draft,
        "options": [o.model_dump() for o in reply.options],
        # What a live channel sends instead while the wording is DRAFT (emergency: numbers).
        "live_text": reply.fallback if reply.draft and reply.fallback else None,
        "outbound": reply.key in OUTBOUND_KEYS,
    }


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get("", include_in_schema=False)
def page() -> FileResponse:
    return FileResponse(STATIC / "demo.html", media_type="text/html; charset=utf-8")


@router.get("/demo.js", include_in_schema=False)
def script() -> FileResponse:
    return FileResponse(STATIC / "demo.js", media_type="text/javascript; charset=utf-8")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class RegisterIn(BaseModel):
    session: str = Field(min_length=1, max_length=64)


@router.post("/api/register")
def register(body: RegisterIn, conn: ConnDep, settings: SettingsDep) -> dict[str, Any]:
    """A simulated registration form submission: signed and parsed by the real intake
    adapter, stored by register_client with source = simulation."""
    if settings.intake_hmac_secret is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "INTAKE_HMAC_SECRET is not set in .env"
        )
    business = _business(conn)
    patient, now = seed.LIVE_PATIENT, _now()
    payload = json.dumps(
        {
            "clinic_id": str(business.id),
            "external_patient_id": patient.external_id,
            "name": patient.name,
            "phone": patient.phone,
            "preferred_language": patient.language,
            "whatsapp_opt_in": True,
            "call_opt_in": False,
            "consent_at": now.isoformat(),
            "consent_source": CONSENT_SOURCE,
        }
    ).encode()
    secret = settings.intake_hmac_secret.get_secret_value()
    timestamp = int(clock.time())
    headers = {TIMESTAMP_HEADER: str(timestamp), SIGNATURE_HEADER: sign(secret, timestamp, payload)}
    try:
        record = HmacWebhookIntake(secret).parse(headers, payload, now)
    except IntakeRejectedError as exc:
        raise HTTPException(exc.status_code, exc.reason) from exc
    with _lock:
        result = register_client(conn, record, now=now, source=LeadSource.SIMULATION)
        messages: list[dict[str, Any]] = []
        if result.created:
            conv = _conversation(conn, business, body.session, patient.external_id)
            messages = _messages(conv.welcome(now))
            _keep(body.session, patient.external_id, conv)
    return {
        "patient": patient.external_id,
        "name": patient.name,
        "created": result.created,
        "simulated": True,
        "messages": messages,
    }


class ChatIn(BaseModel):
    session: str = Field(min_length=1, max_length=64)
    patient: str = Field(default=seed.LIVE_PATIENT.external_id, max_length=64)
    text: str = Field(default="", max_length=1000)
    choice: str | None = Field(default=None, max_length=200)


@router.post("/api/chat")
def chat(body: ChatIn, conn: ConnDep) -> dict[str, Any]:
    business = _business(conn)
    with _lock:
        conv = _conversation(conn, business, body.session, body.patient)
        turn = conv.handle(Inbound(text=body.text, choice=body.choice, received_at=_now()))
        _keep(body.session, body.patient, conv)
    return {
        "messages": _messages(turn),
        "inbound_language": turn.inbound_language,
        "tasks": [t.kind for t in turn.tasks],
        "opt_out": turn.opt_out,
    }


class TakeIn(BaseModel):
    session: str = Field(min_length=1, max_length=64)
    patient: str = Field(default=seed.LIVE_PATIENT.external_id, max_length=64)


@router.post("/api/other-patient-takes-slot")
def other_patient_takes_slot(body: TakeIn, conn: ConnDep) -> dict[str, Any]:
    """Book the first time offered to the chat patient, as other seeded patients, through the
    booking engine: once per doctor free at that time, so the time is really gone."""
    business = _business(conn)
    with _lock:
        conv = _conversation(conn, business, body.session, body.patient)
        offered = [o for o in conv.state.offered if o.id.startswith("time:")]
        if not offered:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "no time is on offer in the chat: pick a day first"
            )
        starts = datetime.fromisoformat(offered[0].id[5:])
        others = conn.execute(
            "select id, name from public.clients where business_id = %s and external_id <> %s "
            "and external_id like 'demo-%%' order by external_id",
            (business.id, body.patient),
        ).fetchall()
        visit = conv.port.visit_type()
        tz = ZoneInfo(business.timezone)
        day = starts.astimezone(tz).date()
        now = _now()
        actor = Actor(type="client", channel=Channel.API)
        taken = []
        pending = iter(others)
        for practitioner_id, _ in conv.port.weekly_hours():
            scope = Scope(visit=visit, practitioner_id=practitioner_id)
            free = booking.open_slots(conn, business.id, day=day, scope=scope, now=now)
            if not any(s.starts_at == starts for s in free):
                continue
            other = next(pending, None)
            if other is None:
                break
            hold = booking.hold_slot(
                conn,
                business.id,
                client_id=other["id"],
                starts_at=starts,
                scope=scope,
                actor=actor,
                now=now,
            )
            booking.book_hold(conn, business.id, hold["id"], actor=actor, now=now)
            taken.append(other["name"])
    local = starts.astimezone(tz)
    return {
        "time": f"{long_date(local.date(), 'en')} at {time_label(local, 'en')}",
        "booked_by": taken,
    }


@router.get("/api/day")
def day_view(conn: ConnDep, day: date | None = None) -> dict[str, Any]:
    business = _business(conn)
    tz = ZoneInfo(business.timezone)
    day = day or _now().astimezone(tz).date()
    start = datetime.combine(day, datetime.min.time(), tz)
    rows = conn.execute(
        """
        select a.practitioner_id, a.starts_at, a.status, c.name, c.preferred_language
        from public.appointments a join public.clients c on c.id = a.client_id
        where a.business_id = %s and a.starts_at >= %s and a.starts_at < %s
          and (a.status <> 'held' or a.hold_expires_at > %s)
        order by a.starts_at
        """,
        (business.id, start, start + timedelta(days=1), _now()),
    ).fetchall()
    doctors = conn.execute(
        "select id, name from public.practitioners where business_id = %s and is_active "
        "order by created_at, id",
        (business.id,),
    ).fetchall()
    return {
        "day": day.isoformat(),
        "label": long_date(day, "en"),
        "clinic": business.name,
        "doctors": [
            {
                "name": d["name"],
                "rows": [
                    {
                        "time": time_label(r["starts_at"].astimezone(tz), "en"),
                        "patient": r["name"],
                        "status": r["status"],
                        "language": r["preferred_language"],
                    }
                    for r in rows
                    if r["practitioner_id"] == d["id"]
                ],
            }
            for d in doctors
        ],
    }


@router.get("/api/funnel")
def funnel(conn: ConnDep) -> dict[str, int]:
    """Counts from the append-only events table."""
    business = _business(conn)
    rows = conn.execute(
        "select type, count(*) as n from public.events where business_id = %s group by type",
        (business.id,),
    ).fetchall()
    counts = {r["type"]: r["n"] for r in rows}
    return {name: counts.get(event.value, 0) for name, event in FUNNEL.items()}


@router.get("/api/tasks")
def tasks(conn: ConnDep) -> list[dict[str, Any]]:
    business = _business(conn)
    tz = ZoneInfo(business.timezone)
    rows = conn.execute(
        """
        select t.kind, t.urgency, t.summary, t.status, t.created_at, c.name
        from public.staff_tasks t left join public.clients c on c.id = t.client_id
        where t.business_id = %s order by (t.urgency = 'urgent') desc, t.created_at desc
        limit 50
        """,
        (business.id,),
    ).fetchall()
    return [
        {
            "kind": r["kind"],
            "urgent": r["urgency"] == "urgent",
            "summary": r["summary"],
            "status": r["status"],
            "patient": r["name"],
            "at": time_label(r["created_at"].astimezone(tz), "en"),
        }
        for r in rows
    ]


@router.post("/api/reset")
def reset(conn: ConnDep, settings: SettingsDep) -> dict[str, str]:
    """Delete and re-seed the demo clinic (and only it)."""
    with _lock:
        try:
            seed.reset(conn, settings)
            seed.seed(conn, settings, _now())
        except seed.RefusedError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"refused: {exc}") from exc
        _sessions.clear()
    return {"status": "reset"}
