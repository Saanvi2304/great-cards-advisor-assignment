"""Which live MCP tools the advisor may use, and how their arguments are checked.

Schemas always come from the live server (`list_tools`); this module only decides
which tools/parameters are exposed to Gemini and validates arguments against the
server's real JSON Schema before any call is made.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema.validators import validator_for

from app.safety.grounding import number_spans, numbers_in

# Tools Gemini may call, with parameters hidden from it (marketing/content-generation
# knobs or internal IDs that a chat user would never supply).
EXPOSED_TOOLS: dict[str, frozenset[str]] = {
    "recommend_cards": frozenset(),
    "list_cards": frozenset({"bank_ids", "credit_score", "sort_by"}),
    "get_card_details": frozenset(),
    "compare_cards": frozenset(),
    "whatsapp_compare_cards": frozenset(),
    "check_eligibility": frozenset(),
    "whatsapp_resolve_card_or_category": frozenset(),
    "generate_content_brief": frozenset({
        "composition_label", "spend_tiers", "detail_level", "per_call_timeout_ms", "include_details",
        "output_format", "audience", "goal", "hook", "spend_focus", "tone", "language",
        "comparison_card", "seo_keyword", "persona", "spend_profile",
    }),
}

# Enum values narrowed further than the server allows (other tools cover the rest).
RESTRICTED_ENUMS: dict[str, dict[str, list[str]]] = {
    "generate_content_brief": {
        "content_type": ["category_best_cards", "fee_justification", "bank_ranking", "upgrade_path"],
    },
}

# Loaded once per catalog refresh and injected into the system prompt, not called by Gemini.
CONTEXT_TOOL = "get_advisor_context"
CONTEXT_TOOL_ARGS = {"topic": "full"}

_SPEND_EXCLUDED_KEYS = {"top_n", "response_format"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    validator: Any = field(repr=False, compare=False)

    @classmethod
    def from_mcp(cls, tool: Any) -> "ToolSpec":
        schema = dict(tool.inputSchema or {"type": "object", "properties": {}})
        validator_cls = validator_for(schema)
        return cls(tool.name, tool.description or "", schema, validator_cls(schema))

    @property
    def hidden(self) -> frozenset[str]:
        return EXPOSED_TOOLS.get(self.name, frozenset())

    def declared_schema(self) -> dict[str, Any]:
        """The schema shown to Gemini: live schema minus hidden params, with narrowed enums."""
        schema = copy.deepcopy(self.input_schema)
        properties = schema.get("properties", {})
        for name in self.hidden:
            properties.pop(name, None)
        for name, values in RESTRICTED_ENUMS.get(self.name, {}).items():
            if name in properties:
                properties[name]["enum"] = values
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in self.hidden]
        return schema


@dataclass(frozen=True)
class ToolCatalog:
    specs: dict[str, ToolSpec]

    def exposed(self) -> list[ToolSpec]:
        return [spec for name, spec in self.specs.items() if name in EXPOSED_TOOLS]

    def get(self, name: str) -> ToolSpec | None:
        return self.specs.get(name) if name in EXPOSED_TOOLS else None


def _normalise_numbers(value: Any) -> Any:
    """Gemini returns JSON numbers as floats; send whole numbers as ints."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalise_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise_numbers(v) for v in value]
    return value


def _coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Fix harmless type slips (number vs numeric string) guided by the real schema."""
    expected = schema.get("type")
    if expected == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    if expected in ("number", "integer") and isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            number = float(cleaned)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if expected == "array" and isinstance(value, str):
        return [value]
    return value


def prepare_arguments(spec: ToolSpec, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return cleaned arguments and a list of schema/policy errors (empty when valid)."""
    properties = spec.input_schema.get("properties", {})
    errors: list[str] = []
    args: dict[str, Any] = {}
    for name, value in _normalise_numbers(raw or {}).items():
        if value is None:
            continue
        if name not in properties or name in spec.hidden:
            errors.append(f"'{name}' is not an accepted parameter for {spec.name}.")
            continue
        args[name] = _coerce(value, properties[name])

    for name, allowed in RESTRICTED_ENUMS.get(spec.name, {}).items():
        if name in args and args[name] not in allowed:
            errors.append(f"'{name}' must be one of {allowed}.")

    for error in sorted(spec.validator.iter_errors(args), key=lambda e: list(e.path)):
        location = ".".join(str(p) for p in error.path) or "arguments"
        errors.append(f"{location}: {error.message}")
    return args, errors[:6]


