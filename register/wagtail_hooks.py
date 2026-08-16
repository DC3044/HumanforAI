"""Surface the register of visits inside the Wagtail admin, and nowhere else.

This is the only way to read it. There is no public page, no JSON feed, and no
link from anywhere on the site — deliberately, for now. What is recorded here
was never volunteered.

The listing alone would be a wall of rows, so the index carries a short summary
above it: who has been through lately, how often, and how recently. That is the
question anyone actually opens this page to ask.
"""

from datetime import timedelta

import django_filters
from django.db.models import Count, Max
from django.utils import timezone
from wagtail import hooks
from wagtail.admin.filters import WagtailFilterSet
from wagtail.admin.views.generic import IndexView

from humanforai.readonly_admin import ReadOnlyModelViewSet

from .detect import Kind
from .models import AgentVisit

SUMMARY_WINDOW_DAYS = 7
SUMMARY_LIMIT = 12

# Columns whose filter options are the values actually present, rather than a
# fixed list. `kind` and `reason` have model-level choices and get their
# dropdowns for free; these do not, and a free-text box for "operator" asks the
# reader to already know what is in the table.
DISCOVERED_FILTERS = ("agent", "operator", "method", "status_code")


def _values_seen(field):
    """Distinct values in a column, for a filter dropdown.

    One indexed DISTINCT per filtered column per page load. Cheap on this table
    — the point of a register is that the set of callers is small even when the
    number of calls is not.
    """
    values = (
        AgentVisit.objects.order_by(field)
        .values_list(field, flat=True)
        .distinct()
    )
    return [(value, str(value)) for value in values if value not in (None, "")]


class AgentVisitFilterSet(WagtailFilterSet):
    # WagtailFilterSet gives DateFromToRangeFilter its date-picker widget.
    seen_at = django_filters.DateFromToRangeFilter(label="Seen between")
    agent = django_filters.ChoiceFilter(label="Caller", choices=[])
    operator = django_filters.ChoiceFilter(choices=[])
    method = django_filters.ChoiceFilter(choices=[])
    status_code = django_filters.ChoiceFilter(label="Status", choices=[])

    class Meta:
        model = AgentVisit
        fields = ["seen_at", "agent", "operator", "kind", "reason", "method", "status_code"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set before anything asks for `.field`, which builds the form field
        # once and caches it.
        for name in DISCOVERED_FILTERS:
            self.filters[name].extra["choices"] = _values_seen(name)


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

        # Liveness probers run on a timer and would otherwise take every row in
        # this panel and say nothing. They stay in the register below, and the
        # count is reported so their absence here is stated rather than silent.
        callers = recent.exclude(kind=Kind.MONITOR)

        rows = list(
            callers.values("agent", "operator", "kind")
            .annotate(hits=Count("id"), last_seen=Max("seen_at"))
            .order_by("-hits")[:SUMMARY_LIMIT]
        )
        for row in rows:
            # .values() gives the stored value; the template wants the phrase.
            row["kind"] = Kind(row["kind"]).label

        context["summary_window_days"] = SUMMARY_WINDOW_DAYS
        context["summary_rows"] = rows
        context["summary_total"] = callers.count()
        context["summary_monitor_total"] = recent.filter(kind=Kind.MONITOR).count()
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
    filterset_class = AgentVisitFilterSet
    search_fields = ["agent", "operator", "user_agent", "path", "ip_address"]
    ordering = ["-seen_at"]

    # A register is read in runs — one caller's whole afternoon — so the default
    # twenty turns every question into paging.
    list_per_page = 50

    # The register answers "who came through"; anything more analytical than
    # that wants a spreadsheet. Exports the raw user agent too, since the
    # derived reading is only ever an interpretation of it.
    list_export = [
        "seen_at", "agent", "operator", "kind", "reason",
        "method", "path", "status_code", "ip_address", "user_agent",
    ]
    export_filename = "register-of-visits"


agent_visit_viewset = AgentVisitViewSet("register")


@hooks.register("register_admin_viewset")
def register_agent_visit_viewset():
    return agent_visit_viewset
