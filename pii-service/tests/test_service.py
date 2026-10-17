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


def setUpModule():
    global analyzer
    analyzer = AnalyzerEngine(supported_languages=["en"])  # loading spaCy takes a few seconds, so share it


class AnalyzeEndpointTest(unittest.TestCase):
    def setUp(self):
        # Entering the client runs the app's startup.
        self.client = self.enterContext(TestClient(create_app(PIIDetector(Settings(), analyzer))))

    def analyze(self, *texts, source="request"):
        response = self.client.post("/v1/analyze", json={"texts": list(texts), "source": source})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_allows_text_without_pii(self):
        self.assertEqual(self.analyze("What is 2 + 2?", "Write a haiku about the sea."), {"block": False, "entities": []})

    def test_blocks_pii(self):
        for text, entity in (
            ("Send the report to jane.doe@example.com", "EMAIL_ADDRESS"),
            ("My social security number is 219-09-9999", "US_SSN"),
            ("My card is 4111 1111 1111 1111", "CREDIT_CARD"),
            ("Call me on +44 20 7946 0958", "PHONE_NUMBER"),
            ("my ip is 192.168.1.20", "IP_ADDRESS"),
            ("John Smith wrote this", "PERSON"),
        ):
            with self.subTest(entity=entity):
                result = self.analyze(text)
                self.assertTrue(result["block"])
                self.assertIn(entity, result["entities"])

    def test_response_never_contains_the_pii(self):
        response = self.client.post("/v1/analyze", json={"texts": ["Email jane.doe@example.com"]})
        self.assertNotIn("jane.doe", response.text)

    def test_detects_pii_split_across_texts(self):
        self.assertIn("EMAIL_ADDRESS", self.analyze("Reply OK. My email is jane@", "example.com")["entities"])

    def test_rejects_unknown_source(self):
        response = self.client.post("/v1/analyze", json={"texts": ["hi"], "source": "other"})
        self.assertEqual(response.status_code, 422)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_metrics_count_decisions_and_entities(self):
        self.analyze("Send the report to jane.doe@example.com")
        self.analyze("What is 2 + 2?", source="response")
        metrics = self.client.get("/metrics").text
        self.assertIn('pii_service_requests_total{decision="block",source="request"} 1.0', metrics)
        self.assertIn('pii_service_requests_total{decision="allow",source="response"} 1.0', metrics)
        self.assertIn('pii_service_entities_detected_total{entity_type="EMAIL_ADDRESS",source="request"} 1.0', metrics)
        self.assertIn('pii_service_analysis_duration_seconds_count{source="request"} 1.0', metrics)


class SettingsTest(unittest.TestCase):
    def test_ignored_entities_are_not_detected(self):
        detector = PIIDetector(Settings(ignored_entities=frozenset({"LOCATION"})), analyzer)
        self.assertNotIn("LOCATION", detector.entities)
        self.assertFalse(detector.decide(["What is the capital of France?"]).block)

    def test_score_threshold_drops_weak_matches(self):
        text = ["Call me on +44 20 7946 0958"]  # Presidio scores this phone number 0.4
        self.assertTrue(PIIDetector(Settings(), analyzer).decide(text).block)
        self.assertFalse(PIIDetector(Settings(score_threshold=0.9), analyzer).decide(text).block)

    def test_reads_settings_from_environment(self):
        env = {"PII_SCORE_THRESHOLD": "0.7", "PII_IGNORED_ENTITIES": "location, date_time", "PII_LANGUAGE": "en"}
        with unittest.mock.patch.dict("os.environ", env):
            settings = Settings.from_env()
        self.assertEqual(settings, Settings(0.7, frozenset({"LOCATION", "DATE_TIME"}), "en"))


if __name__ == "__main__":
    unittest.main()
