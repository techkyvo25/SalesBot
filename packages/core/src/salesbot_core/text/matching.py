"""Tokenizing and phrase matching for patient messages in English, Hindi and Marathi.

Devanagari word boundaries do not work with regex `\\b` (vowel signs are not word characters),
so matching is done on tokens. A phrase matches when its tokens appear consecutively in the
message. A phrase token ending in `*` matches any token with that prefix ("बदल*" matches
"बदलना" and "बदलायची").
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# Split on whitespace and punctuation, keeping ":" "." "/" "-" between digits ("11:30",
# "11.30", "26/10", "26-10").
_SPLIT = re.compile(r"(?:(?<!\d)[:./-]|[:./-](?!\d)|[\s,;!?।॥\"'()\[\]{}\\|])+")


def normalize(text: str) -> str:
    """NFC, casefolded, Devanagari digits as ASCII, nukta-free, single spaces."""
    text = unicodedata.normalize("NFC", text).casefold().translate(_DEVANAGARI_DIGITS)
    # Precomposed nukta letters (ज़, फ़, ड़) are written both ways; compare without the nukta.
    text = unicodedata.normalize("NFD", text).replace("़", "")
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.split())


def tokens(text: str) -> list[str]:
    return [t for t in _SPLIT.split(normalize(text)) if t]


def _token_matches(pattern: str, token: str) -> bool:
    if pattern.endswith("*"):
        return token.startswith(pattern[:-1])
    return token == pattern


def phrase_tokens(phrase: str) -> list[str]:
    parts = tokens(phrase.rstrip("*"))
    if phrase.endswith("*") and parts:
        parts[-1] += "*"
    return parts


def contains_phrase(message_tokens: list[str], phrase: str) -> bool:
    wanted = phrase_tokens(phrase)
    n = len(wanted)
    if n == 0:
        return False
    return any(
        all(_token_matches(w, t) for w, t in zip(wanted, message_tokens[i : i + n], strict=True))
        for i in range(len(message_tokens) - n + 1)
    )


def matches_any(text: str, phrases: Iterable[str]) -> bool:
    toks = tokens(text)
    return any(contains_phrase(toks, p) for p in phrases)


def only_phrases(text: str, phrases: Iterable[str]) -> bool:
    """True when every word of the message is part of one of the phrases. Tokens without a
    letter or digit (emoji, symbols) count as covered. False for a message with no tokens."""
    toks = tokens(text)
    if not toks:
        return False
    covered = {i for i, t in enumerate(toks) if not any(ch.isalnum() for ch in t)}
    for phrase in phrases:
        wanted = phrase_tokens(phrase)
        n = len(wanted)
        for i in range(len(toks) - n + 1) if n else ():
            window = toks[i : i + n]
            if all(_token_matches(w, t) for w, t in zip(wanted, window, strict=True)):
                covered.update(range(i, i + n))
    return len(covered) == len(toks)
