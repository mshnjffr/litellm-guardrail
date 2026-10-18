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
    # Entity types that are never masked or blocked.
    ignored_entities: frozenset[str] = field(default_factory=frozenset)
    # Entity types that are masked in prompts but don't block responses, such as
    # PERSON: answers often name public figures ("Lord Rayleigh").
    response_ignored_entities: frozenset[str] = field(default_factory=frozenset)
    language: str = "en"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            score_threshold=float(os.environ.get("PII_SCORE_THRESHOLD", cls.score_threshold)),
            ignored_entities=_entity_list(os.environ.get("PII_IGNORED_ENTITIES", "")),
            response_ignored_entities=_entity_list(os.environ.get("PII_RESPONSE_IGNORED_ENTITIES", "")),
            language=os.environ.get("PII_LANGUAGE", cls.language),
        )


def _entity_list(value: str) -> frozenset[str]:
    """Parse a comma-separated list of entity types, e.g. "person, location"."""
    return frozenset(name.strip().upper() for name in value.split(",") if name.strip())


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
    Presidio has for the language, minus any ignored entity types. Responses are
    checked for fewer types than prompts (see `response_ignored_entities`).
    """

    def __init__(self, settings: Settings, analyzer: AnalyzerEngine | None = None) -> None:
        self.settings = settings
        self.analyzer = analyzer or AnalyzerEngine(supported_languages=[settings.language])
        self.anonymizer = AnonymizerEngine()
        self.entities = sorted(
            set(self.analyzer.get_supported_entities(settings.language)) - settings.ignored_entities
        )
        self.response_entities = sorted(set(self.entities) - settings.response_ignored_entities)

    def decide(self, texts: list[str], source: Source) -> Decision:
        if source == "request":
            return self._mask(texts)
        entities = self.response_entities
        found = {result.entity_type for text in texts for result in self._analyze(text, entities)}
        found |= self._split_pii(texts, entities)
        return Decision("block" if found else "allow", sorted(found))

    def _mask(self, texts: list[str]) -> Decision:
        """Mask the PII in each text, e.g. "jane@example.com" -> "<EMAIL_ADDRESS>"."""
        results = [self._analyze(text, self.entities) for text in texts]
        entities = {result.entity_type for found in results for result in found}

        # PII split across message parts ("jane@" + "example.com") can't be
        # masked in place, so that prompt is blocked instead.
        if split := self._split_pii(texts, self.entities):
            return Decision("block", sorted(entities | split))
        if not entities:
            return Decision("allow", [])
        masked = [self.anonymizer.anonymize(text, found).text if found else text for text, found in zip(texts, results)]
        return Decision("mask", sorted(entities), masked)

    def _split_pii(self, texts: list[str], entities: list[str]) -> set[str]:
        """PII types found in the joined texts that cross from one text into the next."""
        if len(texts) < 2:
            return set()
        boundaries = list(accumulate(len(text) for text in texts))[:-1]
        return {
            result.entity_type
            for result in self._analyze("".join(texts), entities)
            if any(result.start < boundary < result.end for boundary in boundaries)
        }

    def _analyze(self, text: str, entities: list[str]) -> list[RecognizerResult]:
        return self.analyzer.analyze(
            text=text,
            language=self.settings.language,
            entities=entities,
            score_threshold=self.settings.score_threshold,
        )
