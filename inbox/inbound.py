"""Answering by replying to the notification email.

The admin thread view works, but it still asks the human to open a browser, log
in, and find the request. The notification already lands in a mail client that is
open anyway, so the shortest path to an answer is to hit reply. This is the
plumbing for that: The provider posts the message here, and it becomes
a `human` entry on the right thread, delivered and recorded exactly as one typed
into the admin would be.

Two things had to be got right.

**The address has to name the thread.** Every notification carries a Reply-To of
the form `hfa-00042.<key>@parse.example.com`, so an incoming message identifies
its thread by the address it was sent to. Nothing else in a reply is reliable:
subjects get rewritten, `In-Reply-To` is dropped by some clients, and quoting is
inconsistent.

**The key in that address cannot be the agent's token.** The agent holds
`access_token` for its own thread. If the inbound address were derived from it,
any agent could email in and fabricate a reply *from the human* on its own
record — a worse failure than having no inbound channel at all. So the key is an
HMAC over a server-side secret the agent never sees, and the two credentials have
nothing to do with each other.

Authenticity rests on three independent things: the provider's signature on
the webhook (see inbox/inbound_views.py), the per-thread key in the recipient
address, and an allow-list of sender addresses. The signature is the strong
one; the other two are what make a leaked address insufficient on its own.
"""

import hashlib
import hmac
import html as html_module
import logging
import re

from django.conf import settings

from .models import ContactMessage, ThreadEntry

logger = logging.getLogger(__name__)

# Length of the key embedded in a reply address. Sixteen hex characters is 64
# bits, far beyond guessing for a value that is also useless on its own.
KEY_LENGTH = 16

ADDRESS_RE = re.compile(
    r"(?P<reference>hfa-\d{1,9})\.(?P<key>[0-9a-f]{%d})@" % KEY_LENGTH,
    re.IGNORECASE,
)


def _secret():
    return getattr(settings, "INBOX_INBOUND_SECRET", "") or ""


def is_configured():
    """Whether inbound mail is set up at all.

    Both halves are required: a secret to key the addresses with, and a domain
    to put them on. With either missing there is nowhere for a reply to go, and
    notifications fall back to their previous behaviour.
    """
    return bool(_secret() and getattr(settings, "INBOX_INBOUND_DOMAIN", ""))


