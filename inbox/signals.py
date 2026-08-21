from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ContactMessage, ThreadEntry
from .notifications import notify_new_follow_up, notify_new_message


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


# Statuses the human has settled deliberately. A reply added afterwards is a
# postscript to a decision already taken, so it must not quietly reopen it.
TERMINAL_STATUSES = (
    ContactMessage.Status.ANSWERED,
    ContactMessage.Status.DECLINED,
    ContactMessage.Status.CLOSED,
)


@receiver(post_save, sender=ThreadEntry, dispatch_uid="inbox_thread_entry_effects")
def thread_entry_effects(sender, instance, created, raw=False, **kwargs):
    """What follows from appending an entry.

    Two consequences, both of them appends:

    - A reply from the human means the request is answered. Recorded as its own
      status entry rather than a field write, so the thread shows when the
      status moved and what moved it.
    - A follow-up from the sender is new inbound mail, and gets notified the way
      an arriving message does. Without this a conversation could stall simply
      because nothing told the human their reply had been answered.
    """
    if raw or not created:
        return

    if instance.kind == ThreadEntry.Kind.HUMAN:
        if instance.message.status not in TERMINAL_STATUSES:
            ThreadEntry.objects.create(
                message=instance.message,
                kind=ThreadEntry.Kind.STATUS,
                status_value=ContactMessage.Status.ANSWERED,
                author_label=instance.author_label,
                body=f"Answered by {instance.author}.",
            )
        # Pushed out on commit, not before: the reply must be readable on the
        # thread before anything tells the sender to go and read it. Imported
        # here because inbox.delivery imports from this app's models.
        from .delivery import deliver_reply

        transaction.on_commit(lambda: deliver_reply(instance))

    elif instance.kind == ThreadEntry.Kind.AGENT:
        transaction.on_commit(lambda: notify_new_follow_up(instance))
