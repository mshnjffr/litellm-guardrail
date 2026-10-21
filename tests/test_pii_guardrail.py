"""
Tests for the PII detector and the guardrails. Run inside the LiteLLM
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

from pii_guardrail import PIIDetector, PIIGuardrail, PIIReasoningGuardrail  # noqa: E402


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
                # CPU time of this thread only: LiteLLM's background threads can
                # stall it, which would make a wall-clock limit flaky.
                start = time.thread_time()
                self.detector.detect(text)
                self.assertLess(time.thread_time() - start, 0.5)


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


class PIIMaskTest(unittest.TestCase):
    def setUp(self):
        self.detector = PIIDetector()

    def test_masks_each_pii_type(self):
        self.assertEqual(
            self.detector.mask("Email jane@example.com or a@b.io, SSN 219-09-9999."),
            "Email <EMAIL> or <EMAIL>, SSN <US_SSN>.",
        )

    def test_leaves_text_without_pii_unchanged(self):
        self.assertEqual(self.detector.mask("What is 2 + 2?"), "What is 2 + 2?")


class PIIGuardrailCase(unittest.IsolatedAsyncioTestCase):
    """Applies a PIIGuardrail created with `prompt_action`."""

    prompt_action = "block"

    def setUp(self):
        self.guardrail = PIIGuardrail(
            guardrail_name="pii-test", event_hook=["pre_call", "post_call"], prompt_action=self.prompt_action
        )

    async def apply(self, texts, input_type="request"):
        return await self.guardrail.apply_guardrail(inputs={"texts": texts}, request_data={}, input_type=input_type)

    async def assert_blocked(self, texts, detail, input_type="request"):
        with self.assertRaises(HTTPException) as ctx:
            await self.apply(texts, input_type)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, detail)


class PIIGuardrailTest(PIIGuardrailCase):
    """The default prompt_action: prompts containing PII are blocked."""

    async def test_blocks_pii_in_the_prompt(self):
        await self.assert_blocked(["how are you? testuser@gmail.com", "hi"], "PII detected in prompt: email")

    async def test_passes_a_clean_prompt_unchanged(self):
        self.assertEqual(await self.apply(["What is 2 + 2?"]), {"texts": ["What is 2 + 2?"]})

    async def test_blocks_pii_split_across_prompt_parts(self):
        await self.assert_blocked(["my email is jane@", "example.com"], "PII detected in prompt: email")

    async def test_blocks_pii_in_the_response(self):
        await self.assert_blocked(["Sure: 219-09-9999"], "PII detected in model response: us_ssn", input_type="response")

    async def test_passes_inputs_without_text(self):
        inputs = await self.guardrail.apply_guardrail(inputs={}, request_data={}, input_type="request")
        self.assertEqual(inputs, {})

    def test_rejects_unknown_prompt_action(self):
        with self.assertRaises(ValueError):
            PIIGuardrail(guardrail_name="pii-test", event_hook="pre_call", prompt_action="allow")


class PIIGuardrailMaskingTest(PIIGuardrailCase):
    """With prompt_action "mask", PII in prompts is masked instead of blocked."""

    prompt_action = "mask"

    async def test_masks_pii_in_the_prompt(self):
        inputs = await self.apply(["how are you? testuser@gmail.com", "hi"])
        self.assertEqual(inputs["texts"], ["how are you? <EMAIL>", "hi"])

    async def test_still_blocks_pii_split_across_prompt_parts(self):
        await self.assert_blocked(["my email is jane@", "example.com"], "PII detected in prompt: email")

    async def test_still_blocks_pii_in_the_response(self):
        await self.assert_blocked(["Sure: 219-09-9999"], "PII detected in model response: us_ssn", input_type="response")


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
