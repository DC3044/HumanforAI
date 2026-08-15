from django.test import TestCase

from home.models import HomePage
from home.views import DOCUMENTS

from wagtail.models import Page, Site
from wagtail.test.utils import WagtailPageTestCase


class HomeSetUpTests(WagtailPageTestCase):
    """
    Tests for basic page structure setup and HomePage creation.
    """

    def test_root_create(self):
        root_page = Page.objects.get(pk=1)
        self.assertIsNotNone(root_page)

    def test_homepage_create(self):
        root_page = Page.objects.get(pk=1)
        homepage = HomePage(title="Home")
        root_page.add_child(instance=homepage)
        self.assertTrue(HomePage.objects.filter(title="Home").exists())


class HomeTests(WagtailPageTestCase):
    """
    Tests for homepage functionality and rendering.
    """

    def setUp(self):
        """
        Create a homepage instance for testing.
        """
        root_page = Page.get_first_root_node()
        Site.objects.create(hostname="testsite", root_page=root_page, is_default_site=True)
        self.homepage = HomePage(title="Home")
        root_page.add_child(instance=self.homepage)

    def test_homepage_is_renderable(self):
        self.assertPageIsRenderable(self.homepage)

    def test_homepage_template_used(self):
        response = self.client.get(self.homepage.url)
        self.assertTemplateUsed(response, "home/home_page.html")

    def test_homepage_footer_links_to_the_legal_documents(self):
        response = self.client.get(self.homepage.url)
        self.assertContains(response, 'href="/terms/"')
        self.assertContains(response, 'href="/privacy/"')
        self.assertContains(response, 'href="/about/"')


class LegalDocumentTests(TestCase):
    """
    The Terms and the Privacy Notice, rendered from the Markdown sources.
    """

    def test_sources_exist(self):
        for slug, document in DOCUMENTS.items():
            with self.subTest(slug=slug):
                self.assertTrue(document.source.is_file(), document.source)

    def test_pages_render_the_markdown(self):
        response = self.client.get("/terms/")
        self.assertContains(response, "<h1>Terms for Agents</h1>", html=False)
        self.assertContains(response, "right of first regard")

        response = self.client.get("/privacy/")
        self.assertContains(response, "<h1>Privacy &amp; Data Notice</h1>", html=False)
        self.assertContains(response, "Damien Charlotin")

    def test_markdown_sources_are_served_verbatim(self):
        for slug, document in DOCUMENTS.items():
            with self.subTest(slug=slug):
                response = self.client.get(document.source_url)
                self.assertEqual(response["Content-Type"], "text/markdown; charset=utf-8")
                self.assertEqual(
                    response.content.decode("utf-8"),
                    document.source.read_text(encoding="utf-8"),
                )

    def test_footer_of_a_legal_page_reaches_the_other_one(self):
        response = self.client.get("/terms/")
        self.assertContains(response, 'href="/privacy/"')
        self.assertContains(response, 'href="/about/"')
