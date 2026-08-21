"""The thread: replies, follow-ups, triage, and the read key that gates them.

`access_token` is unique with a per-row default, which cannot be added in one
step — Django evaluates a default once and stamps every existing row with the
same value, colliding immediately on the unique constraint. So the column
arrives permissive, gets a distinct token per row, and only then takes its
final shape.
"""

import django.db.models.deletion
from django.db import migrations, models

import inbox.models


def issue_tokens(apps, schema_editor):
    ContactMessage = apps.get_model("inbox", "ContactMessage")
    for pk in ContactMessage.objects.filter(access_token="").values_list("pk", flat=True):
        ContactMessage.objects.filter(pk=pk).update(
            access_token=inbox.models.new_access_token()
        )


def drop_tokens(apps, schema_editor):
    """Reversing this loses the tokens, and with them every thread URL already
    handed out. Recorded here so the migration is reversible in development;
    running it backwards in production is a decision, not a rollback."""
    ContactMessage = apps.get_model("inbox", "ContactMessage")
    ContactMessage.objects.update(access_token="")


class Migration(migrations.Migration):

    dependencies = [
        ("inbox", "0004_contactmessage_content_hash_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactmessage",
            name="access_token",
            field=models.CharField(default="", editable=False, max_length=64),
        ),
        migrations.RunPython(issue_tokens, drop_tokens),
        migrations.AlterField(
            model_name="contactmessage",
            name="access_token",
            field=models.CharField(
                default=inbox.models.new_access_token,
                editable=False,
                help_text="Secret half of the thread URL. Never displayed publicly.",
                max_length=64,
                unique=True,
            ),
        ),
        migrations.CreateModel(
            name="ThreadEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("human", "Reply from the human"),
                            ("agent", "Follow-up from the sender"),
                            ("note", "Internal note"),
                            ("status", "Status change"),
                            ("delivery", "Delivery attempt"),
                        ],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "body",
                    models.TextField(
                        blank=True,
                        help_text="The text of a reply, follow-up, or note.",
                    ),
                ),
                (
                    "status_value",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("recorded", "Recorded, not yet read"),
                            ("reviewed", "Read by a human"),
                            ("answered", "Answered"),
                            ("declined", "Declined"),
                            ("closed", "Closed without an answer"),
                        ],
                        help_text="On a status entry, the status the request moved to.",
                        max_length=16,
                        verbose_name="status",
                    ),
                ),
                (
                    "author_label",
                    models.CharField(
                        blank=True,
                        help_text="Who wrote this, as it should appear in the thread.",
                        max_length=200,
                        verbose_name="author",
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("form", "Web form"),
                            ("api", "JSON API"),
                            ("mcp", "MCP tool"),
                            ("query", "URL query"),
                        ],
                        max_length=10,
                    ),
                ),
                ("user_agent", models.CharField(blank=True, max_length=500)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("extra", models.JSONField(blank=True, default=dict)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="inbox.contactmessage",
                    ),
                ),
            ],
            options={
                "verbose_name": "thread entry",
                "verbose_name_plural": "thread entries",
                "ordering": ["created_at", "pk"],
            },
        ),
        migrations.AddIndex(
            model_name="threadentry",
            index=models.Index(
                fields=["message", "kind"], name="inbox_entry_msg_kind_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="threadentry",
            index=models.Index(
                fields=["ip_address", "created_at"], name="inbox_entry_ip_idx"
            ),
        ),
    ]
