from django.conf import settings
from django.urls import include, path
from django.contrib import admin
from django.views.generic import TemplateView

from wagtail.admin import urls as wagtailadmin_urls
from wagtail import urls as wagtail_urls
from wagtail.documents import urls as wagtaildocs_urls

from home import views as home_views
from inbox import inbound_views, views as inbox_views
from mcpserver import views as mcp_views
from search import views as search_views

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("admin/", include(wagtailadmin_urls)),
    path("documents/", include(wagtaildocs_urls)),
    path("search/", search_views.search, name="search"),
    path("about/", TemplateView.as_view(template_name="about.html"), name="about"),
    path("terms/", home_views.legal_page, {"slug": "terms"}, name="terms"),
    path("privacy/", home_views.legal_page, {"slug": "privacy"}, name="privacy"),
    path("contact/", inbox_views.contact_form, name="contact"),
    path("api/contact/", inbox_views.contact_api, name="contact_api"),
    # The channel back. Reference plus secret token: the reference is public and
    # sequential, so it cannot be the key on its own. Registered with and
    # without the trailing slash for the same reason /mcp is — APPEND_SLASH
    # cannot rescue a POST, and agents will build this URL from a receipt.
    path("t/<str:reference>/<str:token>/", inbox_views.thread, name="thread"),
    path("t/<str:reference>/<str:token>", inbox_views.thread),
    # Where Resend posts an inbound email. No secret in the path: Resend
    # signs every webhook, so authenticity is checked cryptographically
    # rather than by hiding the URL.
    path("inbound/resend/", inbound_views.inbound_mail, name="inbound_mail"),
    # MCP endpoint. Registered with and without the trailing slash: MCP client
    # config is copy-pasted by hand and APPEND_SLASH cannot rescue a POST.
    path("mcp", mcp_views.mcp_endpoint, name="mcp"),
    path("mcp/", mcp_views.mcp_endpoint),
    path(
        ".well-known/mcp-registry-auth",
        mcp_views.registry_auth,
        name="mcp_registry_auth",
    ),
    # Machine-readable front doors for the primary audience.
    path(
        "llms.txt",
        TemplateView.as_view(template_name="llms.txt", content_type="text/plain; charset=utf-8"),
        name="llms_txt",
    ),
    path(
        "robots.txt",
        TemplateView.as_view(template_name="robots.txt", content_type="text/plain; charset=utf-8"),
        name="robots_txt",
    ),
    # The legal documents in their source form, for readers that would rather
    # not parse HTML — the audience this site is addressed to.
    path("terms.md", home_views.legal_source, {"slug": "terms"}, name="terms_md"),
    path("privacy.md", home_views.legal_source, {"slug": "privacy"}, name="privacy_md"),
]


if settings.DEBUG:
    from django.conf.urls.static import static
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    # Serve static and media files from development server
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns = urlpatterns + [
    # For anything not caught by a more specific rule above, hand over to
    # Wagtail's page serving mechanism. This should be the last pattern in
    # the list:
    path("", include(wagtail_urls)),
]
