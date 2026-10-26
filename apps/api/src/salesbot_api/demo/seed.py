"""Seed or reset the demo clinic (FAKE data only) in the database in SUPABASE_DB_URL.

    make demo-seed      # idempotent: creates what is missing
    make demo-reset     # deletes the demo clinic (and only it), then seeds again

Refuses to run unless DEMO_MODE=true and APP_ENV=local, and if the database looks like
production (patients outside the demo clinic that were not simulated). The demo clinic is
found by its slug AND the `demo` marker in businesses.settings; a business with the slug but
no marker is never touched.

Patients register through the real intake code (source=simulation). Appointments go through
the booking engine, with times relative to now (IST).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
import yaml

from salesbot_api import booking, db
from salesbot_api.intake import IntakeRecord, register_client
from salesbot_api.records import Actor
from salesbot_api.settings import Settings
from salesbot_api.tools.load_clinic import ClinicIn, load
from salesbot_core.events import Channel, LeadSource
from salesbot_core.scheduling import Scope, VisitType

CLINIC_FILE = Path(__file__).with_name("clinic.yaml")
CONSENT_SOURCE = "demo-seed (fake patient, no real consent form)"
STAFF = Actor(type="staff", channel=Channel.STAFF)


@dataclass(frozen=True)
class FakePatient:
    external_id: str
    name: str
    phone: str  # +91 0000... : not a dialable Indian number
    language: str


# Seeded before the demo. The patient who registers live is LIVE_PATIENT.
PATIENTS = [
    FakePatient("demo-ravi", "Ravi Demo", "+910000000001", "hi"),
    FakePatient("demo-meera", "Meera Demo", "+910000000002", "en"),
]
LIVE_PATIENT = FakePatient("demo-asha", "Asha Demo", "+910000000003", "en")


# For Sarvam's browser "test agent" (docs/SARVAM_AGENT.md, browser test). A browser session
# has no caller number, so the test overrides the agent variable `patient_id` with this fixed
# id. The number is not dialable. Added and removed on its own: `make voice-test-patient`.
VOICE_TEST_PATIENT_ID = UUID("7e570000-0000-4000-8000-000000000001")
VOICE_TEST_PATIENT = FakePatient("voice-test", "Voice Test Patient (fake)", "+910000000099", "en")


class RefusedError(Exception):
    """The database or settings do not look like a local demo."""


def demo_clinic() -> ClinicIn:
    return ClinicIn.model_validate(yaml.safe_load(CLINIC_FILE.read_text(encoding="utf-8")))


def check_safe(conn: psycopg.Connection, settings: Settings) -> None:
    """Raise RefusedError unless this is a local demo database."""
    if settings.app_env != "local":
        raise RefusedError(f"APP_ENV is {settings.app_env}; the demo runs only with APP_ENV=local")
    if not settings.demo_mode:
        raise RefusedError("DEMO_MODE is not true")
    slug = demo_clinic().slug
    real = conn.execute(
        """
        select count(*) as n from public.clients c
        join public.businesses b on b.id = c.business_id
        where b.slug <> %s and c.source not in ('simulation', 'test_call')
        """,
        (slug,),
    ).fetchone()["n"]
    if real:
        raise RefusedError(
            f"{real} patient(s) outside the demo clinic were not simulated: "
            "this looks like a production database"
        )
    clash = conn.execute(
        "select 1 from public.businesses where slug = %s "
        "and coalesce(settings->>'demo', '') <> 'true'",
        (slug,),
    ).fetchone()
    if clash:
        raise RefusedError(f"a business with slug {slug!r} exists without the demo marker")


def find_demo(conn: psycopg.Connection) -> UUID | None:
    row = conn.execute(
        "select id from public.businesses where slug = %s and settings->>'demo' = 'true'",
        (demo_clinic().slug,),
    ).fetchone()
    return row["id"] if row else None


def reset(conn: psycopg.Connection, settings: Settings) -> bool:
    """Delete the demo clinic; its rows (events included) go with it by cascade.
    Returns whether there was one."""
    check_safe(conn, settings)
    bid = find_demo(conn)
    if bid is None:
        return False
    with conn.transaction():
        conn.execute(
            "delete from public.businesses where id = %s and settings->>'demo' = 'true'", (bid,)
        )
    return True


def register(
    conn: psycopg.Connection, business_id: UUID, patient: FakePatient, now: datetime
) -> UUID:
    record = IntakeRecord(
        clinic_id=business_id,
        external_patient_id=patient.external_id,
        name=patient.name,
        phone=patient.phone,
        preferred_language=patient.language,  # type: ignore[arg-type]
        whatsapp_opt_in=True,
        call_opt_in=False,
        consent_at=now,
        consent_source=CONSENT_SOURCE,
    )
    return register_client(conn, record, now=now, source=LeadSource.SIMULATION).client_id


def _book_first_after(
    conn: psycopg.Connection, business_id: UUID, client_id: UUID, after: datetime, now: datetime
) -> dict[str, Any]:
    scope = Scope(visit=VisitType.FIRST_VISIT)
    slot, _ = booking.first_opening(conn, business_id, scope=scope, now=max(after, now))
    if slot is None:
        raise RuntimeError("the demo clinic has no free time")
    hold = booking.hold_slot(
        conn,
        business_id,
        client_id=client_id,
        starts_at=slot.starts_at,
        scope=scope,
        actor=STAFF,
        now=now,
    )
    return booking.book_hold(conn, business_id, hold["id"], actor=STAFF, now=now)


def seed(conn: psycopg.Connection, settings: Settings, now: datetime) -> UUID:
    """Idempotent: the clinic is upserted, patients are registered once, and appointments are
    added only for patients without an upcoming one."""
    check_safe(conn, settings)
    clinic = demo_clinic()
    bid = load(conn, clinic)
    with conn.transaction():
        conn.execute(
            "update public.businesses set settings = settings || '{\"demo\": true}' where id = %s",
            (bid,),
        )
    clients = {p.external_id: register(conn, bid, p, now) for p in PATIENTS}

    tz = ZoneInfo(clinic.timezone)
    tomorrow_10 = datetime.combine(
        now.astimezone(tz).date() + timedelta(days=1), datetime.min.time(), tz
    ) + timedelta(hours=10)
    plan = [
        # Ravi: tomorrow from 10:00, confirmed. Meera: the next free time from now.
        ("demo-ravi", tomorrow_10, True),
        ("demo-meera", now, False),
    ]
    for external_id, after, confirm in plan:
        client_id = clients[external_id]
        if _has_upcoming(conn, bid, client_id, now):
            continue
        appt = _book_first_after(conn, bid, client_id, after, now)
        if confirm:
            booking.confirm(conn, bid, appt["id"], actor=STAFF, now=now)
    return bid


def _has_upcoming(
    conn: psycopg.Connection, business_id: UUID, client_id: UUID, now: datetime
) -> bool:
    return (
        conn.execute(
            "select 1 from public.appointments where business_id = %s and client_id = %s "
            "and starts_at > %s and status in ('booked', 'confirmed', 'unconfirmed')",
            (business_id, client_id, now),
        ).fetchone()
        is not None
    )


def add_voice_test_patient(conn: psycopg.Connection, settings: Settings, now: datetime) -> bool:
    """Idempotent. The demo clinic must exist. Returns whether the patient was created.
    source = test_call keeps it out of the funnel."""
    check_safe(conn, settings)
    bid = find_demo(conn)
    if bid is None:
        raise RefusedError("no demo clinic: run `make demo-reset` first")
    p = VOICE_TEST_PATIENT
    row = conn.execute(
        """
        insert into public.clients (id, business_id, external_id, name, phone,
          preferred_language, whatsapp_opt_in, call_opt_in, consent_at, consent_source, source)
        values (%s, %s, %s, %s, %s, %s, false, true, %s, %s, 'test_call')
        on conflict do nothing
        returning id
        """,
        (VOICE_TEST_PATIENT_ID, bid, p.external_id, p.name, p.phone, p.language, now,
         "voice test (fake patient, no real consent form)"),
    ).fetchone()  # fmt: skip
    return row is not None


def remove_voice_test_patient(conn: psycopg.Connection, settings: Settings) -> bool:
    """Deletes the voice test patient (and, by cascade, its appointments and tasks)."""
    check_safe(conn, settings)
    bid = find_demo(conn)
    if bid is None:
        return False
    row = conn.execute(
        "delete from public.clients where id = %s and business_id = %s returning id",
        (VOICE_TEST_PATIENT_ID, bid),
    ).fetchone()
    return row is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed or reset the demo clinic (fake data)")
    parser.add_argument(
        "action", choices=["seed", "reset", "voice-patient", "voice-patient-remove"]
    )
    args = parser.parse_args(argv)
    settings = Settings()
    now = datetime.now(UTC)
    try:
        with db.connect(settings, autocommit=True) as conn:
            if args.action == "voice-patient":
                created = add_voice_test_patient(conn, settings, now)
                print(f"voice test patient {'added' if created else 'already there'}")
                print(f"  clinic_slug = {demo_clinic().slug}")
                print(f"  patient_id  = {VOICE_TEST_PATIENT_ID}")
                return 0
            if args.action == "voice-patient-remove":
                gone = remove_voice_test_patient(conn, settings)
                print("voice test patient removed" if gone else "no voice test patient")
                return 0
            if args.action == "reset":
                print("demo clinic deleted" if reset(conn, settings) else "no demo clinic yet")
            bid = seed(conn, settings, now)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except db.DatabaseNotConfiguredError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"demo clinic ready: {demo_clinic().slug} ({bid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
