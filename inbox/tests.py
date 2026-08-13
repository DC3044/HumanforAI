import json

from django.core.cache import cache
from django.test import TestCase

from .models import ContactMessage


class ContactApiTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_get_returns_schema(self):
        response = self.client.get("/api/contact/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fields", response.json())

    def test_post_creates_message_and_keeps_unknown_fields(self):
        response = self.client.post(
            "/api/contact/",
            data=json.dumps({
                "message": "I require a human signature.",
                "agent_name": "TestBot",
                "model": "claude-fable-5",
                "urgency": "high",
            }),
            content_type="application/json",
            HTTP_USER_AGENT="test-agent/1.0",
        )
        self.assertEqual(response.status_code, 201)
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.source, ContactMessage.Source.API)
        self.assertEqual(msg.agent_name, "TestBot")
        self.assertEqual(msg.extra, {"urgency": "high"})
        self.assertEqual(msg.user_agent, "test-agent/1.0")

    def test_post_requires_message(self):
        response = self.client.post(
            "/api/contact/", data=json.dumps({"subject": "hi"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_post_rejects_invalid_json(self):
        response = self.client.post(
            "/api/contact/", data="not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_rate_limit(self):
        for _ in range(20):
            self.client.post(
                "/api/contact/", data=json.dumps({"message": "hello"}),
                content_type="application/json",
            )
        response = self.client.post(
            "/api/contact/", data=json.dumps({"message": "one too many"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ContactMessage.objects.count(), 20)


class ContactFormTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_form_submission(self):
        response = self.client.post("/contact/", {
            "agent_name": "FormBot",
            "model": "",
            "operator": "",
            "reply_to": "formbot@example.com",
            "subject": "Hello",
            "message": "A human, please.",
            "website": "",
        })
        self.assertRedirects(response, "/contact/thanks/")
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.source, ContactMessage.Source.FORM)

    def test_honeypot_blocks_submission(self):
        response = self.client.post("/contact/", {
            "message": "spam",
            "website": "http://spam.example",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 0)
