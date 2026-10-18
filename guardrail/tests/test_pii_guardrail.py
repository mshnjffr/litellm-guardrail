"""
Tests for the LiteLLM guardrails, against a stub PII service on a local port.
They need LiteLLM, so run them in the LiteLLM image, from the repo root:

    docker run --rm -v "$PWD/guardrail:/guardrail:ro" --entrypoint python \
      docker.litellm.ai/berriai/litellm:v1.105.1 -m unittest discover -s /guardrail/tests -v
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import HTTPException

from litellm import ModelResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pii_guardrail import PIIGuardrail, PIIReasoningGuardrail  # noqa: E402


class StubPIIService:
    """A PII service that records each request and returns a canned decision."""

    def __init__(self):
        self.requests = []
        self.decision = {"action": "allow", "entities": [], "texts": None}
        self.status = 200
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(stub.decision).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GuardrailTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = StubPIIService()
        self.addCleanup(self.service.close)

    def block(self, *entities):
        self.service.decision = {"action": "block", "entities": list(entities), "texts": None}

    def mask(self, entities, texts):
        self.service.decision = {"action": "mask", "entities": entities, "texts": texts}


class PIIGuardrailTest(GuardrailTestCase):
    def setUp(self):
        super().setUp()
        self.guardrail = PIIGuardrail(api_base=self.service.url, guardrail_name="pii-test", event_hook=["pre_call", "post_call"])

    async def apply(self, texts, input_type="request"):
        return await self.guardrail.apply_guardrail(inputs={"texts": texts}, request_data={}, input_type=input_type)

    async def test_sends_texts_and_source_to_the_service(self):
        await self.apply(["hello", "world"], input_type="response")
        self.assertEqual(self.service.requests, [{"texts": ["hello", "world"], "source": "response"}])

    async def test_allows_clean_text_unchanged(self):
        self.assertEqual(await self.apply(["What is 2 + 2?"]), {"texts": ["What is 2 + 2?"]})

    async def test_masks_the_prompt_when_the_service_masks_it(self):
        self.mask(["EMAIL_ADDRESS", "PERSON"], ["Email <PERSON> at <EMAIL_ADDRESS>", "hi"])
        inputs = await self.apply(["Email John at john@example.com", "hi"])
        self.assertEqual(inputs["texts"], ["Email <PERSON> at <EMAIL_ADDRESS>", "hi"])

    async def test_blocks_the_prompt_when_the_service_blocks_it(self):
        self.block("EMAIL_ADDRESS")
        with self.assertRaises(HTTPException) as ctx:
            await self.apply(["My email is jane@", "example.com"])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "PII detected in prompt: EMAIL_ADDRESS")

    async def test_blocks_pii_in_the_response(self):
        self.block("US_SSN")
        with self.assertRaises(HTTPException) as ctx:
            await self.apply(["219-09-9999"], input_type="response")
        self.assertEqual(ctx.exception.detail, "PII detected in model response: US_SSN")

    async def test_does_not_call_the_service_without_text(self):
        await self.guardrail.apply_guardrail(inputs={}, request_data={}, input_type="request")
        self.assertEqual(self.service.requests, [])

    async def test_fails_closed_when_the_service_errors(self):
        self.service.status = 500
        with self.assertRaises(Exception) as ctx:
            await self.apply(["hello"])
        self.assertNotIsInstance(ctx.exception, HTTPException)  # a failure, not a PII block

    async def test_fails_closed_when_the_service_is_down(self):
        self.service.close()
        with self.assertRaises(Exception):
            await self.apply(["hello"])

    def test_holds_streamed_responses_until_scanned(self):
        self.assertTrue(self.guardrail.streaming_buffer_until_moderated)


class PIIReasoningGuardrailTest(GuardrailTestCase):
    def setUp(self):
        super().setUp()
        self.guardrail = PIIReasoningGuardrail(api_base=self.service.url, guardrail_name="pii-reasoning-test", event_hook="post_call")

    async def check(self, content, reasoning):
        response = ModelResponse(
            choices=[{"message": {"role": "assistant", "content": content, "reasoning_content": reasoning}}]
        )
        await self.guardrail.async_post_call_success_hook(data={}, user_api_key_dict=None, response=response)

    async def test_sends_only_the_reasoning(self):
        await self.check("done", "Thinking it over.")
        self.assertEqual(self.service.requests, [{"texts": ["Thinking it over."], "source": "reasoning"}])

    async def test_blocks_pii_in_the_reasoning(self):
        self.block("EMAIL_ADDRESS")
        with self.assertRaises(HTTPException) as ctx:
            await self.check("done", "The address is john@example.com.")
        self.assertEqual(ctx.exception.detail, "PII detected in model reasoning: EMAIL_ADDRESS")

    async def test_skips_responses_without_reasoning(self):
        await self.check("4", None)
        self.assertEqual(self.service.requests, [])


if __name__ == "__main__":
    unittest.main()
