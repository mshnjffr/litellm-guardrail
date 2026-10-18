"""
The PII decision, made with Presidio: allow the texts, mask the PII in them, or
block them.

Prompts are masked, because chat clients resend the whole conversation every
turn: a blocked message would block every later turn, while a masked one lets
the conversation carry on. Model responses and reasoning are blocked, because
LiteLLM can't reliably apply a rewrite to a streamed response.
"""

import os
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Literal

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_anonymizer import AnonymizerEngine

Source = Literal["request", "response", "reasoning"]
Action = Literal["allow", "mask", "block"]


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
    """
    What to do with the texts, and which PII types were found. `texts` holds the
    masked texts when the action is "mask", and is None otherwise, so a decision
    never carries the PII itself.
    """

    action: Action
    entities: list[str]
    texts: list[str] | None = None


class PIIDetector:
    """
    Decides what to do with texts that may contain PII, using every recognizer
    Presidio has for the language, minus any ignored entity types.
    """

    def __init__(self, settings: Settings, analyzer: AnalyzerEngine | None = None) -> None:
        self.settings = settings
        self.analyzer = analyzer or AnalyzerEngine(supported_languages=[settings.language])
        self.anonymizer = AnonymizerEngine()
        self.entities = sorted(
            set(self.analyzer.get_supported_entities(settings.language)) - settings.ignored_entities
        )

    def decide(self, texts: list[str], source: Source) -> Decision:
        if source == "request":
            return self._mask(texts)
        found = sorted({result.entity_type for text in texts for result in self._analyze(text)} | self._split_pii(texts))
        return Decision("block" if found else "allow", found)

    def _mask(self, texts: list[str]) -> Decision:
        """Mask the PII in each text, e.g. "jane@example.com" -> "<EMAIL_ADDRESS>"."""
        results = [self._analyze(text) for text in texts]
        entities = {result.entity_type for found in results for result in found}

        # PII split across message parts ("jane@" + "example.com") can't be
        # masked in place, so that prompt is blocked instead.
        if split := self._split_pii(texts):
            return Decision("block", sorted(entities | split))
        if not entities:
            return Decision("allow", [])
        masked = [self.anonymizer.anonymize(text, found).text if found else text for text, found in zip(texts, results)]
        return Decision("mask", sorted(entities), masked)

    def _split_pii(self, texts: list[str]) -> set[str]:
        """PII types found in the joined texts that cross from one text into the next."""
        if len(texts) < 2:
            return set()
        boundaries = list(accumulate(len(text) for text in texts))[:-1]
        return {
            result.entity_type
            for result in self._analyze("".join(texts))
            if any(result.start < boundary < result.end for boundary in boundaries)
        }

    def _analyze(self, text: str) -> list[RecognizerResult]:
        return self.analyzer.analyze(
            text=text,
            language=self.settings.language,
            entities=self.entities,
            score_threshold=self.settings.score_threshold,
        )
