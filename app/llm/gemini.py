"""Thin async wrapper around the Gemini API (google-genai) for tool-calling chat turns."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

# JSON Schema keywords Gemini accepts in function declarations; others are dropped.
_SCHEMA_KEYS = {
    "type", "description", "enum", "properties", "required", "items", "minimum", "maximum",
    "minItems", "maxItems", "minLength", "maxLength", "format", "anyOf", "title", "additionalProperties",
}


class LLMError(Exception):
    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def to_gemini_schema(schema: Any) -> Any:
    """Reduce an MCP JSON Schema to the subset Gemini function declarations support."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "exclusiveMinimum" and isinstance(value, (int, float)) and "minimum" not in schema:
            out["minimum"] = value
        elif key not in _SCHEMA_KEYS:
            continue
        elif key == "properties":
            out[key] = {name: to_gemini_schema(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = to_gemini_schema(value)
        elif key == "anyOf":
            out[key] = [to_gemini_schema(sub) for sub in value]
        else:
            out[key] = value
    return out


def build_tool(declarations: list[dict[str, Any]]) -> types.Tool:
    """declarations: [{name, description, parameters (JSON Schema)}]."""
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=d["name"],
            description=d["description"],
            parameters_json_schema=to_gemini_schema(d["parameters"]),
        )
        for d in declarations
    ])


def user_content(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def model_content(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def function_response_part(call: types.FunctionCall, payload: dict[str, Any]) -> types.Part:
    return types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=payload))


def function_responses_content(parts: list[types.Part]) -> types.Content:
    return types.Content(role="user", parts=parts)


def response_content(response: types.GenerateContentResponse) -> types.Content | None:
    """The model turn exactly as returned (keeps thought signatures needed for tool calling)."""
    if response.candidates and response.candidates[0].content:
        return response.candidates[0].content
    return None


def function_calls(response: types.GenerateContentResponse) -> list[types.FunctionCall]:
    content = response_content(response)
    if content is None:
        return []
    return [part.function_call for part in content.parts or [] if part.function_call]


def response_text(response: types.GenerateContentResponse) -> str:
    content = response_content(response)
    if content is None:
        return ""
    return "".join(part.text for part in content.parts or [] if part.text and not part.thought).strip()


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout_seconds: float):
        self._model = model
        self._timeout = timeout_seconds
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )

    async def generate(
        self,
        contents: list[types.Content],
        *,
        system_instruction: str,
        tool: types.Tool | None = None,
        allow_tool_calls: bool = True,
        require_tool_call: bool = False,
    ) -> types.GenerateContentResponse:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.2,
            max_output_tokens=1500,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if tool is not None:
            config.tools = [tool]
            mode = "NONE" if not allow_tool_calls else "ANY" if require_tool_call else "AUTO"
            config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=mode)
            )
        try:
            return await asyncio.wait_for(
                self._client.aio.models.generate_content(model=self._model, contents=contents, config=config),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise LLMError("The assistant took too long to respond. Please try again.") from exc
        except errors.APIError as exc:
            logger.warning("Gemini API error: status=%s", exc.code)
            if exc.code == 429:
                raise LLMError("The assistant is busy right now. Please try again in a minute.") from exc
            if exc.code in (400, 401, 403) and "key" in str(exc.message or "").lower():
                raise LLMError("The assistant isn't configured correctly (Gemini API key rejected).") from exc
            raise LLMError("The assistant couldn't process that request. Please try again.") from exc
        except Exception as exc:
            logger.warning("Gemini request failed: %s", type(exc).__name__)
            raise LLMError("The assistant is temporarily unavailable. Please try again.") from exc

    async def aclose(self) -> None:
        try:
            await self._client.aio.aclose()
        except Exception:
            logger.debug("Ignoring error while closing Gemini client", exc_info=True)
