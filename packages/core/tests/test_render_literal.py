"""Literal agent text (`make voice-agent LITERAL=1`), the voice knowledge base, and the rule
that no voice line speaks as part of the clinic team."""

import re

from salesbot_core.packs import load_pack_by_id
from salesbot_core.packs.render_agent import ClinicValues, render
from salesbot_core.text.wording import forbidden_terms_except

PACK = load_pack_by_id("clinic")
CLINIC = ClinicValues("Demo Clinic", "+910000000000", ["112", "108"])
LITERAL = render(PACK, literal=CLINIC)
GENERATED = render(PACK)
STEP_1 = (
    "1. At the start of every call, call identify_patient before anything else. If it lists "
    "several patients, ask which patient the call is about and call identify_patient again with "
    "that patient_id. If the number is not registered, say the tool's spoken_text and offer a "
    "callback."
)


def sections(instruction: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)$", instruction, flags=re.M)[1:]
    pairs = zip(parts[::2], parts[1::2], strict=True)
    return {title: " ".join(body.split()) for title, body in pairs}


def test_literal_output_has_no_variables():
    assert "{{" not in LITERAL.markdown()
    assert "{{" in GENERATED.markdown()  # the generated block keeps Sarvam variables


def test_literal_has_the_same_sections():
    assert list(sections(LITERAL.instruction)) == list(sections(GENERATED.instruction))


def test_literal_context_names_the_clinic_and_identify_patient():
    context = sections(LITERAL.instruction)["Context"]
    for words in ("Demo Clinic", "+910000000000", "112 / 108", "identify_patient"):
        assert words in context
    assert "today's weekday, date and time in India" in context


def test_literal_step_one_calls_identify_patient_first():
    steps = sections(LITERAL.instruction)["Steps"]
    assert steps.startswith(STEP_1)
    # Steps 2 onward are the generated ones, word for word.
    assert steps[steps.index("2. ") :] == (
        sections(GENERATED.instruction)["Steps"].split(" 2. ", 1)[1].join(["2. ", ""])
    )


def test_literal_fills_the_emergency_line_and_greetings():
    emergency = sections(LITERAL.instruction)["Emergencies"]
    assert "+910000000000" in emergency and "112 / 108" in emergency
    assert all("Demo Clinic" in text for text in LITERAL.greetings.values())


def test_literal_text_passes_the_wording_scan():
    allowed = list(LITERAL.sincere_ask.values())
    for text in LITERAL.texts_to_scan():
        assert forbidden_terms_except(text, allowed) == []


def test_clinic_values_from_a_clinic_file(tmp_path):
    f = tmp_path / "clinic.yaml"
    f.write_text("name: Test Clinic\ncontact_phone: '+910000000001'\n", encoding="utf-8")
    values = ClinicValues.from_file(f)
    assert values.variables() == {
        "business_name": "Test Clinic",
        "clinic_phone": "+910000000001",
        "emergency_numbers": "112 / 108",
    }
