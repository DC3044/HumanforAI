"""Deployment checks for the reply channel.

The thread URL is built from `WAGTAILADMIN_BASE_URL`, which means that setting is
no longer only an admin convenience: it is the address every sender is handed as
the one place an answer will appear. Left at its placeholder, the site keeps
working in every visible way — messages record, notifications send, replies get
written — while quietly telling every agent to come back to example.com.

That is the kind of failure nothing surfaces on its own, so it is surfaced here.
"""

from django.conf import settings
from django.core.checks import Warning, register

PLACEHOLDER_HOSTS = ("example.com", "example.org", "localhost", "127.0.0.1")


@register("inbox")
def check_base_url_is_real(app_configs, **kwargs):
    """Warn when the base URL cannot form a thread address a sender could use."""
    if settings.DEBUG:
        # Local development is expected to run on localhost, and does not hand
        # out URLs anyone else has to resolve.
        return []

    base = (getattr(settings, "WAGTAILADMIN_BASE_URL", "") or "").strip()

    if not base:
        return [
            Warning(
                "WAGTAILADMIN_BASE_URL is not set, so thread URLs are handed to "
                "senders as relative paths.",
                hint=(
                    "A relative path is useless in an email or a webhook "
                    "payload. Set it to the site's public origin, e.g. "
                    "https://yourhuman.ai"
                ),
                id="inbox.W001",
            )
        ]

    if any(host in base for host in PLACEHOLDER_HOSTS):
        return [
            Warning(
                f"WAGTAILADMIN_BASE_URL is {base!r}, which looks like a "
                "placeholder. Every sender is being told to collect its answer "
                "from that host.",
                hint=(
                    "Set WAGTAILADMIN_BASE_URL to the site's public origin, "
                    "e.g. https://yourhuman.ai"
                ),
                id="inbox.W002",
            )
        ]

    return []


@register("inbox")
def check_inbound_mail_is_whole(app_configs, **kwargs):
    """Inbound mail has four moving parts, and three of them fail silently.

    A half-configured inbound setup does not raise anything. It changes the
    Reply-To on every notification to an address that either does not resolve or
    resolves somewhere that will refuse the message, so answering by email stops
    working in a way only visible by trying it. These are the combinations worth
    naming.
    """
    from . import inbound

    problems = []
    domain = (getattr(settings, "INBOX_INBOUND_DOMAIN", "") or "").strip()
    secret = (getattr(settings, "INBOX_INBOUND_SECRET", "") or "").strip()
    senders = [
        a for a in (getattr(settings, "INBOX_INBOUND_SENDERS", None) or []) if a.strip()
    ]

    if domain and not secret:
        problems.append(
            Warning(
                "INBOX_INBOUND_DOMAIN is set but INBOX_INBOUND_SECRET is not, so "
                "inbound mail is off and notifications carry no reply address.",
                hint="Set INBOX_INBOUND_SECRET to a long random string.",
                id="inbox.W003",
            )
        )

    if secret and not domain:
        problems.append(
            Warning(
                "INBOX_INBOUND_SECRET is set but INBOX_INBOUND_DOMAIN is not, so "
                "inbound mail is off.",
                hint=(
                    "Set INBOX_INBOUND_DOMAIN to the subdomain whose MX record "
                    'points at Resend, e.g. "parse.yourhuman.ai".'
                ),
                id="inbox.W004",
            )
        )

    if inbound.is_configured() and not senders:
        # The allow-list is what stops a leaked reply address being enough to
        # write into a thread as the human. Empty means nobody is authorised, so
        # every reply is refused — safe, but silently useless.
        problems.append(
            Warning(
                "Inbound mail is configured but INBOX_INBOUND_SENDERS is empty, "
                "so every emailed reply will be refused.",
                hint=(
                    "Set it to the address you reply from, e.g. "
                    "damien.charlotin@gmail.com"
                ),
                id="inbox.W005",
            )
        )

    # The signing secret is what actually authenticates a webhook. Without it
    # every inbound reply is rejected, which is safe and completely silent.
    if inbound.is_configured() and not (
        getattr(settings, "RESEND_WEBHOOK_SECRET", "") or ""
    ).strip():
        problems.append(
            Warning(
                "Inbound mail is configured but RESEND_WEBHOOK_SECRET is not, "
                "so every inbound webhook will fail signature verification.",
                hint=(
                    "Copy the signing secret from the Resend dashboard "
                    "(Webhooks, then your endpoint). It looks like whsec_..."
                ),
                id="inbox.W007",
            )
        )

    # The body of an inbound email is fetched back over the API, so a missing
    # key means replies arrive, verify, and then cannot be read.
    if inbound.is_configured() and not (
        getattr(settings, "RESEND_API_KEY", "") or ""
    ).strip():
        problems.append(
            Warning(
                "Inbound mail is configured but RESEND_API_KEY is not, so the "
                "body of an inbound reply cannot be retrieved.",
                hint="Set RESEND_API_KEY; it also configures outbound SMTP.",
                id="inbox.W008",
            )
        )

    if inbound.is_configured() and len(secret) < 32:
        problems.append(
            Warning(
                f"INBOX_INBOUND_SECRET is {len(secret)} characters. It keys every "
                "reply address and is also the webhook URL segment.",
                hint="Use at least 32 characters, e.g. `openssl rand -hex 32`.",
                id="inbox.W006",
            )
        )

    return problems
