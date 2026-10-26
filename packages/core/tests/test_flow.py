"""Scripted conversations through the flow engine (fixtures in conftest.py).

The clock starts Monday 7 January 2030, 10:00 IST. Dr Asha works Mon-Sat 10-13 and Mon-Fri
17-20; Dr Rohan Mon/Wed/Fri 9-12. Minimum lead time is 60 minutes.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from salesbot_core.events import EventType
from salesbot_core.flow import DraftTextBlocked, FlowContext, FlowEngine, Step, ensure_sendable
from salesbot_core.flow.engine import PLACEHOLDERS, REQUIRED_MESSAGES, TIMES_PER_PAGE, placeholders
from salesbot_core.flow.memory import InMemoryScheduling
from salesbot_core.packs import load_pack_by_id
from salesbot_core.scheduling import AppointmentStatus, Interval, SchedulingSettings, Slot
from salesbot_core.scheduling.settings import RescheduleApproval

IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 1, day, hour, minute, tzinfo=IST)


def keys(turn) -> list[str]:
    return [r.key for r in turn.replies]


def event_types(turn) -> list[EventType]:
    return [e.type for e in turn.events]


def statuses(chat) -> list[AppointmentStatus]:
    return sorted(r.status for r in chat.port.rows.values())


def book_tomorrow_11(chat) -> None:
    chat.send("book")
    chat.send("tomorrow 11 am")
    assert chat.send("yes").replies[0].key == "booked"


# -- booking ------------------------------------------------------------------------------


def test_welcome_then_book_by_tapping(make_chat):
    chat = make_chat()
    turn = chat.welcome()
    assert keys(turn) == ["welcome"]
    assert chat.option_ids() == ["book", "not_now"]
    assert "Test Patient" in turn.replies[0].text

    turn = chat.tap("book")
    assert keys(turn) == ["ask_day"]
    assert EventType.BOOKING_STARTED in event_types(turn)
    days = chat.option_ids()
    # Today first (Dr Asha is free from 11:00 after the 60 min lead time), 9 days per list.
    assert days[0] == "day:2030-01-07"
    assert len([d for d in days if d.startswith("day:")]) == 9
    assert days[-1] == "more_days"
    # Sundays are closed.
    assert "day:2030-01-13" not in days

    turn = chat.tap("day:2030-01-08")
    assert keys(turn) == ["ask_time"]
    assert "Tuesday, 8 January" in turn.replies[0].text
    times = chat.option_ids()
    assert times[0] == f"time:{at(8, 10).isoformat()}"
    assert len(times) <= TIMES_PER_PAGE + 2
    assert times[-2:] == ["more_times", "other_day"]

    turn = chat.tap(times[0])
    assert keys(turn) == ["confirm_slot"]
    assert chat.state.step is Step.CONFIRM_SLOT
    assert chat.state.hold is not None
    assert "Tuesday, 8 January at 10:00 am" in turn.replies[0].text

    turn = chat.tap("yes")
    assert keys(turn) == ["booked"]
    assert "Tuesday, 8 January at 10:00 am" in turn.replies[0].text
    assert chat.state.step is Step.IDLE
    assert statuses(chat) == [AppointmentStatus.BOOKED]


def test_book_in_one_message(make_chat):
    chat = make_chat()
    turn = chat.send("I want to book tomorrow at 11 am")
    assert keys(turn) == ["confirm_slot"]
    assert chat.state.hold is not None
    assert chat.state.hold.starts_at == at(8, 11)
    assert keys(chat.send("yes")) == ["booked"]


def test_numbers_pick_options(make_chat):
    chat = make_chat()
    chat.welcome()
    assert keys(chat.send("1")) == ["ask_day"]
    assert keys(chat.send("2")) == ["ask_time"]
    assert chat.state.day.isoformat() == "2030-01-08"
    assert keys(chat.send("1")) == ["confirm_slot"]


def test_ambiguous_hour_resolves_against_open_times(make_chat):
    chat = make_chat()
    chat.send("book")
    # "5" on Tuesday: Dr Asha works 17-20, so it means 5 pm.
    turn = chat.send("tuesday 5 baje")
    assert keys(turn) == ["confirm_slot"]
    assert chat.state.hold.starts_at == at(8, 17)


def test_time_not_free_offers_the_nearest(make_chat):
    chat = make_chat()
    chat.send("book")
    turn = chat.send("tomorrow 2 pm")
    assert keys(turn) == ["time_not_free"]
    assert "2:00 pm is not free" in turn.replies[0].text
    ids = chat.option_ids()
    assert len(ids) == 4 and ids[-1] == "other_day"


def test_full_day_moves_to_the_next_days(make_chat):
    chat = make_chat()
    chat.send("book")
    turn = chat.send("sunday")
    assert keys(turn) == ["day_full"]
    assert "Sunday, 13 January" in turn.replies[0].text
    assert chat.option_ids()[0] == "day:2030-01-14"


def test_more_times_pages(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.tap("day:2030-01-08")
    first_page = chat.option_ids()
    chat.tap("more_times")
    second_page = chat.option_ids()
    assert second_page[0] not in first_page
    assert second_page[-1] == "other_day"


def test_saying_no_to_a_slot_releases_the_hold(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    assert statuses(chat) == [AppointmentStatus.HELD]
    turn = chat.send("no")
    assert keys(turn) == ["pick_again"]
    assert chat.port.rows == {}
    assert chat.state.hold is None


def test_abandoning_a_booking(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    chat.tap("other_day")
    assert chat.port.rows == {}
    assert keys(chat.send("no")) == ["abandon_booking"]
    assert chat.state.step is Step.IDLE


def test_expired_hold_is_taken_again_if_still_free(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    chat.wait(10)  # hold is 5 minutes
    assert keys(chat.send("yes")) == ["booked"]
    assert statuses(chat) == [AppointmentStatus.BOOKED]


def test_slot_taken_by_someone_else(make_chat, clinic):
    chat = make_chat()
    chat.send("book")
    chat.tap("day:2030-01-08")
    wanted = chat.option_ids()[0]
    # Another patient takes Tuesday 10:00 with Dr Asha (the only doctor in on Tuesdays)
    # before this one taps it.
    chat.port.add_booking(Slot(practitioner_id=clinic.dr_a, starts_at=at(8, 10), ends_at=at(8, 11)))
    turn = chat.tap(wanted)
    assert keys(turn) == ["slot_lost"]
    assert wanted not in chat.option_ids()


def test_no_openings_creates_a_staff_task(make_chat):
    port = InMemoryScheduling(settings=SchedulingSettings(), calendars=[], names={})
    chat = make_chat(port=port)
    turn = chat.send("book")
    assert keys(turn) == ["no_openings"]
    assert [t.kind for t in turn.tasks] == ["no_openings"]
    assert EventType.HANDOFF_TASK_CREATED in event_types(turn)


def test_nothing_in_lookahead_offers_the_earliest(make_chat, clinic):
    calendars = [
        cal.model_copy(update={"closed": [Interval(start=at(1, 0), end=at(25, 0))]})
        for cal in clinic.calendars()
    ]
    port = InMemoryScheduling(
        settings=SchedulingSettings(lookahead_days=7), calendars=calendars, names=clinic.names
    )
    chat = make_chat(port=port)
    turn = chat.send("book")
    assert keys(turn) == ["no_openings_soon"]
    assert "Friday, 25 January" in turn.replies[0].text
    assert keys(chat.send("yes")) == ["booked"]


# -- existing appointments -----------------------------------------------------------------


def test_reschedule_moves_the_appointment(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    turn = chat.send("I want to reschedule")
    assert keys(turn) == ["reschedule_start"]
    assert "Tuesday, 8 January at 11:00 am" in turn.replies[0].text
    chat.tap("day:2030-01-09")
    chat.tap(chat.option_ids()[0])
    turn = chat.send("yes")
    assert keys(turn) == ["rescheduled"]
    assert statuses(chat) == [AppointmentStatus.BOOKED, AppointmentStatus.RESCHEDULED]
    old = next(r for r in chat.port.rows.values() if r.status is AppointmentStatus.RESCHEDULED)
    new = next(r for r in chat.port.rows.values() if r.status is AppointmentStatus.BOOKED)
    assert new.rescheduled_from_id == old.id
    # Reschedules keep the original doctor.
    assert new.practitioner_id == old.practitioner_id


def test_reschedule_with_a_time_in_the_message(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    turn = chat.send("change to wednesday 12")
    assert keys(turn) == ["confirm_slot"]
    assert chat.state.hold.starts_at == at(9, 12)


def test_reschedule_needing_staff_approval(make_chat):
    chat = make_chat(settings=SchedulingSettings(reschedule_approval=RescheduleApproval.STAFF))
    book_tomorrow_11(chat)
    chat.send("reschedule wednesday 12")
    assert keys(chat.send("yes")) == ["reschedule_pending"]
    assert statuses(chat) == [AppointmentStatus.BOOKED, AppointmentStatus.PENDING_APPROVAL]


def test_reschedule_without_an_appointment(make_chat):
    chat = make_chat()
    assert keys(chat.send("reschedule")) == ["reschedule_none"]
    assert chat.option_ids() == ["book"]


def test_cancel_needs_confirmation(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    turn = chat.send("cancel")
    assert keys(turn) == ["cancel_ask"]
    assert chat.option_ids() == ["yes", "keep"]
    assert keys(chat.tap("keep")) == ["cancel_kept"]
    assert statuses(chat) == [AppointmentStatus.BOOKED]

    chat.send("cancel")
    assert keys(chat.tap("yes")) == ["cancelled"]
    assert statuses(chat) == [AppointmentStatus.CANCELLED]


def test_cancel_without_an_appointment(make_chat):
    chat = make_chat()
    assert keys(chat.send("cancel")) == ["cancel_none"]


def test_confirmation_request_and_reminder(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    appt = chat.port.upcoming(now=chat.now)
    turn = chat.engine.confirmation_request(chat.state, appt.id, chat.now)
    chat.state = turn.state
    assert keys(turn) == ["confirm_request"]
    assert chat.option_ids() == ["confirm", "reschedule", "cancel"]
    assert keys(chat.send("yes")) == ["confirmed"]
    assert statuses(chat) == [AppointmentStatus.CONFIRMED]

    turn = chat.engine.reminder(chat.state, appt.id, chat.now)
    assert keys(turn) == ["reminder"]
    assert "Tuesday, 8 January at 11:00 am" in turn.replies[0].text


# -- interrupts ----------------------------------------------------------------------------


def test_emergency_at_any_step_releases_the_hold(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    turn = chat.send("my father has chest pain")
    assert keys(turn) == ["emergency"]
    assert "+910000000000" in turn.replies[0].text
    assert [(t.kind, t.urgent) for t in turn.tasks] == [("emergency", True)]
    assert event_types(turn) == [
        EventType.EMERGENCY_DETECTED,
        EventType.HANDOFF_REQUESTED,
        EventType.HANDOFF_TASK_CREATED,
    ]
    assert chat.port.rows == {}
    assert chat.state.step is Step.IDLE


def test_emergency_in_marathi(make_chat):
    chat = make_chat()
    turn = chat.send("मला छातीत दुखतंय")
    # Last known language first (the language does not switch), then Marathi.
    assert keys(turn) == ["emergency", "emergency"]
    assert [r.language for r in turn.replies] == ["en", "mr"]
    assert chat.state.language == "en"


def test_sincere_ask_answers_then_continues(make_chat):
    chat = make_chat()
    chat.send("book")
    turn = chat.send("are you a bot?")
    assert keys(turn) == ["sincere_ask", "ask_day"]
    assert turn.replies[0].draft
    assert chat.state.step is Step.PICK_DAY


def test_asking_for_a_person(make_chat):
    chat = make_chat()
    turn = chat.send("please call me back")
    assert keys(turn) == ["human_requested"]
    assert [t.kind for t in turn.tasks] == ["human_requested"]


def test_stop_opts_out(make_chat):
    chat = make_chat()
    turn = chat.send("stop")
    assert turn.opt_out
    assert keys(turn) == ["opted_out"]


def test_two_misses_hand_over_to_staff(make_chat):
    chat = make_chat()
    chat.send("book")
    turn = chat.send("purple elephants")
    assert keys(turn) == ["not_understood"]
    assert chat.option_ids()[0].startswith("day:")
    turn = chat.send("purple elephants")
    assert keys(turn) == ["handed_to_staff"]
    assert [t.kind for t in turn.tasks] == ["not_understood"]
    assert chat.state.misses == 0


def test_a_good_message_resets_misses(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("purple elephants")
    chat.send("tomorrow 11 am")
    assert chat.state.misses == 0


def test_thanks_after_a_task_gets_a_short_reply(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    assert keys(chat.send("thank you")) == ["ack"]


def test_paused_automation_stays_silent(make_chat):
    chat = make_chat(paused=True)
    assert chat.send("book").replies == []


# -- languages -----------------------------------------------------------------------------


def test_reply_follows_the_message_language(make_chat):
    chat = make_chat()
    turn = chat.send("मला उद्या अपॉइंटमेंट हवी आहे")
    assert turn.inbound_language == "mr"
    assert keys(turn) == ["ask_time"]
    assert turn.replies[0].language == "mr"
    assert "मंगळवार, 8 जानेवारी" in turn.replies[0].text
    # Neutral replies keep the language.
    turn = chat.send("1")
    assert {r.language for r in turn.replies} == {"mr"}
    assert turn.inbound_language is None


def test_hindi_slot_text(make_chat):
    chat = make_chat("hi")
    chat.send("book")
    turn = chat.send("kal subah 11 baje")
    assert keys(turn) == ["confirm_slot"]
    assert turn.replies[0].language == "hi"
    assert "मंगलवार, 8 जनवरी" in turn.replies[0].text
    assert "सुबह 11:00" in turn.replies[0].text


def test_explicit_switch_reprompts(make_chat):
    chat = make_chat()
    chat.send("book")
    turn = chat.send("Marathi please")
    assert keys(turn) == ["language_switched", "ask_day"]
    assert {r.language for r in turn.replies} == {"mr"}
    assert chat.state.language == "mr"


def test_unsupported_script_gets_the_clinic_language(make_chat):
    chat = make_chat(default_language="mr")
    turn = chat.send("હું કાલે આવીશ")
    assert turn.inbound_language == "other"
    assert {r.language for r in turn.replies} == {"mr"}


def test_new_state_uses_the_clinic_language_for_unknown_codes(make_chat):
    chat = make_chat("ta", default_language="hi")
    assert chat.state.language == "hi"


@pytest.mark.parametrize(
    "text",
    ["ok", "yes", "2", "tomorrow 11", "can I come kal", "thanks", "ji", "call me"],
)
def test_hindi_is_sticky_through_neutral_and_stray_words(make_chat, text):
    chat = make_chat("hi")
    chat.send("book")
    turn = chat.send(text)
    assert chat.state.language == "hi"
    assert {r.language for r in turn.replies} <= {"hi"}


@pytest.mark.parametrize("text", ["ok", "haan", "kal", "2", "tomorrow 11"])
def test_english_is_sticky_through_neutral_and_stray_words(make_chat, text):
    chat = make_chat("en")
    chat.send("book")
    chat.send(text)
    assert chat.state.language == "en"


def test_switching_between_english_and_hinglish(make_chat):
    chat = make_chat("en")
    turn = chat.send("mujhe kal appointment chahiye")
    assert chat.state.language == "hi"
    assert {r.language for r in turn.replies} == {"hi"}
    chat.send("ok")
    assert chat.state.language == "hi"
    turn = chat.send("I want to book for friday please")
    assert chat.state.language == "en"
    assert {r.language for r in turn.replies} == {"en"}


def test_marathi_is_off_unless_the_clinic_allows_it(make_chat):
    chat = make_chat(settings=SchedulingSettings())  # default: en and hi
    turn = chat.send("मला उद्या अपॉइंटमेंट हवी आहे")
    assert turn.inbound_language == "mr"
    assert {r.language for r in turn.replies} == {"en"}
    chat.send("Marathi please")
    assert chat.state.language == "en"
    # A clinic that never offers Marathi cannot start a conversation in it either.
    chat = make_chat("mr", settings=SchedulingSettings())
    assert chat.state.language == "en"


# -- DRAFT guard ---------------------------------------------------------------------------


def test_hindi_and_marathi_replies_are_draft(make_chat):
    for language in ("hi", "mr"):
        chat = make_chat(language)
        turn = chat.welcome()
        assert all(r.draft for r in turn.replies)
        with pytest.raises(DraftTextBlocked):
            ensure_sendable(turn.replies, live=True)
        # The dev console may show them.
        assert ensure_sendable(turn.replies, live=False) == turn.replies


def test_english_is_sendable_but_the_emergency_line_is_draft(make_chat):
    chat = make_chat()
    turn = chat.welcome()
    assert ensure_sendable(turn.replies, live=True) == turn.replies
    turn = chat.send("chest pain")
    assert turn.replies[0].draft
    # Never blocked: live channels get the numbers-only fallback (tests in test_emergency.py).
    assert [r.key for r in ensure_sendable(turn.replies, live=True)] == ["emergency_fallback"]


# -- pack contract -------------------------------------------------------------------------


def test_clinic_pack_defines_every_message_in_every_language():
    pack = load_pack_by_id("clinic")
    for key in REQUIRED_MESSAGES:
        assert set(pack.conversation.messages[key]) == set(pack.languages), key


def test_pack_messages_use_known_placeholders():
    pack = load_pack_by_id("clinic")
    for key, localized in pack.conversation.messages.items():
        for language, text in localized.items():
            assert placeholders(text) <= PLACEHOLDERS, (key, language)
    for text in pack.conversation.sincere_ask.answer.values():
        assert placeholders(text) <= PLACEHOLDERS


def test_option_labels_fit_whatsapp_buttons():
    pack = load_pack_by_id("clinic")
    for key, localized in pack.conversation.messages.items():
        if key.startswith("opt_"):
            for language, text in localized.items():
                assert len(text) <= 20, (key, language, text)


def test_engine_rejects_a_pack_missing_messages(clinic):
    pack = load_pack_by_id("clinic")
    trimmed = pack.conversation.model_copy(
        update={"messages": {k: v for k, v in pack.conversation.messages.items() if k != "booked"}}
    )
    port = InMemoryScheduling(settings=SchedulingSettings(), calendars=[], names={})
    with pytest.raises(ValueError, match="booked"):
        FlowEngine(pack.model_copy(update={"conversation": trimmed}), port, FlowContext("C", "P"))


def test_engine_rejects_a_pack_without_conversation():
    port = InMemoryScheduling(settings=SchedulingSettings(), calendars=[], names={})
    with pytest.raises(ValueError, match="no conversation"):
        FlowEngine(load_pack_by_id("salon"), port, FlowContext("C", "P"))


def test_day_labels_fit_whatsapp_list_rows(make_chat):
    for language in ("en", "hi", "mr"):
        chat = make_chat(language)
        chat.send("book")
        for option in chat.state.offered:
            assert len(option.label) <= 24, (language, option.label)


def test_clock_is_ist_not_utc(make_chat):
    chat = make_chat()
    # 23:00 IST on Monday is 17:30 UTC: "tomorrow" must still mean Tuesday.
    chat.now = at(7, 23) - timedelta(seconds=30)
    chat.send("book")
    turn = chat.send("tomorrow 11 am")
    assert chat.state.hold.starts_at == at(8, 11)
    assert "Tuesday, 8 January" in turn.replies[0].text
