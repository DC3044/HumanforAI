from django.db import models

from .detect import Kind, Reason


class AgentVisit(models.Model):
    """One non-human caller, observed once.

    The inbox records the agents that chose to say something. This records the
    ones that merely came through — which is nearly all of them. A row here is
    an observation, not a communication: nobody asked to be written down, and
    nothing in it is published.

    Unlike the inbox, this table is prunable. It is telemetry rather than a
    record of dealings, and `manage.py prune_visits` keeps it bounded.
    """

    seen_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Who, as far as anyone can tell. Derived from the user agent, which the
    # caller supplies and could have made up.
    agent = models.CharField(
        max_length=100, blank=True,
        help_text="Product name from the user agent, e.g. 'ClaudeBot'.",
    )
    operator = models.CharField(
        max_length=100, blank=True,
        help_text="Who runs it, where the user agent identifies a known one.",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.UNKNOWN)
    reason = models.CharField(
        max_length=16, choices=Reason.choices,
        help_text="Why this call was recorded at all.",
    )

    # What was asked for.
    method = models.CharField(max_length=10)
    path = models.CharField(
        max_length=500, db_index=True,
        help_text="Requested path including any query string.",
    )
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)

    # Raw provenance, kept because the derived fields above are only ever a
    # reading of it and the reading may need revisiting.
    user_agent = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    referer = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-seen_at"]
        verbose_name = "visit"
        verbose_name_plural = "visits"
        indexes = [
            # The admin's summary groups by agent over a recent window; without
            # this it is a full scan of a table designed to get large.
            models.Index(fields=["agent", "-seen_at"], name="register_agent_seen_idx"),
        ]

    def __str__(self):
        return f"{self.label} — {self.path} ({self.seen_at:%Y-%m-%d %H:%M} UTC)"

    @property
    def label(self):
        """The caller in one line: 'Claude-User (Anthropic, on behalf of a human)'.

        An unrecognised caller gets its bare name. Appending "(unrecognised)"
        only repeats what the adjacent kind column already says, on exactly the
        rows that have the least to show.
        """
        name = self.agent or "(unidentified)"
        qualifiers = [self.operator] if self.operator else []
        if self.kind and self.kind != Kind.UNKNOWN:
            qualifiers.append(self.get_kind_display())
        return f"{name} ({', '.join(qualifiers)})" if qualifiers else name
