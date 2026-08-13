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
