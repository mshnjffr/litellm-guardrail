"""
LiteLLM custom guardrails for PII. The PII service (Presidio) makes the
decision; these guardrails send it the text over HTTP and enforce it: a prompt
containing PII is blocked, or masked before it reaches the model, and a model
response or reasoning containing PII is blocked.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

from fastapi import HTTPException

from litellm.integrations.custom_guardrail import CustomGuardrail, log_guardrail_information
from litellm.llms.custom_httpx.http_handler import get_async_httpx_client, httpxSpecialProvider
from litellm.types.utils import Choices, GenericGuardrailAPIInputs

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

Source = Literal["request", "response", "reasoning"]

LOCATIONS: dict[str, str] = {"request": "prompt", "response": "model response", "reasoning": "model reasoning"}


@dataclass(frozen=True)
class PIIDecision:
    """The PII service's decision: "allow", "mask" (with the masked texts) or "block"."""

    action: Literal["allow", "mask", "block"]
    entities: list[str]
    texts: list[str] | None = None


class PIIServiceClient:
    """Asks the PII service what to do with texts that may contain PII."""

    def __init__(self, api_base: str, timeout: float) -> None:
        self.url = f"{api_base.rstrip('/')}/v1/analyze"
        self.timeout = timeout

    async def decide(self, texts: list[str], source: Source) -> PIIDecision:
        """
        Raises if the service can't be reached or errors, so the call fails
        closed rather than letting unchecked text through.
        """
        client = get_async_httpx_client(llm_provider=httpxSpecialProvider.GuardrailCallback)
        response = await client.post(self.url, json={"texts": texts, "source": source}, timeout=self.timeout)
        response.raise_for_status()
        return PIIDecision(**response.json())


class PIIServiceGuardrail(CustomGuardrail):
    """
    Shared by both guardrails: the PII service client and enforcing its decision.

    `api_base` (the PII service URL) and `timeout` come from the guardrail's
    litellm_params in config.yaml.
    """

    def __init__(self, api_base: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pii_service = PIIServiceClient(api_base, timeout=self.timeout or 10)

    async def enforce(self, texts: list[str], source: Source) -> list[str]:
        """
        Return the texts to send on: masked if the service masked them, otherwise
        unchanged. If the service blocks them, raise the 400 that LiteLLM records
        as a guardrail intervention, naming the PII types but never the values.
        """
        if not texts:
            return texts
        decision = await self.pii_service.decide(texts, source)
        if decision.action == "block":
            raise HTTPException(status_code=400, detail=f"PII detected in {LOCATIONS[source]}: {', '.join(decision.entities)}")
        return decision.texts if decision.action == "mask" else texts


class PIIGuardrail(PIIServiceGuardrail):
    """
    Checks the prompt (pre_call) and the model response (post_call), and
    enforces the PII service's decision. A prompt containing PII is blocked, or
    masked if the service is set to mask prompts; a response containing PII is
    blocked.
    """

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
        if texts := inputs.get("texts"):
            inputs["texts"] = await self.enforce(texts, input_type)
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
        await self.enforce(reasoning, "reasoning")
