"""Identifier parsing and comparison for the verification gate.

Pure functions, no LLM concern -- same shape as booking.py is specified to
have. Kept out of agent.py so the matching rules can be unit-tested directly,
since this is the security-critical comparison in the build.

TOLERANCE POLICY (PLAN.md Section 3a rule 3) -- EXACT MATCH ON PARSED VALUES.

A spoken date reaches us through STT, so the *spelling* varies even when the
patient answers perfectly: "4 July 1970", "July 4th, 1970" and "1970-07-04"
are the same answer. Comparing strings would reject legitimate patients for
Deepgram's formatting choices.

So: parse the stated form into a `datetime.date`, then compare `date == date`.
That is exact matching on the value rather than on the spelling. It is NOT
fuzzy matching -- there is no edit distance, no threshold, no partial credit,
and no accepting two components out of three. Input resolves to exactly one
date or it does not parse at all.

Deliberately NOT using a general-purpose parser such as dateutil: `parse()`
backfills missing components from a default date, so "March 1988" would silently
become a complete date. A gate must not invent the part the person did not say.

AMBIGUITY IS REJECTED, NEVER RESOLVED. "03/04/1988" is March 4th or April 3rd
depending on locale. We return None rather than guess -- and crucially we never
resolve it by checking which reading matches the record, because using the
answer to interpret the question is exactly the oracle this gate exists to
avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "twentieth": 20, "thirtieth": 30,
}
_NOISE = {"the", "of", "on", "my", "birthday", "born", "date", "birth", "is", "it", "i", "was"}


def _words_to_number(tokens: list[str]) -> int | None:
    """Turn ['twenty','second'] into 22. Returns None if not a number phrase."""
    total = 0
    matched = False
    for tok in tokens:
        if tok in _TENS:
            total += _TENS[tok]; matched = True
        elif tok in _UNITS:
            total += _UNITS[tok]; matched = True
        else:
            return None
    return total if matched else None


def _tokenise(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text)  # 22nd -> 22
    tokens = re.findall(r"[a-z]+|\d+", text)
    return [t for t in tokens if t not in _NOISE]


def _year_from(tokens: list[str]) -> tuple[int, list[str]] | None:
    """Pull a 4-digit year, or a spoken one like 'nineteen eighty eight'."""
    for i, tok in enumerate(tokens):
        if tok.isdigit() and len(tok) == 4:
            return int(tok), tokens[:i] + tokens[i + 1 :]
    # Spoken: a century word followed by 0-99, e.g. nineteen + eighty + eight.
    # Scanned RIGHT TO LEFT: the year is stated last, and "twenty" can also open
    # a day ("twenty second of March nineteen eighty eight") -- taking the first
    # match would read that day as the year.
    for i in range(len(tokens) - 1, -1, -1):
        tok = tokens[i]
        if tok not in ("nineteen", "twenty"):
            continue
        century = _UNITS.get(tok) or _TENS.get(tok)
        for take in (3, 2, 1):
            chunk = tokens[i + 1 : i + 1 + take]
            if len(chunk) != take:
                continue
            n = _words_to_number(chunk)
            if n is not None and 0 <= n <= 99:
                return century * 100 + n, tokens[:i] + tokens[i + 1 + take :]
    return None


def parse_stated_date(text: str) -> date | None:
    """Parse a spoken or written date of birth. None if incomplete or ambiguous.

    Returning None means "could not understand", which is NOT the same as
    "did not match" -- the caller must keep them apart so a bad line does not
    burn a verification attempt.
    """
    if not text or not text.strip():
        return None

    # ISO first: unambiguous by definition.
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    tokens = _tokenise(text)
    found = _year_from(tokens)
    if found is None:
        return None  # no year stated -> incomplete, never guess one
    year, rest = found

    # Month as a word: the day is then whatever number remains.
    month = None
    for i, tok in enumerate(rest):
        if tok in MONTHS:
            month = MONTHS[tok]
            rest = rest[:i] + rest[i + 1 :]
            break

    if month is not None:
        day = None
        for i, tok in enumerate(rest):
            if tok.isdigit():
                day = int(tok); break
        if day is None:
            day = _words_to_number(rest)
        return _safe_date(year, month, day) if day is not None else None

    # All numeric: only accept when one value cannot be a month.
    numbers = [int(t) for t in rest if t.isdigit()]
    if len(numbers) != 2:
        return None
    a, b = numbers
    if a > 12 and b <= 12:
        return _safe_date(year, b, a)
    if b > 12 and a <= 12:
        return _safe_date(year, a, b)
    # Both <= 12: 03/04 is March 4th or April 3rd. Refuse rather than guess.
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalise_patient_id(text: str) -> str | None:
    """Pull a patient id like P001 out of speech. None if none found."""
    if not text:
        return None
    # Anchored on word boundaries. An unanchored search matches a letter
    # followed by digits ANYWHERE, so "March 1988" would read as id "H1988",
    # fail the comparison and burn a verification attempt on what was really an
    # incomplete date. The boundary makes the letter start a word.
    match = re.search(r"\b([A-Z])[\s\-]?0*(\d{1,6})\b", text.upper())
    if match:
        return f"{match.group(1)}{int(match.group(2)):03d}"
    # spoken digits: "p zero zero one"
    tokens = re.findall(r"[a-z]+|\d", text.lower())
    if tokens and len(tokens[0]) == 1 and tokens[0].isalpha():
        digits = []
        for tok in tokens[1:]:
            if tok.isdigit():
                digits.append(tok)
            elif tok in _UNITS and _UNITS[tok] <= 9:
                digits.append(str(_UNITS[tok]))
            else:
                break
        if digits:
            return f"{tokens[0].upper()}{int(''.join(digits)):03d}"
    return None


# --- outcomes -------------------------------------------------------------
#
# PLAN.md Section 3a rule 4 names four outcomes. Three are produced by the tool.
#
#   verified            -> the tool returns a VerifiedAgent instead of a string,
#                          because returning an Agent is what triggers handoff
#   not_verified        -> parsed cleanly, did not match
#   attempts_exhausted  -> the cap (2) is spent
#   wrong_person        -> NOT produced by the tool; see the note below
#
# COULD_NOT_UNDERSTAND is a deliberate addition to that list. It means the
# person said something we could not parse into a complete, unambiguous
# identifier -- a bad line, a half-answer, "I don't remember". It is not a
# wrong answer, so it must not spend one of the two attempts; otherwise two
# coughs reject a legitimate patient, which is the business harm the exact-match
# policy is meant to avoid.
#
# It is not an oracle. The response is identical whatever the record says, so it
# reveals nothing about the expected value -- unlike "that's not the right year",
# which would.
VERIFIED = "verified"
NOT_VERIFIED = "not_verified"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
COULD_NOT_UNDERSTAND = "could_not_understand"
WRONG_PERSON = "wrong_person"

MAX_VERIFICATION_ATTEMPTS = 2


@dataclass(frozen=True)
class VerificationAttempt:
    """One call to the verification tool, for the Phase 5 record (D4, D11).

    `stated` is kept because the transcript already contains it, so storing it
    adds no disclosure. The EXPECTED value is never stored here -- this record
    goes to the observability platform.
    """

    outcome: str
    identifier_kind: str  # "date_of_birth" | "patient_id" | "unrecognised"
    stated: str
    attempt_number: int
    consumed_attempt: bool
