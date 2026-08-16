import time
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import buffer
from .detect import Kind, Reason, _fallback_name, identify, looks_like_a_browser
from .models import AgentVisit

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLAUDEBOT_UA = "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"


class RegisterTestCase(TestCase):
    """Base for tests that make requests.

    The visit buffer is module-level state, so unlike the database it is not
    rolled back between tests — a test that deliberately leaves something queued
    would otherwise have it flushed by whichever test ran next.
    """

    def setUp(self):
        super().setUp()
        buffer._pending.clear()

    def tearDown(self):
        buffer._pending.clear()
        super().tearDown()


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

    def test_liveness_probers_are_named_and_set_apart(self):
        # Found this server through the MCP Registry listing. Genuine traffic,
        # but on a timer, so it gets a kind of its own to keep out of the way.
        sighting = identify("mcpbeat/0.1")
        self.assertEqual(sighting.agent, "mcpbeat")
        self.assertEqual(sighting.kind, Kind.MONITOR)

    def test_javascript_runtimes_count_as_clients(self):
        for user_agent, agent in [("Bun/1.1.34", "Bun"), ("Deno/2.0.6", "Deno")]:
            with self.subTest(user_agent=user_agent):
                sighting = identify(user_agent)
                self.assertEqual(sighting.agent, agent)
                self.assertEqual(sighting.kind, Kind.CLIENT)


class FallbackNameTests(TestCase):
    def test_a_user_agent_that_embeds_a_url_is_not_cut_mid_scheme(self):
        self.assertEqual(
            _fallback_name(
                "GoogleStackdriverMonitoring-UptimeChecks"
                "(https://cloud.google.com/monitoring)"
            ),
            "GoogleStackdriverMonitoring-UptimeChecks",
        )

    def test_ordinary_product_tokens_survive(self):
        self.assertEqual(_fallback_name("some-unknown-thing/0.1"), "some-unknown-thing")
        self.assertEqual(_fallback_name("Mozilla/5.0 (compatible; Thing)"), "Mozilla")

    def test_nothing_at_all(self):
        self.assertEqual(_fallback_name(""), "(no user agent)")


class BrowserPresumptionTests(RegisterTestCase):
    """The gate on ordinary pages: presume human, record everything else.

    The pattern table names callers; it does not decide whether they count.
    That split is the point — a caller nobody has heard of is most interesting
    on the day it first calls, which is necessarily before anyone has added it
    to a list.
    """

    REAL_BROWSERS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    ]

    NOT_BROWSERS = [
        # Browser-shaped, but announcing themselves inside the string.
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Mozilla/5.0 (Linux; Android 6.0.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Mozilla/5.0 (compatible; SomeBrandNewAgent/0.3; +https://example.com/agent)",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36",
        # Not even pretending.
        "python-requests/2.32.3",
        "SentinelOracle/1.0",
        "",
    ]

    def test_real_browsers_are_presumed_human(self):
        for user_agent in self.REAL_BROWSERS:
            with self.subTest(user_agent=user_agent[:40]):
                self.assertTrue(looks_like_a_browser(user_agent))

    def test_everything_else_is_not(self):
        for user_agent in self.NOT_BROWSERS:
            with self.subTest(user_agent=user_agent[:40]):
                self.assertFalse(looks_like_a_browser(user_agent))

    def test_an_unknown_agent_on_an_ordinary_page_is_recorded(self):
        # The case the pattern table could never have covered: a product that
        # did not exist when the table was written.
        self.client.get("/", HTTP_USER_AGENT="SentinelOracle/1.0")

        visit = AgentVisit.objects.get()
        self.assertEqual(visit.agent, "SentinelOracle")
        self.assertEqual(visit.reason, Reason.NOT_A_BROWSER)
        self.assertEqual(visit.kind, Kind.UNKNOWN)

    def test_a_browser_shaped_crawler_is_named_from_inside_its_string(self):
        self.client.get(
            "/",
            HTTP_USER_AGENT="Mozilla/5.0 (compatible; SomeBrandNewAgent/0.3; +https://example.com/agent)",
        )
        # Not "Mozilla", which is what every one of these leads with.
        self.assertEqual(AgentVisit.objects.get().agent, "SomeBrandNewAgent")

    def test_a_browser_on_an_ordinary_page_is_still_not_recorded(self):
        self.client.get("/", HTTP_USER_AGENT=BROWSER_UA)
        self.assertFalse(AgentVisit.objects.exists())

    def test_a_recognised_agent_keeps_its_attribution(self):
        # Inversion decides whether to record; it does not take over naming.
        self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        visit = AgentVisit.objects.get()
        self.assertEqual(visit.reason, Reason.USER_AGENT)
        self.assertEqual(visit.operator, "Anthropic")
        self.assertEqual(visit.kind, Kind.CRAWLER)


class IgnoredAgentTests(RegisterTestCase):
    def test_self_monitoring_is_not_recorded_even_on_an_agent_surface(self):
        # The case that prompted this: Cloud Monitoring POSTs to /mcp, which the
        # surface rule would otherwise record once a minute forever.
        self.client.get(
            "/llms.txt",
            HTTP_USER_AGENT="GoogleStackdriverMonitoring-UptimeChecks(https://cloud.google.com/monitoring)",
        )
        self.assertFalse(AgentVisit.objects.exists())

    def test_third_party_probers_are_still_recorded(self):
        # Not our infrastructure, so it stays in the register — it is simply
        # kept out of the admin summary.
        self.client.get("/llms.txt", HTTP_USER_AGENT="mcpbeat/0.1")
        self.assertEqual(AgentVisit.objects.get().kind, Kind.MONITOR)

    @override_settings(REGISTER_IGNORE_AGENTS=())
    def test_the_list_is_a_setting_not_a_hard_rule(self):
        self.client.get("/llms.txt", HTTP_USER_AGENT="GoogleStackdriverMonitoring-UptimeChecks")
        self.assertTrue(AgentVisit.objects.exists())


class MiddlewareTests(RegisterTestCase):
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
            "register.models.AgentVisit.objects.bulk_create",
            side_effect=RuntimeError("database is on fire"),
        ):
            with self.assertLogs("register.buffer", level="ERROR"):
                response = self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentVisit.objects.exists())
        # Re-queued rather than discarded: a failed flush usually means the
        # database blinked, not that these rows are bad.
        self.assertEqual(buffer.pending(), 1)

    def test_the_middleware_still_swallows_its_own_errors(self):
        with mock.patch(
            "register.middleware.should_record", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("register.middleware", level="ERROR"):
                response = self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(response.status_code, 200)

    @override_settings(REGISTER_ENABLED=False)
    def test_disabled_records_nothing(self):
        self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)
        self.assertFalse(AgentVisit.objects.exists())


