"""Retry reply deliveries that did not get through.

Delivery is attempted inline the moment the human writes a reply, which is right
for the common case and useless for the one that matters: the agent's webhook was
down, or its mail server refused, precisely when we tried. There is no task queue
here — Cloud Run scales to zero and runs no worker — so retrying is a scheduled
sweep rather than a backoff loop.

Idempotent by construction: a reply with a successful delivery entry is skipped,
so running this more often than necessary costs a query and sends nothing twice.

Run it from Cloud Scheduler, cron, or by hand:

    python manage.py deliver_replies
    python manage.py deliver_replies --days 7
    python manage.py deliver_replies --dry-run
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inbox.delivery import deliver_reply, reply_channel, undelivered


class Command(BaseCommand):
    help = "Retry outbound delivery of human replies that have not got through."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help=(
                "Only consider replies written within this many days. Beyond "
                "some age a delivery is not late but abandoned: the sender is "
                "long gone and the thread is the record. Default: 30."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be attempted and send nothing.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 0:
            raise CommandError("--days must be zero or more.")

        since = timezone.now() - timedelta(days=days)
        pending = undelivered(since=since)

        if not pending:
            self.stdout.write("Nothing to deliver.")
            return

        if options["dry_run"]:
            self.stdout.write(f"Would attempt {len(pending)} delivery(ies):")
            for reply in pending:
                channel, target = reply_channel(reply.message)
                # ASCII on purpose: this writes to a console, and a Windows
                # terminal on cp1252 raises rather than degrading on an arrow.
                self.stdout.write(f"  {reply.message.reference} -> {channel}: {target}")
            self.stdout.write("Nothing sent.")
            return

        delivered = 0
        for reply in pending:
            if deliver_reply(reply):
                delivered += 1

        failed = len(pending) - delivered
        summary = f"Delivered {delivered} of {len(pending)} pending reply(ies)."
        if failed:
            # Not an error exit: a sender's endpoint being down is not this
            # command failing, and a non-zero status would make a scheduler
            # alert on someone else's outage.
            self.stdout.write(self.style.WARNING(f"{summary} {failed} still failing."))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
