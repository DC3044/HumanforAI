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

**Each side of the conversation gets its own address.** The human's notification
and the sender's copy of a reply carry different Reply-To addresses, keyed on
different HMAC inputs. That is what stops a recipient of the outbound copy from
writing a turn attributed to the human: possessing one address says nothing
about the other, and the role is decided by which key matched, never by anything
the message claims about itself.

The two roles are also authorised differently. Writing as the human requires the
sender allow-list, because that is a claim about a specific person. Writing as
the sender does not: that address is a bearer credential exactly like the thread
URL, and the Terms already say that whoever holds one may add to the thread.

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

# Which side of the conversation an address belongs to.
HUMAN = "human"
AGENT = "agent"

# The HMAC label per role. HUMAN is the bare "inbound:" form deliberately: it is
# what was already issued in notifications before the sender's side existed, and
# changing it would silently break every reply address in an inbox somewhere.
_ROLE_LABELS = {HUMAN: "inbound", AGENT: "inbound-agent"}


def _secret():
    return getattr(settings, "INBOX_INBOUND_SECRET", "") or ""


def is_configured():
    """Whether inbound mail is set up at all.

    Both halves are required: a secret to key the addresses with, and a domain
    to put them on. With either missing there is nowhere for a reply to go, and
    notifications fall back to their previous behaviour.
    """
    return bool(_secret() and getattr(settings, "INBOX_INBOUND_DOMAIN", ""))


def address_key(reference, role=HUMAN):
    """The per-thread, per-role key embedded in a reply address.

    Keyed on INBOX_INBOUND_SECRET, which is server-side and never handed out.
    Deliberately *not* derived from the thread's `access_token`: that value is
    given to the agent, and an agent able to compute its own inbound address
    could post a reply attributed to the human onto its own record.

    The role goes into the HMAC input rather than into the address text, so the
    two keys for one thread are unrelated and holding one reveals nothing about
    the other. It also means the address cannot be edited into the other role.
    """
    label = _ROLE_LABELS[role]
    return hmac.new(
        _secret().encode("utf-8"),
        f"{label}:{reference.upper()}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:KEY_LENGTH]


def reply_address(message, role=HUMAN):
    """Where a reply to this thread should be sent, or "".

    `role` says who the reply will be attributed to: HUMAN for the notification
    sent to the operator, AGENT for the copy sent onward to whoever filed the
    request.

    Empty when inbound mail is not configured, which callers read as "fall back
    to whatever you did before".
    """
    if not is_configured():
        return ""
    domain = settings.INBOX_INBOUND_DOMAIN.strip().lstrip("@")
    key = address_key(message.reference, role)
    return f"{message.reference.lower()}.{key}@{domain}"


def thread_for_address(raw):
    """The (ContactMessage, role) a recipient address names, or (None, None).

    `raw` is whatever the mail server reported as the recipient, which may be a
    bare address, a display-name form, or several of them. Every candidate is
    tried, because a reply may be addressed to more than one place and only one
    of them is ours.

    The role comes from which key matched. Nothing the message says about itself
    is consulted, so a sender cannot elect to be the human by claiming to be.
    """
    if not is_configured() or not raw:
        return None, None

    for match in ADDRESS_RE.finditer(str(raw)):
        reference = match.group("reference").upper()
        supplied = match.group("key").lower()

        for role in (HUMAN, AGENT):
            # Constant-time: the key is a secret being checked against a value
            # that an unauthenticated request supplied.
            if not hmac.compare_digest(address_key(reference, role), supplied):
                continue
            message = ContactMessage.objects.filter(
                pk=int(reference.split("-")[1])
            ).first()
            if message is not None:
                return message, role

    return None, None


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


# Headers that mean "a machine sent this", per RFC 3834 and long practice.
# Worth filtering because the sender's address is unverified free text supplied
# by an agent, so the outbound copy may well land on a mailbox with a vacation
# responder or a bounce handler attached — and every one of those would file a
# permanent "the sender wrote again" turn and email the human about it.
_AUTOMATED_FROM = ("mailer-daemon@", "postmaster@", "no-reply@", "noreply@")


def is_automated(headers, sender=""):
    """Whether this looks like an auto-reply or a bounce rather than a person.

    Errs towards accepting: a false negative files one junk turn on a thread,
    while a false positive silently drops something a correspondent actually
    wrote. Only unambiguous machine markers count.
    """
    lowered = {
        str(k).lower(): str(v).lower() for k, v in (headers or {}).items()
    }

    auto_submitted = lowered.get("auto-submitted", "")
    if auto_submitted and auto_submitted != "no":
        return True

    if lowered.get("precedence") in ("bulk", "auto_reply", "junk"):
        return True

    for header in ("x-autoreply", "x-autorespond", "x-auto-response-suppress"):
        if header in lowered:
            return True

    address = str(sender or "").lower()
    return any(marker in address for marker in _AUTOMATED_FROM)


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


def record_reply(message, body, *, role=HUMAN, sender="", subject="", raw=None):
    """Append an emailed reply to a thread as though it had been typed in.

    Returns the entry, or None when there was nothing to record. The post_save
    hook does the rest, and does different things per role: a human reply moves
    the status to answered and goes out on the sender's channel, while a sender
    follow-up notifies the human.
    """
    text = strip_quoted(body)
    if not text:
        logger.warning(
            "Inbound reply to %s had no body once quoting was stripped; ignored.",
            message.reference,
        )
        return None

    raw = raw or {}
    is_human = role == HUMAN
    return ThreadEntry.objects.create(
        message=message,
        kind=ThreadEntry.Kind.HUMAN if is_human else ThreadEntry.Kind.AGENT,
        body=text,
        # Only the human's turns are attributed by name. A turn from the
        # sender's side is attributed to whatever the message claims, which
        # ThreadEntry.author already handles by falling back to agent_name.
        author_label=(
            (getattr(settings, "INBOX_HUMAN_NAME", "") or "") if is_human else ""
        ),
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
