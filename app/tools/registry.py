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

# Tools Gemini may call, with parameters hidden from it (marketing/content-generation
# knobs or internal IDs that a chat user would never supply).
EXPOSED_TOOLS: dict[str, frozenset[str]] = {
    "recommend_cards": frozenset(),
    "list_cards": frozenset({"bank_ids", "credit_score", "sort_by"}),
    "get_card_details": frozenset(),
    "compare_cards": frozenset(),
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


def grounding_problem(name: str, args: dict[str, Any], user_text: str) -> str | None:
    """Reject calls built on values the user never provided (no guessing)."""
    if name == "check_eligibility":
        pincode = str(args.get("pincode", ""))
        if not re.search(rf"(?<!\d){re.escape(pincode)}(?!\d)", user_text):
            return ("The user has not provided this pincode. Do not assume one; "
                    "call ask_clarification to request their 6-digit pincode.")
        if not _mentions_amount(user_text.replace(pincode, "")):
            return ("The user has not stated their income. Do not assume it; "
                    "call ask_clarification to request their monthly or annual income.")
    if name == "recommend_cards":
        spends = [v for k, v in args.items()
                  if k not in _SPEND_EXCLUDED_KEYS and isinstance(v, (int, float)) and v > 0]
        if not spends or not _mentions_amount(user_text):
            return ("The user has not stated any spending amounts, so a personalised ranking would rest on guesses. "
                    "Ask for their main monthly spends with ask_clarification, or use list_cards / "
                    "generate_content_brief for a general category answer.")
    return None
