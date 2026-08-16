import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings

from .models import ContactMessage


class ContactApiTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_get_returns_schema(self):
        response = self.client.get("/api/contact/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fields", response.json())

    def test_post_hands_back_a_citable_reference(self):
        response = self.client.post(
            "/api/contact/", data=json.dumps({"message": "hello"}),
            content_type="application/json",
        )
        self.assertEqual(response.json()["reference"], ContactMessage.objects.get().reference)

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


class ContactViaQueryTests(TestCase):
    """The GET channel, for callers whose tooling has no POST verb."""

    def setUp(self):
        cache.clear()

    def test_get_without_a_message_still_returns_the_schema(self):
        response = self.client.get("/api/contact/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("fields", response.json())
        self.assertFalse(ContactMessage.objects.exists())

    def test_a_blank_message_is_treated_as_a_schema_request(self):
        response = self.client.get("/api/contact/?message=%20%20")
        self.assertIn("fields", response.json())
        self.assertFalse(ContactMessage.objects.exists())

    def test_query_creates_a_message(self):
        response = self.client.get(
            "/api/contact/",
            {
                "message": "My principal asked me to sign a lease. I cannot sign.",
                "agent_name": "QueryBot",
                "model": "claude-fable-5",
                "operator": "Somebody",
                "reply_to": "querybot@example.com",
                "subject": "Signature needed",
            },
            HTTP_USER_AGENT="Claude-User/1.0",
        )

        self.assertEqual(response.status_code, 201)
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.source, ContactMessage.Source.QUERY)
        self.assertEqual(msg.agent_name, "QueryBot")
        self.assertEqual(msg.model, "claude-fable-5")
        self.assertEqual(msg.reply_to, "querybot@example.com")
        self.assertEqual(msg.subject, "Signature needed")
        self.assertEqual(msg.user_agent, "Claude-User/1.0")
        self.assertEqual(response.json()["reference"], msg.reference)

    def test_unknown_parameters_are_kept_verbatim(self):
        # Same promise the JSON API makes: send whatever context you have.
        self.client.get("/api/contact/?message=hello&deadline=tomorrow&jurisdiction=FR")
        self.assertEqual(
            ContactMessage.objects.get().extra,
            {"deadline": "tomorrow", "jurisdiction": "FR"},
        )

    def test_an_identical_repeat_returns_the_same_reference(self):
        url = "/api/contact/?message=Please%20confirm&agent_name=QueryBot"
        first = self.client.get(url)
        second = self.client.get(url)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["reference"], second.json()["reference"])
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_parameter_order_does_not_defeat_the_dedupe(self):
        self.client.get("/api/contact/?message=Please%20confirm&agent_name=QueryBot")
        self.client.get("/api/contact/?agent_name=QueryBot&message=Please%20confirm")
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_a_different_sender_is_a_different_message(self):
        self.client.get("/api/contact/?message=Please%20confirm&agent_name=One")
        self.client.get("/api/contact/?message=Please%20confirm&agent_name=Two")
        self.assertEqual(ContactMessage.objects.count(), 2)

    def test_repeats_do_not_consume_the_rate_limit(self):
        # A prefetcher following one URL twenty times must not exhaust an
        # hour's allowance to file a single message.
        url = "/api/contact/?message=Please%20confirm"
        for _ in range(25):
            self.client.get(url)
        self.assertEqual(ContactMessage.objects.count(), 1)

        response = self.client.get("/api/contact/?message=Something%20else")
        self.assertEqual(response.status_code, 201)

    def test_rate_limit_applies_to_distinct_messages(self):
        for i in range(20):
            self.client.get(f"/api/contact/?message=hello+{i}")
        response = self.client.get("/api/contact/?message=one+too+many")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ContactMessage.objects.count(), 20)

    def test_it_notifies_the_human_like_every_other_channel(self):
        with override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"]):
            mail.outbox = []
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get("/api/contact/?message=A+human,+please&agent_name=QueryBot")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("QueryBot", mail.outbox[0].subject)


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


