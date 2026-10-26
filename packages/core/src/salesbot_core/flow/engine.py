"""The channel-agnostic conversation engine for appointment management.

One engine for WhatsApp, voice and the dev console. It reads a message (`Inbound`), the saved
`ConversationState` and the clinic's pack, calls scheduling through `SchedulingPort`, and
returns a `Turn`: replies (text + options), conversation events, staff tasks and the new state.

Rules that hold for every turn:
- Every patient-facing sentence is a pack message (wording policy, docs/DECISIONS.md).
- The reply language follows the patient's message (salesbot_core.text.language), within the
  languages both the pack and the clinic (`allowed_languages`) offer.
- A possible emergency is checked first, before the language is updated: the reply is in the
  last known language (plus the message's language when that is clear and different), always
  carries the clinic phone and emergency numbers, and has a numbers-only fallback for live
  channels while the wording is DRAFT.
- A request for a person, a sincere "are you a bot?" and "stop" are handled at any step.
- A pack FAQ is answered at any step, then the current question is asked again; it never
  creates a staff task.
- Nothing is booked or confirmed except as the answer to a pending question about that exact
  slot or appointment. Acknowledgements ("ok", "thanks", emoji) never start anything.
- A time or part of day in the message beats a bare "no" ("no, evening instead").
- A held slot is released whenever the patient leaves the confirmation step.
- Booking events are written by the booking engine behind the port; this engine only emits
  conversation events (booking_started, question_unanswered, handoff ...).

Pure apart from the port: `now` comes from the message, so tests control the clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from string import Formatter
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from salesbot_core.events import EventType
from salesbot_core.flow.models import (
    ConversationState,
    FlowEvent,
    Inbound,
    Option,
    Purpose,
    Reply,
    StaffTask,
    Step,
    Turn,
)
from salesbot_core.flow.ports import (
    Appointment,
    HoldExpired,
    NotActive,
    SchedulingPort,
    SlotUnavailable,
)
from salesbot_core.packs import Pack
from salesbot_core.packs.schema import FAQ, Intent, Language
from salesbot_core.scheduling import Scope, Slot
from salesbot_core.text.formatting import day_label, long_date, time_label, weekly_hours
from salesbot_core.text.language import (
    Detected,
    detect,
    recorded_language,
    reply_language,
    requested_language,
)
from salesbot_core.text.matching import matches_any, normalize, only_phrases
from salesbot_core.text.when import ClockTime, When, bare_number, parse_when

# Messages the engine sends. A pack used with the engine must define all of them.
REQUIRED_MESSAGES = frozenset(
    {
        "welcome", "not_now", "menu", "ask_day", "day_full", "no_openings_soon", "no_openings",
        "ask_time", "time_not_free", "confirm_slot", "booked", "slot_lost", "pick_again",
        "abandon_booking", "reschedule_none", "reschedule_start", "rescheduled",
        "reschedule_pending", "cancel_none", "cancel_ask", "cancelled", "cancel_kept",
        "confirm_request", "confirmed", "nothing_to_confirm", "reminder", "not_understood",
        "handed_to_staff", "human_requested", "emergency", "language_switched", "opted_out",
        "opt_book", "opt_reschedule", "opt_cancel", "opt_human", "opt_yes", "opt_no",
        "opt_confirm", "opt_not_now", "opt_more_days", "opt_more_times", "opt_other_day",
        "opt_cancel_confirm", "opt_keep", "ack", "second_ask", "opt_book_another",
    }
)  # fmt: skip
# Placeholders the engine fills. Pack messages may use only these.
PLACEHOLDERS = frozenset(
    {
        "business_name", "client_name", "doctor", "date", "time", "days", "clinic_phone",
        "emergency_numbers", "hours",
    }
)  # fmt: skip
# Every emergency reply must carry these, in every language.
EMERGENCY_PLACEHOLDERS = frozenset({"clinic_phone", "emergency_numbers"})
SINCERE_ASK_KEY = "sincere_ask"
# WhatsApp list messages hold at most 10 rows: 8 times + "later times" + "another day".
TIMES_PER_PAGE = 8
# Consecutive messages not understood before a staff task is created.
MAX_MISSES = 2


@dataclass(frozen=True)
class FlowContext:
    business_name: str
    client_name: str
    # The clinic's default language: replies to unsupported languages use it.
    default_language: Language = "en"
    # Business values for placeholders. "clinic_phone" (the clinic's number for patients) is
    # required: every emergency reply gives it.
    values: dict[str, str] = field(default_factory=dict)
    # Staff took over this client: the engine stays silent.
    automation_paused: bool = False
    # Local demo only (docs/DEMO.md): answer the pack's FAQs marked `demo` (fictional text).
    demo: bool = False


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


class FlowEngine:
    def __init__(self, pack: Pack, port: SchedulingPort, ctx: FlowContext) -> None:
        if pack.conversation is None:
            raise ValueError(f"pack {pack.id!r} has no conversation section")
        missing = REQUIRED_MESSAGES - set(pack.conversation.messages)
        if missing:
            raise ValueError(f"pack {pack.id!r} is missing messages: {sorted(missing)}")
        for language, text in pack.conversation.messages["emergency"].items():
            if not placeholders(text) >= EMERGENCY_PLACEHOLDERS:
                raise ValueError(
                    f"pack {pack.id!r}: emergency message ({language}) needs "
                    f"{sorted(EMERGENCY_PLACEHOLDERS)}"
                )
        if not ctx.values.get("clinic_phone"):
            raise ValueError("FlowContext.values needs clinic_phone (emergency replies give it)")
        allowed = port.settings.allowed_languages
        self.languages: list[Language] = [lang for lang in pack.languages if lang in allowed]
        if not self.languages:
            raise ValueError(f"pack {pack.id!r} offers none of the clinic's languages {allowed}")
        self.pack = pack
        self.conversation = pack.conversation
        self.port = port
        self.ctx = ctx
        self.tz = ZoneInfo(port.settings.timezone)
        self.faqs: list[FAQ] = [f for f in pack.faqs if f.keywords and (ctx.demo or not f.demo)]

    def new_state(self, language: Language) -> ConversationState:
        if language not in self.languages:
            fallback = self.ctx.default_language
            language = fallback if fallback in self.languages else self.languages[0]
        return ConversationState(language=language)

    # -- outbound starters (welcome after intake, confirmation requests, reminders) ------

    def welcome(self, state: ConversationState, now: datetime) -> Turn:
        s = _Session(self, state, now)
        s.state.step = Step.WELCOME
        s.say("welcome", [s.option("book", "opt_book"), s.option("not_now", "opt_not_now")])
        return s.turn()

    def confirmation_request(
        self, state: ConversationState, appointment_id: UUID, now: datetime
    ) -> Turn:
        s = _Session(self, state, now)
        s.ask_confirm_appointment(appointment_id, "confirm_request")
        return s.turn()

    def reminder(self, state: ConversationState, appointment_id: UUID, now: datetime) -> Turn:
        s = _Session(self, state, now)
        s.ask_confirm_appointment(appointment_id, "reminder")
        return s.turn()

    # -- inbound ------------------------------------------------------------------------

    def handle(self, state: ConversationState, inbound: Inbound) -> Turn:
        s = _Session(self, state, inbound.received_at)
        if self.ctx.automation_paused:
            return s.turn()
        s.receive(inbound)
        return s.turn()


class _Session:
    """One turn. Mutates a copy of the state and collects output."""

    def __init__(self, engine: FlowEngine, state: ConversationState, now: datetime) -> None:
        self.e = engine
        self.state = state.model_copy(deep=True)
        self.now = now
        self.today = now.astimezone(engine.tz).date()
        self.replies: list[Reply] = []
        self.events: list[FlowEvent] = []
        self.tasks: list[StaffTask] = []
        self.inbound_language: str | None = None
        self.opt_out = False
        self.missed = False

    def turn(self) -> Turn:
        return Turn(
            state=self.state,
            replies=self.replies,
            events=self.events,
            tasks=self.tasks,
            inbound_language=self.inbound_language,
            opt_out=self.opt_out,
        )

    # -- text ---------------------------------------------------------------------------

    def _text(self, key: str, language: Language | None = None) -> tuple[str, Language]:
        if key == SINCERE_ASK_KEY:
            localized = self.e.conversation.sincere_ask.answer
        else:
            localized = self.e.conversation.messages[key]
        wanted = language or self.state.language
        language = wanted if wanted in localized else "en"
        return localized[language], language

    def _values(self, extra: dict[str, str]) -> dict[str, str]:
        return {
            "business_name": self.e.ctx.business_name,
            "client_name": self.e.ctx.client_name,
            "days": str(self.e.port.settings.lookahead_days),
            "emergency_numbers": " / ".join(self.e.port.settings.emergency_numbers),
            **self.e.ctx.values,
            **extra,
        }

    def option(self, option_id: str, key: str) -> Option:
        return Option(id=option_id, label=self._text(key)[0])

    def say(
        self,
        key: str,
        options: list[Option] | None = None,
        *,
        in_language: Language | None = None,
        fallback: str | None = None,
        **values: str,
    ) -> None:
        template, language = self._text(key, in_language)
        self._add(key, template, language, options, fallback, values)

    def _add(
        self,
        key: str,
        template: str,
        language: Language,
        options: list[Option] | None,
        fallback: str | None,
        values: dict[str, str],
    ) -> None:
        filled = self._values(values)
        text = template.format_map(filled)
        draft = self.e.pack.is_draft(language, key) or (
            key == SINCERE_ASK_KEY and self.e.conversation.sincere_ask.draft
        )
        self.replies.append(
            Reply(
                key=key,
                text=text,
                language=language,
                options=options or [],
                draft=draft,
                fallback=fallback.format_map(filled) if fallback else None,
            )
        )
        self.state.offered = list(options or [])

    def event(self, event_type: EventType, **properties: Any) -> None:
        self.events.append(FlowEvent(type=event_type, properties=properties))

    def slot_values(self, practitioner_id: UUID, starts_at: datetime) -> dict[str, str]:
        local = starts_at.astimezone(self.e.tz)
        return {
            "doctor": self.e.port.practitioner_name(practitioner_id, self.state.language),
            "date": long_date(local.date(), self.state.language),
            "time": time_label(local, self.state.language),
        }

    # -- understanding ------------------------------------------------------------------

    def intents(self, text: str) -> set[Intent]:
        return {
            intent
            for intent, phrases in self.e.conversation.intents.items()
            if matches_any(text, phrases)
        }

    def _as_choice(self, text: str) -> str | None:
        """A typed number or label that picks one of the options last offered."""
        n = bare_number(text)
        if n is not None and 1 <= n <= len(self.state.offered):
            return self.state.offered[n - 1].id
        wanted = normalize(text)
        for option in self.state.offered:
            if normalize(option.label) == wanted:
                return option.id
        return None

    def receive(self, inbound: Inbound) -> None:
        text = inbound.text.strip()
        typed = bool(text) and inbound.choice is None
        detected = detect(text) if typed else Detected(None, "none")
        self.inbound_language = recorded_language(detected)

        # Before anything else, and before the language changes (patient safety).
        if typed and self.e.pack.emergency and matches_any(text, self.e.pack.emergency.triggers):
            self.on_emergency(detected)
            self.state.misses = 0
            return

        requested = requested_language(text) if typed else None
        if requested is not None and requested in self.e.languages:
            self.state.language = requested
            self.say("language_switched")
            self.reprompt()
            return
        self.state.language = reply_language(
            detected,
            self.state.language,
            supported=self.e.languages,
            fallback=self.e.ctx.default_language,
        )

        self.dispatch(inbound, text)
        if not self.missed:
            self.state.misses = 0

    def dispatch(self, inbound: Inbound, text: str) -> None:
        choice = inbound.choice or self._as_choice(text)
        if choice is not None:
            self.on_choice(choice)
            return
        if not text:
            return

        if matches_any(text, self.e.conversation.sincere_ask.triggers):
            self.say(SINCERE_ASK_KEY, [self.option("human", "opt_human")])
            self.reprompt()
            return
        intents = self.intents(text)
        if "stop" in intents:
            self.on_stop()
            return
        if "human" in intents:
            self.on_human()
            return
        self.on_text(text, intents)

    # -- choices ------------------------------------------------------------------------

    def on_choice(self, choice: str) -> None:
        step = self.state.step
        if choice.startswith("day:"):
            self.choose_day(date.fromisoformat(choice[4:]))
        elif choice.startswith("time:"):
            self.try_hold(datetime.fromisoformat(choice[5:]))
        elif choice == "book":
            self.start_booking()
        elif choice == "reschedule":
            self.start_reschedule()
        elif choice == "cancel":
            self.start_cancel()
        elif choice == "human":
            self.on_human()
        elif choice == "confirm":
            self.confirm_appointment()
        elif choice == "more_days":
            self.ask_day(from_day=self.state.days_from)
        elif choice == "more_times":
            self.state.times_offset += TIMES_PER_PAGE
            self.ask_time()
        elif choice == "other_day":
            self.drop_hold()
            self.ask_day()
        elif choice == "not_now":
            self.reset()
            self.say("not_now")
        elif choice in ("yes", "no", "keep") and step in _YES_NO_STEPS:
            self.on_yes_no(choice == "yes")
        else:
            self.miss()

    # -- free text ----------------------------------------------------------------------

    def on_text(self, text: str, intents: set[Intent]) -> None:
        step = self.state.step
        when = parse_when(text, self.today, bare_number_is_time=step is Step.PICK_TIME)
        acting = bool(intents & _ACTIONS) or not when.empty

        faqs = [f for f in self.e.faqs if matches_any(text, f.keywords)]
        if faqs:
            for faq in faqs:
                self.answer_faq(faq, last=not acting and faq is faqs[-1])
            if not acting:
                if step is not Step.IDLE:
                    self.reprompt()
                return

        yes = bool(intents & {"yes", "confirm"})
        no = "no" in intents
        time_beats_no = not when.empty and step in _TIME_BEATS_NO
        if step in _YES_NO_STEPS and yes != no and (not time_beats_no or self.is_pending(when)):
            self.on_yes_no(yes)
            return
        if when.empty and self.is_acknowledgement(text):
            # "ok", "thanks", emoji: never a booking, never a confirmation.
            if step is Step.IDLE:
                self.say("ack")
            else:
                self.reprompt()
            return
        if step is Step.CONFIRM_APPOINTMENT and not when.empty:
            self.start_reschedule(when)
            return
        if step is Step.CONFIRM_SECOND and not when.empty and not no:
            self.start_booking(when, another=True)
            return

        picking = step in (Step.PICK_DAY, Step.PICK_TIME, Step.CONFIRM_SLOT, Step.OFFER_EARLIEST)
        if picking and ("cancel" in intents or "no" in intents) and when.empty:
            self.abandon()
        elif "reschedule" in intents:
            self.start_reschedule(when)
        elif "cancel" in intents:
            self.start_cancel()
        elif picking and not when.empty:
            self.drop_hold()
            self.apply_when(when)
        elif "confirm" in intents:
            self.reset()
            self.confirm_appointment()
        elif "more" in intents and step is Step.PICK_DAY:
            self.ask_day(from_day=self.state.days_from)
        elif "more" in intents and step is Step.PICK_TIME:
            self.state.times_offset += TIMES_PER_PAGE
            self.ask_time()
        elif "book" in intents or (step in (Step.IDLE, Step.WELCOME) and not when.empty):
            self.start_booking(when)
        elif "menu" in intents:
            self.reset()
            self.menu()
        else:
            self.miss()

    def is_acknowledgement(self, text: str) -> bool:
        intents = self.e.conversation.intents
        return only_phrases(text, [*intents.get("yes", []), *intents.get("thanks", [])])

    def is_pending(self, when: When) -> bool:
        """The message names the very slot being asked about ("yes, 11 am is fine")."""
        st = self.state
        starts = st.hold.starts_at if st.hold is not None else st.earliest
        if starts is None or when.time is None:
            return False
        local = starts.astimezone(self.e.tz)
        if when.day is not None and when.day != local.date():
            return False
        return any(
            c.hour == local.hour and c.minute == local.minute for c in when.time.candidates()
        )

    # -- FAQs ---------------------------------------------------------------------------

    def answer_faq(self, faq: FAQ, *, last: bool) -> None:
        wanted = self.state.language
        language: Language = wanted if wanted in faq.answer else "en"
        values: dict[str, str] = {}
        if faq.source == "opening_hours":
            values["hours"] = "\n".join(
                f"{self.e.port.practitioner_name(pid, language)}: {weekly_hours(windows, language)}"
                for pid, windows in self.e.port.weekly_hours()
                if windows
            )
        options: list[Option] = []
        if faq.offer_human:
            options.append(self.option("human", "opt_human"))
        elif last and self.state.step is Step.IDLE:
            options = self.menu_options()
        offered = self.state.offered
        self._add(f"faq:{faq.id}", faq.answer[language], language, options, None, values)
        if not options:
            self.state.offered = offered

    def on_yes_no(self, yes: bool) -> None:
        step = self.state.step
        if step is Step.WELCOME:
            if yes:
                self.start_booking()
            else:
                self.reset()
                self.say("not_now")
        elif step is Step.CONFIRM_SLOT:
            if yes:
                self.book_held()
            else:
                self.drop_hold()
                self.ask_time(prefix_key="pick_again")
        elif step is Step.OFFER_EARLIEST:
            if yes and self.state.earliest is not None:
                self.try_hold(self.state.earliest, book_now=True)
            else:
                self.ask_day()
        elif step is Step.CONFIRM_CANCEL:
            if yes:
                self.cancel_appointment()
            else:
                self.keep_appointment(self.state.appointment_id)
        elif step is Step.CONFIRM_SECOND:
            if yes:
                self.start_booking(another=True)
            else:
                upcoming = self.e.port.upcoming(now=self.now)
                self.keep_appointment(upcoming.id if upcoming else None)
        elif step is Step.CONFIRM_APPOINTMENT:
            if yes:
                self.confirm_appointment()
            else:
                self.reset()
                self.menu()

    # -- interrupts ---------------------------------------------------------------------

    def on_emergency(self, detected: Detected) -> None:
        """Reply in the last known language; the conversation language does not change. If
        the message is clearly in another language the clinic offers, add that language too."""
        self.reset()
        fallback = self.e.conversation.emergency_fallback
        self.say("emergency", fallback=fallback)
        other = detected.language
        if other is not None and other != self.state.language and other in self.e.languages:
            self.say("emergency", in_language=other, fallback=fallback)
        self.tasks.append(StaffTask(kind="emergency", reason="emergency words", urgent=True))
        self.event(EventType.EMERGENCY_DETECTED)
        self.event(EventType.HANDOFF_REQUESTED, reason="emergency")
        self.event(EventType.HANDOFF_TASK_CREATED, kind="emergency")

    def on_human(self) -> None:
        self.reset()
        self.say("human_requested")
        self.tasks.append(StaffTask(kind="human_requested", reason="asked for the clinic team"))
        self.event(EventType.HANDOFF_REQUESTED, reason="asked")
        self.event(EventType.HANDOFF_TASK_CREATED, kind="human_requested")

    def on_stop(self) -> None:
        self.reset()
        self.opt_out = True
        self.say("opted_out")
        self.tasks.append(StaffTask(kind="opt_out", reason="asked to stop messages"))

    def miss(self) -> None:
        self.missed = True
        self.state.misses += 1
        self.event(EventType.QUESTION_UNANSWERED, step=self.state.step.value)
        if self.state.misses >= MAX_MISSES:
            self.reset()
            self.state.misses = 0
            self.say("handed_to_staff")
            self.tasks.append(StaffTask(kind="not_understood", reason="messages not understood"))
            self.event(EventType.HANDOFF_TASK_CREATED, kind="not_understood")
            return
        offered = self.state.offered or self.menu_options()
        self.say("not_understood", offered)

    # -- menu and resets ----------------------------------------------------------------

    def reset(self) -> None:
        self.drop_hold()
        language, misses = self.state.language, self.state.misses
        self.state = ConversationState(language=language, misses=misses)

    def drop_hold(self) -> None:
        if self.state.hold is not None:
            self.e.port.release(self.state.hold.id)
            self.state.hold = None

    def menu_options(self) -> list[Option]:
        if self.e.port.upcoming(now=self.now) is not None:
            return [
                self.option("reschedule", "opt_reschedule"),
                self.option("cancel", "opt_cancel"),
                self.option("human", "opt_human"),
            ]
        return [self.option("book", "opt_book"), self.option("human", "opt_human")]

    def menu(self) -> None:
        self.say("menu", self.menu_options())

    def reprompt(self) -> None:
        """Ask the current step's question again (after a language switch or an aside)."""
        st = self.state
        if st.step is Step.PICK_DAY:
            self.ask_day()
        elif st.step is Step.PICK_TIME and st.day is not None:
            self.ask_time()
        elif st.step is Step.CONFIRM_SLOT and st.hold is not None:
            self.say(
                "confirm_slot",
                self.yes_no(),
                **self.slot_values(st.hold.practitioner_id, st.hold.starts_at),
            )
        elif st.step in (Step.CONFIRM_CANCEL, Step.CONFIRM_APPOINTMENT) and st.appointment_id:
            appt = self.e.port.get(st.appointment_id)
            if appt is not None:
                key = "cancel_ask" if st.step is Step.CONFIRM_CANCEL else "confirm_request"
                options = self.cancel_options() if key == "cancel_ask" else self.confirm_options()
                self.say(key, options, **self.slot_values(appt.practitioner_id, appt.starts_at))
        elif st.step is Step.WELCOME:
            self.say(
                "welcome", [self.option("book", "opt_book"), self.option("not_now", "opt_not_now")]
            )
        elif st.step is Step.OFFER_EARLIEST:
            self.ask_day()
        elif st.step is Step.CONFIRM_SECOND:
            self.ask_second()
        else:
            self.menu()

    def yes_no(self) -> list[Option]:
        return [self.option("yes", "opt_yes"), self.option("no", "opt_no")]

    def cancel_options(self) -> list[Option]:
        return [self.option("yes", "opt_cancel_confirm"), self.option("keep", "opt_keep")]

    def confirm_options(self) -> list[Option]:
        return [
            self.option("confirm", "opt_confirm"),
            self.option("reschedule", "opt_reschedule"),
            self.option("cancel", "opt_cancel"),
        ]

    # -- booking ------------------------------------------------------------------------

    def scope(self) -> Scope:
        if self.state.purpose is Purpose.RESCHEDULE and self.state.appointment_id:
            appt = self.e.port.get(self.state.appointment_id)
            if appt is not None:
                return Scope(visit=appt.visit, practitioner_id=appt.practitioner_id)
        return Scope(visit=self.e.port.visit_type())

    def start_booking(self, when: When | None = None, *, another: bool = False) -> None:
        self.reset()
        if not another and self.ask_second():
            return
        self.state.purpose = Purpose.BOOK
        self.event(EventType.BOOKING_STARTED, purpose="book")
        self.begin(when)

    def ask_second(self) -> bool:
        """The patient already has an upcoming appointment: ask before booking another."""
        appt = self.e.port.upcoming(now=self.now)
        if appt is None:
            return False
        self.state.step = Step.CONFIRM_SECOND
        self.state.appointment_id = appt.id
        self.say(
            "second_ask",
            [
                self.option("yes", "opt_book_another"),
                self.option("reschedule", "opt_reschedule"),
                self.option("keep", "opt_keep"),
            ],
            **self.slot_values(appt.practitioner_id, appt.starts_at),
        )
        return True

    def keep_appointment(self, appointment_id: UUID | None) -> None:
        """Nothing changes; say so with the appointment's day and time."""
        self.reset()
        appt = self.e.port.get(appointment_id) if appointment_id else None
        if appt is None:
            self.menu()
            return
        self.say("cancel_kept", **self.slot_values(appt.practitioner_id, appt.starts_at))

    def begin(self, when: When | None) -> None:
        """Common start of booking and rescheduling: check there is anything to offer."""
        slot, within = self.e.port.first_opening(self.scope(), now=self.now)
        if slot is None:
            self.no_openings()
            return
        if not within:
            self.state.step = Step.OFFER_EARLIEST
            self.state.earliest = slot.starts_at
            self.say(
                "no_openings_soon",
                self.yes_no(),
                **self.slot_values(slot.practitioner_id, slot.starts_at),
            )
            return
        if when is not None and not when.empty:
            self.apply_when(when)
        else:
            self.ask_day()

    def no_openings(self) -> None:
        purpose = self.state.purpose
        self.reset()
        self.say("no_openings")
        self.tasks.append(StaffTask(kind="no_openings", reason=f"no free time to {purpose}"))
        self.event(EventType.HANDOFF_TASK_CREATED, kind="no_openings")

    def apply_when(self, when: When) -> None:
        if when.day is not None:
            if when.day < self.today:
                self.ask_day()
            else:
                self.choose_day(when.day, when.time, when.part)
        elif when.time is not None and self.state.day is not None:
            self.choose_day(self.state.day, when.time, when.part)
        elif when.part is not None and self.state.day is not None and self._has_part(when.part):
            # "no, evening instead": the same day, if it has that part of the day free.
            self.choose_day(self.state.day, None, when.part)
        elif when.time is not None or when.part is not None:
            # A time without a day: the first day that has it.
            self.time_on_first_day(when)
        else:
            self.ask_day(from_day=when.from_day)

    def _has_part(self, part: str) -> bool:
        day = self.state.day
        if day is None or day < self.today:
            return False
        slots = self.e.port.open_slots(self.scope(), day=day, now=self.now)
        return _in_part(slots, part, self.e.tz)

    def time_on_first_day(self, when: When) -> None:
        for day in self.e.port.open_days(self.scope(), now=self.now):
            slots = self.e.port.open_slots(self.scope(), day=day, now=self.now)
            if when.time is not None and _matching(slots, when.time, self.e.tz):
                self.choose_day(day, when.time)
                return
            if when.time is None and _in_part(slots, when.part, self.e.tz):
                self.choose_day(day, None, when.part)
                return
        self.ask_day()

    def ask_day(
        self, from_day: date | None = None, prefix_key: str = "ask_day", **values: str
    ) -> None:
        limit = self.e.port.settings.days_per_list
        start = from_day if from_day is not None and from_day > self.today else None
        days = self.e.port.open_days(self.scope(), now=self.now, from_day=start, limit=limit + 1)
        if not days:
            self.no_openings()
            return
        more = len(days) > limit
        days = days[:limit]
        self.state.step = Step.PICK_DAY
        self.state.day = None
        self.state.days_from = days[-1] + timedelta(days=1)
        options = [
            Option(id=f"day:{d.isoformat()}", label=day_label(d, self.state.language, self.today))
            for d in days
        ]
        if more:
            options.append(self.option("more_days", "opt_more_days"))
        self.say(prefix_key, options, **values)

    def choose_day(
        self, day: date, clock: ClockTime | None = None, part: str | None = None
    ) -> None:
        slots = self.e.port.open_slots(self.scope(), day=day, now=self.now)
        if not slots:
            self.ask_day(
                from_day=day + timedelta(days=1),
                prefix_key="day_full",
                date=long_date(day, self.state.language),
            )
            return
        self.state.day = day
        self.state.times_offset = 0
        if clock is not None:
            self.resolve_time(slots, clock)
            return
        if part is not None:
            first = next(
                (i for i, s in enumerate(slots) if _part_of(s.starts_at, self.e.tz) == part), 0
            )
            self.state.times_offset = first
        self.ask_time(slots)

    def ask_time(
        self, slots: list[Slot] | None = None, prefix_key: str = "ask_time", **values: str
    ) -> None:
        day = self.state.day
        if day is None or day < self.today:
            self.ask_day()
            return
        if slots is None:
            slots = self.e.port.open_slots(self.scope(), day=day, now=self.now)
        if not slots:
            self.ask_day(
                from_day=day + timedelta(days=1),
                prefix_key="day_full",
                date=long_date(day, self.state.language),
            )
            return
        offset = self.state.times_offset if self.state.times_offset < len(slots) else 0
        self.state.times_offset = offset
        page = slots[offset : offset + TIMES_PER_PAGE]
        options = [self._time_option(s) for s in page]
        if offset + TIMES_PER_PAGE < len(slots):
            options.append(self.option("more_times", "opt_more_times"))
        options.append(self.option("other_day", "opt_other_day"))
        self.state.step = Step.PICK_TIME
        self.say(prefix_key, options, date=long_date(day, self.state.language), **values)

    def _time_option(self, slot: Slot) -> Option:
        local = slot.starts_at.astimezone(self.e.tz)
        return Option(
            id=f"time:{slot.starts_at.isoformat()}", label=time_label(local, self.state.language)
        )

    def resolve_time(self, slots: list[Slot], clock: ClockTime) -> None:
        exact = _matching(slots, clock, self.e.tz)
        if exact is not None:
            self.try_hold(exact.starts_at)
            return
        nearest = _nearest(slots, clock, self.e.tz, 3)
        asked = _asked_time(slots, clock, self.e.tz)
        options = [self._time_option(s) for s in nearest]
        options.append(self.option("other_day", "opt_other_day"))
        self.state.step = Step.PICK_TIME
        self.say(
            "time_not_free",
            options,
            date=long_date(self.state.day or self.today, self.state.language),
            time=time_label(asked, self.state.language),
        )

    def try_hold(self, starts_at: datetime, *, book_now: bool = False) -> None:
        self.drop_hold()
        reschedule_of = (
            self.state.appointment_id if self.state.purpose is Purpose.RESCHEDULE else None
        )
        try:
            hold = self.e.port.hold(
                self.scope(), starts_at=starts_at, now=self.now, reschedule_of=reschedule_of
            )
        except SlotUnavailable:
            self.state.day = starts_at.astimezone(self.e.tz).date()
            self.state.times_offset = 0
            self.ask_time(prefix_key="slot_lost")
            return
        except NotActive:
            self.reset()
            self.say("reschedule_none", [self.option("book", "opt_book")])
            return
        self.state.hold = hold
        self.state.day = starts_at.astimezone(self.e.tz).date()
        if book_now:
            self.book_held()
            return
        self.state.step = Step.CONFIRM_SLOT
        self.say(
            "confirm_slot", self.yes_no(), **self.slot_values(hold.practitioner_id, hold.starts_at)
        )

    def book_held(self) -> None:
        hold = self.state.hold
        if hold is None:
            self.ask_day()
            return
        try:
            appt = self.e.port.book(hold.id, now=self.now)
        except HoldExpired:
            # The patient took longer than the hold. If the time is still free, take it again.
            self.state.hold = None
            try:
                again = self.e.port.hold(
                    self.scope(),
                    starts_at=hold.starts_at,
                    now=self.now,
                    reschedule_of=self.state.appointment_id
                    if self.state.purpose is Purpose.RESCHEDULE
                    else None,
                )
                appt = self.e.port.book(again.id, now=self.now)
            except (SlotUnavailable, HoldExpired):
                self.state.day = hold.starts_at.astimezone(self.e.tz).date()
                self.state.times_offset = 0
                self.ask_time(prefix_key="slot_lost")
                return
            except NotActive:
                self.reset()
                self.say("reschedule_none", [self.option("book", "opt_book")])
                return
        except NotActive:
            self.state.hold = None
            self.reset()
            self.say("reschedule_none", [self.option("book", "opt_book")])
            return
        self.state.hold = None
        purpose = self.state.purpose
        values = self.slot_values(appt.practitioner_id, appt.starts_at)
        self.reset()
        if purpose is Purpose.RESCHEDULE:
            key = "reschedule_pending" if appt.status == "pending_approval" else "rescheduled"
            self.say(key, **values)
        else:
            self.say("booked", **values)

    def abandon(self) -> None:
        if self.state.purpose is Purpose.RESCHEDULE:
            self.keep_appointment(self.state.appointment_id)
            return
        self.reset()
        self.say("abandon_booking")

    # -- existing appointments ----------------------------------------------------------

    def start_reschedule(self, when: When | None = None) -> None:
        self.reset()
        appt = self.e.port.upcoming(now=self.now)
        if appt is None:
            self.say("reschedule_none", [self.option("book", "opt_book")])
            return
        self.state.purpose = Purpose.RESCHEDULE
        self.state.appointment_id = appt.id
        self.state.practitioner_id = appt.practitioner_id
        self.event(EventType.BOOKING_STARTED, purpose="reschedule")
        if when is not None and not when.empty:
            self.begin(when)
            return
        slot, within = self.e.port.first_opening(self.scope(), now=self.now)
        if slot is None or not within:
            self.begin(None)
            return
        self.ask_day(
            prefix_key="reschedule_start", **self.slot_values(appt.practitioner_id, appt.starts_at)
        )

    def start_cancel(self) -> None:
        self.reset()
        appt = self.e.port.upcoming(now=self.now)
        if appt is None:
            self.say("cancel_none", [self.option("book", "opt_book")])
            return
        self.state.step = Step.CONFIRM_CANCEL
        self.state.appointment_id = appt.id
        self.say(
            "cancel_ask",
            self.cancel_options(),
            **self.slot_values(appt.practitioner_id, appt.starts_at),
        )

    def cancel_appointment(self) -> None:
        appointment_id = self.state.appointment_id
        self.reset()
        try:
            if appointment_id is None:
                raise NotActive("nothing to cancel")
            appt = self.e.port.cancel(appointment_id, now=self.now)
        except NotActive:
            self.say("cancel_none", [self.option("book", "opt_book")])
            return
        self.say(
            "cancelled",
            [self.option("book", "opt_book")],
            **self.slot_values(appt.practitioner_id, appt.starts_at),
        )

    def ask_confirm_appointment(self, appointment_id: UUID, key: str) -> None:
        appt = self.e.port.get(appointment_id)
        if appt is None:
            return
        self.reset()
        self.state.step = Step.CONFIRM_APPOINTMENT
        self.state.appointment_id = appt.id
        self.say(
            key, self.confirm_options(), **self.slot_values(appt.practitioner_id, appt.starts_at)
        )

    def confirm_appointment(self) -> None:
        appointment_id = self.state.appointment_id
        if appointment_id is None:
            upcoming = self.e.port.upcoming(now=self.now)
            appointment_id = upcoming.id if upcoming else None
        self.reset()
        try:
            if appointment_id is None:
                raise NotActive("nothing to confirm")
            appt: Appointment = self.e.port.confirm(appointment_id, now=self.now)
        except NotActive:
            self.say("nothing_to_confirm", self.menu_options())
            return
        self.say("confirmed", **self.slot_values(appt.practitioner_id, appt.starts_at))


