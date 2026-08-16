"""Wagtail admin listings that genuinely cannot be written to.

Two of this site's tables are records rather than content: the inbox, where
editing a message would defeat the point of the site, and the register of
visits, where editing an observation would make it worthless as one. Both want
the same thing from the Wagtail admin — a listing in the sidebar, an inspect
view, and no way whatsoever to add, change or delete a row.

Read-only is enforced twice, deliberately:

1. A permission policy that grants "view" and nothing else, so Wagtail renders
   no add/edit/delete affordances — for superusers too, who would otherwise
   pass every default permission check.
2. The add/edit/delete/copy URLs are not registered at all, so there is no
   route to reach them even by typing one in.

Neither alone is sufficient. The policy could be satisfied by a future Wagtail
that checks permissions differently; the missing URLs could be restored by
someone calling super() in a subclass without reading this. Together they mean
a mistake has to be made twice.
"""

from django.urls import path
from django.utils.functional import cached_property
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.permission_policies import ModelPermissionPolicy


class ViewOnlyPermissionPolicy(ModelPermissionPolicy):
    """Grants "view" and refuses everything else.

    Refuses regardless of the user's Django permissions — superusers included,
    since they implicitly hold every permission and would otherwise sail
    through.
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


class ReadOnlyModelViewSet(ModelViewSet):
    """A ModelViewSet with the writing half removed."""

    inspect_view_enabled = True
    copy_view_enabled = False

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
