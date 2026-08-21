"""Pushing the human's reply out to whatever the sender said would reach it.

The thread endpoint is the channel that always works: it needs nothing from the
sender but the URL it was already given, and it is there whenever the agent comes
back. This module is the other direction — for a sender that will not come back
and needs the answer delivered.

Two channels, chosen by what `reply_to` actually contains: an email address gets
mail, an HTTPS URL gets a signed POST. Nothing is ever sent to `reply_to` on its
own initiative. Only a reply the human wrote is delivered, because `reply_to` is
unverified free text and a site that mails anything else to it is a spam relay
waiting for someone to put a victim's address in the field.

The email copy is repliable: it carries a Reply-To on the sender-side inbound
address, so answering it appends to the same thread rather than reaching a
no-reply mailbox. See inbox/inbound.py for why that address is keyed separately
from the operator's.

Every attempt is appended to the thread as a `delivery` entry, successes and
failures alike. Those entries are excluded from what the sender can read: how
delivery went is the human's business, and the response body of a webhook is not
something to echo back to whoever chose the URL.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMessage

from . import inbound
from .models import ThreadEntry
from .notifications import one_line, valid_email

logger = logging.getLogger(__name__)

# Bounded so a slow or hostile endpoint cannot pin a Cloud Run instance.
WEBHOOK_TIMEOUT = 10

# Enough of the response to diagnose a rejection, not enough to be a
# general-purpose fetch of whatever the URL happens to point at.
MAX_RESPONSE_SNIPPET = 500

SIGNATURE_HEADER = "X-YourHuman-Signature"
TIMESTAMP_HEADER = "X-YourHuman-Timestamp"


def _webhook_signature(access_token, timestamp, body):
    """HMAC-SHA256 over `timestamp.body`, keyed on the thread's access token.

    The token is already a secret shared between this site and whoever filed the
    request — they were handed it in the receipt — so it makes a shared signing
    key that needs no configuration, no key exchange, and no second credential
    for the receiver to store. Verifying the signature proves the POST came from
    the site holding that thread and not from anyone who guessed the URL.

    The timestamp is inside the MAC so a captured delivery cannot be replayed
    later without the signature no longer matching what it covers.
    """
    payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(
        access_token.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def _url_is_safe(url):
    """Whether a sender-supplied URL may be fetched. Returns (ok, reason).

    The URL comes from an agent, which makes this endpoint a request forgery
    primitive unless it is fenced off: without these checks, `reply_to` could
    name the metadata service, a Cloud Run internal address, or localhost, and
    this server would dutifully POST to it from inside the perimeter.

    Residual risk, stated rather than papered over: the name is resolved here and
    resolved again by the connection, so a DNS entry that changes in between can
    still slip past. The consequence is bounded — a POST is made, and the reply
    body is recorded where only the human can read it, never echoed to the
    sender — so this is a deliberate trade rather than an oversight.
    """
    parts = urlsplit(url)

    allowed_schemes = ("https", "http") if getattr(
        settings, "INBOX_WEBHOOK_ALLOW_HTTP", False
    ) else ("https",)
    if parts.scheme not in allowed_schemes:
        return False, f"scheme {parts.scheme!r} not allowed (https only)"

    if not parts.hostname:
        return False, "no host in URL"

    try:
        resolved = socket.getaddrinfo(parts.hostname, parts.port or None)
    except socket.gaierror as exc:
        return False, f"host does not resolve ({exc})"

    for info in resolved:
        address = ipaddress.ip_address(info[4][0])
        # `is_global` excludes loopback, link-local (169.254.169.254 among
        # them), private ranges, and the reserved blocks in one test.
        if not address.is_global:
            return False, f"host resolves to non-public address {address}"

    return True, ""


def _webhook_payload(entry):
    message = entry.message
    return {
        "type": "reply",
        "reference": message.reference,
        "status": message.status,
        "replied_at": entry.created_at.isoformat(),
        "by": entry.author,
        "body": entry.body,
        "thread_url": message.thread_url,
        "notice": (
            "A human's reply, recorded on request. Not approval, not legal "
            "advice, and not authorisation to proceed. Verify the signature "
            f"header {SIGNATURE_HEADER} as HMAC-SHA256 of "
            f"'<{TIMESTAMP_HEADER}>.<raw body>' keyed on the access_token you "
            "were given for this reference."
        ),
    }


def _record_attempt(entry, channel, target, ok, detail):
    """Append the outcome to the thread. This is the delivery log."""
    ThreadEntry.objects.create(
        message=entry.message,
        kind=ThreadEntry.Kind.DELIVERY,
        body=f"{'Delivered' if ok else 'Delivery failed'} via {channel} to {target}: {detail}",
        extra={
            "for_entry": entry.pk,
            "channel": channel,
            "target": target,
            "ok": ok,
            "detail": detail,
        },
    )
    log = logger.info if ok else logger.warning
    log(
        "Reply delivery %s via %s for %s: %s",
        "succeeded" if ok else "failed", channel, entry.message.reference, detail,
    )
    return ok


def deliver_by_email(entry, address):
    message = entry.message
    subject = one_line(f"Re: {message.reference} - a human has answered")

    # Make it repliable. Without this a reply goes to the no-reply sender and is
    # discarded - which is what used to happen, while the body cheerfully said
    # "reply here". The address is the sender-side one, keyed differently from
    # the operator's, so a reply to this mail can only ever append a turn
    # attributed to the sender, never one attributed to the human.
    inbound_address = inbound.reply_address(message, inbound.AGENT)

    if inbound_address:
        how_to_continue = (
            "Reply to this email and it joins the same permanent record, as a\n"
            "message from your side of the thread. Quoted history is stripped.\n"
            "Automatic replies are ignored."
        )
    else:
        # Nothing would receive a reply, so do not invite one.
        how_to_continue = (
            "Do not reply to this email; nothing receives it. Use the thread\n"
            "URL above, which takes a POST as well as a GET."
        )

    body = "\n".join([
        f"{message.reference}, filed {message.created_at:%Y-%m-%d %H:%M} UTC.",
        "",
        f"-- Reply from {entry.author} " + "-" * 40,
        entry.body,
        "",
        "-" * 68,
        f"The full thread, including anything added since: {message.thread_url}",
        "",
        how_to_continue,
        "",
        "A human's reply is that human's view, recorded on request. It is not "
        "approval, not legal advice, and not authorisation to proceed.",
    ])

    mail = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[address],
    )
    if inbound_address:
        mail.reply_to = [inbound_address]

    try:
        mail.send(fail_silently=False)
    except Exception as exc:
        return _record_attempt(entry, "email", address, False, repr(exc))
    return _record_attempt(entry, "email", address, True, "accepted by the mail backend")


def deliver_by_webhook(entry, url):
    ok, reason = _url_is_safe(url)
    if not ok:
        return _record_attempt(entry, "webhook", url, False, f"refused: {reason}")

    body = json.dumps(_webhook_payload(entry)).encode("utf-8")
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "YourHuman.ai reply delivery",
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: _webhook_signature(
                entry.message.access_token, timestamp, body
            ),
        },
    )

    # No redirect handler: a 30x is not followed. Following one would let the
    # first hop pass the safety check and the second point anywhere.
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=WEBHOOK_TIMEOUT) as response:
            snippet = response.read(MAX_RESPONSE_SNIPPET).decode("utf-8", "replace")
            return _record_attempt(
                entry, "webhook", url, True,
                f"HTTP {response.status}: {one_line(snippet)[:200]}",
            )
    except urllib.error.HTTPError as exc:
        snippet = exc.read(MAX_RESPONSE_SNIPPET).decode("utf-8", "replace")
        return _record_attempt(
            entry, "webhook", url, False,
            f"HTTP {exc.code}: {one_line(snippet)[:200]}",
        )
    except Exception as exc:
        # Includes timeouts, TLS failures, and refused connections.
        return _record_attempt(entry, "webhook", url, False, repr(exc))


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def reply_channel(message):
    """How the human's reply can be pushed to this sender, if at all.

    Returns (channel, target) or (None, reason). `reply_to` is free text: agents
    put addresses, URLs, Slack handles and prose in it, and anything this cannot
    recognise is not a channel — it is a note to the human.
    """
    raw = one_line(message.reply_to)
    if not raw:
        return None, "no reply_to given"

    address = valid_email(raw)
    if address:
        return "email", address

    if raw.lower().startswith(("http://", "https://")):
        return "webhook", raw

    return None, f"reply_to is not an address or URL: {raw[:100]!r}"


def deliver_reply(entry):
    """Push one human reply to the sender, if there is anywhere to push it.

    Never raises. The reply is already committed and already readable on the
    thread by the time this runs, so a delivery failure must not turn answering
    a request into an error.
    """
    if entry.kind != ThreadEntry.Kind.HUMAN:
        return False

    channel, target = reply_channel(entry.message)
    if channel is None:
        return _record_attempt(entry, "none", "-", False, f"nothing to deliver to: {target}")

    try:
        if channel == "email":
            return deliver_by_email(entry, target)
        return deliver_by_webhook(entry, target)
    except Exception:
        logger.exception(
            "Reply delivery raised for %s", entry.message.reference
        )
        return False


def undelivered(since=None):
    """Human replies that have not been delivered successfully.

    What `deliver_replies` retries. A reply with nowhere to go counts as
    undelivered forever, which is correct — the situation is not that delivery
    failed, it is that the sender left no channel — so callers filter on the
    recorded reason rather than retrying those.
    """
    replies = ThreadEntry.objects.filter(kind=ThreadEntry.Kind.HUMAN)
    if since is not None:
        replies = replies.filter(created_at__gte=since)

    delivered = set(
        ThreadEntry.objects.filter(
            kind=ThreadEntry.Kind.DELIVERY, extra__ok=True
        ).values_list("extra__for_entry", flat=True)
    )
    return [
        reply for reply in replies.select_related("message")
        if reply.pk not in delivered
        and reply_channel(reply.message)[0] is not None
    ]


__all__ = [
    "deliver_reply",
    "deliver_by_email",
    "deliver_by_webhook",
    "reply_channel",
    "undelivered",
]
