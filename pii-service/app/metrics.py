"""Prometheus metrics for the PII service, served at /metrics."""

from prometheus_client import CollectorRegistry, Counter, GCCollector, Histogram, PlatformCollector, ProcessCollector


class ServiceMetrics:
    """The service's metrics, in their own registry so tests can start from zero."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        for collector in (ProcessCollector, PlatformCollector, GCCollector):
            collector(registry=self.registry)

        self.requests = Counter(
            "pii_service_requests",
            "Analyze requests, by what was checked (request, response or reasoning) and the decision.",
            ["source", "decision"],
            registry=self.registry,
        )
        self.entities = Counter(
            "pii_service_entities_detected",
            "Analyze requests in which each PII entity type was detected.",
            ["source", "entity_type"],
            registry=self.registry,
        )
        self.errors = Counter(
            "pii_service_errors",
            "Analyze requests that failed.",
            ["source"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "pii_service_analysis_duration_seconds",
            "Time taken to analyze a request.",
            ["source"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )

    def record_decision(self, source: str, block: bool, entities: list[str]) -> None:
        self.requests.labels(source=source, decision="block" if block else "allow").inc()
        for entity in entities:
            self.entities.labels(source=source, entity_type=entity).inc()