@override_settings(REGISTER_FLUSH_ROWS=1000, REGISTER_FLUSH_SECONDS=99_999)
class BufferTests(RegisterTestCase):
    """Batching, which exists so a serverless database can suspend.

    Serving an agent is otherwise free of the database entirely — /mcp and
    /llms.txt are zero queries — so writing per request was the only thing
    keeping it awake, and the only thing being billed for.
    """

    def test_visits_wait_in_memory_rather_than_hitting_the_database(self):
        # /llms.txt is the shape of traffic this exists for: serving it costs
        # no queries at all, so the register was the only reason the database
        # woke up. Under batching it stays asleep.
        with self.assertNumQueries(0):
            self.client.get("/llms.txt", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(buffer.pending(), 1)
        self.assertFalse(AgentVisit.objects.exists())

    def test_a_full_batch_is_written(self):
        with override_settings(REGISTER_FLUSH_ROWS=3):
            for i in range(3):
                self.client.get(f"/page-{i}/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(buffer.pending(), 0)
        self.assertEqual(AgentVisit.objects.count(), 3)

    def test_an_elapsed_interval_is_written(self):
        with override_settings(REGISTER_FLUSH_SECONDS=0):
            self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(AgentVisit.objects.count(), 1)

    def test_an_elapsed_interval_with_nothing_waiting_writes_nothing(self):
        # A browser on an ordinary page queues nothing, and an elapsed interval
        # alone must not send an empty batch at a sleeping database.
        with override_settings(REGISTER_FLUSH_SECONDS=0):
            with mock.patch("register.models.AgentVisit.objects.bulk_create") as write:
                self.client.get("/about/", HTTP_USER_AGENT=BROWSER_UA)

        write.assert_not_called()

    def test_shutdown_writes_what_is_waiting(self):
        # Registered with atexit, so this is what a Cloud Run SIGTERM runs.
        self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)
        self.assertFalse(AgentVisit.objects.exists())

        self.assertEqual(buffer.flush(), 1)
        self.assertEqual(AgentVisit.objects.count(), 1)

    def test_seen_at_is_the_moment_of_the_visit_not_of_the_flush(self):
        """The trap in batching, pinned.

        `auto_now_add` stamps a row when it reaches the database, and
        `bulk_create` honours it — so under batching every visit in a group
        would claim to have happened at the flush, up to half an hour late. The
        field uses a default instead, evaluated when the instance is built.
        """
        self.client.get("/", HTTP_USER_AGENT=CLAUDEBOT_UA)
        happened_at = buffer._pending[0].seen_at

        time.sleep(0.05)
        buffer.flush()

        self.assertEqual(AgentVisit.objects.get().seen_at, happened_at)

    def test_the_buffer_is_bounded_when_flushes_keep_failing(self):
        with override_settings(REGISTER_BUFFER_MAX=3):
            with self.assertLogs("register.buffer", level="WARNING"):
                for i in range(6):
                    self.client.get(f"/page-{i}/", HTTP_USER_AGENT=CLAUDEBOT_UA)

        self.assertEqual(buffer.pending(), 3)
        # The oldest go first, so what survives is the most recent.
        self.assertEqual(
            [v.path for v in buffer._pending], ["/page-3/", "/page-4/", "/page-5/"]
        )


class LabelTests(TestCase):
    def test_reads_as_one_line(self):
        visit = AgentVisit(agent="Claude-User", operator="Anthropic", kind=Kind.ON_BEHALF)
        self.assertEqual(visit.label, "Claude-User (Anthropic, on behalf of a human)")

    def test_copes_with_an_unattributed_caller(self):
        visit = AgentVisit(agent="curl", operator="", kind=Kind.CLIENT)
        self.assertEqual(visit.label, "curl (a bare HTTP client)")

    def test_an_unrecognised_caller_is_not_told_so_twice(self):
        # The kind column beside it already says "unrecognised".
        visit = AgentVisit(agent="mcpbeat", operator="", kind=Kind.UNKNOWN)
        self.assertEqual(visit.label, "mcpbeat")


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

    def test_the_shipped_retention_is_the_measured_one(self):
        # Pinned because it is a capacity decision, not a taste one: ~100 calls
        # an hour at ~440 bytes a row settles near 35 MB at thirty days and
        # 110 MB at ninety. Changing it changes the database bill.
        self.assertEqual(settings.REGISTER_RETENTION_DAYS, 30)

    def test_it_is_cheap_when_there_is_nothing_to_delete(self):
        # This runs at container start, so its cost on the common path — an
        # instance recycling minutes after the last one pruned — is the thing
        # that matters. Two queries, no scan of the table.
        self._run("--days", "365")
        with self.assertNumQueries(2):
            self._run("--days", "365")


class AdminTests(RegisterTestCase):
    def setUp(self):
        super().setUp()
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

    def test_summary_sets_liveness_probes_aside_but_counts_them(self):
        for _ in range(5):
            AgentVisit.objects.create(
                agent="mcpbeat", kind=Kind.MONITOR, reason=Reason.USER_AGENT,
                method="POST", path="/mcp", status_code=200,
            )

        response = self.client.get("/admin/register/")
        self.assertEqual(response.context["summary_total"], 1)
        self.assertEqual(response.context["summary_monitor_total"], 5)
        self.assertNotIn("mcpbeat", [r["agent"] for r in response.context["summary_rows"]])
        self.assertContains(response, "besides 5 liveness checks not shown here")
        # Still in the register itself, just not in the panel above it.
        self.assertEqual(AgentVisit.objects.filter(agent="mcpbeat").count(), 5)

    def test_there_is_no_way_to_write_to_the_register(self):
        # Not merely forbidden — the routes do not exist, for a superuser who
        # would otherwise hold every permission.
        for path in ("/admin/register/new/", "/admin/register/edit/1/", "/admin/register/delete/1/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_filter_options_are_the_values_actually_present(self):
        AgentVisit.objects.create(
            agent="GPTBot", operator="OpenAI", kind=Kind.CRAWLER,
            reason=Reason.USER_AGENT, method="POST", path="/mcp", status_code=404,
        )
        response = self.client.get("/admin/register/")
        choices = dict(response.context["filters"].filters["agent"].extra["choices"])
        self.assertEqual(set(choices), {"ClaudeBot", "GPTBot"})
        # A caller that has never called is not offered as a filter.
        self.assertNotIn("Bytespider", choices)

    def test_filtering_by_caller_narrows_the_listing(self):
        AgentVisit.objects.create(
            agent="GPTBot", operator="OpenAI", kind=Kind.CRAWLER,
            reason=Reason.USER_AGENT, method="GET", path="/", status_code=200,
        )
        response = self.client.get("/admin/register/?agent=GPTBot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([v.agent for v in response.context["object_list"]], ["GPTBot"])

    def test_the_summary_links_through_to_a_callers_own_rows(self):
        response = self.client.get("/admin/register/")
        self.assertContains(response, "?agent=ClaudeBot")

    def test_a_date_range_filter_is_offered(self):
        response = self.client.get("/admin/register/")
        self.assertIn("seen_at", response.context["filters"].filters)

    def test_the_register_can_be_exported(self):
        response = self.client.get("/admin/register/?export=csv")
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("ClaudeBot", body)
        # The raw user agent goes too: the derived reading is an interpretation
        # of it, and an export is where someone re-does the interpretation.
        self.assertIn("user_agent", body.splitlines()[0].replace(" ", "_").lower())

    def test_inspect_view_is_available(self):
        visit = AgentVisit.objects.get()
        response = self.client.get(f"/admin/register/inspect/{visit.pk}/")
        self.assertEqual(response.status_code, 200)
