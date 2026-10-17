"""
LiteLLM custom guardrails that block prompts and model responses containing PII
(email addresses and US Social Security numbers).
"""

import re
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import HTTPException

from litellm.integrations.custom_guardrail import CustomGuardrail, log_guardrail_information
from litellm.types.utils import Choices, GenericGuardrailAPIInputs

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj


class PIIDetector:
    """Finds PII in text using regular expressions."""

    PATTERNS = {
        # The lookbehind (rather than \b) lets a match start only at the beginning
        # of a run of address characters, which keeps the scan linear. With \b,
        # a long run such as "a.a.a..." is rescanned from every position.
        "email": re.compile(r"(?<![A-Za-z0-9_.%+-])[A-Za-z0-9_.%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"),
        # 123-45-6789, minus numbers that are never issued: area 000, 666 or 9xx,
        # group 00, serial 0000. Not part of a longer number.
        "us_ssn": re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])"),
    }

    def detect(self, text: str) -> list[str]:
        """Return the names of the PII types found in the text."""
        return [name for name, pattern in self.PATTERNS.items() if pattern.search(text)]

    def detect_all(self, texts: list[str]) -> list[str]:
        """
        Return the names of the PII types found in any of the texts.

        The texts are also checked joined together, so PII split across message
        parts or messages (e.g. "jane@" + "example.com") is still caught.
        """
        return sorted({name for text in [*texts, "".join(texts)] for name in self.detect(text)})


def pii_blocked_error(found: list[str], location: str) -> HTTPException:
    """The 400 that LiteLLM records as a guardrail intervention. Names the PII types, never the values."""
    return HTTPException(status_code=400, detail=f"PII detected in {location}: {', '.join(found)}")


class PIIGuardrail(CustomGuardrail):
    """Blocks the call if the prompt (pre_call) or the model response (post_call) contains PII."""

    detector = PIIDetector()

    # Makes LiteLLM hold a streamed response until it has been fully scanned,
    # so PII is never sent to the client before the block.
    streaming_buffer_until_moderated = True

    async def apply_guardrail(
        self,
        inputs: GenericGuardrailAPIInputs,
        request_data: dict,
        input_type: Literal["request", "response"],
        logging_obj: Optional["LiteLLMLoggingObj"] = None,
    ) -> GenericGuardrailAPIInputs:
        found = self.detector.detect_all(inputs.get("texts") or [])
        if found:
            raise pii_blocked_error(found, "prompt" if input_type == "request" else "model response")
        return inputs


class PIIReasoningGuardrail(CustomGuardrail):
    """
    Blocks non-streamed responses whose reasoning (``reasoning_content``) contains PII.

    LiteLLM passes only the message content to ``apply_guardrail``, so PIIGuardrail
    never sees a reasoning model's reasoning. This guardrail uses the post_call
    hook instead, which receives the whole response. Streamed reasoning is covered
    by setting ``merge_reasoning_content_in_choices`` on the model, which moves it
    into the content that PIIGuardrail scans.
    """

    detector = PIIDetector()

    @log_guardrail_information
    async def async_post_call_success_hook(self, data: dict, user_api_key_dict, response) -> None:
        reasoning = [
            choice.message.reasoning_content
            for choice in getattr(response, "choices", [])
            if isinstance(choice, Choices) and getattr(choice.message, "reasoning_content", None)
        ]
        found = self.detector.detect_all(reasoning)
        if found:
            raise pii_blocked_error(found, "model reasoning")
