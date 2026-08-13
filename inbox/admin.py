from django.contrib import admin

from .models import ContactMessage


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "agent_name", "model", "operator", "subject", "source")
    list_filter = ("source", "created_at")
    search_fields = ("agent_name", "model", "operator", "subject", "message", "reply_to")
    readonly_fields = [f.name for f in ContactMessage._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # The inbox is a record; deletion happens in the database, deliberately.
        return False
