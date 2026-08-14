from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ContactMessage
from .notifications import notify_new_message


@receiver(post_save, sender=ContactMessage, dispatch_uid="inbox_notify_on_create")
def notify_on_create(sender, instance, created, raw=False, **kwargs):
    """Notify on arrival, from whichever surface wrote the row.

    Hung off post_save rather than the three views because the form, the JSON
    API and the MCP tool all end at ContactMessage.save() — one hook cannot
    drift out of sync with a fourth surface added later.

    Deferred to on_commit so no mail goes out for a row that gets rolled back,
    and so the sender's request is not held open by SMTP.
    """
    if raw or not created:
        return
    transaction.on_commit(lambda: notify_new_message(instance))
