"""The endpoint Resend posts an inbound email to.

Resend signs every webhook (Svix), which is the main reason this is worth
preferring to the alternative: authenticity rests on a real signature rather
than on a secret smuggled through the URL. The per-thread address key and the
sender allow-list remain, but as defence in depth rather than as the whole
defence.

The payload carries **metadata only** — from, to, subject, an email id. Resend
does not include the body, because a webhook has to fit in a serverless request.
So the body is fetched back over the API, which introduces a failure mode the
rest of this app does not have: the notification arrives, the fetch fails, and
the human's answer would be lost.

That shapes how this endpoint answers, and the distinction is deliberate:

* A **refusal** — unknown thread, unauthorised sender, nothing left after
  stripping quotes — answers **200**. It is a decision, not a failure, and
  retrying it only reproduces the same decision.
* A **transient failure** — the body fetch did not work — answers **5xx**, so
  Resend retries on its own schedule. Losing a reply because an API call
  blipped would be the worst outcome available here.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import inbound

logger = logging.getLogger(__name__)

# Where the body is fetched from. The Resend SDK spells this
# `resend.emails.receiving.get(email_id)`; this is the REST path that maps to.
# If Resend moves it, every inbound reply fails loudly with this URL in the log
# rather than quietly doing nothing — see `fetch_body`.
RECEIVING_URL = "https://api.resend.com/emails/receiving/{email_id}"

FETCH_TIMEOUT = 10

# How far out of date a signed webhook may be. Svix's own recommendation, and
# what stops a captured request being replayed days later.
TIMESTAMP_TOLERANCE = 5 * 60


def _signing_key():
    """The Svix signing secret, as bytes.

    Resend presents it as `whsec_<base64>`. The prefix is a label, not part of
    the key, and signing with it included silently produces wrong signatures.
    """
    secret = (getattr(settings, "RESEND_WEBHOOK_SECRET", "") or "").strip()
    if not secret:
        return b""
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_"):]
    try:
        return base64.b64decode(secret)
    except (ValueError, TypeError):
        logger.error("RESEND_WEBHOOK_SECRET is not valid base64; cannot verify webhooks.")
        return b""


def verify_signature(headers, body):
    """Whether this request really came from Resend.

    Implements Svix verification directly rather than pulling in the SDK: it is
    an HMAC over `id.timestamp.body` and a constant-time compare, and a
    dependency for twenty lines that must not drift is a poor trade.
    """
    key = _signing_key()
    if not key:
        return False

    message_id = headers.get("svix-id") or headers.get("webhook-id")
    timestamp = headers.get("svix-timestamp") or headers.get("webhook-timestamp")
    signatures = headers.get("svix-signature") or headers.get("webhook-signature") or ""
    if not (message_id and timestamp and signatures):
        return False

    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > TIMESTAMP_TOLERANCE:
        logger.warning("Rejected an inbound webhook %ss out of date.", int(age))
        return False

    signed = message_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + body
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode("utf-8")

    # The header holds space-separated `v1,<signature>` pairs; more than one
    # during a secret rotation, when either may be the valid one.
    for candidate in signatures.split():
        version, _, signature = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return True

    return False


def fetch_body(email_id):
    """The text and HTML parts of a received email, from the Resend API.

    Raises on failure rather than returning empty, because the caller has to
    tell "the human wrote nothing" apart from "we could not find out what the
    human wrote". Those need opposite responses.
    """
    api_key = (getattr(settings, "RESEND_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set; cannot retrieve the body.")

    url = RECEIVING_URL.format(email_id=email_id)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The URL is in the message on purpose: if Resend ever moves this path,
        # this line is what says so, rather than replies quietly vanishing.
        raise RuntimeError(
            f"Resend returned HTTP {exc.code} for {url}. If this is a 404, the "
            f"receiving endpoint has moved and RECEIVING_URL needs updating."
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach {url}: {exc!r}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected response shape from {url}.")

    # Resend has returned the object bare in some versions and wrapped in
    # `data` in others; accept either rather than break on a shape change.
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return body.get("text") or "", body.get("html") or ""


def _accepted(note, **extra):
    return JsonResponse({"status": "accepted", "note": note, **extra}, status=200)


def _refused(note, **extra):
    """Refused deliberately. 200, so Resend does not retry a decision."""
    logger.warning("Inbound mail refused: %s %s", note, extra or "")
    return JsonResponse({"status": "refused", "note": note, **extra}, status=200)


def _try_again(note):
    """Transient. 503, so Resend retries and the reply is not lost."""
    logger.error("Inbound mail deferred: %s", note)
    return JsonResponse({"status": "error", "note": note}, status=503)


@csrf_exempt
@require_POST
def inbound_mail(request):
    """Record an emailed reply against the thread its address names."""
    if not inbound.is_configured():
        raise Http404

    if not verify_signature(request.headers, request.body):
        # 401 rather than 404: this endpoint's existence is not a secret, and a
        # signature failure is worth seeing in Resend's own delivery log.
        logger.warning("Inbound webhook failed signature verification.")
        return HttpResponse("Invalid signature.", status=401)

    try:
        event = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _refused("body was not valid JSON")

    if not isinstance(event, dict):
        return _refused("body was not a JSON object")

    if event.get("type") != "email.received":
        # Resend posts other event types to the same endpoint if configured to.
        # Not an error, just not ours.
        return _accepted("ignored: not an email.received event")

    data = event.get("data")
    if not isinstance(data, dict):
        return _refused("event carried no data object")

    # `to` is a list. Every entry is offered to the resolver, since a reply may
    # be addressed to several places and only one of them is ours.
    recipients = data.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    message = inbound.thread_for_address(" ".join(str(r) for r in recipients))
    if message is None:
        return _refused("no thread matches the recipient address")

    sender = data.get("from", "")
    if not inbound.sender_allowed(sender):
        logger.warning(
            "Inbound mail for %s from unauthorised sender %r",
            message.reference, str(sender)[:200],
        )
        return _refused("sender is not authorised to reply", reference=message.reference)

    email_id = data.get("email_id") or data.get("id")
    if not email_id:
        return _refused("event carried no email id", reference=message.reference)

    try:
        text, html = fetch_body(email_id)
    except RuntimeError as exc:
        # The one case that must not be answered 200. The human wrote a reply
        # and it is sitting in Resend; a retry can still recover it.
        return _try_again(f"could not retrieve the body of {email_id}: {exc}")

    # The text part is what almost every client sends. The HTML part is a
    # fallback for those that send only that, and is flattened first or the
    # record fills with markup.
    body = text if text.strip() else inbound.html_to_text(html)

    entry = inbound.record_reply(
        message,
        body,
        sender=sender,
        subject=data.get("subject", ""),
        raw={"provider": "resend", "email_id": email_id},
    )
    if entry is None:
        return _refused("nothing to record once quoting was stripped",
                        reference=message.reference)

    logger.info("Inbound reply recorded on %s", message.reference)
    return _accepted("reply recorded", reference=message.reference)
