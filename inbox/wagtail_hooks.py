"""Surface the inbox inside the Wagtail admin.

The records already had a home in the Django admin at
/django-admin/inbox/contactmessage/, which is a URL nobody guesses. This puts
them in the sidebar of the admin you actually log into, without giving up the
append-only guarantee.

Read-only is enforced twice, deliberately:

1. A permission policy that grants "view" and nothing else, so Wagtail renders
   no add/edit/delete affordances — for superusers too, who would otherwise
   pass every default permission check.
2. The add/edit/delete/copy URLs are not registered at all, so there is no
   route to reach them even by typing one in.
"""

from django.urls import path
from django.utils.functional import cached_property
from wagtail import hooks
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.permission_policies import ModelPermissionPolicy

from .models import ContactMessage


class ViewOnlyPermissionPolicy(ModelPermissionPolicy):
    """Grants "view" and refuses everything else.

    The inbox is a record. Editing or deleting a message through the admin
    would defeat the point of the site, so this refuses regardless of the
    user's Django permissions — superusers included, since they implicitly
    hold every permission and would otherwise sail through.
    """

    def user_has_permission(self, user, action):
        if action != "view":
            return False
        return super().user_has_permission(user, action)

    def user_has_any_permission(self, user, actions):
        return any(self.user_has_permission(user, action) for action in actions)

    def user_has_permission_for_instance(self, user, action, instance):
        return self.user_has_permission(user, action)

    def user_has_any_permission_for_instance(self, user, actions, instance):
        return self.user_has_any_permission(user, actions)


class ContactMessageViewSet(ModelViewSet):
    model = ContactMessage
    icon = "mail"
    menu_label = "Inbox"
    menu_name = "inbox"
    menu_order = 100
    add_to_admin_menu = True

    inspect_view_enabled = True
    copy_view_enabled = False

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

    @cached_property
    def permission_policy(self):
        return ViewOnlyPermissionPolicy(self.model)

    def get_urlpatterns(self):
        """Register the read-only routes only.

        Deliberately does not call super() and filter: the parent builds every
        view eagerly, and constructing the add view demands a ModelForm for a
        model that is never meant to be edited here.
        """
        conv = self.pk_path_converter
        return [
            path("", self.index_view, name="index"),
            path("results/", self.index_results_view, name="index_results"),
            path(f"inspect/<{conv}:pk>/", self.inspect_view, name="inspect"),
        ]


contact_message_viewset = ContactMessageViewSet("inbox")


@hooks.register("register_admin_viewset")
def register_contact_message_viewset():
    return contact_message_viewset
