"""Code-level check that rupee figures in an answer are backed by evidence.

Evidence = numbers found in MCP tool results and in the user's own messages.
A rupee figure in the answer that matches no evidence number (allowing for display
rounding and monthly/annual conversion) is treated as unsupported.
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
_AMOUNT_RE = re.compile(
    rf"(?:₹|\bRs\.?|\bINR)\s*(?P<a>\d[\d,]*(?:\.\d+)?)(?:\s*(?P<au>{_UNIT_RE})\b)?"
    rf"|(?<!\w)(?P<b>\d[\d,]*(?:\.\d+)?)(?:\s*(?P<bu>{_UNIT_RE}))?\s*(?:rupees|inr)\b",
    re.IGNORECASE,
)

UNVERIFIED_PLACEHOLDER = "[amount not verified]"


def _to_float(number: str) -> float | None:
    try:
        return float(number.replace(",", "").rstrip("."))
    except ValueError:
        return None


def numbers_in(text: str) -> set[float]:
    """Every number in `text`, both as written and scaled by any unit (50k -> 50000)."""
    found: set[float] = set()
    for match in _NUMBER_RE.finditer(text):
        value = _to_float(match.group("num") + (match.group("dec") or ""))
        if value is None:
            continue
        found.add(value)
        unit = (match.group("unit") or "").lower()
        if unit in _UNITS:
            found.add(value * _UNITS[unit])
    return found


def expand_evidence(numbers: set[float]) -> set[float]:
    """Allow straightforward monthly <-> annual conversions of evidence figures."""
    expanded = set(numbers)
    for value in numbers:
        expanded.add(value * 12)
        expanded.add(value / 12)
    return expanded


def _parse_amount(match: re.Match[str]) -> tuple[float, float] | None:
    """Return (value, display precision) for a rupee amount match."""
    raw = match.group("a") or match.group("b")
    unit = (match.group("au") or match.group("bu") or "").lower()
    value = _to_float(raw)
    if value is None:
        return None
    multiplier = _UNITS.get(unit, 1.0)
    decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
    return value * multiplier, multiplier * (10 ** -decimals)


def _is_supported(value: float, precision: float, allowed: set[float]) -> bool:
    tolerance = max(1.0, precision / 2, value * 0.01)
    return any(abs(value - candidate) <= tolerance for candidate in allowed)


def unsupported_amounts(text: str, allowed: set[float]) -> list[str]:
    problems = []
    for match in _AMOUNT_RE.finditer(text):
        parsed = _parse_amount(match)
        if parsed and not _is_supported(*parsed, allowed):
            problems.append(match.group(0).strip())
    return problems


def strip_unsupported_amounts(text: str, allowed: set[float]) -> str:
    def replace(match: re.Match[str]) -> str:
        parsed = _parse_amount(match)
        if parsed is None or _is_supported(*parsed, allowed):
            return match.group(0)
        return UNVERIFIED_PLACEHOLDER

    return _AMOUNT_RE.sub(replace, text)
