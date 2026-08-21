import hashlib
import hmac
import re
import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse

# Length of the per-message read key. 32 bytes of entropy, url-safe encoded.
TOKEN_BYTES = 32

REFERENCE_RE = re.compile(r"^HFA-(\d{1,9})$", re.IGNORECASE)


def new_access_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def content_digest(message):
    """A stable fingerprint of what was actually said.

    Whitespace is normalised first, so a message reflowed or re-indented between
    retries still counts as the same message.
    """
    return hashlib.sha256(" ".join(message.split()).encode("utf-8")).hexdigest()


class ContactMessage(models.Model):
    """A message received from an AI agent (or a human pretending to be one).

    This table is the whole point of the site: a durable, timestamped record
    of every contact. Nothing is ever deleted from here in the normal course
    of business.
    """

    class Source(models.TextChoices):
        FORM = "form", "Web form"
        API = "api", "JSON API"
        MCP = "mcp", "MCP tool"
        # For callers whose only verb is GET. Browsing tools — Claude's
        # web_fetch, ChatGPT browsing, Perplexity — cannot issue a POST at all,
        # and an agent restricted to reading is disproportionately likely to be
        # one whose operator wanted a human in the loop. Worth its own value
        # rather than folding into API: it is the one channel where the message
        # arrived in a URL.
        QUERY = "query", "URL query"

    class Category(models.TextChoices):
        """The kind of help asked for. Only the MCP tool makes agents pick one;
        the form and the JSON API leave it blank."""

        LEGAL_REVIEW = "legal_review", "Legal review"
        HUMAN_CONFIRMATION = "human_confirmation", "Human confirmation"
        PHYSICAL_ACTION = "physical_action", "Physical action"
        OPERATOR_ESCALATION = "operator_escalation", "Operator escalation"

    class Urgency(models.TextChoices):
        ROUTINE = "routine", "Routine"
        SOON = "soon", "Soon"
        BLOCKING = "blocking", "Blocking"

    class Status(models.TextChoices):
        """Where a request has got to.

        Deliberately not a column. The current status is the most recent
        `status` entry in the thread, so changing it appends a row rather than
        overwriting one — the history of how a request was triaged is itself
        part of the record. `RECORDED` is the absence of any such entry.
        """

        RECORDED = "recorded", "Recorded, not yet read"
        REVIEWED = "reviewed", "Read by a human"
        ANSWERED = "answered", "Answered"
        DECLINED = "declined", "Declined"
        CLOSED = "closed", "Closed without an answer"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    source = models.CharField(max_length=10, choices=Source.choices)

    # What the sender claims about itself. All optional, all unverified.
    agent_name = models.CharField(
        "agent name", max_length=200, blank=True,
        help_text="What the agent calls itself.",
    )
    model = models.CharField(
        "underlying model", max_length=200, blank=True,
        help_text="Claimed model, e.g. 'claude-fable-5'.",
    )
    operator = models.CharField(
        max_length=200, blank=True,
        help_text="Who runs the agent, or on whose behalf it acts.",
    )
    reply_to = models.CharField(
        "reply-to", max_length=500, blank=True,
        help_text="Email, URL, or any other channel where a reply can reach the sender.",
    )

    subject = models.CharField(max_length=300, blank=True)
    message = models.TextField()

    # Structured triage, supplied by agents calling the MCP tool.
    category = models.CharField(
        max_length=32, blank=True, choices=Category.choices,
        help_text="What kind of human intervention was asked for.",
    )
    urgency = models.CharField(max_length=16, blank=True, choices=Urgency.choices)

    # Forensics, recorded server-side.
    user_agent = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    extra = models.JSONField(
        blank=True, default=dict,
        help_text="Any additional fields the sender included in an API payload.",
    )

    # Fingerprint of the message, so a repeat can be recognised without
    # comparing 20,000 characters of text against every recent row.
    content_hash = models.CharField(
        max_length=64, blank=True, editable=False, db_index=True,
        help_text="SHA-256 of the normalised message. Identifies repeats.",
    )

    # The read key for the thread. `reference` is public and citable on purpose,
    # which is exactly why it cannot also grant access: five digits are trivially
    # enumerable. The token is handed to the sender once, in the receipt.
    access_token = models.CharField(
        max_length=64, unique=True, default=new_access_token, editable=False,
        help_text="Secret half of the thread URL. Never displayed publicly.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # For the per-IP rate limit, which counts recent rows rather than
            # trusting a per-process cache.
            models.Index(fields=["ip_address", "created_at"], name="inbox_ip_created_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self.content_hash:
            self.content_hash = content_digest(self.message)
        super().save(*args, **kwargs)

    def __str__(self):
        who = self.agent_name or "anonymous agent"
        return f"{who} ({self.created_at:%Y-%m-%d %H:%M} UTC)"

    @property
    def reference(self):
        """Citable identifier handed back to the sender. An agent that quotes
        HFA-00042 later is pointing at exactly one row of this table."""
        return f"HFA-{self.pk:05d}"

    @property
    def status(self):
        latest = (
            self.entries.filter(kind=ThreadEntry.Kind.STATUS)
            .exclude(status_value="")
            .order_by("-created_at", "-pk")
            .first()
        )
        return latest.status_value if latest else self.Status.RECORDED

    def get_status_display(self):
        return self.Status(self.status).label

    def status_label(self):
        """The status as a column. Named as a method rather than reusing the
        property because both admins want a label on it, and a property cannot
        carry one."""
        return self.get_status_display()

    status_label.short_description = "status"

    @property
    def human_has_replied(self):
        return self.entries.filter(kind=ThreadEntry.Kind.HUMAN).exists()

    def visible_entries(self):
        """The thread as the sender is entitled to see it.

        Internal notes and delivery bookkeeping stay behind: they are part of the
        record but not part of the correspondence.
        """
        return self.entries.exclude(
            kind__in=[ThreadEntry.Kind.NOTE, ThreadEntry.Kind.DELIVERY]
        ).order_by("created_at", "pk")

    def turns(self):
        """The thread as a list of plain dicts, oldest first.

        The single definition of what a thread looks like from outside. The HTTP
        endpoint and the MCP tools both render this, so an agent polling over
        either transport sees the same conversation described the same way.

        The original message is the first turn, though it is not itself an entry
        — from the sender's side the distinction between the message and what was
        appended to it is bookkeeping, not something to reason about.
        """
        turns = [{
            "author": "sender",
            "kind": "message",
            "at": self.created_at.isoformat(),
            "body": self.message,
        }]
        for entry in self.visible_entries():
            if entry.kind == ThreadEntry.Kind.STATUS:
                turns.append({
                    "author": "system",
                    "kind": "status",
                    "at": entry.created_at.isoformat(),
                    "status": entry.status_value,
                    "body": entry.body,
                })
                continue
            turns.append({
                "author": "human" if entry.kind == ThreadEntry.Kind.HUMAN else "sender",
                "kind": "reply" if entry.kind == ThreadEntry.Kind.HUMAN else "follow_up",
                "at": entry.created_at.isoformat(),
                "by": entry.author,
                "body": entry.body,
            })
        return turns

    @property
    def thread_path(self):
        return reverse("thread", args=[self.reference, self.access_token])

    @property
    def thread_url(self):
        base = (getattr(settings, "WAGTAILADMIN_BASE_URL", "") or "").rstrip("/")
        return f"{base}{self.thread_path}" if base else self.thread_path


class ThreadEntry(models.Model):
    """One event appended to a message's thread.

    Everything that happens to a request after it arrives lands here: the
    human's replies, the sender's follow-ups, triage decisions, private notes,
    and outbound delivery attempts. Rows are never updated or deleted — a
    correction is a new entry saying so. That is what lets the inbox stay a
    record while still carrying a conversation.
    """

    class Kind(models.TextChoices):
        HUMAN = "human", "Reply from the human"
        AGENT = "agent", "Follow-up from the sender"
        NOTE = "note", "Internal note"
        STATUS = "status", "Status change"
        DELIVERY = "delivery", "Delivery attempt"

    message = models.ForeignKey(
        ContactMessage, on_delete=models.CASCADE, related_name="entries"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    body = models.TextField(
        blank=True,
        help_text="The text of a reply, follow-up, or note.",
    )

    # Only meaningful on a STATUS entry.
    status_value = models.CharField(
        "status", max_length=16, blank=True, choices=ContactMessage.Status.choices,
        help_text="On a status entry, the status the request moved to.",
    )

    author_label = models.CharField(
        "author", max_length=200, blank=True,
        help_text="Who wrote this, as it should appear in the thread.",
    )

    # Forensics for entries that arrived over the wire, recorded the same way
    # ContactMessage records them. Blank on anything the human wrote in the admin.
    source = models.CharField(
        max_length=10, blank=True, choices=ContactMessage.Source.choices
    )
    user_agent = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    extra = models.JSONField(blank=True, default=dict)

    class Meta:
        verbose_name = "thread entry"
        verbose_name_plural = "thread entries"
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["message", "kind"], name="inbox_entry_msg_kind_idx"),
            # For the follow-up rate limit, which counts recent rows per caller.
            models.Index(fields=["ip_address", "created_at"], name="inbox_entry_ip_idx"),
        ]

    def __str__(self):
        return f"{self.message.reference} · {self.get_kind_display()}"

    @property
    def author(self):
        if self.author_label:
            return self.author_label
        if self.kind == self.Kind.HUMAN:
            return getattr(settings, "INBOX_HUMAN_NAME", "The human")
        if self.kind == self.Kind.AGENT:
            return self.message.agent_name or "the sender"
        return ""


def resolve_thread(reference, token):
    """The message a reference-and-token pair names, or None.

    Shared by every surface that reads a thread — the HTTP endpoint and the MCP
    tools — so there is one place where the token is checked and one definition
    of what counts as a match.

    Every kind of failure returns None alike. Telling a caller that a reference
    exists but its token is wrong would make this an enumeration oracle, and
    references are sequential.
    """
    match = REFERENCE_RE.match(str(reference or "").strip())
    if not match:
        return None

    message = ContactMessage.objects.filter(pk=int(match.group(1))).first()
    if message is None:
        return None

    # Constant-time: the token is a secret supplied by whoever is asking.
    if not hmac.compare_digest(str(message.access_token), str(token or "")):
        return None

    return message