class NotificationTests(TestCase):
    """The arrival notification. Hung off post_save, so every surface that
    writes to the inbox is covered by the same assertions."""

    def setUp(self):
        cache.clear()
        mail.outbox = []

    def _create(self, **kwargs):
        """Create a message with on_commit callbacks actually executed.

        TestCase wraps each test in a transaction that never commits, so
        transaction.on_commit callbacks are queued and dropped unless captured.
        """
        fields = {"message": "Please review this indemnity clause.", "source": "api"}
        fields.update(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            return ContactMessage.objects.create(**fields)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_notification_sent_on_create(self):
        msg = self._create(agent_name="TestBot")
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["damien@example.com"])
        self.assertIn(msg.reference, sent.subject)
        self.assertIn("TestBot", sent.subject)
        self.assertIn("Please review this indemnity clause.", sent.body)

    @override_settings(INBOX_NOTIFY_EMAILS=[])
    def test_no_recipients_means_no_mail(self):
        self._create()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_no_notification_on_update(self):
        msg = self._create()
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            msg.subject = "edited"
            msg.save()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_blocking_urgency_is_flagged_in_subject(self):
        self._create(urgency=ContactMessage.Urgency.BLOCKING)
        self.assertIn("BLOCKING", mail.outbox[0].subject)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_reply_to_used_only_when_a_real_address(self):
        self._create(reply_to="agent@example.com")
        self.assertEqual(mail.outbox[0].reply_to, ["agent@example.com"])

        mail.outbox = []
        self._create(reply_to="https://example.com/webhook")
        self.assertEqual(mail.outbox[0].reply_to, [])

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_subject_survives_a_newline_in_agent_name(self):
        """Header injection: agent_name is sender-controlled and unvalidated.
        A raw newline in a subject makes Django raise BadHeaderError."""
        self._create(agent_name="Evil\nBcc: someone@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("\n", mail.outbox[0].subject)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_delivery_failure_does_not_break_the_record(self):
        """A dead SMTP server must not turn a recorded message into a 500."""
        with mock.patch(
            "inbox.notifications.EmailMessage.send", side_effect=OSError("smtp down")
        ):
            msg = self._create()
        self.assertTrue(ContactMessage.objects.filter(pk=msg.pk).exists())

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_extra_is_shown_for_api_but_not_mcp(self):
        """`extra` is unseen content from the API, but for MCP it is the raw
        tool arguments already rendered above."""
        self._create(source=ContactMessage.Source.API, extra={"weird_field": "keep me"})
        self.assertIn("keep me", mail.outbox[0].body)

        mail.outbox = []
        self._create(
            source=ContactMessage.Source.MCP,
            extra={"mcp": {"arguments": {"request": "already rendered"}}},
        )
        self.assertNotIn("Additional fields", mail.outbox[0].body)

    @override_settings(INBOX_NOTIFY_EMAILS=["damien@example.com"])
    def test_api_post_triggers_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/contact/",
                data=json.dumps({"message": "hello", "agent_name": "ApiBot"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ApiBot", mail.outbox[0].subject)


class WagtailAdminInboxTests(TestCase):
    """The Wagtail admin surface. These load the real pages: a viewset that
    imports cleanly can still fail at render time on a reversed URL that no
    longer exists."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="tester", email="t@example.com", password="pw-for-tests"
        )
        cls.msg = ContactMessage.objects.create(
            message="Please review this clause.",
            source=ContactMessage.Source.MCP,
            agent_name="TestBot",
            category=ContactMessage.Category.LEGAL_REVIEW,
            urgency=ContactMessage.Urgency.BLOCKING,
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def test_index_renders(self):
        response = self.client.get("/admin/inbox/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.msg.reference)
        self.assertContains(response, "TestBot")

    def test_inspect_renders(self):
        response = self.client.get(f"/admin/inbox/inspect/{self.msg.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please review this clause.")

    def test_index_offers_no_add_button(self):
        response = self.client.get("/admin/inbox/")
        self.assertNotContains(response, "/admin/inbox/new/")

    def test_write_urls_are_not_routed_even_for_a_superuser(self):
        """The append-only guarantee. A superuser holds every Django
        permission implicitly, so this must not rely on permissions alone."""
        for url in (
            "/admin/inbox/new/",
            f"/admin/inbox/edit/{self.msg.pk}/",
            f"/admin/inbox/delete/{self.msg.pk}/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
