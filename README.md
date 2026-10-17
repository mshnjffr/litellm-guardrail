# LiteLLM PII guardrail

A [custom LiteLLM guardrail](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail)
that blocks PII in prompts, model responses (including streamed responses) and
model reasoning. A separate Python service built on
[Microsoft Presidio](https://microsoft.github.io/presidio/) decides what counts
as PII, and the guardrail calls it over HTTP. Both run in Kubernetes, deployed
with Helm, and the service exposes Prometheus metrics. Everything runs locally:
the model is `deepseek-r1:8b` in Ollama.

How it's built and integrated, with architecture diagrams:
[docs/implementation.md](docs/implementation.md).

| Path | Purpose |
| --- | --- |
| `pii-service/` | The PII detection service (FastAPI + Presidio) and its Dockerfile |
| `guardrail/pii_guardrail.py` | The LiteLLM guardrails, which call the PII service |
| `charts/litellm-guardrail/` | Helm chart: the official LiteLLM chart, Postgres and the PII service |
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
kubectl -n litellm get pods   # litellm, pii-service and postgresql should be Running
```

**5. Open the gateway on localhost:4000.** Keep this running in its own terminal:

```sh
kubectl -n litellm port-forward svc/litellm 4000:4000
```

**6. Get the master key** (the admin API key, and the UI password for user `admin`):

```sh
export LITELLM_MASTER_KEY=$(kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d)
```

**7. Check both guardrails are loaded:**

```sh
curl -s localhost:4000/guardrails/list -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

The response should list `pii-guardrail` and `pii-reasoning-guardrail`.

## Try it

The first request loads the model into memory, so it can take a minute.

A prompt without PII is answered (HTTP 200):

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "messages": [{"role": "user", "content": "Spell the word cat backwards. Reply with only the result."}]}'
```

Every Presidio entity type blocks except `DATE_TIME` (a date or time isn't PII
on its own). That includes names, places and nationalities, and these often
appear in the model's reasoning. For example,
"Write a haiku about the sea." is often blocked with `PII detected in model
reasoning: NRP`, because the model reasons about haiku being Japanese. See
[Configuration](#configuration) to narrow it.

PII in the prompt is blocked before it reaches the model (HTTP 400):

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "messages": [{"role": "user", "content": "My social security number is 219-09-9999"}]}'
# {"error": {"message": "PII detected in prompt: US_SSN", ...}}
```

PII the model writes is blocked too, and a streamed response sends only the error:

```sh
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1:8b", "stream": true, "messages": [{"role": "user", "content": "Make up a realistic example email address for a fictional user. Reply with only the address."}]}'
# data: {"error": {"message": "PII detected in model response: EMAIL_ADDRESS, ...", ...}}
```

### The PII service and its metrics

In another terminal, open the service on localhost:8000:

```sh
kubectl -n litellm port-forward svc/pii-service 8000:8000
```

Ask it directly:

```sh
curl -s localhost:8000/v1/analyze -H 'Content-Type: application/json' \
  -d '{"texts": ["Call John Smith on +44 20 7946 0958"], "source": "request"}'
# {"block": true, "entities": ["PERSON", "PHONE_NUMBER"]}
```

See its Prometheus metrics, which count decisions and PII types for the
requests the gateway has sent:

```sh
curl -s localhost:8000/metrics | grep pii_service_
```

Blocked requests also appear in the LiteLLM UI at <http://localhost:4000/ui>
under **Logs**, with guardrail status `guardrail_intervened`.

## Configuration

The detection settings are in [`charts/litellm-guardrail/values.yaml`](charts/litellm-guardrail/values.yaml):

```yaml
piiService:
  scoreThreshold: 0.4    # Presidio confidence needed to count as PII (0-1)
  ignoredEntities: [DATE_TIME]   # entity types that never block, e.g. add LOCATION, NRP
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
| Port 4000 already in use | Forward another port: `kubectl -n litellm port-forward svc/litellm 4001:4000`. |
| Model requests fail with a connection error | Check Ollama is running (`ollama list`). |
| Requests fail with a 500 mentioning `pii-service` | The PII service is down, so the guardrail fails closed. Check `kubectl -n litellm get pods`. |

To remove everything, including the database:

```sh
helm -n litellm uninstall litellm-guardrail
kubectl delete namespace litellm
```

## Limitations

- Ordinary requests are sometimes blocked because answers and reasoning
  contain names, places or nationalities.
- `DATE_TIME` is ignored, so a date of birth on its own isn't blocked.
- Detection uses Presidio's English model.
- Tool-call arguments aren't checked, and PII is blocked, never masked.

See [docs/implementation.md](docs/implementation.md#limitations).
