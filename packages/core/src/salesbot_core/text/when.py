"""Days and times in patient messages, in English, Hindi and Marathi (Devanagari or romanized).

Pure: the caller passes `today` in the clinic's timezone, so "tomorrow" late at night means the
next IST day, not the next UTC day. Nothing here checks availability; the flow engine matches
the result against open slots.

Rules (also in the tests):
- "kal"/"कल" means tomorrow: in a booking conversation nobody books yesterday.
- A weekday name means the nearest such day, today included. "next"/"agle"/"पुढच्या" + weekday
  means the nearest such day after today.
- A date without a year that has already passed this year means next year. "26 tarikh" alone
  means the 26th of this month, or of next month if that has passed.
- A clock time without am/pm and without a part of day ("11", "5 baje") stays ambiguous; the
  flow engine resolves it against the open slots. A lone number is a time only next to a day or
  a part of day ("tomorrow 11", "sakali 10"), or when the caller says so
  (`bare_number_is_time`), because "2" on its own usually picks option 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Literal

from salesbot_core.text.matching import contains_phrase, normalize, tokens

Part = Literal["morning", "afternoon", "evening", "night"]
Meridiem = Literal["am", "pm"]

_TODAY = ("today", "aaj", "आज", "aj")
_TOMORROW = ("tomorrow", "tmrw", "tmr", "tomorow", "kal", "कल", "udya", "uddya", "udyaa", "उद्या")
_DAY_AFTER = (
    "day after tomorrow",
    "day after",
    "parso",
    "parson",
    "परसों",
    "parva",
    "parwa",
    "परवा",
)
_NEXT = ("next", "agle", "agla", "अगले", "अगला", "pudhchya", "pudhcha", "पुढच्या", "पुढील", "pudhil")
_NEXT_WEEK = (
    "next week",
    "agle hafte",
    "अगले हफ्ते",
    "अगले सप्ताह",
    "pudhchya athavdyat",
    "पुढच्या आठवड्यात",
    "पुढील आठवड्यात",
)

_WEEKDAYS: dict[int, tuple[str, ...]] = {
    0: ("monday", "mon", "somvar", "somwar", "सोमवार", "सोमवारी"),
    1: ("tuesday", "tue", "tues", "mangalvar", "mangalwar", "मंगलवार", "मंगळवार", "मंगळवारी"),
    2: ("wednesday", "wed", "budhvar", "budhwar", "बुधवार", "बुधवारी"),
    3: (
        "thursday",
        "thu",
        "thur",
        "thurs",
        "guruvar",
        "guruwar",
        "गुरुवार",
        "गुरूवार",
        "गुरुवारी",
        "brihaspativar",
        "बृहस्पतिवार",
    ),
    4: ("friday", "fri", "shukravar", "shukrawar", "शुक्रवार", "शुक्रवारी"),
    5: ("saturday", "sat", "shanivar", "shaniwar", "शनिवार", "शनिवारी"),
    6: ("sunday", "sun", "ravivar", "raviwar", "itwar", "रविवार", "इतवार", "रविवारी"),
}

_MONTHS: dict[int, tuple[str, ...]] = {
    1: ("january", "jan", "जनवरी", "जानेवारी"),
    2: ("february", "feb", "फरवरी", "फेब्रुवारी"),
    3: ("march", "mar", "मार्च"),
    4: ("april", "apr", "अप्रैल", "एप्रिल"),
    5: ("may", "मई", "मे"),
    6: ("june", "jun", "जून"),
    7: ("july", "jul", "जुलाई", "जुलै"),
    8: ("august", "aug", "अगस्त", "ऑगस्ट"),
    9: ("september", "sep", "sept", "सितंबर", "सितम्बर", "सप्टेंबर"),
    10: ("october", "oct", "अक्टूबर", "अक्तूबर", "ऑक्टोबर"),
    11: ("november", "nov", "नवंबर", "नवम्बर", "नोव्हेंबर"),
    12: ("december", "dec", "दिसंबर", "दिसम्बर", "डिसेंबर"),
}
_MONTH_BY_NAME = {normalize(n): m for m, names in _MONTHS.items() for n in names}
_DATE_OF_MONTH = ("tarikh", "tareekh", "तारीख", "तारीखको", "tarkhela", "तारखेला", "तारखेस", "तारखेची")

_PARTS: dict[Part, tuple[str, ...]] = {
    "morning": ("morning", "subah", "सुबह", "sakali", "sakal", "सकाळी", "सकाळ", "savere", "सवेरे"),
    "afternoon": ("afternoon", "dopahar", "dopehar", "दोपहर", "dupari", "दुपारी", "दुपार"),
    "evening": ("evening", "shaam", "sham", "शाम", "sandhyakali", "संध्याकाळी", "संध्याकाळ"),
    "night": ("night", "raat", "रात", "ratri", "रात्री"),
}

_AMPM = r"(a\.?m\.?|p\.?m\.?)"
_CLOCK = re.compile(rf"(?<!\d)(\d{{1,2}})[:.](\d{{2}})(?!\d)\s*{_AMPM}?")
_HOUR_AMPM = re.compile(rf"(?<!\d)(\d{{1,2}})\s*{_AMPM}(?![a-z])")
_HOUR_WORD = re.compile(r"(?<!\d)(\d{1,2})\s*(?:baje|बजे|vajta|vajata|वाजता|वाजे|o'?clock)")
# "sade 10" = 10:30, "sava 10" = 10:15, "paune 10" = 9:45
_FRACTION = re.compile(
    r"(sade|saade|saadhe|साढ़े|साढे|साडे|sava|savva|सवा|paune|pone|पौने|pavne|पावणे)\s*(\d{1,2})"
)
_FRACTION_MINUTES = {
    "sade": 30, "saade": 30, "saadhe": 30, "साढे": 30, "साडे": 30,
    "sava": 15, "savva": 15, "सवा": 15,
    "paune": -15, "pone": -15, "पौने": -15, "pavne": -15, "पावणे": -15,
}  # fmt: skip
_HALF_WORDS = {"dedh": (1, 30), "डेढ": (1, 30), "दीड": (1, 30), "deed": (1, 30),
               "dhai": (2, 30), "ढाई": (2, 30), "adich": (2, 30), "अडीच": (2, 30)}  # fmt: skip
# Number words 1-12, converted to digits only next to a time cue ("das baje", "साडे दहा").
_NUMBER_WORDS: dict[int, tuple[str, ...]] = {
    1: ("one", "ek", "एक"),
    2: ("two", "do", "don", "दो", "दोन"),
    3: ("three", "teen", "tin", "तीन"),
    4: ("four", "char", "chaar", "चार"),
    5: ("five", "paanch", "panch", "pach", "पांच", "पाँच", "पाच"),
    6: ("six", "chhe", "chhah", "saha", "छह", "छः", "सहा"),
    7: ("seven", "saat", "sat", "सात"),
    8: ("eight", "aath", "aat", "आठ"),
    9: ("nine", "nau", "nav", "नौ", "नऊ"),
    10: ("ten", "das", "daha", "दस", "दहा"),
    11: ("eleven", "gyarah", "gyara", "akra", "akara", "ग्यारह", "अकरा"),
    12: ("twelve", "barah", "bara", "बारह", "बारा"),
}
_NUMBER_BY_WORD = {normalize(w): n for n, words in _NUMBER_WORDS.items() for w in words}
_TIME_CUES_AFTER = frozenset(
    ["baje", "बजे", "vajta", "vajata", "वाजता", "वाजे", "o'clock", "oclock", "am", "pm",
     "a.m.", "p.m."]
)  # fmt: skip
_TIME_CUES_BEFORE = frozenset(normalize(w) for w in _FRACTION_MINUTES) | frozenset(
    normalize(w) for names in _PARTS.values() for w in names
)
_NUM_DATE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)")
_BARE_NUMBER = re.compile(r"^\s*(\d{1,2})\s*$")


@dataclass(frozen=True)
class ClockTime:
    """A time as said. `hour` is 0-23 when unambiguous, else 1-12 with no meridiem."""

    hour: int
    minute: int
    meridiem: Meridiem | None = None
    part: Part | None = None

    def candidates(self) -> list[time]:
        """The wall-clock times this could mean, most likely first."""
        h, m = self.hour, self.minute
        if h == 0 or h > 12:
            return [time(h % 24, m)]
        if self.meridiem == "am":
            return [time(h % 12, m)]
        if self.meridiem == "pm":
            return [time(h % 12 + 12, m)]
        if self.part == "morning":
            return [time(h % 12, m)]
        if self.part == "afternoon":
            return [time(h if h == 12 else h + 12, m)] if h <= 5 or h == 12 else [time(h, m)]
        if self.part in ("evening", "night"):
            return [time(h if h == 12 else h + 12, m)]
        if h == 12:
            return [time(12, m)]
        return [time(h, m), time(h + 12, m)]


@dataclass(frozen=True)
class When:
    day: date | None = None
    time: ClockTime | None = None
    # A part of day said without a clock time ("kal sham").
    part: Part | None = None
    # "next week": list days from this date.
    from_day: date | None = None

    @property
    def empty(self) -> bool:
        return self.day is None and self.time is None and self.part is None and not self.from_day


def _any(toks: list[str], phrases: tuple[str, ...]) -> bool:
    return any(contains_phrase(toks, p) for p in phrases)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _future_date(today: date, month: int, day: int, year: int | None) -> date | None:
    if year is not None:
        return _safe_date(year if year >= 100 else 2000 + year, month, day)
    found = _safe_date(today.year, month, day)
    if found is not None and found < today:
        found = _safe_date(today.year + 1, month, day)
    return found


def _parse_day(
    text: str, toks: list[str], today: date
) -> tuple[date | None, date | None, int | None]:
    """(day, from_day, index of the token holding the day's digits, if any)."""
    m = _NUM_DATE.search(text)
    if m:
        day_n, month_n = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        found = _future_date(today, month_n, day_n, year)
        if found:
            return found, None, None

    for i, tok in enumerate(toks):
        month = _MONTH_BY_NAME.get(tok.rstrip("."))
        if month is None:
            continue
        # "26 oct", "26th october", "oct 26"
        for j in (i - 1, i + 1):
            if 0 <= j < len(toks):
                digits = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?$", toks[j])
                if digits:
                    found = _future_date(today, month, int(digits.group(1)), None)
                    if found:
                        return found, None, j

    for i, tok in enumerate(toks):
        if tok in _DATE_OF_MONTH and i > 0 and toks[i - 1].isdigit():
            n = int(toks[i - 1])
            found = _safe_date(today.year, today.month, n)
            if found is None or found < today:
                nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                found = _safe_date(nxt.year, nxt.month, n)
            if found:
                return found, None, i - 1

    if _any(toks, _DAY_AFTER):
        return today + timedelta(days=2), None, None
    if _any(toks, _TOMORROW):
        return today + timedelta(days=1), None, None
    if _any(toks, _TODAY):
        return today, None, None

    if _any(toks, _NEXT_WEEK):
        return None, today + timedelta(days=7 - today.weekday()), None

    next_said = _any(toks, _NEXT)
    for weekday, names in _WEEKDAYS.items():
        if any(t in names for t in toks):
            ahead = (weekday - today.weekday()) % 7
            if next_said and ahead == 0:
                ahead = 7
            return today + timedelta(days=ahead), None, None
    return None, None, None


def _meridiem(raw: str | None) -> Meridiem | None:
    if not raw:
        return None
    return "am" if raw.startswith("a") else "pm"


def _parse_part(toks: list[str]) -> Part | None:
    for part, names in _PARTS.items():
        if any(t in names for t in toks):
            return part
    if "noon" in toks:
        return "afternoon"
    return None


def _parse_time(
    text: str, toks: list[str], part: Part | None, lone_number_is_time: bool
) -> ClockTime | None:
    def valid(h: int, m: int) -> bool:
        return 0 <= h <= 23 and 0 <= m <= 59

    # Two different hours ("he finishes at 4, so after 6"): guessing one could hold a time the
    # patient ruled out. Ask instead.
    if len({int(t) for t in toks if t.isdigit() and 1 <= int(t) <= 23}) > 1:
        return None

    m = _CLOCK.search(text)
    if m and valid(int(m.group(1)), int(m.group(2))):
        return ClockTime(int(m.group(1)), int(m.group(2)), _meridiem(m.group(3)), part)

    m = _FRACTION.search(text)
    if m:
        word, h = normalize(m.group(1)), int(m.group(2))
        minutes = _FRACTION_MINUTES.get(word, 30)
        if 1 <= h <= 12:
            if minutes < 0:
                h, minutes = (h - 1 or 12), 60 + minutes
            return ClockTime(h, minutes, None, part)

    for tok in toks:
        if tok in _HALF_WORDS:
            h, minutes = _HALF_WORDS[tok]
            return ClockTime(h, minutes, None, part)

    m = _HOUR_AMPM.search(text)
    if m and 1 <= int(m.group(1)) <= 12:
        return ClockTime(int(m.group(1)), 0, _meridiem(m.group(2)), part)

    m = _HOUR_WORD.search(text)
    if m and valid(int(m.group(1)), 0):
        return ClockTime(int(m.group(1)), 0, None, part)

    if "noon" in toks and not any(t.isdigit() for t in toks):
        return ClockTime(12, 0, "pm", "afternoon")

    # "sakali 11", "tomorrow 5": a part of day or a day makes a lone small number a time.
    if part is not None or lone_number_is_time:
        numbers = [int(t) for t in toks if t.isdigit()]
        if len(numbers) == 1 and 1 <= numbers[0] <= 23:
            return ClockTime(numbers[0], 0, None, part)
    return None


def _number_words_to_digits(toks: list[str]) -> list[str]:
    out = list(toks)
    for i, tok in enumerate(toks):
        n = _NUMBER_BY_WORD.get(tok)
        if n is None:
            continue
        after = toks[i + 1] if i + 1 < len(toks) else ""
        before = toks[i - 1] if i > 0 else ""
        if after in _TIME_CUES_AFTER or before in _TIME_CUES_BEFORE:
            out[i] = str(n)
    return out


def parse_when(text: str, today: date, *, bare_number_is_time: bool = False) -> When:
    toks = _number_words_to_digits(tokens(text))
    norm = " ".join(toks)
    day, from_day, day_token = _parse_day(norm, toks, today)
    # Keep a date's digits from being read as a time ("26/10", "26 oct", "26 tarikh").
    time_toks = [t for i, t in enumerate(toks) if i != day_token]
    time_text = _NUM_DATE.sub(" ", " ".join(time_toks))
    time_toks = tokens(time_text)
    part = _parse_part(toks)
    lone_number_is_time = bare_number_is_time or day is not None
    clock = _parse_time(time_text, time_toks, part, lone_number_is_time)
    return When(day=day, time=clock, part=None if clock else part, from_day=from_day)


def bare_number(text: str) -> int | None:
    """ "2" -> 2. Used to pick a numbered option."""
    m = _BARE_NUMBER.match(normalize(text))
    return int(m.group(1)) if m else None
