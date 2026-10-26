"""Days and times as patients read them, in the clinic's timezone.

Times use a 12-hour clock. English adds am/pm; Hindi and Marathi put the part of the day first
("सुबह 11:00", "संध्याकाळी 5:30"), which is how people say it. Digits stay ASCII: that is what
WhatsApp users type and read. Hindi and Marathi names are DRAFT with the rest of the hi/mr text.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time

from salesbot_core.packs.schema import Language
from salesbot_core.scheduling import WeeklyWindow

_WEEKDAYS: dict[Language, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "hi": ("सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
    "mr": ("सोमवार", "मंगळवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
}
_WEEKDAYS_SHORT: dict[Language, tuple[str, ...]] = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "hi": ("सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि"),
    "mr": ("सोम", "मंगळ", "बुध", "गुरु", "शुक्र", "शनि", "रवि"),
}
_MONTHS: dict[Language, tuple[str, ...]] = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"),
    "hi": ("जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर",
           "अक्टूबर", "नवंबर", "दिसंबर"),
    "mr": ("जानेवारी", "फेब्रुवारी", "मार्च", "एप्रिल", "मे", "जून", "जुलै", "ऑगस्ट", "सप्टेंबर",
           "ऑक्टोबर", "नोव्हेंबर", "डिसेंबर"),
}  # fmt: skip
_RELATIVE: dict[Language, tuple[str, str]] = {
    "en": ("Today", "Tomorrow"),
    "hi": ("आज", "कल"),
    "mr": ("आज", "उद्या"),
}
_PARTS: dict[Language, tuple[str, str, str, str]] = {
    # morning (5-12), afternoon (12-16), evening (16-20), night (20-5)
    "en": ("", "", "", ""),
    "hi": ("सुबह", "दोपहर", "शाम", "रात"),
    "mr": ("सकाळी", "दुपारी", "संध्याकाळी", "रात्री"),
}


def _part_index(hour: int) -> int:
    if 5 <= hour < 12:
        return 0
    if 12 <= hour < 16:
        return 1
    if 16 <= hour < 20:
        return 2
    return 3


def day_label(day: date, language: Language, today: date) -> str:
    """Short, for option lists (WhatsApp list rows are at most 24 characters)."""
    if day == today:
        rel = _RELATIVE[language][0]
    elif (day - today).days == 1:
        rel = _RELATIVE[language][1]
    else:
        rel = None
    short = f"{_WEEKDAYS_SHORT[language][day.weekday()]} {day.day} {_month(day, language, 3)}"
    return f"{rel}, {short}" if rel else short


def _month(day: date, language: Language, max_len: int | None = None) -> str:
    name = _MONTHS[language][day.month - 1]
    return name[:max_len] if language == "en" and max_len else name


def long_date(day: date, language: Language) -> str:
    """For sentences: "Monday, 26 October" / "सोमवार, 26 अक्टूबर"."""
    return f"{_WEEKDAYS[language][day.weekday()]}, {day.day} {_month(day, language)}"


def time_label(moment: datetime, language: Language) -> str:
    """`moment` must already be in the clinic's timezone."""
    hour12 = moment.hour % 12 or 12
    clock = f"{hour12}:{moment.minute:02d}"
    if language == "en":
        return f"{clock} {'am' if moment.hour < 12 else 'pm'}"
    return f"{_PARTS[language][_part_index(moment.hour)]} {clock}"


def _day_list(weekdays: list[int], language: Language) -> str:
    """ISO weekdays as "Mon–Fri" for runs of three or more, else "Mon, Wed"."""
    names = _WEEKDAYS_SHORT[language]
    runs: list[list[int]] = []
    for day in sorted(weekdays):
        if runs and runs[-1][-1] == day - 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    parts = []
    for run in runs:
        if len(run) >= 3:
            parts.append(f"{names[run[0] - 1]}–{names[run[-1] - 1]}")
        else:
            parts.extend(names[d - 1] for d in run)
    return ", ".join(parts)


def _clock(t: time, language: Language) -> str:
    return time_label(datetime.combine(date(2000, 1, 3), t), language)


def weekly_hours(windows: Sequence[WeeklyWindow], language: Language) -> str:
    """One person's working hours: "Mon–Fri 10:00 am–1:00 pm, 5:00 pm–8:00 pm; Sat ...".
    Days with the same hours are grouped. Breaks are not shown."""
    by_day: dict[int, tuple[tuple[time, time], ...]] = {}
    for day in range(1, 8):
        spans = sorted((w.start, w.end) for w in windows if w.weekday == day)
        if spans:
            by_day[day] = tuple(spans)
    groups: dict[tuple[tuple[time, time], ...], list[int]] = {}
    for day, spans in by_day.items():
        groups.setdefault(spans, []).append(day)
    return "; ".join(
        _day_list(days, language)
        + " "
        + ", ".join(f"{_clock(a, language)}–{_clock(b, language)}" for a, b in spans)
        for spans, days in sorted(groups.items(), key=lambda g: g[1][0])
    )