_YES_NO_STEPS = frozenset(
    {
        Step.WELCOME,
        Step.CONFIRM_SLOT,
        Step.OFFER_EARLIEST,
        Step.CONFIRM_CANCEL,
        Step.CONFIRM_APPOINTMENT,
        Step.CONFIRM_SECOND,
    }
)
# Yes/no questions where a day or time in the answer means "this instead".
_TIME_BEATS_NO = frozenset(
    {
        Step.WELCOME,
        Step.CONFIRM_SLOT,
        Step.OFFER_EARLIEST,
        Step.CONFIRM_APPOINTMENT,
        Step.CONFIRM_SECOND,
    }
)
# Intents that ask for something to be done (an FAQ alongside them is answered first).
_ACTIONS: frozenset[Intent] = frozenset({"book", "reschedule", "cancel", "confirm", "human"})


def _part_of(moment: datetime, tz: ZoneInfo) -> str:
    hour = moment.astimezone(tz).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 16:
        return "afternoon"
    if 16 <= hour < 20:
        return "evening"
    return "night"


def _in_part(slots: list[Slot], part: str | None, tz: ZoneInfo) -> bool:
    return any(_part_of(s.starts_at, tz) == part for s in slots)


def _local_minutes(moment: datetime, tz: ZoneInfo) -> int:
    local = moment.astimezone(tz)
    return local.hour * 60 + local.minute


