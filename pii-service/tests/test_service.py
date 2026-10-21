"""
Tests for the PII service, against the real Presidio engine and spaCy model.
Run by building the test stage of the image:

    docker build --target test pii-service
"""

import unittest
import unittest.mock

from fastapi.testclient import TestClient
from presidio_analyzer import AnalyzerEngine

from app.detector import PIIDetector, Settings
from app.main import create_app

analyzer: AnalyzerEngine

# A prompt with PII, the entity type found, and the prompt once masked.
PII_PROMPTS = (
    ("Send the report to jane.doe@example.com", "EMAIL_ADDRESS", "Send the report to <EMAIL_ADDRESS>"),
    ("My social security number is 219-09-9999", "US_SSN", "My social security number is <US_SSN>"),
    ("My card is 4111 1111 1111 1111", "CREDIT_CARD", "My card is <CREDIT_CARD>"),
    ("Call me on +44 20 7946 0958", "PHONE_NUMBER", "Call me on <PHONE_NUMBER>"),
    ("my ip is 192.168.1.20", "IP_ADDRESS", "my ip is <IP_ADDRESS>"),
    ("John Smith wrote this", "PERSON", "<PERSON> wrote this"),
)


def setUpModule():
    global analyzer
    analyzer = AnalyzerEngine(supported_languages=["en"])  # loading spaCy takes a few seconds, so share it


class ServiceTestCase(unittest.TestCase):
    """Runs the service with `settings`, sharing the module's Presidio engine."""

    settings = Settings()

    def setUp(self):
        # Entering the client runs the app's startup.
        self.client = self.enterContext(TestClient(create_app(PIIDetector(self.settings, analyzer))))

    def analyze(self, *texts, source="request"):
        response = self.client.post("/v1/analyze", json={"texts": list(texts), "source": source})
        self.assertEqual(response.status_code, 200)
        return response.json()


class AnalyzeEndpointTest(ServiceTestCase):
    """The default settings: prompts, responses and reasoning containing PII are blocked."""

    def test_allows_text_without_pii(self):
        for source in ("request", "response", "reasoning"):
            with self.subTest(source=source):
                self.assertEqual(
                    self.analyze("What is 2 + 2?", "Write a haiku about the sea.", source=source),
                    {"action": "allow", "entities": [], "texts": None},
                )

    def test_blocks_pii_in_prompts(self):
        for text, entity, _ in PII_PROMPTS:
            with self.subTest(entity=entity):
                result = self.analyze(text)
                self.assertEqual(result["action"], "block")
                self.assertIn(entity, result["entities"])
                self.assertIsNone(result["texts"])

    def test_blocks_pii_split_across_prompt_parts(self):
        for texts in (["Reply OK. My email is jane@", "example.com"], ["SSN 219-09-", "9999 please"]):
            with self.subTest(texts=texts):
                result = self.analyze(*texts)
                self.assertEqual(result["action"], "block")
                self.assertIsNone(result["texts"])

    def test_blocks_pii_in_responses_and_reasoning(self):
        for source in ("response", "reasoning"):
            with self.subTest(source=source):
                result = self.analyze("Sure: jane.doe@example.com", source=source)
                self.assertEqual(result["action"], "block")
                self.assertIn("EMAIL_ADDRESS", result["entities"])
                self.assertIsNone(result["texts"])

    def test_never_returns_the_pii(self):
        for source in ("request", "response"):
            with self.subTest(source=source):
                response = self.client.post("/v1/analyze", json={"texts": ["Email jane.doe@example.com"], "source": source})
                self.assertNotIn("jane.doe", response.text)

    def test_rejects_unknown_source(self):
        response = self.client.post("/v1/analyze", json={"texts": ["hi"], "source": "other"})
        self.assertEqual(response.status_code, 422)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_metrics_count_decisions_and_entities(self):
        self.analyze("Send the report to jane.doe@example.com")
        self.analyze("Sure: jane.doe@example.com", source="response")
        self.analyze("What is 2 + 2?", source="reasoning")
        metrics = self.client.get("/metrics").text
        self.assertIn('pii_service_requests_total{decision="block",source="request"} 1.0', metrics)
        self.assertIn('pii_service_requests_total{decision="block",source="response"} 1.0', metrics)
        self.assertIn('pii_service_requests_total{decision="allow",source="reasoning"} 1.0', metrics)
        self.assertIn('pii_service_entities_detected_total{entity_type="EMAIL_ADDRESS",source="request"} 1.0', metrics)
        self.assertIn('pii_service_analysis_duration_seconds_count{source="request"} 1.0', metrics)


