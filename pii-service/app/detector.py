"""The PII decision: does any of the given text contain PII, according to Presidio?"""

import os
from dataclasses import dataclass, field

from presidio_analyzer import AnalyzerEngine


@dataclass(frozen=True)
class Settings:
    """Detection settings, read from environment variables."""

    # Presidio scores phone numbers at 0.4, so a higher default would miss them.
    # Very weak pattern matches (0.01-0.05, e.g. any 9-digit number) stay below it.
    score_threshold: float = 0.4
    ignored_entities: frozenset[str] = field(default_factory=frozenset)
    language: str = "en"

    @classmethod
    def from_env(cls) -> "Settings":
        ignored = os.environ.get("PII_IGNORED_ENTITIES", "")
        return cls(
            score_threshold=float(os.environ.get("PII_SCORE_THRESHOLD", cls.score_threshold)),
            ignored_entities=frozenset(name.strip().upper() for name in ignored.split(",") if name.strip()),
            language=os.environ.get("PII_LANGUAGE", cls.language),
        )


@dataclass(frozen=True)
class Decision:
    """Whether to block, and which PII types were found. Never holds the PII itself."""

    block: bool
    entities: list[str]


class PIIDetector:
    """
    Decides whether texts contain PII, using every recognizer Presidio has for
    the language, minus any ignored entity types.
    """

    def __init__(self, settings: Settings, analyzer: AnalyzerEngine | None = None) -> None:
        self.settings = settings
        self.analyzer = analyzer or AnalyzerEngine(supported_languages=[settings.language])
        self.entities = sorted(
            set(self.analyzer.get_supported_entities(settings.language)) - settings.ignored_entities
        )

    def decide(self, texts: list[str]) -> Decision:
        found = sorted({result.entity_type for text in self._texts_to_scan(texts) for result in self._analyze(text)})
        return Decision(block=bool(found), entities=found)

    def _analyze(self, text: str):
        return self.analyzer.analyze(
            text=text,
            language=self.settings.language,
            entities=self.entities,
            score_threshold=self.settings.score_threshold,
        )

    @staticmethod
    def _texts_to_scan(texts: list[str]) -> list[str]:
        # Also scan the texts joined, so PII split across message parts
        # ("jane@" + "example.com") is still caught.
        return texts if len(texts) < 2 else [*texts, "".join(texts)]
