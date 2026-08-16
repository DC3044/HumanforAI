from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .detect import Kind, Reason, identify
from .models import AgentVisit

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLAUDEBOT_UA = "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"


class IdentifyTests(TestCase):
    def test_names_the_vendor_families(self):
        cases = [
            (CLAUDEBOT_UA, "ClaudeBot", "Anthropic", Kind.CRAWLER),
            ("Claude-User/1.0", "Claude-User", "Anthropic", Kind.ON_BEHALF),
            ("Mozilla/5.0 ... GPTBot/1.2; +https://openai.com/gptbot", "GPTBot", "OpenAI", Kind.CRAWLER),
            ("ChatGPT-User/1.0; +https://openai.com/bot", "ChatGPT-User", "OpenAI", Kind.ON_BEHALF),
            ("OAI-SearchBot/1.0", "OAI-SearchBot", "OpenAI", Kind.SEARCH),
            ("PerplexityBot/1.0", "PerplexityBot", "Perplexity", Kind.SEARCH),
            ("Perplexity-User/1.0", "Perplexity-User", "Perplexity", Kind.ON_BEHALF),
            ("Mozilla/5.0 (compatible; Googlebot/2.1)", "Googlebot", "Google", Kind.CRAWLER),
            ("CCBot/2.0 (https://commoncrawl.org/faq/)", "CCBot", "Common Crawl", Kind.CRAWLER),
            ("python-requests/2.32.3", "python-requests", "", Kind.CLIENT),
            ("curl/8.7.1", "curl", "", Kind.CLIENT),
        ]
        for user_agent, agent, operator, kind in cases:
            with self.subTest(user_agent=user_agent):
                sighting = identify(user_agent)
                self.assertIsNotNone(sighting)
                self.assertEqual(sighting.agent, agent)
                self.assertEqual(sighting.operator, operator)
                self.assertEqual(sighting.kind, kind)
                self.assertEqual(sighting.reason, Reason.USER_AGENT)

    def test_specific_patterns_win_over_general_ones(self):
        # The whole point of the ordering: these are not ClaudeBot or GPTBot,
        # and the difference between crawling and acting for a human is the
        # most interesting thing the register records.
        self.assertEqual(identify("Claude-User/1.0").kind, Kind.ON_BEHALF)
        self.assertEqual(identify("Claude-SearchBot/1.0").kind, Kind.SEARCH)
        self.assertEqual(identify("ChatGPT-User/1.0").agent, "ChatGPT-User")

    def test_browser_and_empty_user_agents_are_not_identified(self):
        self.assertIsNone(identify(BROWSER_UA))
        self.assertIsNone(identify(""))
        self.assertIsNone(identify(None))


class MiddlewareTests(TestCase):
    def test_records_a_recognised_agent(self):
        response = self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        visit = AgentVisit.objects.get()
        self.assertEqual(visit.agent, "ClaudeBot")
        self.assertEqual(visit.operator, "Anthropic")
        self.assertEqual(visit.kind, Kind.CRAWLER)
        self.assertEqual(visit.reason, Reason.USER_AGENT)
        self.assertEqual(visit.method, "GET")
        self.assertEqual(visit.path, "/")
        self.assertEqual(visit.status_code, response.status_code)
        self.assertEqual(visit.user_agent, CLAUDEBOT_UA)

    def test_records_an_unrecognised_caller_on_an_agent_surface(self):
        self.client.get("/llms.txt", HTTP_USER_AGENT="some-unknown-thing/0.1")

        visit = AgentVisit.objects.get()
        self.assertEqual(visit.reason, Reason.SURFACE)
        self.assertEqual(visit.kind, Kind.UNKNOWN)
        # Named from the leading token, so the listing is readable.
        self.assertEqual(visit.agent, "some-unknown-thing")

    def test_records_a_caller_with_no_user_agent_at_all(self):
        self.client.get("/llms.txt")

        visit = AgentVisit.objects.get()
        self.assertEqual(visit.reason, Reason.SURFACE)
        self.assertEqual(visit.agent, "(no user agent)")

    def test_ignores_a_browser_on_an_ordinary_page(self):
        self.client.get("/", HTTP_USER_AGENT=BROWSER_UA)
        self.assertFalse(AgentVisit.objects.exists())

    def test_ignores_the_admin_and_static_prefixes(self):
        for path in ("/admin/", "/django-admin/", "/static/css/humanforai.css"):
            with self.subTest(path=path):
                self.client.get(path, HTTP_USER_AGENT=CLAUDEBOT_UA)
        self.assertFalse(AgentVisit.objects.exists())

    def test_records_the_status_code_of_a_miss(self):
        # Trailing slash on purpose: Wagtail's catch-all route makes the slashed
        # form of any path resolvable, so APPEND_SLASH answers "/no-such-page"
        # with a 301 rather than a 404. Both are recorded; this pins the 404.
        self.client.get("/no-such-page/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        visit = AgentVisit.objects.get()
        self.assertEqual(visit.status_code, 404)
        self.assertEqual(visit.path, "/no-such-page/")

    def test_records_a_redirect_as_readily_as_a_page(self):
        self.client.get("/no-such-page", HTTP_USER_AGENT=CLAUDEBOT_UA)
        self.assertEqual(AgentVisit.objects.get().status_code, 301)

    def test_keeps_the_query_string(self):
        self.client.get("/llms.txt?from=somewhere", HTTP_USER_AGENT=CLAUDEBOT_UA)
        self.assertEqual(AgentVisit.objects.get().path, "/llms.txt?from=somewhere")

    def test_takes_the_client_ip_from_the_forwarded_header(self):
        self.client.get(
            "/", HTTP_USER_AGENT=CLAUDEBOT_UA,
            HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1",
        )
        self.assertEqual(AgentVisit.objects.get().ip_address, "203.0.113.7")

    def test_a_forged_forwarded_header_does_not_break_the_write(self):
        # Postgres stores this column as inet and would reject the row — and
        # poison the transaction — so junk becomes a null instead.
        self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA, HTTP_X_FORWARDED_FOR="not-an-ip")
        self.assertIsNone(AgentVisit.objects.get().ip_address)

    def test_a_failing_write_does_not_break_the_response(self):
        with mock.patch(
            "register.middleware.AgentVisit.objects.create",
            side_effect=RuntimeError("database is on fire"),
        ):
            with self.assertLogs("register.middleware", level="ERROR"):
                response = self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentVisit.objects.exists())

    @override_settings(REGISTER_ENABLED=False)
    def test_disabled_records_nothing(self):
        self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)
        self.assertFalse(AgentVisit.objects.exists())


