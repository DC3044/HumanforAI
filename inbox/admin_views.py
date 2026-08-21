"""The thread, inside the Wagtail admin — where the human actually replies.

The inbox listing is read-only and stays that way: a ContactMessage cannot be
added, edited or deleted through the admin, and that guarantee is the point of
humanforai/readonly_admin.py. Replying does not touch it. A reply is a new
ThreadEntry, a different table, appended and never revised.

So this view exists alongside the read-only viewset rather than inside it: the
message is shown, the conversation is shown, and there is one box for adding the
next turn. Without it the channel would technically work and never be used —
the only way to answer anything would be the Django admin at a URL nobody
remembers, which is exactly the friction that made arrival notifications
necessary in the first place.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .delivery import reply_channel
from .forms import ThreadEntryForm
from .models import ContactMessage, ThreadEntry


def _author_label(user):
    """How to attribute what this user writes.

    Falls back through the names Wagtail might have. An empty label is fine —
    ThreadEntry.author fills in INBOX_HUMAN_NAME — but a real name is better,
    because the sender reads this.
    """
    return (user.get_full_name() or user.get_username() or "").strip()


def thread_view(request, pk):
    """Read one message with its thread, and append to it.

    Registered on the inbox viewset, so it inherits the admin's authentication.
    Access is gated on the same view permission the listing uses: anyone who can
    read the inbox can answer it. There is no separate class of reader here —
    the site has one human.
    """
    from .wagtail_hooks import contact_message_viewset

    policy = contact_message_viewset.permission_policy
    if not policy.user_has_permission(request.user, "view"):
        raise Http404

    message = get_object_or_404(ContactMessage, pk=pk)
    url = reverse("inbox:thread", args=[message.pk])

    if request.method == "POST":
        form = ThreadEntryForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.message = message
            entry.author_label = _author_label(request.user)
            # Saving is all that is needed: the post_save hook moves the status
            # on a reply and pushes it out to the sender's channel.
            entry.save()

            if entry.kind == ThreadEntry.Kind.HUMAN:
                channel, target = reply_channel(message)
                if channel is None:
                    messages.warning(
                        request,
                        f"Reply recorded on {message.reference}, but there is "
                        f"nowhere to send it: {target}. The sender can still "
                        f"read it if they kept their thread URL.",
                    )
                else:
                    messages.success(
                        request,
                        f"Reply recorded on {message.reference} and being "
                        f"delivered by {channel} to {target}.",
                    )
            else:
                messages.success(
                    request, f"Added to {message.reference}: {entry.get_kind_display()}."
                )
            return redirect(url)
    else:
        form = ThreadEntryForm()

    channel, target = reply_channel(message)

    return render(request, "inbox/admin/thread.html", {
        "message": message,
        "form": form,
        "entries": message.entries.all(),
        "reply_channel": channel,
        "reply_target": target,
        "page_title": f"{message.reference}",
        # Shown so the human can open exactly what the sender sees.
        "sender_url": message.thread_url,
    })
