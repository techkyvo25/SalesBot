"""Fixes from the message battery (docs/DEMO.md): confirm intent, FAQs, a time beats "no",
acknowledgements, a second appointment, opt-out, and appointment text with weekday, date and
time. Same clinic and clock as test_flow.py (Monday 7 January 2030, 10:00 IST).
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from salesbot_core.events import EventType
from salesbot_core.flow import Step
from salesbot_core.packs import load_pack_by_id
from salesbot_core.scheduling import AppointmentStatus

IST = ZoneInfo("Asia/Kolkata")
TOMORROW_11 = "Tuesday, 8 January at 11:00 am"


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
    assert keys(chat.send("yes")) == ["booked"]


# -- confirm ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["confirm my appointment", "confirm", "can you confirm it"])
def test_confirm_without_an_appointment_says_so_and_never_books(make_chat, text):
    chat = make_chat()
    turn = chat.send(text)
    assert keys(turn) == ["nothing_to_confirm"]
    assert EventType.BOOKING_STARTED not in event_types(turn)
    assert chat.state.step is Step.IDLE
    assert chat.port.rows == {}


@pytest.mark.parametrize("text", ["confirm my appointment", "confirm"])
def test_confirm_confirms_the_upcoming_appointment(make_chat, text):
    chat = make_chat()
    book_tomorrow_11(chat)
    turn = chat.send(text)
    assert keys(turn) == ["confirmed"]
    assert TOMORROW_11 in turn.replies[0].text
    assert statuses(chat) == [AppointmentStatus.CONFIRMED]


@pytest.mark.parametrize("word", ["yes", "ok", "haan", "confirm", "हाँ"])
def test_yes_words_still_answer_a_pending_question(make_chat, word):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    assert keys(chat.send(word)) == ["booked"]


# -- FAQs ---------------------------------------------------------------------------------


def test_opening_hours_come_from_the_doctors_hours(make_chat):
    chat = make_chat()
    turn = chat.send("what are your opening hours")
    assert keys(turn) == ["faq:hours"]
    text = turn.replies[0].text
    assert "Dr. Asha: Mon–Fri 10:00 am–1:00 pm, 5:00 pm–8:00 pm; Sat 10:00 am–1:00 pm" in text
    assert "Dr. Rohan: Mon, Wed, Fri 9:00 am–12:00 pm" in text
    assert turn.tasks == []
    assert EventType.QUESTION_UNANSWERED not in event_types(turn)


@pytest.mark.parametrize(
    ("text", "faq", "expected"),
    [
        ("where is the clinic, how do i get there", "address", "12 Example Road, Pune"),
        ("what's the parking like", "parking", "building basement"),
        ("how much does a consultation cost", "fee", "Rs 500"),
        ("do you take insurance? i have star health", "insurance", "policy card"),
    ],
)
def test_demo_faqs_answer_from_the_pack(make_chat, text, faq, expected):
    chat = make_chat(demo=True)
    turn = chat.send(text)
    assert keys(turn) == [f"faq:{faq}"]
    assert expected in turn.replies[0].text
    assert "(demo text)" in turn.replies[0].text or "(fictional)" in turn.replies[0].text
    assert turn.tasks == []


def test_demo_faqs_stay_silent_outside_the_demo(make_chat):
    chat = make_chat()
    assert keys(chat.send("how much does a consultation cost")) == ["not_understood"]


def test_faq_then_the_current_question_again(make_chat):
    chat = make_chat(demo=True)
    chat.send("book")
    chat.send("tomorrow")
    offered = chat.option_ids()
    turn = chat.send("is there parking?")
    assert keys(turn) == ["faq:parking", "ask_time"]
    assert chat.state.step is Step.PICK_TIME
    assert chat.option_ids() == offered
    assert turn.tasks == []
    # Answered questions never count towards a hand-over.
    for _ in range(3):
        assert chat.send("how much is the fee").tasks == []
    assert chat.state.misses == 0


def test_faq_while_a_slot_is_held_keeps_the_hold(make_chat):
    chat = make_chat(demo=True)
    chat.send("book")
    chat.send("tomorrow 11 am")
    turn = chat.send("ok and what is the fee")
    assert keys(turn) == ["faq:fee", "confirm_slot"]
    assert statuses(chat) == [AppointmentStatus.HELD]


def test_is_the_doctor_good_gets_a_neutral_line(make_chat):
    chat = make_chat()
    turn = chat.send("is the doctor good?")
    assert keys(turn) == ["faq:doctor_quality"]
    reply = turn.replies[0]
    assert "human" in [o.id for o in reply.options]
    for claim in ("good", "best", "experienced", "excellent", "qualified", "great"):
        assert claim not in reply.text.lower()
    assert turn.tasks == []


def test_hindi_faq_is_draft(make_chat):
    chat = make_chat("hi", demo=True)
    turn = chat.send("फीस कितनी है")
    assert keys(turn) == ["faq:fee"]
    assert turn.replies[0].language == "hi"
    assert turn.replies[0].draft


# -- a time or part of day beats "no" -------------------------------------------------------


def test_no_evening_instead_shows_evening_times(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    turn = chat.send("no, evening instead")
    assert keys(turn) == ["ask_time"]
    first = chat.option_ids()[0]
    assert first == f"time:{at(8, 17).isoformat()}"
    assert chat.port.rows == {}  # the morning hold is released


def test_no_with_a_time_holds_that_time(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    turn = chat.send("no, 5 pm please")
    assert keys(turn) == ["confirm_slot"]
    assert chat.state.hold.starts_at == at(8, 17)
    assert statuses(chat) == [AppointmentStatus.HELD]


def test_plain_no_still_declines(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    assert keys(chat.send("no")) == ["pick_again"]


# -- acknowledgements ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["ok", "thanks", "haan", "👍", "🙂", "🙂🙂", "ok 👍", "🙏"])
def test_acknowledgement_with_nothing_pending(make_chat, text):
    chat = make_chat()
    turn = chat.send(text)
    assert keys(turn) == ["ack"]
    assert turn.replies[0].text == "Okay. Message us any time if you need anything."
    assert turn.tasks == [] and turn.events == []
    assert chat.port.rows == {}


def test_acknowledgement_after_booking_does_nothing(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    for text in ("ok", "haan", "👍"):
        assert keys(chat.send(text)) == ["ack"]
    assert statuses(chat) == [AppointmentStatus.BOOKED]


def test_ok_never_books_a_rejected_time(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    chat.send("no")  # rejected: the other times are shown again
    turn = chat.send("ok")
    assert keys(turn) == ["ask_time"]
    assert chat.port.rows == {}


def test_ok_while_picking_repeats_the_question(make_chat):
    chat = make_chat()
    chat.send("book")
    turn = chat.send("ok")
    assert keys(turn) == ["ask_day"]
    assert chat.state.misses == 0


def test_emoji_never_books(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    turn = chat.send("👍")
    assert keys(turn) == ["confirm_slot"]
    assert statuses(chat) == [AppointmentStatus.HELD]


def test_two_times_in_one_message_hold_nothing(make_chat):
    """A school pick-up at 4 and "after 6": no time is guessed, so a later "ok" cannot book."""
    chat = make_chat()
    turn = chat.send(
        "mera beta 4 baje aata hai to mujhe shaam ko 6 ke baad ka time chahiye appointment"
    )
    assert "confirm_slot" not in keys(turn)
    assert chat.state.hold is None
    chat.send("ok")
    assert all(r.status is not AppointmentStatus.BOOKED for r in chat.port.rows.values())


# -- a second appointment -----------------------------------------------------------------


def test_second_booking_asks_first(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    turn = chat.send("I want to book an appointment")
    assert keys(turn) == ["second_ask"]
    assert TOMORROW_11 in turn.replies[0].text
    assert chat.option_ids() == ["yes", "reschedule", "keep"]
    assert EventType.BOOKING_STARTED not in event_types(turn)
    assert keys(chat.send("yes")) == ["ask_day"]


def test_second_booking_declined_keeps_the_first(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    chat.tap("book")
    turn = chat.tap("keep")
    assert keys(turn) == ["cancel_kept"]
    assert TOMORROW_11 in turn.replies[0].text
    assert statuses(chat) == [AppointmentStatus.BOOKED]


def test_second_booking_can_move_the_first_instead(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    chat.send("book")
    assert keys(chat.tap("reschedule")) == ["reschedule_start"]


# -- opt-out ------------------------------------------------------------------------------


def test_stop_asks_the_channel_to_record_the_opt_out(make_chat):
    chat = make_chat()
    chat.send("book")
    chat.send("tomorrow 11 am")
    turn = chat.send("stop")
    assert turn.opt_out
    assert keys(turn) == ["opted_out"]
    assert chat.port.rows == {}  # the hold is released


# -- appointment text ---------------------------------------------------------------------

# Messages that mention appointments but are about having none.
NO_APPOINTMENT = {"reschedule_none", "cancel_none", "nothing_to_confirm"}


def test_every_appointment_message_gives_weekday_date_and_time():
    messages = load_pack_by_id("clinic").conversation.messages
    for key, localized in messages.items():
        if key.startswith("opt_") or key in NO_APPOINTMENT:
            continue
        if "appointment" not in localized["en"].lower():
            continue
        for language, text in localized.items():
            assert "{date}" in text and "{time}" in text, (key, language)


def test_cancel_messages_name_the_appointment(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    chat.send("cancel")
    turn = chat.send("yes")
    assert keys(turn) == ["cancelled"]
    assert TOMORROW_11 in turn.replies[0].text
    book_tomorrow_11(chat)
    chat.send("cancel")
    turn = chat.send("no")
    assert keys(turn) == ["cancel_kept"]
    assert TOMORROW_11 in turn.replies[0].text


def test_appointment_times_are_ist(make_chat):
    chat = make_chat()
    book_tomorrow_11(chat)
    text = chat.send("confirm").replies[0].text
    assert re.search(r"Tuesday, 8 January at 11:00 am", text)
