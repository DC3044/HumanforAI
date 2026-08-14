from django import forms

from .models import ContactMessage


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
