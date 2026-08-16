from django.contrib import admin

from .models import AgentVisit


@admin.register(AgentVisit)
class AgentVisitAdmin(admin.ModelAdmin):
    list_display = (
        "seen_at", "agent", "operator", "kind", "method", "path",
        "status_code", "ip_address", "reason",
    )
    list_filter = ("operator", "kind", "reason", "method", "status_code", "seen_at")
    search_fields = ("agent", "operator", "user_agent", "path", "ip_address")
    readonly_fields = [f.name for f in AgentVisit._meta.fields]
    date_hierarchy = "seen_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Individual rows are observations and are not edited away one at a
        # time. Bulk expiry is `manage.py prune_visits`, which is honest about
        # being a retention policy rather than a correction.
        return False
