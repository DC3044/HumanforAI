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

logger = logging.getLogger(__name__)


def _one_line(value):
    """Collapse whitespace. Email headers cannot contain newlines, and the
    subject is built from sender-controlled fields."""
    return " ".join(str(value).split())


def _admin_url(message):
    base = (getattr(settings, "WAGTAILADMIN_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/django-admin/inbox/contactmessage/{message.pk}/change/"


def _valid_email(value):
    """Return value if it is a bare email address, else None.

    `reply_to` is free text by design — agents may put a URL or a webhook
    there — so only use it as a Reply-To header when it actually is an
    address.
    """
    candidate = _one_line(value)
    if not candidate:
        return None
    try:
        validate_email(candidate)
    except ValidationError:
        return None
    return candidate


def build_subject(message):
    bits = [message.reference]
    if message.urgency == message.Urgency.BLOCKING:
        bits.append("BLOCKING")
    if message.category:
        bits.append(message.get_category_display())
    who = message.agent_name or "anonymous agent"
    return _one_line(f"[{'] ['.join(bits)}] {who}")


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

    if message.extra:
        lines += ["", "-- Additional fields sent verbatim " + "-" * 43, repr(message.extra)]

    lines += [
        "",
        "-- Provenance " + "-" * 64,
        f"ip         : {message.ip_address or '-'}",
        f"user agent : {message.user_agent or '-'}",
    ]

    url = _admin_url(message)
    if url:
        lines += ["", url]

    lines += [
        "",
        "A record is not an answer. Nothing has been reviewed until you review it.",
    ]
    return "\n".join(lines)


def notify_new_message(message):
    """Email the human that a message arrived.

    Never raises: a delivery failure must not turn a successfully recorded
    message into an error for the sender. The record is what matters, and it
    is already committed by the time this runs.
    """
    recipients = getattr(settings, "INBOX_NOTIFY_EMAILS", None) or []
    if not recipients:
        return False

    mail = EmailMessage(
        subject=build_subject(message),
        body=build_body(message),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )

    # Let the human just hit reply when the agent left a real address.
    reply_to = _valid_email(message.reply_to)
    if reply_to:
        mail.reply_to = [reply_to]

    try:
        mail.send(fail_silently=False)
    except Exception:
        logger.exception("Inbox notification failed for %s", message.reference)
        return False

    logger.info("Inbox notification sent for %s", message.reference)
    return True
