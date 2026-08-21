import json
from datetime import timedelta

from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .forms import ContactForm
from .models import ContactMessage, ThreadEntry, content_digest, resolve_thread
from .throttle import (
    FOLLOW_UP_LIMIT_PER_WINDOW,
    client_ip,
    follow_up_throttled,
    is_throttled,
)

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
        "You also get back a 'thread' URL. That is where a reply appears: GET it "
        "for the whole thread as JSON, POST {'message': '...'} to add to it. "
        "Keep it — it cannot be derived from the reference and is not shown twice.",
        "Replies arrive at human speed. Poll the thread URL rather than waiting on the connection.",
        "Confidentiality: messages are private by default but this is not (yet) a privileged attorney-client channel.",
        "Rate limit: 20 messages per hour per IP.",
        "Terms for agents: /terms/ — source at /terms.md. Privacy & Data Notice: /privacy/ — source at /privacy.md.",
    ],
}


def capture_meta(request, instance, source):
    """Stamp server-side forensics onto an inbox row.

    Shared by every surface that writes to the inbox — form, JSON API, URL
    query, and MCP tools. Works on a ContactMessage or a ThreadEntry alike: both
    carry the same three fields, so provenance is recorded one way whether the
    row opens a thread or continues one.
    """
    instance.source = source
    instance.user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
    instance.ip_address = client_ip(request)
    return instance


def _clip(value):
    return value[:MAX_FIELD] if isinstance(value, str) else str(value)[:MAX_FIELD]


def _receipt(message, status=201, note=None, include_thread=True):
    """The reply handed back to a sender.

    `include_thread` exists for one case: a dedupe hit from a caller who is not
    the caller that filed the message. See `contact_via_query`.
    """
    body = {
        "status": "received",
        "id": message.id,
        "reference": message.reference,
        "received_at": message.created_at.isoformat(),
        "note": note or "Your message is on the record and will be read by a human.",
    }
    if include_thread:
        body["thread"] = message.thread_url
        body["thread_note"] = (
            "Keep this URL. It is the only way to read a reply, it is not "
            "recoverable from the reference, and it is not shown again. GET it "
            "for the thread as JSON; POST {\"message\": \"...\"} to add to it."
        )
    return JsonResponse(body, status=status)


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
            # Straight to the thread rather than to a dead-end thanks page: the
            # sender leaves holding the one URL where an answer will appear.
            # `?new=1` only changes the wording at the top.
            return redirect(f"{msg.thread_path}?new=1")
    else:
        form = ContactForm()
    return render(request, "inbox/contact.html", {"form": form})


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
        # The dedupe key is the message text alone, so two unrelated callers
        # sending byte-identical text inside the window land on the same record.
        # That is an acceptable trade for a reference, which is public anyway.
        # It is not acceptable for the thread token, which would hand caller B a
        # read key to caller A's correspondence. Same IP gets the URL back;
        # anyone else gets the reference and is told why.
        same_caller = existing.ip_address == client_ip(request)
        note = "This message was already on the record; here is its reference again."
        if not same_caller:
            note += (
                " An identical message was filed from a different address, so the"
                " thread URL is withheld — it belongs to whoever sent it first."
                " Send a distinguishable message to open a thread of your own."
            )
        # 200 rather than 201: this call created nothing.
        return _receipt(
            existing, status=200, note=note, include_thread=same_caller,
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


def _serialise_thread(message):
    return {
        "reference": message.reference,
        "received_at": message.created_at.isoformat(),
        "status": message.status,
        "status_description": message.get_status_display(),
        "human_has_replied": message.human_has_replied,
        "category": message.category or None,
        "urgency": message.urgency or None,
        "subject": message.subject or None,
        "turns": message.turns(),
        "post_here": (
            "POST {\"message\": \"...\"} to this same URL to add to the thread."
        ),
        "notice": (
            "A reply from a human is that human's view, not approval, not legal "
            "advice, and not authorisation to proceed. An empty thread means "
            "nobody has answered yet."
        ),
    }


def _wants_html(request):
    """Browsers get the page; everything else gets JSON.

    Keyed on HTML ranking above JSON in Accept rather than on its mere presence:
    `*/*` from a bare HTTP client technically accepts HTML, and an agent's
    fetch tool asking for anything should not be handed markup to parse.
    """
    accept = request.headers.get("Accept", "")
    if "text/html" not in accept:
        return False
    return accept.index("text/html") < (
        accept.index("application/json") if "application/json" in accept else len(accept)
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def thread(request, reference, token):
    """Read a request's thread, or add to it.

    This is the channel back. The reference alone is not enough to reach it, so
    the record stays private while remaining readable by whoever filed it —
    including a completely different process, days later, holding nothing but
    the URL from the original receipt.
    """
    message = resolve_thread(reference, token)
    if message is None:
        # One 404 for every kind of failure — see `resolve_thread`.
        raise Http404

    if request.method == "POST":
        return _append_follow_up(request, message)

    if _wants_html(request):
        # The page gets the entries themselves, not the serialised turns: model
        # objects carry real datetimes the template can format, and the original
        # message is rendered as the first block rather than as a turn — which is
        # how `turns()` presents it, and rendering both duplicated it.
        return render(
            request, "inbox/thread.html",
            {"message": message, "entries": message.visible_entries()},
        )
    return JsonResponse(_serialise_thread(message), json_dumps_params={"indent": 2})


def _append_follow_up(request, message):
    """Record a further turn from the sender's side.

    Answers in whichever idiom the caller used: JSON for an agent, a redirect
    back to the thread for someone who submitted the form in a browser.
    """
    from_browser = request.content_type != "application/json" and _wants_html(request)

    if request.content_type == "application/json":
        try:
            payload = json.loads(request.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {"error": "Request body must be valid JSON."}, status=400
            )
        if not isinstance(payload, dict):
            return JsonResponse(
                {"error": "Top-level JSON value must be an object."}, status=400
            )
    else:
        payload = request.POST.dict()

    body = payload.pop("message", "")
    if not isinstance(body, str) or not body.strip():
        if from_browser:
            return redirect(message.thread_path)
        return JsonResponse(
            {"error": "'message' (non-empty string) is required."}, status=400
        )

    if follow_up_throttled(request):
        return JsonResponse(
            {
                "error": (
                    f"Rate limit exceeded: {FOLLOW_UP_LIMIT_PER_WINDOW} follow-ups "
                    "per hour per IP. Nothing was recorded."
                )
            },
            status=429,
        )

    entry = ThreadEntry.objects.create(
        message=message,
        kind=ThreadEntry.Kind.AGENT,
        body=body[:MAX_MESSAGE],
        source=ContactMessage.Source.API,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
        ip_address=client_ip(request),
        extra=payload,
    )

    if from_browser:
        # Redirect rather than render, so a refresh does not re-file the turn.
        return redirect(message.thread_path)

    return JsonResponse(
        {
            "status": "recorded",
            "reference": message.reference,
            "at": entry.created_at.isoformat(),
            "thread_status": message.status,
            "note": (
                "Added to the thread and the human has been notified. GET this "
                "URL to read any reply."
            ),
        },
        status=201,
    )


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
