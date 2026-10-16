"""Offline checks for sensitive-data redaction (runs before Gemini, history and logs).

Run:  venv\\Scripts\\python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.safety.redaction import has_meaningful_content, redact

VALID_CARD = "4111 1111 1111 1111"     # passes Luhn
INVALID_CARD = "4111 1111 1111 1112"   # fails Luhn


class CardNumbers(unittest.TestCase):
    def test_luhn_valid_number_is_redacted(self):
        result = redact(f"here is {VALID_CARD}")
        self.assertNotIn("4111", result.text)
        self.assertIn("card number", result.findings)
        self.assertTrue(result.critical)

    def test_luhn_failing_number_next_to_card_is_still_redacted(self):
        result = redact(f"my card number is {INVALID_CARD}")
        self.assertNotIn("1112", result.text)
        self.assertIn("card number", result.findings)

    def test_luhn_failing_number_far_from_card_is_kept(self):
        # More than 15 characters between "card" and the digits, and no Luhn match: not a card number.
        text = f"I got this credit card ages ago and my order id is {INVALID_CARD.replace(' ', '')}"
        self.assertNotIn("card number", redact(text).findings)


class Aadhaar(unittest.TestCase):
    def test_twelve_digits_are_redacted(self):
        result = redact("Aadhaar 2345 6789 0123")
        self.assertIn("Aadhaar number", result.findings)
        self.assertNotIn("0123", result.text)

    def test_part_of_a_16_digit_number_is_not_aadhaar(self):
        self.assertNotIn("Aadhaar number", redact(f"card {VALID_CARD}").findings)

    def test_leading_0_or_1_is_not_aadhaar(self):
        self.assertNotIn("Aadhaar number", redact("ref 1234 5678 9012").findings)


class Secrets(unittest.TestCase):
    def test_cvv(self):
        result = redact("cvv is 123")
        self.assertEqual(result.text, "cvv is [REDACTED CVV]")
        self.assertTrue(result.critical)

    def test_otp(self):
        self.assertIn("OTP", redact("the OTP is 482913").findings)

    def test_four_digit_pin_but_not_six_digit_pincode(self):
        self.assertIn("PIN", redact("my pin 4321").findings)
        self.assertNotIn("PIN", redact("my pin 400071").findings)

    def test_pan(self):
        self.assertIn("PAN", redact("PAN ABCPE1234F").findings)


class KeptForEligibility(unittest.TestCase):
    def test_pincode_and_income_are_not_redacted(self):
        text = "I earn ₹8,00,000 a year, pincode 400071"
        result = redact(text)
        self.assertEqual(result.text, text)
        self.assertEqual(result.findings, ())


class MeaningfulContent(unittest.TestCase):
    def test_only_secrets_has_nothing_to_answer(self):
        self.assertFalse(has_meaningful_content(redact(f"my card {VALID_CARD} cvv 123").text))

    def test_question_survives_redaction(self):
        self.assertTrue(has_meaningful_content(redact(f"is {VALID_CARD} good for travel rewards").text))


if __name__ == "__main__":
    unittest.main()
