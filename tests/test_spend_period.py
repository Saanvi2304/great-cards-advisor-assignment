"""Offline regression checks: spend amounts need a stated period (monthly/annual) before ranking.

Covers session_20261016-092659_e561d101032c_4192.log, where "I spend ₹10,000 on Myntra" (no period)
was sent to recommend_cards as a monthly spend. No Gemini or MCP calls are made.

Run:  venv\\Scripts\\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from google.genai import types

from app.chat.pipeline import ChatPipeline
from app.chat.transcript import TurnTrace
from app.safety.grounding import numbers_in, unsupported_amounts
from app.tools.registry import ToolCatalog, ToolSpec, grounding_problem

MYNTRA = ("okay so I spend ₹10,000 on Myntra. Recommend a credit card and tell me exactly how much "
          "I will save annually after fees.")
# Abridged from the tool result logged for that turn.
MYNTRA_TOOL_RESULT = ('{"recommendations":[{"rank":1,"card_name":"HSBC Live+ Credit Card","net_annual_savings":'
                      '"₹10,821","annual_rewards_value":"₹12,000","joining_fee":"₹1,179 (incl. GST)",'
                      '"annual_fee":"₹1,179 (incl. GST)"}]}')


def recommend(spends: dict, request: str, earlier: str = "") -> str | None:
    """grounding_problem as the pipeline calls it: user_text = earlier messages + current request."""
    return grounding_problem("recommend_cards", spends, f"{earlier}\n{request}", request)


class PeriodNotStated(unittest.TestCase):
    def test_logged_myntra_turn_is_rejected_and_asks_for_period(self):
        problem = recommend({"other_online_spends": 10000, "top_n": 3, "response_format": "full"}, MYNTRA)
        self.assertIsNotNone(problem)
        self.assertIn("monthly or annual", problem)
        self.assertIn("ask_clarification", problem)

    def test_annual_conversion_of_unstated_amount_is_also_rejected(self):
        self.assertIn("monthly or annual", recommend({"other_online_spends": 833}, MYNTRA))

    def test_savings_word_does_not_count_as_spend_period(self):
        # "save annually" describes the savings, not the ₹10,000 spend.
        self.assertIn("monthly or annual", recommend({"other_online_spends": 10000}, MYNTRA))

    def test_earlier_mixed_periods_do_not_settle_it(self):
        earlier = "I spend 15k a month on Amazon and 2 lakh a year on flights"
        self.assertIn("monthly or annual",
                      recommend({"other_online_spends": 10000}, "And ₹10,000 on Myntra?", earlier))

    def test_spend_aware_comparison_gets_the_same_check(self):
        args = {"card_aliases": ["a", "b"], "spend_profile": {"other_online_spends": 10000}}
        self.assertIn("monthly or annual", grounding_problem("whatsapp_compare_cards", args, MYNTRA, MYNTRA))

    def test_mcp_tool_is_not_called(self):
        problem, called = asyncio.run(_execute_recommend({"other_online_spends": 10000}, MYNTRA))
        self.assertFalse(called)
        self.assertIn("monthly or annual", problem)


class PeriodEstablished(unittest.TestCase):
    def test_stated_monthly(self):
        self.assertIsNone(recommend({"other_online_spends": 10000}, "I spend ₹10,000 a month on Myntra"))
        self.assertIsNone(recommend({"other_online_spends": 10000}, "10k pm on Myntra"))

    def test_stated_annual_is_converted_to_monthly(self):
        request = "I spend ₹1.2 lakh a year on Myntra"
        self.assertIsNone(recommend({"other_online_spends": 10000}, request))
        # Sending the yearly figure as a monthly spend would inflate annual savings 12x.
        self.assertIn("do not match the period", recommend({"other_online_spends": 120000}, request))

    def test_annual_fields(self):
        self.assertIsNone(recommend({"flights_annual": 200000}, "I spend 2 lakh a year on flights"))
        self.assertIsNone(recommend({"flights_annual": 120000}, "I spend 10k a month on flights"))
        self.assertIn("do not match the period",
                      recommend({"flights_annual": 10000}, "I spend 10k a month on flights"))

    def test_period_shared_within_a_sentence(self):
        request = "I spend 15000 on Amazon and 5000 on groceries every month"
        self.assertIsNone(recommend({"amazon_spends": 15000, "grocery_spends_online": 5000}, request))

    def test_reply_to_clarifying_question(self):
        # pending_request (the original message) + the user's answer form the current request.
        self.assertIsNone(recommend({"other_online_spends": 10000}, MYNTRA + "\nmonthly"))
        yearly = MYNTRA + "\nIt's yearly"
        self.assertIsNone(recommend({"other_online_spends": 833}, yearly))
        self.assertIn("do not match the period", recommend({"other_online_spends": 10000}, yearly))

    def test_period_from_previous_conversation(self):
        earlier = "I spend ₹15,000 a month on Amazon"
        self.assertIsNone(recommend({"other_online_spends": 10000}, "Also ₹10,000 on Myntra", earlier))

    def test_mcp_tool_is_called(self):
        problem, called = asyncio.run(
            _execute_recommend({"other_online_spends": 10000}, "I spend ₹10,000 every month on Myntra"))
        self.assertIsNone(problem)
        self.assertTrue(called)


class P1GroundingPreserved(unittest.TestCase):
    def test_invented_amount_still_rejected(self):
        problem = recommend({"flights_annual": 480000, "hotels_annual": 120000}, "I take 4 flights a month")
        self.assertIn("not amounts the user stated", problem)

    def test_no_spends_still_rejected(self):
        self.assertIn("has not stated any spending amounts", recommend({"top_n": 3}, "Recommend a travel card"))

    def test_invented_annual_savings_flagged(self):
        allowed = ChatPipeline._allowed_figures(numbers_in(MYNTRA_TOOL_RESULT), "I spend ₹10,000 a month on Myntra")
        self.assertEqual(unsupported_amounts("Net savings: ₹10,821 a year after the ₹1,179 fee.", allowed), [])
        # Model arithmetic on top of tool figures (e.g. 10,821 x 2, or savings x 12) is not allowed.
        self.assertTrue(unsupported_amounts("Over two years you save ₹21,642.", allowed))
        self.assertTrue(unsupported_amounts("You save ₹1,29,852 a year.", allowed))


_RECOMMEND_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "amazon_spends": {"type": "number"},
        "flipkart_spends": {"type": "number"},
        "other_online_spends": {"type": "number"},
        "other_offline_spends": {"type": "number"},
        "grocery_spends_online": {"type": "number"},
        "online_food_ordering": {"type": "number"},
        "fuel": {"type": "number"},
        "dining_or_going_out": {"type": "number"},
        "flights_annual": {"type": "number"},
        "hotels_annual": {"type": "number"},
        "domestic_lounge_usage_quarterly": {"type": "number"},
        "international_lounge_usage_quarterly": {"type": "number"},
        "mobile_phone_bills": {"type": "number"},
        "electricity_bills": {"type": "number"},
        "water_bills": {"type": "number"},
        "insurance_health_annual": {"type": "number"},
        "insurance_car_or_bike_annual": {"type": "number"},
        "rent": {"type": "number"},
        "school_fees": {"type": "number"},
        "offline_grocery": {"type": "number"},
        "life_insurance": {"type": "number"},
        "top_n": {"type": "number", "default": 5},
        "response_format": {"type": "string", "enum": ["full", "brief", "comparison"], "default": "full"},
    },
}


async def _execute_recommend(args: dict, request: str) -> tuple[str | None, bool]:
    """Run ChatPipeline._execute with a fake MCP connection; return (error text, whether MCP was called)."""
    spec = ToolSpec.from_mcp(SimpleNamespace(name="recommend_cards", description="", inputSchema=_RECOMMEND_SCHEMA))

    class FakeConnection:
        called = False

        async def call(self, name, arguments):
            FakeConnection.called = True
            return SimpleNamespace(ok=True, data={}, text="{}")

    pipeline = ChatPipeline(sessions=None, llm=None, tools=None)  # type: ignore[arg-type]
    call = types.FunctionCall(name="recommend_cards", args=args)
    outcome = await pipeline._execute(FakeConnection(), ToolCatalog({"recommend_cards": spec}), call,
                                      request, request, TurnTrace("test"))
    return outcome.payload.get("error"), FakeConnection.called


if __name__ == "__main__":
    unittest.main()

