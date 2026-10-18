"""
PII detection service. LiteLLM's guardrail sends it the text of each prompt,
model response or model reasoning, and it decides whether to allow it, mask the
PII in it, or block it.

    POST /v1/analyze  {"texts": ["Email jane@example.com"], "source": "request"}
                      -> {"action": "mask", "entities": ["EMAIL_ADDRESS"], "texts": ["Email <EMAIL_ADDRESS>"]}
    GET  /healthz     liveness and readiness
    GET  /metrics     Prometheus metrics
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.detector import Action, PIIDetector, Settings, Source
from app.metrics import ServiceMetrics


class AnalyzeRequest(BaseModel):
    texts: list[str]
    # What the texts are: the prompt (masked), or the model's response or reasoning (blocked).
    source: Source = "request"


class AnalyzeResponse(BaseModel):
    action: Action
    entities: list[str]  # PII types found, never the values
    texts: list[str] | None = None  # the masked texts, when action is "mask"


def create_app(detector: PIIDetector | None = None) -> FastAPI:
    metrics = ServiceMetrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Loading the spaCy model takes a few seconds, so do it once at startup.
        app.state.detector = detector or PIIDetector(Settings.from_env())
        yield

    app = FastAPI(title="PII service", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    # A plain `def` runs in FastAPI's thread pool, so analysis doesn't block the event loop.
    @app.post("/v1/analyze")
    def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
        with metrics.duration.labels(source=request.source).time():
            try:
                decision = app.state.detector.decide(request.texts, request.source)
            except Exception:
                metrics.errors.labels(source=request.source).inc()
                raise
        metrics.record_decision(request.source, decision.action, decision.entities)
        return AnalyzeResponse(action=decision.action, entities=decision.entities, texts=decision.texts)

    return app


app = create_app()
