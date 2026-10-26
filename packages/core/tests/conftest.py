"""Fixtures for flow-engine tests: a two-doctor clinic in memory and a chat driver."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from salesbot_core.flow import ConversationState, FlowContext, FlowEngine, Inbound, Turn
from salesbot_core.flow.memory import InMemoryScheduling
from salesbot_core.packs import load_pack_by_id
from salesbot_core.scheduling import PractitionerCalendar, SchedulingSettings, WeeklyWindow

# Monday 7 January 2030, 10:00 IST
NOW = datetime(2030, 1, 7, 4, 30, tzinfo=UTC)
DR_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
DR_B = UUID("bbbbbbbb-0000-0000-0000-000000000002")


def clinic_calendars() -> list[PractitionerCalendar]:
    return [
        # Dr A: Mon-Sat 10:00-13:00, Mon-Fri 17:00-20:00; 15 min, first visit 30 min
        PractitionerCalendar(
            practitioner_id=DR_A,
            consultation_minutes=15,
            first_visit_minutes=30,
            work=[WeeklyWindow(weekday=d, start=time(10), end=time(13)) for d in range(1, 7)]
            + [WeeklyWindow(weekday=d, start=time(17), end=time(20)) for d in range(1, 6)],
        ),
        # Dr B: Mon/Wed/Fri 09:00-12:00; 20 min
        PractitionerCalendar(
            practitioner_id=DR_B,
            consultation_minutes=20,
            work=[WeeklyWindow(weekday=d, start=time(9), end=time(12)) for d in (1, 3, 5)],
        ),
    ]


NAMES = {
    DR_A: {"en": "Dr. Asha", "hi": "डॉ. आशा", "mr": "डॉ. आशा"},
    DR_B: {"en": "Dr. Rohan", "hi": "डॉ. रोहन", "mr": "डॉ. रोहन"},
}


class Chat:
    """Drives one patient's conversation; the clock advances 30 s per message."""

    def __init__(self, engine: FlowEngine, port: InMemoryScheduling, language: str) -> None:
        self.engine = engine
        self.port = port
        self.state: ConversationState = engine.new_state(language)  # type: ignore[arg-type]
        self.now = NOW
        self.turns: list[Turn] = []

    def _keep(self, turn: Turn) -> Turn:
        self.state = turn.state
        self.turns.append(turn)
        return turn

    def send(self, text: str) -> Turn:
        self.now += timedelta(seconds=30)
        return self._keep(self.engine.handle(self.state, Inbound(text=text, received_at=self.now)))

    def tap(self, choice: str) -> Turn:
        self.now += timedelta(seconds=30)
        inbound = Inbound(choice=choice, received_at=self.now)
        return self._keep(self.engine.handle(self.state, inbound))

    def welcome(self) -> Turn:
        return self._keep(self.engine.welcome(self.state, self.now))

    def wait(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)

    @property
    def last(self) -> Turn:
        return self.turns[-1]

    def option_ids(self) -> list[str]:
        return [o.id for o in self.state.offered]


ChatFactory = Callable[..., Chat]


@pytest.fixture
def make_chat() -> ChatFactory:
    def factory(
        language: str = "en",
        settings: SchedulingSettings | None = None,
        port: InMemoryScheduling | None = None,
        default_language: str = "en",
        paused: bool = False,
        demo: bool = False,
    ) -> Chat:
        port = port or InMemoryScheduling(
            # Test clinics offer all three languages unless a test passes its own settings.
            settings=settings or SchedulingSettings(allowed_languages=["en", "hi", "mr"]),
            calendars=clinic_calendars(),
            names=NAMES,
        )
        engine = FlowEngine(
            load_pack_by_id("clinic"),
            port,
            FlowContext(
                business_name="Test Clinic",
                client_name="Test Patient",
                default_language=default_language,  # type: ignore[arg-type]
                values={"clinic_phone": "+910000000000"},
                automation_paused=paused,
                demo=demo,
            ),
        )
        return Chat(engine, port, language)

    return factory


@pytest.fixture
def clinic() -> SimpleNamespace:
    """Constants for tests (conftest is not importable under --import-mode=importlib)."""
    return SimpleNamespace(now=NOW, dr_a=DR_A, dr_b=DR_B, calendars=clinic_calendars, names=NAMES)
