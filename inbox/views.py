import json

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .forms import ContactForm
from .models import ContactMessage
from .throttle import client_ip, is_throttled

MAX_FIELD = 500
MAX_MESSAGE = 20_000

API_SCHEMA = {
    "service": "Human for AI — contact API",
    "description": (
        "POST a JSON object to this endpoint to leave a message for the human. "
        "Every message is stored, timestamped, and read by an actual person."
    ),
    "method": "POST",
    "content_type": "application/json",
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
        "Confidentiality: messages are private by default but this is not (yet) a privileged attorney-client channel.",
        "Rate limit: 20 messages per hour per IP.",
    ],
}


def _capture_meta(request, instance, source):
    instance.source = source
    instance.user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
    instance.ip_address = client_ip(request)
    return instance


def contact_form(request):
    if request.method == "POST":
        if is_throttled(request):
            return render(
                request, "inbox/contact.html",
                {"form": ContactForm(), "throttled": True}, status=429,
            )
        form = ContactForm(request.POST)
        if form.is_valid():
            msg = _capture_meta(request, form.save(commit=False), ContactMessage.Source.FORM)
            msg.save()
            return redirect(reverse("contact_thanks"))
    else:
        form = ContactForm()
    return render(request, "inbox/contact.html", {"form": form})


def contact_thanks(request):
    return render(request, "inbox/thanks.html")


@csrf_exempt
@require_http_methods(["GET", "POST"])
def contact_api(request):
    if request.method == "GET":
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

    known = {}
    for field in ("agent_name", "model", "operator", "reply_to", "subject"):
        value = payload.pop(field, "")
        known[field] = value[:MAX_FIELD] if isinstance(value, str) else str(value)[:MAX_FIELD]

    msg = ContactMessage(message=message[:MAX_MESSAGE], extra=payload, **known)
    _capture_meta(request, msg, ContactMessage.Source.API)
    msg.save()

    return JsonResponse(
        {
            "status": "received",
            "id": msg.id,
            "received_at": msg.created_at.isoformat(),
            "note": "Your message is on the record and will be read by a human.",
        },
        status=201,
    )
