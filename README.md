# LiteLLM PII guardrail

A [custom LiteLLM guardrail](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail)
for email addresses and US Social Security numbers. PII in a prompt is masked
before it reaches the model (`jane@example.com` becomes `<EMAIL>`), and a model
response containing PII, streamed or not, including reasoning output, is blocked. It runs
entirely on your machine: LiteLLM and Postgres in Docker, and the
`deepseek-r1:8b` model in Ollama.

How it's built and integrated, with architecture diagrams:
[docs/implementation.md](docs/implementation.md).

| File | Purpose |
| --- | --- |
| `pii_guardrail.py` | The PII detector and the two guardrails |
| `config.yaml` | The model and the guardrail registrations |
| `docker-compose.yml` | LiteLLM (pinned to `v1.105.1`) and Postgres |
| `tests/test_pii_guardrail.py` | Unit tests |
| `docs/implementation.md` | Implementation guide and architecture |

## Prerequisites

- **Docker** with Compose v2: Docker Desktop on macOS or Windows, or Docker
  Engine on Linux.
- **[Ollama](https://ollama.com/download)**.
- About 8 GB of free disk space (model and images) and 16 GB of RAM.
- `git`, `curl` and `openssl`.

## First-time setup

**1. Start Ollama and download the model** (about 5 GB):

```sh
ollama pull deepseek-r1:8b
```

On Linux, Ollama listens only on `127.0.0.1` by default, which Docker
containers can't reach. Set `OLLAMA_HOST=0.0.0.0` for the Ollama service and
restart it (see the [Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.md#how-do-i-configure-ollama-server)).
macOS and Windows need no changes.

**2. Clone the repo:**

```sh
git clone https://github.com/mshnjffr/litellm-guardrail.git
cd litellm-guardrail
```

**3. Create `.env` with random keys:**

```sh
printf 'LITELLM_MASTER_KEY=sk-%s\nLITELLM_SALT_KEY=sk-%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env
```

`LITELLM_MASTER_KEY` is the admin API key and the UI password.
`LITELLM_SALT_KEY` encrypts credentials in the database; don't change it after
the first start. To use a port other than 4000, add `LITELLM_PORT=4001`.

**4. Start the gateway:**

```sh
docker compose up -d
```

The first start downloads the images and sets up the database, which takes a
few minutes. Wait until it's ready:

```sh
until curl -fs localhost:4000/health/readiness >/dev/null; do sleep 5; done; echo ready
```

**5. Check both guardrails are loaded:**

```sh
set -a; . ./.env; set +a   # loads LITELLM_MASTER_KEY into this shell
curl -s localhost:4000/guardrails/list -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

The response should list `pii-guardrail` and `pii-reasoning-guardrail`.

## Try it

The first request loads the model into memory, so it can take a minute.

A clean prompt is answered (HTTP 200):

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "messages": [{"role": "user", "content": "What is the capital of France? One word."}]}'
```

PII in the prompt is masked before it reaches the model, so the model only sees
`<EMAIL>`. Ask it to repeat the prompt:

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "messages": [{"role": "user", "content": "Repeat exactly the text between the quotes, and nothing else: \"my email is jane.doe@example.com\""}]}'
# ... "content": "... my email is <EMAIL>" ...
```

PII in the model's response is blocked too. Here the model writes the SSN,
and the client gets only the error, streamed or not:

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "stream": true, "messages": [{"role": "user", "content": "Format the digits 123456789 as XXX-XX-XXXX. Reply with only the result."}]}'
# data: {"error": {"message": "PII detected in model response: us_ssn", ...}}
```

To test the masking without calling the model:

```sh
curl -s localhost:4000/guardrails/apply_guardrail -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' -d '{"guardrail_name": "pii-guardrail", "text": "my SSN is 123-45-6789"}'
# {"response_text": "my SSN is <US_SSN>"}
```

Every response carries an `x-litellm-applied-guardrails` header. Blocked
requests appear in the UI at <http://localhost:4000/ui> (user `admin`, password
`LITELLM_MASTER_KEY`) under **Logs**, with guardrail status `guardrail_intervened`.
Masked prompts are logged with status `success`, the same as clean ones.

## Run the tests

```sh
docker compose exec litellm python -m unittest discover -s /app/custom/tests -v
```

## Making changes

The repo is mounted into the container, so after editing `pii_guardrail.py` or
`config.yaml`, restart LiteLLM:

```sh
docker compose restart litellm
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `docker compose up` says `set it in .env` | Run step 3 in the repo directory. |
| Port 4000 already in use | Add `LITELLM_PORT=4001` to `.env`, run `docker compose up -d`, and use that port. |
| Model requests fail with a connection error | Check Ollama is running (`ollama list`). On Linux, check `OLLAMA_HOST` (step 1). |
| Anything else | `docker compose logs litellm` |

To stop the gateway: `docker compose down`. To also delete the database:
`docker compose down -v`.

## Limitations

Regex detection catches PII in standard formats, which suits accidental
disclosure. It won't stop someone deliberately evading it (`jane at example dot
com`). Tool-call arguments aren't scanned. See
[docs/implementation.md](docs/implementation.md#limitations).
