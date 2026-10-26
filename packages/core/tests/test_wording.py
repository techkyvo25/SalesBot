"""The wording scan (owner's policy, docs/DECISIONS.md) over every patient-facing text.

Patient-facing text never mentions AI, bots, virtual or automated assistants and never claims
to be a person or staff member. The only allowed exception is the sincere-ask answer.
"""

import pytest

from salesbot_core.packs import load_all_packs, load_pack_by_id
from salesbot_core.text.wording import forbidden_terms, patient_texts

PACKS = load_all_packs()


@pytest.mark.parametrize(
    "text",
    [
        "I am an AI assistant",
        "This is an A.I. service",
        "our chatbot will help",
        "a virtual receptionist",
        "this is automated",
        "an automatic reply",
        "I'm a person",
        "talk to a real person",
        "a human will call",
        "यह एआई सहायक है",
        "मैं एक बॉट हूँ",
        "यह स्वचालित सेवा है",
        "हम इंसान हैं",
        "ही स्वयंचलित सेवा आहे",
        "मी माणूस आहे",
        "व्हर्च्युअल सहाय्यक",
        "रोबोट",
    ],
)
def test_scan_catches_forbidden_terms(text):
    assert forbidden_terms(text)


@pytest.mark.parametrize(
    "text",
    [
        "Your appointment is booked: Dr. Asha, Tuesday, 8 January at 11:00 am.",
        "Please call the clinic now.",
        "Wait a moment",  # "ai" inside a word
        "Email us",
        "आपका अपॉइंटमेंट बुक हो गया है",
        "तुमची अपॉइंटमेंट बुक झाली आहे",
    ],
)
def test_scan_allows_ordinary_text(text):
    assert forbidden_terms(text) == []


@pytest.mark.parametrize("pack_id", sorted(PACKS))
def test_pack_patient_text_is_clean(pack_id):
    hits = [
        (where, language, forbidden_terms(text))
        for where, language, text in patient_texts(PACKS[pack_id])
        if forbidden_terms(text)
    ]
    assert hits == []


def test_sincere_ask_answer_is_the_only_exception():
    """The answer must stay truthful (it does mention automation) and is DRAFT."""
    sincere = load_pack_by_id("clinic").conversation.sincere_ask
    assert sincere.draft
    for language in ("en", "hi", "mr"):
        assert forbidden_terms(sincere.answer[language]), language
    scanned = {text for _, _, text in patient_texts(load_pack_by_id("clinic"))}
    assert not scanned & set(sincere.answer.values())


SCRIPTS = [
    ["book", "tomorrow 11 am", "yes", "reschedule", "wednesday 12", "yes", "cancel", "yes"],
    ["hello", "purple elephants", "purple elephants"],
    ["book", "sunday", "1", "more", "no", "no"],
    ["please call me back", "stop"],
    ["chest pain"],
    ["cancel", "reschedule", "thank you"],
]


@pytest.mark.parametrize("language", ["en", "hi", "mr"])
@pytest.mark.parametrize("script", SCRIPTS)
def test_engine_replies_are_clean(make_chat, language, script):
    chat = make_chat(language)
    turns = [chat.welcome()]
    turns += [chat.send(line) for line in script]
    appt = chat.port.upcoming(now=chat.now)
    if appt is not None:
        turns.append(chat.engine.confirmation_request(chat.state, appt.id, chat.now))
        turns.append(chat.engine.reminder(chat.state, appt.id, chat.now))
    for turn in turns:
        for reply in turn.replies:
            if reply.key == "sincere_ask":
                continue
            texts = [reply.text, *(o.label for o in reply.options)]
            for text in texts:
                assert forbidden_terms(text) == [], (reply.key, reply.language, text)


FAQ_AND_ACK_SCRIPT = [
    "what are your opening hours",
    "where is the clinic",
    "is there parking",
    "how much is the fee",
    "do you take insurance",
    "is the doctor good?",
    "ok",
    "👍",
    "confirm my appointment",
]


@pytest.mark.parametrize("language", ["en", "hi", "mr"])
def test_faq_and_acknowledgement_replies_are_clean(make_chat, language):
    chat = make_chat(language, demo=True)
    for line in FAQ_AND_ACK_SCRIPT:
        for reply in chat.send(line).replies:
            for text in [reply.text, *(o.label for o in reply.options)]:
                assert forbidden_terms(text) == [], (reply.key, reply.language, text)
