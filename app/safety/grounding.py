"""Code-level check that rupee figures in an answer are backed by evidence.

Evidence = numbers found in this turn's MCP tool results, plus the user's own figures
(with monthly/annual conversion of those). A rupee figure in the answer that matches no
evidence number (allowing for display rounding) is treated as unsupported. Both ends of a
range are checked, and hedged amounts ("around ₹7,000", "typically ₹1,000") must match
evidence exactly, without the 1% tolerance.
"""

from __future__ import annotations

import re

_UNITS = {
    "k": 1e3, "thousand": 1e3,
    "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5, "lpa": 1e5,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
}
_UNIT_RE = r"k|thousand|lakhs?|lacs?|lpa|l|cr|crores?"

_NUMBER_RE = re.compile(
    rf"(?<!\w)(?P<num>\d{{1,3}}(?:,\d{{2,3}})+|\d+)(?P<dec>\.\d+)?(?:\s*(?P<unit>{_UNIT_RE}))?(?![A-Za-z])",
    re.IGNORECASE,
)
_AMOUNT_NUM = r"\d[\d,]*(?:\.\d+)?"
# Range separator: "₹7,000-8,000", "₹1,000 to ₹1,500", "7,000 – 8,000 rupees".
_RANGE_SEP = r"\s*(?:-|–|—|to)\s*(?:₹|\bRs\.?|\bINR)?\s*"
_AMOUNT_RE = re.compile(
    rf"(?:₹|\bRs\.?|\bINR)\s*(?P<a>{_AMOUNT_NUM})(?:\s*(?P<au>{_UNIT_RE})\b)?"
    rf"(?:{_RANGE_SEP}(?P<a2>{_AMOUNT_NUM})(?:\s*(?P<a2u>{_UNIT_RE})\b)?)?"
    rf"|(?<!\w)(?:(?P<b0>{_AMOUNT_NUM})(?:\s*(?P<b0u>{_UNIT_RE}))?{_RANGE_SEP})?"
    rf"(?P<b>{_AMOUNT_NUM})(?:\s*(?P<bu>{_UNIT_RE}))?\s*(?:rupees|inr)\b",
    re.IGNORECASE,
)
# A hedging word up to two words before an amount marks it as an estimate.
_HEDGE_RE = re.compile(
    r"(?:\b(?:typically|usually|generally|around|about|approx\w*|roughly|nearly|almost|estimated)\b|~)"
    r"\W*(?:\w+\W+){0,2}$",
    re.IGNORECASE,
)

UNVERIFIED_PLACEHOLDER = "[amount not verified]"


def _to_float(number: str) -> float | None:
    try:
        return float(number.replace(",", "").rstrip("."))
    except ValueError:
        return None


def number_spans(text: str) -> list[tuple[set[float], int, int]]:
    """Each number in `text` with its values (as written and unit-scaled) and its (start, end) span."""
    spans: list[tuple[set[float], int, int]] = []
    for match in _NUMBER_RE.finditer(text):
        value = _to_float(match.group("num") + (match.group("dec") or ""))
        if value is None:
            continue
        values = {value}
        unit = (match.group("unit") or "").lower()
        if unit in _UNITS:
            values.add(value * _UNITS[unit])
        spans.append((values, match.start(), match.end()))
    return spans


def numbers_in(text: str) -> set[float]:
    """Every number in `text`, both as written and scaled by any unit (50k -> 50000)."""
    found: set[float] = set()
    for values, _, _ in number_spans(text):
        found |= values
    return found


def expand_evidence(numbers: set[float]) -> set[float]:
    """Allow straightforward monthly <-> annual conversions (applied to the user's own figures only)."""
    expanded = set(numbers)
    for value in numbers:
        expanded.add(value * 12)
        expanded.add(value / 12)
    return expanded


def _amount(raw: str, unit: str | None) -> tuple[float, float] | None:
    """Return (value, display precision) for one written amount."""
    value = _to_float(raw)
    if value is None:
        return None
    multiplier = _UNITS.get((unit or "").lower(), 1.0)
    decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
    return value * multiplier, multiplier * (10 ** -decimals)


def _parse_amounts(match: re.Match[str]) -> list[tuple[float, float]]:
    """Every amount in a match: one value, or both ends of a range ("₹1-2 lakh" shares the unit)."""
    if match.group("a"):
        first, end = (match.group("a"), match.group("au")), (match.group("a2"), match.group("a2u"))
    else:
        first, end = (match.group("b0"), match.group("b0u")), (match.group("b"), match.group("bu"))
    if first[0] and end[0] and not first[1]:
        first = (first[0], end[1])
    parsed = [_amount(raw, unit) for raw, unit in (first, end) if raw]
    return [amount for amount in parsed if amount is not None]


def _is_hedged(text: str, start: int) -> bool:
    return bool(_HEDGE_RE.search(text[max(0, start - 40):start]))


def _is_supported(value: float, precision: float, allowed: set[float], hedged: bool) -> bool:
    tolerance = max(0.5, precision / 2) if hedged else max(1.0, precision / 2, value * 0.01)
    return any(abs(value - candidate) <= tolerance for candidate in allowed)


def _match_supported(text: str, match: re.Match[str], allowed: set[float]) -> bool:
    hedged = _is_hedged(text, match.start())
    return all(_is_supported(value, precision, allowed, hedged) for value, precision in _parse_amounts(match))


def unsupported_amounts(text: str, allowed: set[float]) -> list[str]:
    return [match.group(0).strip() for match in _AMOUNT_RE.finditer(text)
            if not _match_supported(text, match, allowed)]


def strip_unsupported_amounts(text: str, allowed: set[float]) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if _match_supported(text, match, allowed) else UNVERIFIED_PLACEHOLDER

    return _AMOUNT_RE.sub(replace, text)
