"""
PII detection service. LiteLLM's guardrail sends it the text of each prompt or
model response, and it decides whether to block.

    POST /v1/analyze  {"texts": ["..."], "source": "request"}  ->  {"block": true, "entities": ["EMAIL_ADDRESS"]}
    GET  /healthz     liveness and readiness
    GET  /metrics     Prometheus metrics
"""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.detector import PIIDetector, Settings
from app.metrics import ServiceMetrics


class AnalyzeRequest(BaseModel):
    texts: list[str]
    # What the texts are, for metrics: the prompt, the model's response, or its reasoning.
    source: Literal["request", "response", "reasoning"] = "request"


class AnalyzeResponse(BaseModel):
    block: bool
    entities: list[str]  # PII types found, never the values


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
                decision = app.state.detector.decide(request.texts)
            except Exception:
                metrics.errors.labels(source=request.source).inc()
                raise
        metrics.record_decision(request.source, decision.block, decision.entities)
        return AnalyzeResponse(block=decision.block, entities=decision.entities)

    return app


app = create_app()
