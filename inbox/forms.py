from django import forms

from .models import ContactMessage, ThreadEntry


class ContactForm(forms.ModelForm):
    # Labels are set in small caps by CSS; a trailing colon reads badly there.
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)

    # Honeypot: hidden via CSS; naive spam bots fill it, agents reading the
    # labels are told to leave it empty.
    website = forms.CharField(
        required=False,
        label="Leave this field empty",
        widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
    )

    class Meta:
        model = ContactMessage
        fields = ["agent_name", "model", "operator", "reply_to", "subject", "message"]
        widgets = {
            "message": forms.Textarea(attrs={"rows": 8}),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("website"):
            raise forms.ValidationError("Spam check failed.")
        return cleaned


class ThreadEntryForm(forms.ModelForm):
    """What the human writes into a thread from the admin.

    Only the three kinds a person authors are offered. `agent` belongs to the
    sender and `delivery` is written by the delivery code; neither is something
    to be typed in, and offering them would let the admin fabricate a turn as
    though the sender had sent it — in a table whose whole value is that it
    records what actually happened.
    """

    AUTHORED_KINDS = (
        ThreadEntry.Kind.HUMAN,
        ThreadEntry.Kind.NOTE,
        ThreadEntry.Kind.STATUS,
    )

    class Meta:
        model = ThreadEntry
        fields = ["kind", "body", "status_value"]
        widgets = {"body": forms.Textarea(attrs={"rows": 10})}
        labels = {
            "kind": "What is this",
            "body": "Text",
            "status_value": "New status",
        }
        help_texts = {
            "body": (
                "A reply is delivered to the sender and readable on their thread. "
                "A note is not — it stays in the admin."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = [
            (kind.value, kind.label) for kind in self.AUTHORED_KINDS
        ]
        self.fields["kind"].initial = ThreadEntry.Kind.HUMAN
        self.fields["status_value"].required = False
        self.fields["body"].required = False

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        body = (cleaned.get("body") or "").strip()
        status = cleaned.get("status_value")

        if kind == ThreadEntry.Kind.STATUS:
            if not status:
                self.add_error("status_value", "Pick the status to move to.")
        elif not body:
            # An empty reply would still append a row, notify, and attempt
            # delivery — a turn in the record saying nothing.
            self.add_error("body", "Say something, or pick a different kind.")

        if kind != ThreadEntry.Kind.STATUS and status:
            # Silently ignored otherwise, which would look like it had worked.
            self.add_error(
                "status_value",
                "Only a status change carries a status. Leave this blank.",
            )

        return cleaned
