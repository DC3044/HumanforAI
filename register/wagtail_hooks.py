"""Surface the register of visits inside the Wagtail admin, and nowhere else.

This is the only way to read it. There is no public page, no JSON feed, and no
link from anywhere on the site — deliberately, for now. What is recorded here
was never volunteered.

The listing alone would be a wall of rows, so the index carries a short summary
above it: who has been through lately, how often, and how recently. That is the
question anyone actually opens this page to ask.
"""

from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone
from wagtail import hooks
from wagtail.admin.views.generic import IndexView

from humanforai.readonly_admin import ReadOnlyModelViewSet

from .detect import Kind
from .models import AgentVisit

SUMMARY_WINDOW_DAYS = 7
SUMMARY_LIMIT = 12


class RegisterIndexView(IndexView):
    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        # Skipped on the AJAX refresh that search and filtering trigger: that
        # request replaces the results table only, so computing the summary
        # again would be a wasted query whose output is thrown away.
        if self.results_only:
            return context

        since = timezone.now() - timedelta(days=SUMMARY_WINDOW_DAYS)
        recent = AgentVisit.objects.filter(seen_at__gte=since)

        rows = list(
            recent.values("agent", "operator", "kind")
            .annotate(hits=Count("id"), last_seen=Max("seen_at"))
            .order_by("-hits")[:SUMMARY_LIMIT]
        )
        for row in rows:
            # .values() gives the stored value; the template wants the phrase.
            row["kind"] = Kind(row["kind"]).label

        context["summary_window_days"] = SUMMARY_WINDOW_DAYS
        context["summary_rows"] = rows
        context["summary_total"] = recent.count()
        return context


class AgentVisitViewSet(ReadOnlyModelViewSet):
    model = AgentVisit
    index_view_class = RegisterIndexView
    template_prefix = "register/"

    icon = "view"
    menu_label = "Register of visits"
    menu_name = "register"
    menu_order = 110  # Directly under the inbox, at 100.
    add_to_admin_menu = True

    list_display = ["seen_at", "label", "kind", "method", "path", "status_code", "ip_address"]
    list_filter = ["operator", "kind", "reason", "method", "status_code"]
    search_fields = ["agent", "operator", "user_agent", "path", "ip_address"]
    ordering = ["-seen_at"]


agent_visit_viewset = AgentVisitViewSet("register")


@hooks.register("register_admin_viewset")
def register_agent_visit_viewset():
    return agent_visit_viewset
