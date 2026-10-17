"""
Tests for the PII detector and the reasoning guardrail. Run inside the LiteLLM
container, where this repo is mounted at /app/custom:

    docker compose exec litellm python -m unittest discover -s /app/custom/tests -v
"""

import os
import sys
import time
import unittest

from fastapi import HTTPException

from litellm import ModelResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_guardrail import PIIDetector, PIIReasoningGuardrail  # noqa: E402


class PIIRegexTest(unittest.TestCase):
    def setUp(self):
        self.detector = PIIDetector()

    def test_detects_email_addresses(self):
        for text in ("contact jane.doe@example.com", "JOHN_SMITH+news@mail.co.uk please", "<a@b.io>"):
            with self.subTest(text=text):
                self.assertEqual(self.detector.detect(text), ["email"])

    def test_ignores_non_emails(self):
        for text in ("user@localhost", "@handle", "email me at jane at example dot com"):
            with self.subTest(text=text):
                self.assertEqual(self.detector.detect(text), [])

    def test_detects_ssns(self):
        for text in ("SSN: 123-45-6789.", "my ssn is 123-45-6789"):
            with self.subTest(text=text):
                self.assertEqual(self.detector.detect(text), ["us_ssn"])

    def test_ignores_ssn_lookalikes(self):
        for text in (
            "123456789",  # no dashes: too many false positives (order IDs etc.)
            "123 45 6789",  # spaces instead of dashes
            "1123-45-6789",  # part of a longer number
            "2024-123-45-6789",  # part of a longer dash-separated number
            "000-12-3456",  # area 000 is never issued
            "666-12-3456",  # area 666 is never issued
            "912-34-5678",  # 9xx areas are never issued
            "123-00-4567",  # group 00 is never issued
            "123-45-0000",  # serial 0000 is never issued
        ):
            with self.subTest(text=text):
                self.assertEqual(self.detector.detect(text), [])

    def test_detects_both_in_one_text(self):
        self.assertEqual(self.detector.detect("SSN 123-45-6789, email a@example.com"), ["email", "us_ssn"])

    def test_scans_long_inputs_quickly(self):
        """Regression: with \\b instead of the lookbehind, each of these took ~10s at 128 KB."""
        size = 256 * 1024
        for text in ("a." * (size // 2), "a@" + "a." * (size // 2), "_." * (size // 2)):
            with self.subTest(text=text[:10] + "..."):
                start = time.perf_counter()
                self.detector.detect(text)
                self.assertLess(time.perf_counter() - start, 0.5)


class PIIDetectAllTest(unittest.TestCase):
    def setUp(self):
        self.detector = PIIDetector()

    def test_detects_pii_in_any_text(self):
        self.assertEqual(self.detector.detect_all(["hello", "a@example.com", "123-45-6789"]), ["email", "us_ssn"])

    def test_detects_pii_split_across_texts(self):
        self.assertEqual(self.detector.detect_all(["my email is jane@", "example.com"]), ["email"])
        self.assertEqual(self.detector.detect_all(["SSN 123-45-", "6789"]), ["us_ssn"])

    def test_still_checks_texts_individually(self):
        # Joined, this is "4123-45-6789", which is not an SSN.
        self.assertEqual(self.detector.detect_all(["4", "123-45-6789"]), ["us_ssn"])

    def test_handles_no_texts(self):
        self.assertEqual(self.detector.detect_all([]), [])


class PIIReasoningGuardrailTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guardrail = PIIReasoningGuardrail(guardrail_name="pii-reasoning-test", event_hook="post_call")

    def response(self, content, reasoning):
        return ModelResponse(
            choices=[{"message": {"role": "assistant", "content": content, "reasoning_content": reasoning}}]
        )

    async def check(self, response):
        await self.guardrail.async_post_call_success_hook(data={}, user_api_key_dict=None, response=response)

    async def test_blocks_pii_in_reasoning(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.check(self.response("done", "The address is john.smith@example.com."))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "PII detected in model reasoning: email")

    async def test_allows_clean_reasoning(self):
        await self.check(self.response("Paris", "The capital of France is Paris."))

    async def test_allows_responses_without_reasoning(self):
        await self.check(self.response("Paris", None))


if __name__ == "__main__":
    unittest.main()