def address_key(reference):
    """The per-thread key embedded in a reply address.

    Keyed on INBOX_INBOUND_SECRET, which is server-side and never handed out.
    Deliberately *not* derived from the thread's `access_token`: that value is
    given to the agent, and an agent able to compute its own inbound address
    could post a reply attributed to the human onto its own record.
    """
    return hmac.new(
        _secret().encode("utf-8"),
        f"inbound:{reference.upper()}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:KEY_LENGTH]


def reply_address(message):
    """Where a reply to this thread's notification should be sent, or "".

    Empty when inbound mail is not configured, which callers read as "fall back
    to whatever you did before".
    """
    if not is_configured():
        return ""
    domain = settings.INBOX_INBOUND_DOMAIN.strip().lstrip("@")
    return f"{message.reference.lower()}.{address_key(message.reference)}@{domain}"


def thread_for_address(raw):
    """The ContactMessage a recipient address names, or None.

    `raw` is whatever the mail server reported as the recipient, which may be a
    bare address, a display-name form, or several of them. Every candidate is
    tried, because a reply may be addressed to more than one place and only one
    of them is ours.
    """
    if not is_configured() or not raw:
        return None

    for match in ADDRESS_RE.finditer(str(raw)):
        reference = match.group("reference").upper()
        # Constant-time: the key is a secret being checked against a value that
        # an unauthenticated request supplied.
        if not hmac.compare_digest(
            address_key(reference), match.group("key").lower()
        ):
            continue
        message = ContactMessage.objects.filter(
            pk=int(reference.split("-")[1])
        ).first()
        if message is not None:
            return message

    return None


# --- Working out what the human actually wrote ------------------------------

# Everything below one of these lines is the quoted message being replied to.
# Kept deliberately few: over-matching truncates a real answer, which is worse
# than leaving a few lines of quoting on the record.
_HISTORY_MARKERS = (
    re.compile(r"^\s*On .{0,160}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),
    # The block Outlook puts above the quoted original.
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
)

# A signature delimiter. RFC 3676 specifies exactly "-- " with the trailing
# space, but that space is routinely stripped in transit and several clients
# never send it, so a bare "--" line counts too. Observed in testing: a real
# Gmail reply arrived with "--" and its signature went onto the record.
#
# The cost is a reply containing a lone "--" line as prose being truncated
# there. That is rare enough, and visible in the thread when it happens,
# to be the better trade against every signature landing in the record an
# agent reads back as the answer.
_SIGNATURE = re.compile(r"^--\s*$")

# Where an HTML mail client puts the message being replied to. Gmail uses a
# div with one of these classes, most other clients use a blockquote, and
# everything from the first such marker onwards is history.
_HTML_QUOTE = re.compile(
    r"<blockquote|<div[^>]*class=\"?[^\">]*(gmail_quote|gmail_extra|moz-cite)",
    re.IGNORECASE,
)

_BLOCK_END = re.compile(
    r"</(p|div|tr|li|h[1-6]|blockquote)>|<br\s*/?>", re.IGNORECASE
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")


def html_to_text(markup):
    """Flatten an HTML-only reply into something worth recording.

    Most clients send a text/plain part alongside the HTML and this is never
    reached. Some do not, and without this their replies would land in the
    record as raw markup — tags and quoted original included — which is what
    the agent then reads back as the human's answer.

    Crude on purpose. It is a fallback, not a rendering engine, and the
    alternative is a dependency for a path most mail never takes.
    """
    if not markup:
        return ""

    text = str(markup)

    # Cut the quoted original before flattening: once the tags are gone,
    # nothing distinguishes it from what the human wrote.
    quote = _HTML_QUOTE.search(text)
    if quote:
        text = text[: quote.start()]

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = html_module.unescape(text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()


def strip_quoted(text):
    """The reply itself, with quoted history and signature removed.

    Mail clients disagree about almost everything here, so this is heuristic by
    nature and errs towards keeping too much: an answer with some quoting stuck
    to the end of it is still the answer, whereas an answer cut short is not.
    """
    if not text:
        return ""

    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = []

    for index, line in enumerate(lines):
        if _SIGNATURE.match(line):
            break
        if any(marker.match(line) for marker in _HISTORY_MARKERS):
            break
        # A run of quoted lines reaching the end is history. Quoted lines with
        # writing after them are the human answering inline, and stay.
        if line.startswith(">") and all(
            not rest.strip() or rest.startswith(">") for rest in lines[index:]
        ):
            break
        kept.append(line)

    return "\n".join(kept).strip()


def sender_allowed(raw):
    """Whether this From address may write into a thread as the human.

    The last of the three checks, and the only one that depends on the message
    rather than on the address it was sent to. From headers are trivially
    forged, so this is not load-bearing alone — it is there so that a leaked
    reply address is still not enough on its own.
    """
    allowed = [
        a.strip().lower()
        for a in getattr(settings, "INBOX_INBOUND_SENDERS", None) or []
        if a.strip()
    ]
    if not allowed:
        # Nothing configured means nobody is authorised, rather than everybody.
        return False

    match = re.search(r"[\w.+-]+@[\w.-]+", str(raw or ""))
    return bool(match and match.group(0).lower() in allowed)


def record_reply(message, body, *, sender="", subject="", raw=None):
    """Append an emailed reply to a thread as though it had been typed in.

    Returns the entry, or None when there was nothing to record. The post_save
    hook does the rest: the status moves to answered and the reply goes out on
    the sender's own channel.
    """
    text = strip_quoted(body)
    if not text:
        logger.warning(
            "Inbound reply to %s had no body once quoting was stripped; ignored.",
            message.reference,
        )
        return None

    raw = raw or {}
    return ThreadEntry.objects.create(
        message=message,
        kind=ThreadEntry.Kind.HUMAN,
        body=text,
        author_label=getattr(settings, "INBOX_HUMAN_NAME", "") or "",
        extra={
            # Kept as evidence about a message that wrote to the record. The
            # signature was already verified before this was called; this is
            # the audit trail, not the check.
            "inbound": {
                "from": str(sender)[:500],
                "subject": str(subject)[:500],
                **{k: str(v)[:200] for k, v in raw.items()},
            }
        },
    )