def _matching(slots: list[Slot], clock: ClockTime, tz: ZoneInfo) -> Slot | None:
    for candidate in clock.candidates():
        for slot in slots:
            local = slot.starts_at.astimezone(tz)
            if local.hour == candidate.hour and local.minute == candidate.minute:
                return slot
    return None


def _candidate_minutes(clock: ClockTime) -> list[int]:
    return [t.hour * 60 + t.minute for t in clock.candidates()]


def _nearest(slots: list[Slot], clock: ClockTime, tz: ZoneInfo, n: int) -> list[Slot]:
    wanted = _candidate_minutes(clock)

    def distance(slot: Slot) -> int:
        m = _local_minutes(slot.starts_at, tz)
        return min(abs(m - w) for w in wanted)

    return sorted(sorted(slots, key=distance)[:n], key=lambda s: s.starts_at)


def _asked_time(slots: list[Slot], clock: ClockTime, tz: ZoneInfo) -> datetime:
    """The asked time as a datetime, choosing am/pm nearest the clinic's open times."""
    day = slots[0].starts_at.astimezone(tz).date()
    best = min(
        clock.candidates(),
        key=lambda t: min(
            abs(_local_minutes(s.starts_at, tz) - (t.hour * 60 + t.minute)) for s in slots
        ),
    )
    return datetime.combine(day, time(best.hour, best.minute), tz)


__all__ = [
    "EMERGENCY_PLACEHOLDERS",
    "REQUIRED_MESSAGES",
    "FlowContext",
    "FlowEngine",
    "placeholders",
]
