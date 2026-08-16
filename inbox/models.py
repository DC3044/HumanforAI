from django.db import models


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

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        who = self.agent_name or "anonymous agent"
        return f"{who} ({self.created_at:%Y-%m-%d %H:%M} UTC)"

    @property
    def reference(self):
        """Citable identifier handed back to the sender. An agent that quotes
        HFA-00042 later is pointing at exactly one row of this table."""
        return f"HFA-{self.pk:05d}"
