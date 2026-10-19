# LiteLLM PII guardrail

A [custom LiteLLM guardrail](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail)
for PII. PII in a prompt is masked before it reaches the model
(`jane@example.com` becomes `<EMAIL_ADDRESS>`), and a model response containing
PII, streamed or not, including reasoning output, is blocked. A separate Python
service built on [Microsoft Presidio](https://microsoft.github.io/presidio/)
decides what counts as PII and what to do, and the guardrail calls it over HTTP. Both run in Kubernetes, deployed
with Helm, and the service exposes Prometheus metrics. Everything runs locally:
the model is `deepseek-r1:8b` in Ollama.

How it's built and integrated, with architecture diagrams:
[docs/implementation.md](docs/implementation.md).

| Path | Purpose |
| --- | --- |
| `pii-service/` | The PII detection service (FastAPI + Presidio) and its Dockerfile |
| `guardrail/pii_guardrail.py` | The LiteLLM guardrails, which call the PII service |
| `charts/litellm-guardrail/` | Helm chart: the official LiteLLM chart, Postgres, the PII service and Prometheus |
| `scripts/deploy.sh` | Builds the service image and installs or upgrades the Helm release |
| `docs/implementation.md` | Implementation guide and architecture |

## Prerequisites

- **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** with at
  least 8 GB of memory (Settings → Resources), and its `kubectl`.
- **[Helm](https://helm.sh/docs/intro/install/)** 3.8 or later: `brew install helm` on macOS.
- **[Ollama](https://ollama.com/download)**.
- About 15 GB of free disk space (the model and container images).
- `git` and `curl`. On Windows, run the commands in Git Bash or WSL.

## First-time setup

**1. Download the model** (about 5 GB):

```sh
ollama pull deepseek-r1:8b
```

**2. Turn on Kubernetes** in Docker Desktop: **Settings → Kubernetes → Enable
Kubernetes → Apply & restart**. Either cluster type (kubeadm or kind) works.
When it's running, check that `kubectl` points at it:

```sh
kubectl config use-context docker-desktop
kubectl get nodes   # STATUS should be Ready
```

**3. Clone the repo:**

```sh
git clone https://github.com/mshnjffr/litellm-guardrail.git
cd litellm-guardrail
git checkout presidio-service
```

**4. Deploy:**

```sh
./scripts/deploy.sh
```

This builds the PII service image, loads it into the cluster, and installs the
Helm release into the `litellm` namespace. The first run downloads about 3 GB of
images and models and takes around 10 minutes. Check everything is running:

```sh
kubectl -n litellm get pods   # litellm, pii-service, postgresql and prometheus should be Running
```

**5. Open the gateway on localhost:4000.** Keep this running in its own terminal:

```sh
kubectl -n litellm port-forward deploy/litellm 4000:4000
```

This targets the LiteLLM deployment rather than its service: right after a
deploy, `svc/litellm` can pick the database migration job's pod and fail with
`does not have a named port 'http'`.

**6. Get the master key** (the admin API key, and the UI password for user `admin`):

```sh
export LITELLM_MASTER_KEY=$(kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d)
```

**7. Check the guardrails are loaded:**

```sh
curl -s localhost:4000/guardrails/list -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

The response should list `pii-prompt-guardrail`, `pii-response-guardrail` and
`pii-reasoning-guardrail`.

## Try it

The first request loads the model into memory, so it can take a minute.

A prompt without PII is answered (HTTP 200):

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "messages": [{"role": "user", "content": "Spell the word cat backwards. Reply with only the result."}]}'
```

PII in the prompt is masked before it reaches the model, so the model only sees
the placeholder. Ask it to repeat the prompt:

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "messages": [{"role": "user", "content": "Repeat exactly the text between the quotes, and nothing else: \"my email is jane.doe@example.com\""}]}'
# ... "content": "... my email is <EMAIL_ADDRESS>" ...
```

PII the model writes is blocked, and a streamed response sends only the error:

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "stream": true, "messages": [{"role": "user", "content": "Make up a realistic example email address for a fictional user. Reply with only the address."}]}'
# data: {"error": {"message": "PII detected in model response: EMAIL_ADDRESS, ...", ...}}
```

Prompts are masked for every Presidio entity type except `DATE_TIME` (a date or
time isn't PII on its own), including names, places and nationalities.
Responses are blocked for identifiers such as emails, phone numbers and card
numbers, but not for names, places or nationalities (`PERSON`, `LOCATION`,
`NRP`). Those appear in most ordinary answers ("Rayleigh scattering, explained by
Lord Rayleigh") and are almost always public knowledge. See
[Configuration](#configuration) to change this.

### The PII service and its metrics

In another terminal, open the service on localhost:8000:

```sh
kubectl -n litellm port-forward svc/pii-service 8000:8000
```

Ask it directly:

```sh
curl -s localhost:8000/v1/analyze -H 'Content-Type: application/json' \
  -d '{"texts": ["Call John Smith on +44 20 7946 0958"], "source": "request"}'
# {"action": "mask", "entities": ["PERSON", "PHONE_NUMBER"], "texts": ["Call <PERSON> on <PHONE_NUMBER>"]}
```

### Prometheus metrics

Two sets of metrics cover the guardrail, from each side of the HTTP call.

**LiteLLM's guardrail metrics** count every guardrail call as the gateway sees
it, at <http://localhost:4000/metrics/> (note the trailing slash; `/metrics`
redirects there):

```sh
curl -s localhost:4000/metrics/ | grep '^litellm_guardrail'
```

| Metric | What it counts |
| --- | --- |
| `litellm_guardrail_requests_total` | Calls to each guardrail, by `hook_type` (`pre_call`, `post_call`) and `status` |
| `litellm_guardrail_latency_seconds` | How long each guardrail takes, including the call to the PII service |
| `litellm_guardrail_errors_total` | Guardrail errors, by `error_type` |

LiteLLM counts a block as an error (`status="error"`, `error_type="HTTPException"`),
and masked prompts as `success`. **The PII service's metrics** fill in what
LiteLLM can't tell apart: each decision (`allow`, `mask` or `block`) and the PII
types found:

```sh
curl -s localhost:8000/metrics | grep pii_service_
```

### Prometheus

The chart also deploys Prometheus, which scrapes both sets of metrics every 15
seconds. Open its UI on <http://localhost:9090>, with this running in its own
terminal:

```sh
kubectl -n litellm port-forward svc/prometheus 9090:9090
```

**Status → Target health** should show `litellm` and `pii-service` as **UP**.
Then try these in the **Query** page (the Graph tab shows them over time):

| Query | Shows |
| --- | --- |
| `sum by (source, decision) (pii_service_requests_total)` | Prompts masked or allowed, and responses blocked or allowed |
| `sum by (entity_type) (pii_service_entities_detected_total)` | Which PII types turn up |
| `sum by (guardrail_name, hook_type, status) (litellm_guardrail_requests_total)` | Guardrail calls, as LiteLLM sees them |
| `histogram_quantile(0.95, sum by (le, guardrail_name) (rate(litellm_guardrail_latency_seconds_bucket[5m])))` | 95th-percentile guardrail latency |

The history is kept in the Prometheus pod and resets when it's replaced.

Blocked requests also appear in the LiteLLM UI at <http://localhost:4000/ui>
under **Logs**, with guardrail status `guardrail_intervened`. Masked prompts are
logged with status `success`, the same as clean ones.

## Configuration

The detection settings are in [`charts/litellm-guardrail/values.yaml`](charts/litellm-guardrail/values.yaml):

```yaml
piiService:
  scoreThreshold: 0.4    # Presidio confidence needed to count as PII (0-1)
  ignoredEntities: [DATE_TIME]                       # never masked or blocked
  responseIgnoredEntities: [PERSON, LOCATION, NRP]   # masked in prompts, but don't block responses
```

Edit them and run `./scripts/deploy.sh` again. The model and guardrail settings
are in the same file, under `litellm.proxy_config`.

## Run the tests

PII service tests, against the real Presidio model:

```sh
docker build --target test pii-service
```

Guardrail tests, inside the LiteLLM image:

```sh
docker run --rm -v "$PWD/guardrail:/guardrail:ro" --entrypoint python \
  docker.litellm.ai/berriai/litellm:v1.105.1 -m unittest discover -s /guardrail/tests -v
```

## Making changes

After changing the service, the guardrail or the chart, run
`./scripts/deploy.sh` again. It rebuilds the image, upgrades the release and
restarts the pods.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `kubectl get nodes` fails | Kubernetes isn't running yet (step 2), or the context is wrong: `kubectl config use-context docker-desktop`. |
| `pii-service` is in `ErrImagePull` or `ErrImageNeverPull` | The image isn't in the cluster. Run `./scripts/deploy.sh` again. |
| A pod keeps restarting | `kubectl -n litellm describe pod <name>` and `kubectl -n litellm logs <name>`. LiteLLM restarting once on first install, while Postgres starts, is normal. |
| Port 4000 already in use | Forward another port: `kubectl -n litellm port-forward deploy/litellm 4001:4000`. |
| Model requests fail with a connection error | Check Ollama is running (`ollama list`). |
| Requests fail with a 500 mentioning `pii-service` | The PII service is down, so the guardrail fails closed. Check `kubectl -n litellm get pods`. |

To remove everything, including the database:

```sh
helm -n litellm uninstall litellm-guardrail
kubectl delete namespace litellm
```

## Limitations

- A response that names a private person or their address isn't blocked,
  because `PERSON` and `LOCATION` don't block responses. Prompts are still masked.
- `DATE_TIME` is ignored, so a date of birth on its own isn't masked or blocked.
- The model sees placeholders such as `<EMAIL_ADDRESS>`, so it can't use the
  masked details.
- Detection uses Presidio's English model.
- Tool-call arguments aren't checked.

See [docs/implementation.md](docs/implementation.md#limitations).
