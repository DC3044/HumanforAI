"""Surface the inbox inside the Wagtail admin.

The records already had a home in the Django admin at
/django-admin/inbox/contactmessage/, which is a URL nobody guesses. This puts
them in the sidebar of the admin you actually log into, without giving up the
append-only guarantee — see humanforai/readonly_admin.py for how that guarantee
is held.
"""

from wagtail import hooks

from humanforai.readonly_admin import ReadOnlyModelViewSet

from .models import ContactMessage


class ContactMessageViewSet(ReadOnlyModelViewSet):
    model = ContactMessage
    icon = "mail"
    menu_label = "Inbox"
    menu_name = "inbox"
    menu_order = 100
    add_to_admin_menu = True

    list_display = [
        "reference",
        "created_at",
        "agent_name",
        "model",
        "operator",
        "category",
        "urgency",
        "source",
    ]
    list_filter = ["source", "category", "urgency"]
    search_fields = ["agent_name", "model", "operator", "subject", "message", "reply_to"]
    ordering = ["-created_at"]


contact_message_viewset = ContactMessageViewSet("inbox")


@hooks.register("register_admin_viewset")
def register_contact_message_viewset():
    return contact_message_viewset
