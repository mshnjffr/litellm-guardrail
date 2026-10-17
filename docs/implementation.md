# Implementing the custom PII guardrail

This guide explains how the guardrail is built and how it plugs into LiteLLM,
following LiteLLM's [custom guardrail docs](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail).
For setup, see the [README](../README.md).

## Architecture

Everything runs locally: the LiteLLM proxy and Postgres in Docker, and the
model in Ollama on the host machine.

```mermaid
flowchart LR
    client["Client<br/>curl, OpenAI SDK, LiteLLM UI"]

    subgraph machine["Local machine"]
        subgraph compose["Docker Compose"]
            subgraph proxy["LiteLLM proxy, localhost:4000<br/>loads config.yaml + pii_guardrail.py from this repo"]
                direction LR
                pre["pii-guardrail<br/>pre_call: scans the prompt"]
                router["Router<br/>deepseek-r1:8b"]
                post["pii-guardrail + pii-reasoning-guardrail<br/>post_call: scan the response and reasoning"]
                pre -- clean --> router
                router --> post
            end
            db[("Postgres<br/>keys, spend and guardrail logs")]
        end
        ollama["Ollama, localhost:11434<br/>deepseek-r1:8b"]
    end

    client -- "POST /v1/chat/completions" --> pre
    router <-- "host.docker.internal:11434" --> ollama
    post -- "200, or 400 if PII" --> client
    pre -. "400 if PII" .-> client
    proxy -. "guardrail results" .-> db
```

### Request flow

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant L as LiteLLM proxy
    participant G as PIIGuardrail
    participant R as PIIReasoningGuardrail
    participant O as Ollama

    C->>L: POST /v1/chat/completions
    L->>G: apply_guardrail(texts, input_type="request")
    alt PII in the prompt
        G-->>L: HTTPException(400)
        L-->>C: 400 "PII detected in prompt: email"
    else clean prompt
        L->>O: chat request
        O-->>L: content + reasoning_content
        L->>G: apply_guardrail(texts, input_type="response")
        L->>R: async_post_call_success_hook(response)
        alt PII in the response or reasoning
            L-->>C: 400 "PII detected in model response: us_ssn"
        else clean response
            L-->>C: 200 with the model's answer
        end
    end
```

For streamed responses, LiteLLM holds the chunks until the whole response has
been scanned (`streaming_buffer_until_moderated`), so a blocked response sends
the client only the error, never part of the PII.

## How it's built

All the code is in [`pii_guardrail.py`](../pii_guardrail.py).

### 1. A detector with one regex per PII type

`PIIDetector` knows nothing about LiteLLM, so it can be tested on its own.

```python
class PIIDetector:
    PATTERNS = {
        "email": re.compile(r"(?<![A-Za-z0-9_.%+-])[A-Za-z0-9_.%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"),
        "us_ssn": re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])"),
    }

    def detect(self, text: str) -> list[str]: ...
    def detect_all(self, texts: list[str]) -> list[str]: ...
```

- **SSN:** the regex rejects numbers that are never issued (area `000`, `666`
  or `9xx`, group `00`, serial `0000`) and numbers that are part of a longer
  number.
- **Email:** the match starts with a lookbehind rather than `\b`. With `\b`, a
  long run like `a.a.a...` is rescanned from every position, and a 128 KB
  prompt froze the gateway for about 10 seconds. With the lookbehind, it takes
  milliseconds.
- **`detect_all`:** checks each text and also all of them joined, so PII split
  across message parts (`"jane@"` + `"example.com"`) is still caught.

### 2. A guardrail class that implements `apply_guardrail`

The docs recommend implementing this single method. LiteLLM extracts the text
from the request or response, for every endpoint and provider, and passes it
in `inputs["texts"]`.

```python
class PIIGuardrail(CustomGuardrail):
    detector = PIIDetector()
    streaming_buffer_until_moderated = True

    async def apply_guardrail(self, inputs, request_data, input_type, logging_obj=None):
        found = self.detector.detect_all(inputs.get("texts") or [])
        if found:
            raise pii_blocked_error(found, "prompt" if input_type == "request" else "model response")
        return inputs
```

Returning `inputs` allows the call. Raising blocks it.

### 3. A second class for reasoning output

Reasoning models such as DeepSeek-R1 return their reasoning in a separate
`reasoning_content` field. LiteLLM doesn't include it in `inputs["texts"]`, and
when a guardrail implements `apply_guardrail`, LiteLLM never calls that
guardrail's other hooks. So reasoning needs its own guardrail, using the
`post_call` hook that receives the whole response:

```python
class PIIReasoningGuardrail(CustomGuardrail):
    @log_guardrail_information
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        ...  # scan choice.message.reasoning_content
```

This hook only runs on non-streamed responses. For streamed ones, the model
setting `merge_reasoning_content_in_choices: true` moves the reasoning into the
content, where `PIIGuardrail` scans it.

### 4. Register the guardrails in `config.yaml`

```yaml
guardrails:
  - guardrail_name: pii-guardrail
    litellm_params:
      guardrail: pii_guardrail.PIIGuardrail   # <file>.<Class>
      mode: [pre_call, post_call]
      default_on: true
  - guardrail_name: pii-reasoning-guardrail
    litellm_params:
      guardrail: pii_guardrail.PIIReasoningGuardrail
      mode: post_call
      default_on: true
```

LiteLLM loads the `.py` file from the directory `config.yaml` is in.
`default_on: true` runs the guardrails on every request, so clients can't skip
them.

### 5. Mount the code and start the proxy

[`docker-compose.yml`](../docker-compose.yml) mounts the repo at `/app/custom`
and starts LiteLLM with `--config /app/custom/config.yaml`. The whole directory
is mounted rather than single files: on Docker Desktop, editing a single-file
mount can make the file vanish from the running container.

## Design decisions

| Decision | Why |
| --- | --- |
| Raise `HTTPException(400)`, not `Exception` | The docs' plain `Exception` returns a 500 and is logged as `guardrail_failed_to_respond`, as if the guardrail had broken. A 400 is logged as `guardrail_intervened`: a policy block. |
| Name the PII types, never the values | Blocked responses and logs don't repeat the PII. |
| `streaming_buffer_until_moderated = True` | Without it, `post_call` on a streamed response only runs after the content has reached the client. |
| Pin `litellm:v1.105.1` | The guardrails depend on behaviour of this release: how the file is loaded, the streaming setting, and which hooks run. |

## Limitations

- Regex catches PII in standard formats and accidental disclosure. It won't
  stop someone deliberately avoiding it (`jane at example dot com`,
  `123 45 6789`).
- Only message text and reasoning are scanned, not tool-call arguments.
- It blocks and never masks.
