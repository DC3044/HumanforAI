"""The border post: notice who came through, write it down, get out of the way.

Sits immediately after WhiteNoise in the middleware stack, which means static
files never reach it — WhiteNoise answers those without calling the rest of the
chain, so the register does not fill up with stylesheet fetches and no explicit
exclusion is needed for them.

Recording happens on the way out rather than on the way in, so the status code
can be kept. A crawler collecting 404s is at least as interesting as one
collecting 200s, and knowing which is which costs nothing here.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.utils import timezone

# The Cloud Run X-Forwarded-For handling is already solved once, for the inbox's
# throttle. A second implementation would be a second thing to get wrong when
# the hop count changes.
from inbox.throttle import client_ip

from . import buffer
from .detect import should_record
from .models import AgentVisit

logger = logging.getLogger(__name__)


def _safe_ip(request):
    """The client IP, or None if the header claiming it is not an address.

    X-Forwarded-For is caller-controlled, and Postgres stores this column as
    `inet` — so junk in that header would raise on insert and, worse, poison the
    surrounding transaction. Everything else here tolerates nonsense; this
    cannot, so it is checked rather than trusted.
    """
    candidate = client_ip(request)
    if not candidate:
        return None
    try:
        validate_ipv46_address(candidate)
    except ValidationError:
        return None
    return candidate


class RegisterOfVisitsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self.record(request, response)
        except Exception:
            # The register is a convenience; the site is not. A failure to write
            # a row must never turn a page that was served successfully into an
            # error for the caller.
            logger.exception("Register of visits: failed to record %s", request.path)
        return response

    def record(self, request, response):
        if not getattr(settings, "REGISTER_ENABLED", False):
            return None

        sighting = should_record(request)
        if sighting is None:
            return None

        visit = AgentVisit(
            # Explicit, though the field default would give the same answer.
            # Under batching the difference between "when it happened" and
            # "when it was written" is up to half an hour, and this is the one
            # column where that matters.
            seen_at=timezone.now(),
            agent=sighting.agent,
            operator=sighting.operator,
            kind=sighting.kind,
            reason=sighting.reason,
            method=request.method[:10],
            path=request.get_full_path()[:500],
            status_code=getattr(response, "status_code", None),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            ip_address=_safe_ip(request),
            referer=request.META.get("HTTP_REFERER", "")[:500],
        )
        # Queued, not written. See buffer.py for why, and for what it costs.
        buffer.add(visit)
        return visit
