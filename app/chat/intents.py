"""Small talk answered locally, without calling Gemini or the MCP server."""

from __future__ import annotations

import re

_CAPABILITIES = (
    "I can help you:\n"
    "- **Find cards** that fit your monthly spending\n"
    "- **Explain a card's** fees, rewards and benefits\n"
    "- **Compare** two or three cards side by side\n"
    "- **Check eligibility** using your income, employment type and pincode"
)

_INTENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^(?:hi+|hello+|hey+|hiya|helo|hai|namaste|namaskar|hola|yo|greetings|"
                   r"good (?:morning|afternoon|evening|day))(?: there| team| all| everyone| bot| buddy)?$"),
        "Hello! I'm the Great.cards Credit Card Advisor.\n\n" + _CAPABILITIES + "\n\n"
        "To get started, tell me where you spend most each month (for example online shopping, fuel, "
        "groceries or travel) or name a card you're curious about.",
    ),
    (
        re.compile(r"^(?:help|menu|start|what can you do|what do you do|who are you|what are you|"
                   r"how does this work|how do you work)$"),
        "I'm the Great.cards Credit Card Advisor for India.\n\n" + _CAPABILITIES + "\n\n"
        "Tip: sharing your rough monthly spends by category gives the most personalised results.",
    ),
    (
        re.compile(r"^(?:ok(?:ay)? )?(?:thanks?|thank you|thank u|thanx|thx|ty|tysm)(?: so much| a lot| very much)?$"),
        "You're welcome! Let me know if you'd like to compare cards or check eligibility next.",
    ),
    (
        re.compile(r"^(?:ok(?:ay)? )?(?:bye|goodbye|good bye|see you|see ya|cya|take care)$"),
        "Goodbye! Come back any time you need help choosing a credit card.",
    ),
)


def _normalise(text: str) -> str:
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def match_small_talk(text: str) -> str | None:
    """Return a canned reply when the whole message is small talk, else None."""
    normalised = _normalise(text)
    if not normalised:
        return None
    for pattern, reply in _INTENTS:
        if pattern.fullmatch(normalised):
            return reply
    return None
