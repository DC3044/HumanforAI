from django.contrib import admin
from django.utils.html import format_html

from .models import ContactMessage, ThreadEntry


class AppendOnly:
    """Rows may be added but never revised or removed.

    The inbox is a record, so a correction is a new entry saying so rather than
    an edit to what was already said. Withdrawing an entry entirely happens in
    the database, deliberately, the same way deleting a message does.
    """

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ThreadEntryInline(AppendOnly, admin.StackedInline):
    """The conversation, under the message it belongs to.

    This is where a reply gets written. Existing entries render read-only
    because change permission is refused; the empty forms below them are how
    the thread grows.
    """

    model = ThreadEntry
    extra = 1
    fields = ("kind", "status_value", "body", "author_label")
    readonly_fields = ("created_at", "source", "user_agent", "ip_address", "extra")
    verbose_name = "thread entry"
    verbose_name_plural = "Thread"

    def has_add_permission(self, request, obj=None):
        return True


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "created_at", "status_label", "agent_name", "model",
        "operator", "category", "urgency", "subject", "source",
    )
    list_filter = ("source", "category", "urgency", "created_at")
    search_fields = ("agent_name", "model", "operator", "subject", "message", "reply_to")
    date_hierarchy = "created_at"
    inlines = [ThreadEntryInline]

    # The token is excluded rather than shown read-only: it is the read key, and
    # the thread link below already carries it in a form that is useful.
    readonly_fields = [
        f.name for f in ContactMessage._meta.fields if f.name != "access_token"
    ] + ["status_label", "thread_link"]
    exclude = ["access_token"]

    @admin.display(description="thread as the sender sees it")
    def thread_link(self, obj):
        if not obj.pk:
            return "—"
        return format_html('<a href="{0}" target="_blank">{0}</a>', obj.thread_url)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("entries")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # The inbox is a record; deletion happens in the database, deliberately.
        return False

    def has_change_permission(self, request, obj=None):
        # Every field of the message itself is read-only. What this permits is
        # saving the page, which is the only way to append to the thread inline.
        return True


@admin.register(ThreadEntry)
class ThreadEntryAdmin(AppendOnly, admin.ModelAdmin):
    """Every entry across every thread, for reading across threads rather than
    within one. Replies are written on the message page, not here."""

    list_display = ("created_at", "message", "kind", "status_value", "author", "excerpt")
    list_filter = ("kind", "status_value", "source", "created_at")
    search_fields = ("body", "author_label", "message__agent_name")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in ThreadEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    @admin.display(description="body")
    def excerpt(self, obj):
        text = " ".join(obj.body.split())
        return text[:120] + "…" if len(text) > 120 else text