class LabelTests(TestCase):
    def test_reads_as_one_line(self):
        visit = AgentVisit(agent="Claude-User", operator="Anthropic", kind=Kind.ON_BEHALF)
        self.assertEqual(visit.label, "Claude-User (Anthropic, on behalf of a human)")

    def test_copes_with_an_unattributed_caller(self):
        visit = AgentVisit(agent="curl", operator="", kind=Kind.CLIENT)
        self.assertEqual(visit.label, "curl (a bare HTTP client)")


class PruneVisitsTests(TestCase):
    def setUp(self):
        self.old = AgentVisit.objects.create(agent="GPTBot", method="GET", path="/", reason=Reason.USER_AGENT)
        self.new = AgentVisit.objects.create(agent="ClaudeBot", method="GET", path="/", reason=Reason.USER_AGENT)
        # auto_now_add cannot be set on create, so age the row afterwards.
        AgentVisit.objects.filter(pk=self.old.pk).update(
            seen_at=timezone.now() - timedelta(days=200)
        )

    def _run(self, *args):
        out = StringIO()
        call_command("prune_visits", *args, stdout=out)
        return out.getvalue()

    def test_deletes_only_rows_past_the_cutoff(self):
        output = self._run("--days", "90")
        self.assertIn("Deleted 1 visit", output)
        self.assertEqual([v.pk for v in AgentVisit.objects.all()], [self.new.pk])

    def test_dry_run_deletes_nothing(self):
        output = self._run("--days", "90", "--dry-run")
        self.assertIn("Would delete 1 visit", output)
        self.assertEqual(AgentVisit.objects.count(), 2)

    @override_settings(REGISTER_RETENTION_DAYS=365)
    def test_defaults_to_the_retention_setting(self):
        self._run()
        self.assertEqual(AgentVisit.objects.count(), 2)


class AdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="human", email="human@example.com", password="password",
        )
        self.client.force_login(self.user)
        AgentVisit.objects.create(
            agent="ClaudeBot", operator="Anthropic", kind=Kind.CRAWLER,
            reason=Reason.USER_AGENT, method="GET", path="/llms.txt", status_code=200,
        )

    def test_index_lists_visits_and_summarises_them(self):
        response = self.client.get("/admin/register/")
        self.assertEqual(response.status_code, 200)
        # The parenthetical form can only come from the `label` column, so this
        # also proves Wagtail resolves a model property in list_display.
        self.assertContains(response, "ClaudeBot (Anthropic, crawling)")
        self.assertContains(response, "Seen in the last 7 days")
        self.assertEqual(response.context["summary_total"], 1)
        self.assertEqual(response.context["summary_rows"][0]["hits"], 1)

    def test_summary_renders_the_kind_as_a_phrase(self):
        response = self.client.get("/admin/register/")
        self.assertEqual(response.context["summary_rows"][0]["kind"], "crawling")

    def test_there_is_no_way_to_write_to_the_register(self):
        # Not merely forbidden — the routes do not exist, for a superuser who
        # would otherwise hold every permission.
        for path in ("/admin/register/new/", "/admin/register/edit/1/", "/admin/register/delete/1/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_inspect_view_is_available(self):
        visit = AgentVisit.objects.get()
        response = self.client.get(f"/admin/register/inspect/{visit.pk}/")
        self.assertEqual(response.status_code, 200)
