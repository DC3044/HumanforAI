"""Per-IP rate limiting, counted in the database.

This used to be a cache counter, which was wrong in a way that only showed up in
production. Nothing configures CACHES, so Django fell back to LocMemCache — one
cache per process — while the container runs gunicorn with two workers behind an
autoscaling Cloud Run service. Every worker enforced its own private allowance,
so the real limit was 20 per hour multiplied by however many processes happened
to exist. An agent testing the contact endpoint filed 32 messages against a
nominal cap of 20.

The database is the only state every process shares, and at these volumes it
costs one indexed count. Counting rows also changes the meaning slightly, for
the better: the limit is now on messages actually recorded rather than on
requests attempted, so a caller sending malformed requests cannot exhaust its
own allowance without anything to show for it.
"""

from datetime import timedelta

from django.utils import timezone

WINDOW_SECONDS = 3600
LIMIT_PER_WINDOW = 20


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        # Cloud Run appends its own hops; the client is the first entry.
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def recent_from(ip_address, seconds=WINDOW_SECONDS):
    from .models import ContactMessage

    since = timezone.now() - timedelta(seconds=seconds)
    return ContactMessage.objects.filter(ip_address=ip_address, created_at__gte=since)


def is_throttled(request):
    ip_address = client_ip(request)
    if not ip_address:
        # Nothing to count against. Refusing every such caller would break the
        # endpoint for anyone whose proxy strips the header.
        return False
    return recent_from(ip_address).count() >= LIMIT_PER_WINDOW
