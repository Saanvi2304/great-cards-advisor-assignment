"""Local detection and redaction of sensitive personal and financial data.

Runs before anything is sent to Gemini, stored in conversation history or logged.
Pincodes and income figures are deliberately *not* redacted: eligibility checks need them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# Findings in this set mean the user shared a secret that must never be requested or used.
CRITICAL = frozenset({"card number", "CVV", "OTP", "PIN", "password"})


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: tuple[str, ...] = ()

    @property
    def critical(self) -> bool:
        return any(finding in CRITICAL for finding in self.findings)


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    check: Callable[[str], bool] | None = None


def _luhn_valid(candidate: str) -> bool:
    digits = [int(d) for d in candidate if d.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# Each pattern captures the sensitive part in the named group "value"; surrounding
# keywords are kept so the sentence still makes sense to the model.
# Order matters: more specific rules run first.
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "card number",
        re.compile(r"(?<!\d)(?P<value>\d{13,19}|\d{4}(?:[ -]\d{4}){2}[ -]\d{1,7}|\d{4}[ -]\d{6}[ -]\d{5})(?!\d)"),
        _luhn_valid,
    ),
    # 13-19 digits right next to the word "card" are treated as a card number even when Luhn fails.
    _Rule(
        "card number",
        re.compile(r"(?i)\bcard\b(?:\s*(?:no\.?|number|num|#))?\D{0,15}?(?<!\d)(?P<value>\d(?:[ -]?\d){12,18})(?!\d)"),
    ),
    _Rule(
        "card number",
        re.compile(r"(?i)(?<!\d)(?P<value>\d(?:[ -]?\d){12,18})(?!\d)\D{0,15}?\bcard\b"),
    ),
    _Rule(
        "CVV",
        re.compile(r"(?i)\b(?:cvv2?|cvc2?|card verification (?:value|code)|security code)\b\D{0,12}?(?P<value>\d{3,4})(?!\d)"),
    ),
    _Rule(
        "OTP",
        re.compile(r"(?i)\b(?:otp|one[- ]time[- ]password|verification code)\b\D{0,15}?(?P<value>\d{4,8})(?!\d)"),
    ),
    _Rule(
        "PIN",
        re.compile(r"(?i)\b(?:atm|card|upi|debit|credit|secret|m)[ -]?pin\b\D{0,10}?(?P<value>\d{4,6})(?!\d)"),
    ),
    _Rule(
        # A bare "pin" with 4 digits is a PIN; with 6 digits it is usually a pincode.
        "PIN",
        re.compile(r"(?i)\bpin\b(?!\s*code)\D{0,10}?(?P<value>\d{4})(?!\d)"),
    ),
    _Rule(
        "password",
        re.compile(r"(?i)\b(?:password|passcode|passwd|pwd|net ?banking (?:password|id)|login id)\b\s*(?:is|:|=|-)?\s*(?P<value>\S+)"),
    ),
    _Rule(
        "bank account number",
        re.compile(r"(?i)\b(?:a/c|acct|account)\s*(?:no\.?|number|num|#)?\s*(?:is|:|=|-)?\s*(?P<value>\d{9,18})(?!\d)"),
    ),
    _Rule(
        "email address",
        re.compile(r"(?P<value>[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})"),
    ),
    _Rule(
        "UPI ID",
        re.compile(r"(?i)(?P<value>\b[\w.-]{2,}@(?:ok\w+|upi|ybl|ibl|axl|apl|paytm|pt\w+|icici|sbi|hdfcbank|axisbank|kotak|freecharge|airtel|jio|yesbank|idfcbank)\b)"),
    ),
    _Rule(
        "phone number",
        re.compile(r"(?<![\d+])(?P<value>(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5})(?!\d)"),
    ),
    _Rule(
        "Aadhaar number",
        # Exactly 12 digits: not part of a longer digit group such as a 16-digit card number.
        re.compile(r"(?<!\d)(?<!\d[ -])(?P<value>[2-9]\d{3}[ -]?\d{4}[ -]?\d{4})(?![ -]?\d)"),
    ),
    _Rule(
        "PAN",
        re.compile(r"(?<![A-Za-z0-9])(?P<value>[A-Za-z]{3}[PCHFATBLJGpchfatbljg][A-Za-z]\d{4}[A-Za-z])(?![A-Za-z0-9])"),
    ),
    _Rule(
        "passport number",
        re.compile(r"(?i)\bpassport\b\D{0,20}?(?P<value>[A-Z]\d{7})\b"),
    ),
    _Rule(
        "date of birth",
        re.compile(r"(?i)\b(?:dob|d\.o\.b\.?|date of birth|born on)\b\D{0,5}(?P<value>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})"),
    ),
)

_TOKEN_RE = re.compile(r"\[REDACTED [A-Z ]+\]")
_FILLER_WORDS = frozenset(
    "my is the a an and here its it this that number no card credit debit cvv cvc otp pin "
    "password code details of for use please pls ok okay hi hello".split()
)


def _token(label: str) -> str:
    return f"[REDACTED {label.upper()}]"


def redact(text: str) -> RedactionResult:
    findings: list[str] = []
    for rule in _RULES:

        def replace(match: re.Match[str], rule: _Rule = rule) -> str:
            value = match.group("value")
            if rule.check and not rule.check(value):
                return match.group(0)
            if rule.label not in findings:
                findings.append(rule.label)
            start, end = match.span("value")
            offset = match.start()
            whole = match.group(0)
            return whole[: start - offset] + _token(rule.label) + whole[end - offset :]

        text = rule.pattern.sub(replace, text)
    return RedactionResult(text=text, findings=tuple(findings))


def has_meaningful_content(redacted_text: str) -> bool:
    """True if something worth answering remains once redaction tokens are removed."""
    remainder = _TOKEN_RE.sub(" ", redacted_text)
    words = [w for w in re.findall(r"[A-Za-z]{2,}", remainder.lower()) if w not in _FILLER_WORDS]
    return len(words) >= 2
