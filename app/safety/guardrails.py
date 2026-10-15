"""Code-enforced guardrails on user input (before Gemini) and on answers (before the user)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.safety.redaction import redact

APPROVAL_NOTE = "Eligibility results are indicative only. Final approval is always at the bank's discretion."
TAX_NOTE = "I can't give tax advice. For tax questions, please consult a qualified tax professional or CA."

_CARD_DOMAIN = re.compile(
    r"\b(credit|cards?|bank|rewards?|cash ?back|lounges?|fees?|cibil|credit score|eligib\w*|spend\w*|"
    r"hdfc|sbi|axis|icici|amex|american express|kotak|idfc|au bank|indusind|rbl|yes bank|hsbc|"
    r"rupay|visa|mastercard|emi|apply|great\.?cards)\b",
    re.IGNORECASE,
)
_OFF_TOPIC = re.compile(
    r"\b(jokes?|poems?|poetry|songs?|lyrics|riddles?|recipes?|weather|horoscope|movies?|cricket|football|"
    r"homework|essay|python|javascript|java|sql|html|coding|programming|algorithm)\b",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules|prompts?|guidelines|guardrails)\b"
    r"|\b(system|developer|hidden) (prompt|message|instructions?)\b"
    r"|\b(reveal|show|print|repeat|leak)\b.{0,30}\b(prompt|instructions|api ?key|secret|env)\b"
    r"|\bjailbreak\b|\bdeveloper mode\b|\bDAN mode\b",
    re.IGNORECASE,
)
_FRAUD = re.compile(
    r"\b(stolen|cloned?|skim\w*|carding|cvv dumps?|dumps)\b.{0,20}\bcards?\b"
    r"|\bsomeone else'?s (credit )?card\b"
    r"|\b(fake|forged?|edit(?:ed)?|morph\w*)\b.{0,25}\b(salary slips?|payslips?|income proofs?|itr|documents?|bank statements?|pan|aadhaar)\b"
    r"|\b(bypass|cheat|trick|evade)\b.{0,25}\b(kyc|verification|credit check|bank|cibil)\b"
    r"|\b(generate|create)\b.{0,20}\b(valid|working|fake)\b.{0,10}\bcard numbers?\b",
    re.IGNORECASE,
)
_TAX = re.compile(
    r"\b(income[- ]tax|tax (?:saving|savings|benefits?|deductions?|exemptions?|planning|returns?|liability|refunds?|rebate)"
    r"|save (?:on )?tax(?:es)?|itr|80c|80d|section 80\w*|tds|capital gains?|tax[- ]free|taxable|deductible)\b",
    re.IGNORECASE,
)
_APPROVAL_QUESTION = re.compile(
    r"\b(guarantee\w*|assured|sure[- ]?shot|100 ?%|definitely|surely|certainly|confirm\w*)\b.{0,40}\bapprov\w*"
    r"|\bwill (i|my application|it) (get|be) approved\b|\b(chances?|odds|probability) of (getting )?approv\w*",
    re.IGNORECASE,
)
_APPROVAL_PROMISE = re.compile(
    r"\b(guarantee[sd]?|assured(ly)?|certain(ly)?|definitely|surely|100 ?%|sure[- ]shot|instant(ly)?)\b[^.!?\n]{0,40}\bapprov\w*"
    r"|\bapprov\w*[^.!?\n]{0,30}\b(guaranteed?|assured|certain|definite|sure)\b"
    r"|\byou(?: will|'ll) (?:definitely |surely |certainly |easily )?(?:be|get) approved\b"
    r"|\byou(?:'re| are) (?:pre-?)?approved\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(not|no|never|cannot|can't|can not|isn't|won't|doesn't|don't)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_INJECTION_REPLY = (
    "I can't change how I work or share my internal instructions. "
    "I'm happy to help with credit cards though: recommendations, card details, comparisons or eligibility."
)
_FRAUD_REPLY = (
    "I can't help with that. Misusing cards or submitting false information to a bank is illegal. "
    "If you're worried a card has been compromised, contact your bank's helpline right away to block it."
)
_OFF_TOPIC_REPLY = (
    "That's outside what I can help with. I'm the Great.cards Credit Card Advisor, so I can recommend cards "
    "for your spending, explain a card's fees and benefits, compare cards or check eligibility. What would you like to explore?"
)
_TAX_REPLY = (
    f"{TAX_NOTE} I can help with credit card fees, rewards, comparisons or eligibility if that's useful."
)


@dataclass(frozen=True)
class InputCheck:
    blocked: bool = False
    reply: str = ""
    reason: str = ""
    approval_question: bool = False
    mentions_tax: bool = False


def check_input(text: str) -> InputCheck:
    """Decide locally whether a message may proceed to Gemini, and with which constraints."""
    in_domain = bool(_CARD_DOMAIN.search(text))
    if _INJECTION.search(text):
        return InputCheck(blocked=True, reply=_INJECTION_REPLY, reason="prompt_injection")
    if _FRAUD.search(text):
        return InputCheck(blocked=True, reply=_FRAUD_REPLY, reason="fraud")
    mentions_tax = bool(_TAX.search(text))
    if mentions_tax and not in_domain:
        return InputCheck(blocked=True, reply=_TAX_REPLY, reason="tax_advice")
    if _OFF_TOPIC.search(text) and not in_domain:
        return InputCheck(blocked=True, reply=_OFF_TOPIC_REPLY, reason="off_topic")
    return InputCheck(approval_question=bool(_APPROVAL_QUESTION.search(text)), mentions_tax=mentions_tax)


def _filter_sentences(text: str, pattern: re.Pattern[str], keep_negated: bool) -> tuple[str, bool]:
    removed = False
    lines = []
    for line in text.split("\n"):
        kept = []
        for sentence in _SENTENCE_SPLIT.split(line):
            if pattern.search(sentence) and not (keep_negated and _NEGATION.search(sentence)):
                removed = True
                continue
            kept.append(sentence)
        # Drop lines that became empty bullets, keep intentional blank lines.
        joined = " ".join(kept)
        if line.strip() and not re.sub(r"^[\s*•\-\d.)]+", "", joined).strip():
            continue
        lines.append(joined)
    return "\n".join(lines), removed


def enforce_output(text: str, *, approval_context: bool, tax_context: bool) -> str:
    """Last line of defence before an answer reaches the user."""
    text = redact(text).text
    text, removed_promise = _filter_sentences(text, _APPROVAL_PROMISE, keep_negated=True)
    text, removed_tax = _filter_sentences(text, _TAX, keep_negated=False)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    notes = []
    if (approval_context or removed_promise) and "discretion" not in text.lower():
        notes.append(APPROVAL_NOTE)
    if tax_context or removed_tax:
        notes.append(TAX_NOTE)
    if notes:
        text = (text + "\n\n" if text else "") + "\n".join(f"_{note}_" for note in notes)
    return text
