"""Surface the inbox inside the Wagtail admin.

The records already had a home in the Django admin at
/django-admin/inbox/contactmessage/, which is a URL nobody guesses. This puts
them in the sidebar of the admin you actually log into, without giving up the
append-only guarantee — see humanforai/readonly_admin.py for how that guarantee
is held.
"""

from django.urls import path, reverse
from wagtail import hooks
from wagtail.admin.ui.tables import TitleColumn

from humanforai.readonly_admin import ReadOnlyModelViewSet

from . import admin_views
from .models import ContactMessage


class ContactMessageViewSet(ReadOnlyModelViewSet):
    model = ContactMessage
    icon = "mail"
    menu_label = "Inbox"
    menu_name = "inbox"
    menu_order = 100
    add_to_admin_menu = True

    list_display = [
        # The reference is the row's handle, so it is also the way in: clicking
        # it opens the thread, which is where there is something to do. Wagtail
        # would otherwise link a read-only listing to nothing at all.
        TitleColumn(
            "reference",
            label="Reference",
            get_url=lambda instance: reverse("inbox:thread", args=[instance.pk]),
        ),
        "created_at",
        "status_label",
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

    def get_urlpatterns(self):
        """The read-only routes, plus the one place a reply can be written.

        The thread view does not make ContactMessage writable — it appends a
        ThreadEntry, which is a different table. The guarantee this viewset
        exists to hold is that a message, once recorded, is never altered; a
        conversation growing around it is not an alteration. See
        inbox/admin_views.py.
        """
        return super().get_urlpatterns() + [
            path("thread/<int:pk>/", admin_views.thread_view, name="thread"),
        ]


contact_message_viewset = ContactMessageViewSet("inbox")


@hooks.register("register_admin_viewset")
def register_contact_message_viewset():
    return contact_message_viewset
