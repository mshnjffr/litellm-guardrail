"""
LiteLLM custom guardrails that block prompts, model responses and model
reasoning containing PII. The PII service (Presidio) makes the decision; these
guardrails send it the text over HTTP and block when it says so.
"""

from typing import TYPE_CHECKING, Literal, Optional

from fastapi import HTTPException

from litellm.integrations.custom_guardrail import CustomGuardrail, log_guardrail_information
from litellm.llms.custom_httpx.http_handler import get_async_httpx_client, httpxSpecialProvider
from litellm.types.utils import Choices, GenericGuardrailAPIInputs

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

Source = Literal["request", "response", "reasoning"]

LOCATIONS: dict[str, str] = {"request": "prompt", "response": "model response", "reasoning": "model reasoning"}


class PIIServiceClient:
    """Asks the PII service whether texts contain PII."""

    def __init__(self, api_base: str, timeout: float) -> None:
        self.url = f"{api_base.rstrip('/')}/v1/analyze"
        self.timeout = timeout

    async def find_pii(self, texts: list[str], source: Source) -> list[str]:
        """
        Return the PII types the service found, or an empty list if the texts
        are clean. Raises if the service can't be reached or errors, so the
        call fails closed rather than letting unchecked text through.
        """
        client = get_async_httpx_client(llm_provider=httpxSpecialProvider.GuardrailCallback)
        response = await client.post(self.url, json={"texts": texts, "source": source}, timeout=self.timeout)
        response.raise_for_status()
        result = response.json()
        return result["entities"] if result["block"] else []


class PIIServiceGuardrail(CustomGuardrail):
    """
    Shared by both guardrails: the PII service client and the block.

    `api_base` (the PII service URL) and `timeout` come from the guardrail's
    litellm_params in config.yaml.
    """

    def __init__(self, api_base: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pii_service = PIIServiceClient(api_base, timeout=self.timeout or 10)

    async def block_if_pii(self, texts: list[str], source: Source) -> None:
        """Raise the 400 that LiteLLM records as a guardrail intervention. Names the PII types, never the values."""
        if not texts:
            return
        found = await self.pii_service.find_pii(texts, source)
        if found:
            raise HTTPException(status_code=400, detail=f"PII detected in {LOCATIONS[source]}: {', '.join(found)}")


class PIIGuardrail(PIIServiceGuardrail):
    """Blocks the call if the prompt (pre_call) or the model response (post_call) contains PII."""

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
        await self.block_if_pii(inputs.get("texts") or [], input_type)
        return inputs


class PIIReasoningGuardrail(PIIServiceGuardrail):
    """
    Blocks non-streamed responses whose reasoning (``reasoning_content``) contains PII.

    LiteLLM passes only the message content to ``apply_guardrail``, so PIIGuardrail
    never sees a reasoning model's reasoning. This guardrail uses the post_call
    hook instead, which receives the whole response. Streamed reasoning is covered
    by setting ``merge_reasoning_content_in_choices`` on the model, which moves it
    into the content that PIIGuardrail scans.
    """

    @log_guardrail_information
    async def async_post_call_success_hook(self, data: dict, user_api_key_dict, response) -> None:
        reasoning = [
            choice.message.reasoning_content
            for choice in getattr(response, "choices", [])
            if isinstance(choice, Choices) and getattr(choice.message, "reasoning_content", None)
        ]
        await self.block_if_pii(reasoning, "reasoning")
