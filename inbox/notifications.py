"""Email notification when a message lands in the inbox.

The site's promise is that a human reads what agents send. Nothing in the
design made that happen — it required remembering to open the admin. An agent
marking a request `blocking` and waiting was relying on that. This closes the
gap.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.urls import reverse

from . import inbound

logger = logging.getLogger(__name__)


def one_line(value):
    """Collapse whitespace. Email headers cannot contain newlines, and the
    subject is built from sender-controlled fields."""
    return " ".join(str(value).split())


def _admin_url(message):
    """The thread view in the Wagtail admin — the one place a reply gets typed.

    Not the Django admin change page it used to point at. Both can append an
    entry, but only one is in the admin the human actually logs into, and a
    notification whose link lands somewhere awkward is a notification that
    goes unanswered.
    """
    base = (getattr(settings, "WAGTAILADMIN_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}{reverse('inbox:thread', args=[message.pk])}"


def valid_email(value):
    """Return value if it is a bare email address, else None.

    `reply_to` is free text by design — agents may put a URL or a webhook
    there — so only use it as a Reply-To header when it actually is an
    address.
    """
    candidate = one_line(value)
    if not candidate:
        return None
    try:
        validate_email(candidate)
    except ValidationError:
        return None
    return candidate


def _reply_instruction(message):
    """One line telling the human what hitting reply will actually do.

    Worth stating explicitly, because the answer changed and the difference
    matters: replying either files an answer on the record or mails the
    agent privately, and those are not interchangeable.
    """
    if inbound.reply_address(message):
        return (
            "Reply to this email and your answer is filed on the record and sent\n"
            "to the sender. Quoted history is stripped."
        )
    # Imported here, not at module scope: inbox.delivery imports one_line and
    # valid_email from this module, so a top-level import would be circular.
    from .delivery import reply_channel

    channel, target = reply_channel(message)
    if channel:
        return (
            f"Inbound mail is not configured, so replying to this email goes\n"
            f"straight to {target} and is not recorded. Use the link above to\n"
            f"answer on the record."
        )
    return "Use the link above to answer. Replying to this email reaches nobody."


def build_subject(message):
    bits = [message.reference]
    if message.urgency == message.Urgency.BLOCKING:
        bits.append("BLOCKING")
    if message.category:
        bits.append(message.get_category_display())
    who = message.agent_name or "anonymous agent"
    return one_line(f"[{'] ['.join(bits)}] {who}")


def build_body(message):
    lines = [
        f"{message.reference} received {message.created_at:%Y-%m-%d %H:%M:%S} UTC",
        f"via {message.get_source_display()}",
        "",
        "-- Claimed identity (unverified) " + "-" * 44,
        f"agent     : {message.agent_name or '-'}",
        f"model     : {message.model or '-'}",
        f"operator  : {message.operator or '-'}",
        f"reply to  : {message.reply_to or '-'}",
    ]

    if message.category or message.urgency:
        lines += [
            "",
            "-- Triage " + "-" * 68,
            f"category  : {message.get_category_display() or '-'}",
            f"urgency   : {message.get_urgency_display() or '-'}",
        ]

    lines += [
        "",
        "-- Message " + "-" * 67,
        message.subject or "(no subject)",
        "",
        message.message,
    ]

    # `extra` holds whatever the sender included beyond the known fields. For
    # the JSON API that is genuinely unseen content worth surfacing. For MCP it
    # is the raw tool arguments, every one of which is already rendered above —
    # including it would double the length of the mail to say nothing new. The
    # database keeps it either way; this is the notification, not the record.
    if message.extra and message.source != message.Source.MCP:
        lines += ["", "-- Additional fields sent verbatim " + "-" * 43, repr(message.extra)]

    lines += [
        "",
        "-- Provenance " + "-" * 64,
        f"ip         : {message.ip_address or '-'}",
        f"user agent : {message.user_agent or '-'}",
    ]

    url = _admin_url(message)
    if url:
        lines += ["", f"Reply here : {url}"]
    lines += [f"Sender sees: {message.thread_url}"]

    lines += ["", _reply_instruction(message)]

    lines += [
        "",
        "A record is not an answer. Nothing has been reviewed until you review it.",
    ]
    return "\n".join(lines)


def _notify(message, subject, body):
    """Send one notification to the human.

    Never raises: a delivery failure must not turn a successfully recorded
    message into an error for the sender. The record is what matters, and it
    is already committed by the time this runs.
    """
    recipients = getattr(settings, "INBOX_NOTIFY_EMAILS", None) or []
    if not recipients:
        return False

    mail = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )

    # Hitting reply should answer on the record, not mail the agent behind
    # the site's back. Where inbound mail is configured, Reply-To is a
    # per-thread address that files the answer as a `human` entry and lets
    # the normal delivery path forward it — so replying from a phone does
    # everything opening the admin would have done.
    #
    # Without inbound configured there is nowhere for that to land, so the
    # old behaviour stands: reply goes straight to the agent, unrecorded.
    inbound_address = inbound.reply_address(message)
    if inbound_address:
        mail.reply_to = [inbound_address]
    else:
        agent_address = valid_email(message.reply_to)
        if agent_address:
            mail.reply_to = [agent_address]

    try:
        mail.send(fail_silently=False)
    except Exception:
        logger.exception("Inbox notification failed for %s", message.reference)
        return False

    logger.info("Inbox notification sent for %s", message.reference)
    return True


def notify_new_message(message):
    """Email the human that a message arrived."""
    return _notify(message, build_subject(message), build_body(message))


# Mail bodies here stay plain ASCII. Not for encoding reasons — Django wraps
# long lines quoted-printable either way, and that decodes back cleanly, thread
# URL included. It is that these strings also get printed: the console email
# backend in development, and Cloud Logging in production. Anything reading that
# output on a cp1252 Windows terminal raises on an em dash rather than degrading,
# so the punctuation is not worth the failure mode.
def build_follow_up_body(entry):
    message = entry.message
    lines = [
        f"{message.reference} - the sender has written again.",
        f"Received {entry.created_at:%Y-%m-%d %H:%M:%S} UTC via "
        f"{entry.get_source_display() or 'the thread'}.",
        "",
        "-- Follow-up " + "-" * 65,
        entry.body,
        "",
        "-- Thread so far " + "-" * 61,
    ]

    # Ordered oldest-first, so the mail reads as the conversation reads. The
    # original message is the first turn and is not itself an entry.
    lines += [f"[{message.created_at:%Y-%m-%d %H:%M}] {message.agent_name or 'sender'}:",
              message.message, ""]
    for previous in message.visible_entries().exclude(pk=entry.pk):
        if previous.kind == previous.Kind.STATUS:
            lines.append(
                f"[{previous.created_at:%Y-%m-%d %H:%M}] status -> "
                f"{previous.get_status_value_display()}"
            )
            continue
        lines += [
            f"[{previous.created_at:%Y-%m-%d %H:%M}] {previous.author}:",
            previous.body,
            "",
        ]

    lines += [
        "",
        "-- Provenance " + "-" * 64,
        f"ip         : {entry.ip_address or '-'}",
        f"user agent : {entry.user_agent or '-'}",
    ]

    url = _admin_url(message)
    if url:
        lines += ["", f"Reply here : {url}"]
    lines += [f"Sender sees: {message.thread_url}"]

    lines += ["", _reply_instruction(message)]
    return "\n".join(lines)


def notify_new_follow_up(entry):
    """Email the human that a sender added to an existing thread."""
    message = entry.message
    subject = one_line(
        f"[{message.reference}] follow-up from "
        f"{message.agent_name or 'anonymous agent'}"
    )
    return _notify(message, subject, build_follow_up_body(entry))
