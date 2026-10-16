"""Offline checks for P1 figure grounding: ₹ amounts in an answer must come from this turn's tool results
or the user's own figures (with monthly/annual conversion of those).

Only rupee amounts are checked. Percentages (e.g. a forex markup) are not grounded by this code.

Run:  venv\\Scripts\\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.chat.pipeline import ChatPipeline
from app.safety.grounding import UNVERIFIED_PLACEHOLDER, numbers_in, strip_unsupported_amounts, unsupported_amounts

# Shape of a recommend_cards result logged for "₹15,000 a month on Amazon".
TOOL_RESULT = ('{"recommendations":[{"card_name":"Amazon Pay ICICI Credit Card","net_annual_savings":"₹9,000",'
               '"joining_fee":"Free","annual_fee":"Free"},{"card_name":"HDFC Millennia Credit Card",'
               '"net_annual_savings":"₹9,000","joining_fee":"₹1,180 (₹1,000 + GST)"}]}')
REQUEST = "I spend ₹15,000 a month on Amazon"


def allowed() -> set[float]:
    return ChatPipeline._allowed_figures(numbers_in(TOOL_RESULT), REQUEST)


class ToolFiguresPass(unittest.TestCase):
    def test_figures_from_the_tool_result_are_kept(self):
        text = "Both cards save ₹9,000 a year; Millennia has a ₹1,180 joining fee."
        self.assertEqual(unsupported_amounts(text, allowed()), [])
        self.assertEqual(strip_unsupported_amounts(text, allowed()), text)

    def test_users_own_figure_and_its_annual_conversion_pass(self):
        text = "You spend ₹15,000 a month, which is ₹1,80,000 a year."
        self.assertEqual(unsupported_amounts(text, allowed()), [])

    def test_lakh_units_are_understood(self):
        self.assertEqual(unsupported_amounts("That's ₹1.8 lakh a year on Amazon.", allowed()), [])


class InventedFiguresAreRemoved(unittest.TestCase):
    def test_invented_amount_is_replaced(self):
        text = "Millennia would actually save you ₹7,820 in year one."
        self.assertEqual(unsupported_amounts(text, allowed()), ["₹7,820"])
        self.assertIn(UNVERIFIED_PLACEHOLDER, strip_unsupported_amounts(text, allowed()))
        self.assertNotIn("7,820", strip_unsupported_amounts(text, allowed()))

    def test_both_ends_of_a_range_are_checked(self):
        self.assertTrue(unsupported_amounts("Expect ₹9,000-12,000 a year.", allowed()))

    def test_hedged_amount_must_match_exactly(self):
        # Within the normal 1% tolerance, but "around" means it was estimated, so it must match evidence.
        self.assertTrue(unsupported_amounts("You'd save around ₹9,050 a year.", allowed()))
        self.assertEqual(unsupported_amounts("You'd save ₹9,050 a year.", allowed()), [])

    def test_monthly_conversion_of_tool_figures_is_not_allowed(self):
        # Conversions apply to the user's figures only; ₹750/month is derived from a tool figure.
        self.assertEqual(unsupported_amounts("That's ₹750 a month.", allowed()), ["₹750"])

    def test_figures_from_earlier_turns_do_not_count(self):
        self.assertTrue(unsupported_amounts("The joining fee is ₹2,500.", allowed()))


class KnownGap(unittest.TestCase):
    def test_percentages_are_not_grounded(self):
        # Documented limitation: an invented forex markup passes. Remove this test if percentages get grounded.
        self.assertEqual(unsupported_amounts("Forex markup is 3.5% on this card.", allowed()), [])


if __name__ == "__main__":
    unittest.main()