class PromptMaskingTest(ServiceTestCase):
    """With prompt_action "mask", PII in prompts is masked instead of blocked."""

    settings = Settings(prompt_action="mask")

    def test_masks_pii_in_prompts(self):
        for text, entity, masked in PII_PROMPTS:
            with self.subTest(entity=entity):
                result = self.analyze(text)
                self.assertEqual(result["action"], "mask")
                self.assertIn(entity, result["entities"])
                self.assertEqual(result["texts"], [masked])

    def test_masks_each_text_of_a_conversation(self):
        result = self.analyze("how are you? testuser@gmail.com", "Error fetching response", "hi")
        self.assertEqual(result["texts"], ["how are you? <EMAIL_ADDRESS>", "Error fetching response", "hi"])

    def test_still_blocks_pii_split_across_prompt_parts(self):
        result = self.analyze("Reply OK. My email is jane@", "example.com")
        self.assertEqual(result["action"], "block")
        self.assertIsNone(result["texts"])

    def test_still_blocks_pii_in_responses(self):
        self.assertEqual(self.analyze("Sure: jane.doe@example.com", source="response")["action"], "block")

    def test_metrics_count_masked_prompts(self):
        self.analyze("Send the report to jane.doe@example.com")
        self.assertIn('pii_service_requests_total{decision="mask",source="request"} 1.0', self.client.get("/metrics").text)


class SettingsTest(unittest.TestCase):
    def test_ignored_entities_are_not_detected(self):
        detector = PIIDetector(Settings(ignored_entities=frozenset({"LOCATION"})), analyzer)
        self.assertNotIn("LOCATION", detector.entities)
        self.assertEqual(detector.decide(["What is the capital of France?"], "request").action, "allow")

    def test_score_threshold_drops_weak_matches(self):
        text = ["Call me on +44 20 7946 0958"]  # Presidio scores this phone number 0.4
        self.assertEqual(PIIDetector(Settings(), analyzer).decide(text, "response").action, "block")
        self.assertEqual(PIIDetector(Settings(score_threshold=0.9), analyzer).decide(text, "response").action, "allow")

    def test_rejects_unknown_prompt_action(self):
        with self.assertRaises(ValueError):
            Settings(prompt_action="allow")

    def test_reads_settings_from_environment(self):
        env = {
            "PII_PROMPT_ACTION": " Mask ",
            "PII_SCORE_THRESHOLD": "0.7",
            "PII_IGNORED_ENTITIES": "location, date_time",
            "PII_RESPONSE_IGNORED_ENTITIES": "person,nrp",
            "PII_LANGUAGE": "en",
        }
        with unittest.mock.patch.dict("os.environ", env):
            settings = Settings.from_env()
        self.assertEqual(
            settings,
            Settings(
                prompt_action="mask",
                score_threshold=0.7,
                ignored_entities=frozenset({"LOCATION", "DATE_TIME"}),
                response_ignored_entities=frozenset({"PERSON", "NRP"}),
                language="en",
            ),
        )


class ResponseIgnoredEntitiesTest(unittest.TestCase):
    """Names and places count in prompts, but don't block responses."""

    ignored = frozenset({"PERSON", "LOCATION", "NRP"})

    def setUp(self):
        self.detector = PIIDetector(Settings(response_ignored_entities=self.ignored), analyzer)

    def test_allows_public_names_and_places_in_responses(self):
        answer = ["The sky is blue because of Rayleigh scattering in the Earth's atmosphere, explained by Lord Rayleigh."]
        for source in ("response", "reasoning"):
            with self.subTest(source=source):
                self.assertEqual(self.detector.decide(answer, source).action, "allow")

    def test_still_blocks_identifiers_in_responses(self):
        decision = self.detector.decide(["Lord Rayleigh's email was rayleigh@example.com"], "response")
        self.assertEqual(decision.action, "block")
        self.assertIn("EMAIL_ADDRESS", decision.entities)
        self.assertNotIn("PERSON", decision.entities)

    def test_still_checks_names_and_places_in_prompts(self):
        prompt = ["John Smith lives in London"]
        decision = self.detector.decide(prompt, "request")
        self.assertEqual((decision.action, decision.entities), ("block", ["LOCATION", "PERSON"]))
        masking = PIIDetector(Settings(prompt_action="mask", response_ignored_entities=self.ignored), analyzer)
        self.assertEqual(masking.decide(prompt, "request").texts, ["<PERSON> lives in <LOCATION>"])


if __name__ == "__main__":
    unittest.main()
