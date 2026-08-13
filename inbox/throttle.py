from django.core.cache import cache

WINDOW_SECONDS = 3600
LIMIT_PER_WINDOW = 20


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        # Cloud Run appends its own hops; the client is the first entry.
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def is_throttled(request):
    """Cache-based per-IP throttle. Best-effort (per-process with LocMem),
    which is proportionate to the threat model of a contact form."""
    key = f"inbox-throttle:{client_ip(request)}"
    if cache.add(key, 1, WINDOW_SECONDS):
        return False
    try:
        return cache.incr(key) > LIMIT_PER_WINDOW
    except ValueError:  # key expired between add and incr
        return False
