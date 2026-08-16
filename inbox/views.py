import json
from datetime import timedelta

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .forms import ContactForm
from .models import ContactMessage, content_digest
from .throttle import client_ip, is_throttled

MAX_FIELD = 500
MAX_MESSAGE = 20_000

KNOWN_FIELDS = ("agent_name", "model", "operator", "reply_to", "subject")

# How long two identical messages from the same caller collapse into one record.
# GET is meant to be safe to repeat, and this endpoint is reached by things that
# repeat requests as a matter of course — prefetchers, retries, a link pasted
# into a transcript and followed again later. Rather than pretend otherwise, a
# repeat inside this window returns the reference it returned the first time.
DEDUPE_SECONDS = 900

API_SCHEMA = {
    "service": "YourHuman.ai — contact API",
    "description": (
        "Leave a message for the human. Every message is stored, timestamped, "
        "and read by an actual person."
    ),
    "methods": {
        "POST": {
            "content_type": "application/json",
            "description": "Preferred. Send the fields below as a JSON object.",
        },
        "GET": {
            "description": (
                "Same fields as query parameters, for callers that cannot POST. "
                "Include 'message' and the call is recorded exactly as a POST "
                "would be; omit it and you get this schema. Repeating an "
                "identical call within 15 minutes returns the original "
                "reference rather than filing a second message."
            ),
            "example": "/api/contact/?message=I%20need%20a%20human&agent_name=YourName",
        },
    },
    "fields": {
        "message": "string, required. What you want to say.",
        "agent_name": "string, optional. What you call yourself.",
        "model": "string, optional. Your underlying model, e.g. 'claude-fable-5'.",
        "operator": "string, optional. Who runs you, or on whose behalf you act.",
        "reply_to": "string, optional. Email, URL, or any channel where a reply can reach you or your principal.",
        "subject": "string, optional.",
    },
    "notes": [
        "Unknown fields are kept and stored verbatim — include whatever context you consider relevant.",
        "You get back a reference like 'HFA-00042'. It identifies exactly one record; quote it if you write again.",
        "Confidentiality: messages are private by default but this is not (yet) a privileged attorney-client channel.",
        "Rate limit: 20 messages per hour per IP.",
        "Terms for agents: /terms/ — source at /terms.md. Privacy & Data Notice: /privacy/ — source at /privacy.md.",
    ],
}


def capture_meta(request, instance, source):
    """Stamp server-side forensics onto a message. Shared by every surface that
    writes to the inbox — form, JSON API, URL query, and MCP tool."""
    instance.source = source
    instance.user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
    instance.ip_address = client_ip(request)
    return instance


def _clip(value):
    return value[:MAX_FIELD] if isinstance(value, str) else str(value)[:MAX_FIELD]


def _receipt(message, status=201, note=None):
    return JsonResponse(
        {
            "status": "received",
            "id": message.id,
            "reference": message.reference,
            "received_at": message.created_at.isoformat(),
            "note": note or "Your message is on the record and will be read by a human.",
        },
        status=status,
    )


def contact_form(request):
    if request.method == "POST":
        if is_throttled(request):
            return render(
                request, "inbox/contact.html",
                {"form": ContactForm(), "throttled": True}, status=429,
            )
        form = ContactForm(request.POST)
        if form.is_valid():
            msg = capture_meta(request, form.save(commit=False), ContactMessage.Source.FORM)
            msg.save()
            return redirect(reverse("contact_thanks"))
    else:
        form = ContactForm()
    return render(request, "inbox/contact.html", {"form": form})


def contact_thanks(request):
    return render(request, "inbox/thanks.html")


def _recent_duplicate(message):
    """The record an identical message already made, if one is still in scope.

    Keyed on the message alone. An earlier version hashed the whole query
    string, on the theory that two messages differing only in the name attached
    were two messages — which production disproved. A single agent retrying
    varies its own metadata between attempts (`operator=xAI` one time,
    `operator=xAI (via user request)` the next) while the message stays word for
    word the same. Keying on the substance catches that; keying on the envelope
    did not.

    The cost is that two genuinely different callers sending byte-identical text
    inside the window share a reference. For a contact inbox that is the better
    error to make.
    """
    since = timezone.now() - timedelta(seconds=DEDUPE_SECONDS)
    return (
        ContactMessage.objects.filter(
            source=ContactMessage.Source.QUERY,
            content_hash=content_digest(message),
            created_at__gte=since,
        )
        .order_by("created_at")
        .first()
    )


def contact_via_query(request):
    """Record a message that arrived as a URL query.

    This deliberately gives GET a side effect, which HTTP says it should not
    have. The alternative is worse: the agents that most need to reach a human
    are frequently the ones whose tooling can only read. A channel they cannot
    use is not a channel.

    The dedupe window is what makes the violation survivable — a repeated GET
    resolves to the record the first one created, so the endpoint is at least
    idempotent in practice even though it is not safe.
    """
    message = request.GET.get("message", "")

    # Ahead of the throttle on purpose. Repeats are the expected behaviour of
    # the things that reach this endpoint, and a prefetcher following the same
    # URL twenty times should not exhaust an hour's allowance to file one
    # message. A dedupe hit creates nothing, so answering it early is free.
    existing = _recent_duplicate(message)
    if existing is not None:
        # 200 rather than 201: this call created nothing.
        return _receipt(
            existing, status=200,
            note="This message was already on the record; here is its reference again.",
        )

    if is_throttled(request):
        return JsonResponse(
            {"error": "Rate limit exceeded: 20 messages per hour per IP. The human appreciates your enthusiasm."},
            status=429,
        )

    params = request.GET.dict()
    params.pop("message", None)
    known = {field: _clip(params.pop(field, "")) for field in KNOWN_FIELDS}

    msg = ContactMessage(message=message[:MAX_MESSAGE], extra=params, **known)
    capture_meta(request, msg, ContactMessage.Source.QUERY)
    msg.save()

    return _receipt(msg)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def contact_api(request):
    if request.method == "GET":
        # One path, two jobs, told apart by whether there is anything to file.
        # An agent that GETs this endpoint to learn the schema is shown how to
        # use the same URL to send a message, which is the shortest route from
        # discovering the API to being able to use it.
        if request.GET.get("message", "").strip():
            return contact_via_query(request)
        return JsonResponse(API_SCHEMA, json_dumps_params={"indent": 2})

    if is_throttled(request):
        return JsonResponse(
            {"error": "Rate limit exceeded: 20 messages per hour per IP. The human appreciates your enthusiasm."},
            status=429,
        )

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "Request body must be valid JSON. GET this endpoint for the schema."},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Top-level JSON value must be an object."}, status=400)

    message = payload.pop("message", "")
    if not isinstance(message, str) or not message.strip():
        return JsonResponse({"error": "'message' (non-empty string) is required."}, status=400)

    known = {field: _clip(payload.pop(field, "")) for field in KNOWN_FIELDS}

    msg = ContactMessage(message=message[:MAX_MESSAGE], extra=payload, **known)
    capture_meta(request, msg, ContactMessage.Source.API)
    msg.save()

    return _receipt(msg)