def _mentions_amount(text: str) -> bool:
    return bool(re.search(r"\d", text))


def _plausible_pincode(pincode: str) -> bool:
    """Indian pincodes are 6 digits; the first two (postal circle) run from 11 to 85."""
    return bool(re.fullmatch(r"\d{6}", pincode)) and 11 <= int(pincode[:2]) <= 85


# Simple conversions allowed from a number the user wrote: rupee spends may be monthly or
# annual; lounge fields are visits per quarter (from per-quarter, per-month or per-year counts).
_SPEND_FACTORS = (1.0, 12.0, 1 / 12)
_LOUNGE_FACTORS = (1.0, 3.0, 0.25)


def _derived_from(value: float, stated: set[float], factors: tuple[float, ...]) -> bool:
    return any(abs(value - number * factor) <= max(0.5, abs(number * factor) * 0.01)
               for number in stated for factor in factors)


def grounding_problem(name: str, args: dict[str, Any], user_text: str, request_text: str) -> str | None:
    """Reject calls built on values the user never provided (no guessing).

    `user_text` is everything the user said this session; `request_text` is the current
    request only (this message, plus earlier messages answered by a clarifying question).
    """
    if name == "check_eligibility":
        pincode = str(args.get("pincode", ""))
        if not re.search(rf"(?<!\d){re.escape(pincode)}(?!\d)", user_text):
            return ("The user has not provided this pincode. Do not assume one; "
                    "call ask_clarification to request their 6-digit pincode.")
        if not _plausible_pincode(pincode):
            return (f"{pincode} is not a valid Indian pincode (6 digits, starting 11-85). Do not call "
                    "check_eligibility with it; call ask_clarification to ask the user to re-check their pincode.")
        if not _mentions_amount(user_text.replace(pincode, "")):
            return ("The user has not stated their income. Do not assume it; "
                    "call ask_clarification to request their monthly or annual income.")
    if name == "recommend_cards":
        spends = {k: v for k, v in args.items()
                  if k not in _SPEND_EXCLUDED_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
        if not spends:
            return ("The user has not stated any spending amounts, so a personalised ranking would rest on guesses. "
                    "Ask for their main monthly spends with ask_clarification, or use list_cards / "
                    "generate_content_brief for a general category answer.")
        return _ungrounded_spends(spends, request_text, user_text)
    if name == "whatsapp_compare_cards" and isinstance(args.get("spend_profile"), dict):
        spends = {k: v for k, v in args["spend_profile"].items()
                  if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
        return _ungrounded_spends(spends, request_text, user_text)
    return None


def _ungrounded_spends(spends: dict[str, Any], request_text: str, user_text: str) -> str | None:
    stated = numbers_in(request_text)
    amounts = _stated_amounts(request_text, user_text)
    ungrounded, unclear, mismatched = [], [], []
    for key, value in sorted(spends.items()):
        if "lounge" in key:   # visit counts, not rupees
            if not _derived_from(value, stated, _LOUNGE_FACTORS):
                ungrounded.append(key)
            continue
        annual_field = key.endswith("_annual") or key in _ANNUAL_SPEND_KEYS
        candidates = [(values, period) for values, period in amounts if _derived_from(value, values, _SPEND_FACTORS)]
        if any(period and _derived_from(value, values, (_PERIOD_FACTOR[period, annual_field],))
               for values, period in candidates):
            continue
        if any(period is None for _, period in candidates):
            unclear.append(key)
        elif candidates:
            mismatched.append(key)
        else:
            ungrounded.append(key)

    problems = []
    if ungrounded:
        problems.append(f"These values are not amounts the user stated in this request: {', '.join(ungrounded)}. "
                        "Do not estimate them or reuse amounts from earlier answers. Call ask_clarification to ask "
                        "the user for those amounts (or to confirm earlier ones), or leave those fields out.")
    if unclear:
        problems.append(f"The user did not say whether the amounts for {', '.join(unclear)} are monthly or annual. "
                        "Do not assume either. Call ask_clarification to ask whether they spend that per month "
                        "or per year.")
    if mismatched:
        problems.append(f"These values do not match the period the user stated: {', '.join(mismatched)}. Fields "
                        "ending in _annual and life_insurance take yearly rupees; every other spend field takes "
                        "monthly rupees (divide a yearly amount by 12, multiply a monthly amount by 12).")
    return " ".join(problems) or None


# Spend periods. A period word next to an amount sets that amount's period; amounts in the same
# sentence without one share the sentence's period. A bare reply such as "monthly" (to a clarifying
# question) sets the whole request, and otherwise earlier messages count only if they used one period.
_MONTHLY_RE = re.compile(r"\bmonth(?:s|ly)?\b|\bmnth\b|/\s*(?:mo|mth|m)\b|\bp\.?\s?m\b\.?", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"\b(?:year|years|yearly|annual|annually|annum|yr|yrs)\b|\bp\.?\s?a\b\.?", re.IGNORECASE)
_BARE_PERIOD_RE = re.compile(
    r"^\W*(?:(?:yes|yeah|yep|ok|okay|sure|it'?s|its|it is|that'?s|that is|they'?re|they are|these are|"
    r"those are|all|both|all of them|all are|both are|spends are|amounts are|in)\W+)*"
    r"(?P<period>monthly|per month|a month|every month|each month|month|yearly|annually|annual|per year|"
    r"a year|every year|each year|year|per annum|p\.?\s?a\.?|p\.?\s?m\.?)"
    r"(?:\W+(?:spends?|spending|amounts?|basis|only|please|pls))*\W*$",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?;\n](?!\d)")
_ANNUAL_SPEND_KEYS = frozenset({"life_insurance"})
# Factor from the period the user stated to the field's unit: (stated period, field is annual) -> factor.
_PERIOD_FACTOR = {("monthly", False): 1.0, ("annual", False): 1 / 12,
                  ("monthly", True): 12.0, ("annual", True): 1.0}


def _period_of(text: str) -> str | None:
    monthly, annual = bool(_MONTHLY_RE.search(text)), bool(_ANNUAL_RE.search(text))
    return "monthly" if monthly and not annual else "annual" if annual and not monthly else None


def _periods_in(text: str) -> list[tuple[set[float], str | None]]:
    """Each number in `text` with the period stated for it (None when not stated)."""
    spans = number_spans(text)
    found: list[tuple[set[float], str | None, int]] = []
    sentence = 0
    for i, (values, start, end) in enumerate(spans):
        prev_end = spans[i - 1][2] if i else 0
        next_start = spans[i + 1][1] if i + 1 < len(spans) else len(text)
        before = _SENTENCE_END_RE.split(text[prev_end:start])[-1]
        after = _SENTENCE_END_RE.split(text[end:next_start])[0]
        if i and _SENTENCE_END_RE.search(text[prev_end:start]):
            sentence += 1
        found.append((values, _period_of(after) or _period_of(before), sentence))
    result = []
    for values, period, sentence in found:
        if period is None:
            shared = {p for _, p, s in found if s == sentence and p}
            period = shared.pop() if len(shared) == 1 else None
        result.append((values, period))
    return result


def _stated_amounts(request_text: str, user_text: str) -> list[tuple[set[float], str | None]]:
    """Amounts in the current request with their periods, filling unstated ones from a bare
    period reply in the request or, failing that, from earlier messages that used only one period."""
    amounts = _periods_in(request_text)
    if all(period for _, period in amounts):
        return amounts
    bare = {_period_of(m.group("period")) for line in request_text.splitlines()
            if (m := _BARE_PERIOD_RE.match(line))}
    earlier = {period for _, period in _periods_in(user_text) if period}
    fallback = bare if bare else earlier
    default = next(iter(fallback)) if len(fallback) == 1 else None
    return [(values, period or default) for values, period in amounts]
