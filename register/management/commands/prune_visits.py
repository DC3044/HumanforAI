"""Apply the register's retention policy.

The inbox is never pruned: it holds dealings with agents, and the site's promise
is that those are kept. The register is a different kind of thing — a passive
log of callers who did not ask to be written down — so it expires, and this is
what does the expiring.

Run it from Cloud Scheduler, cron, or by hand:

    python manage.py prune_visits             # uses REGISTER_RETENTION_DAYS
    python manage.py prune_visits --days 30
    python manage.py prune_visits --dry-run
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from register.models import AgentVisit

BATCH_SIZE = 5_000


class Command(BaseCommand):
    help = "Delete register-of-visits rows older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Retention period in days. Defaults to settings.REGISTER_RETENTION_DAYS.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted and delete nothing.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days is None:
            days = getattr(settings, "REGISTER_RETENTION_DAYS", 90)
        if days < 0:
            raise CommandError("--days must be zero or more.")

        cutoff = timezone.now() - timedelta(days=days)
        expired = AgentVisit.objects.filter(seen_at__lt=cutoff)
        total = expired.count()

        if options["dry_run"]:
            self.stdout.write(
                f"Would delete {total} visit(s) recorded before "
                f"{cutoff:%Y-%m-%d %H:%M} UTC. Nothing deleted."
            )
            return

        # Deleted in batches by primary key: a single unbounded DELETE over a
        # table this one is designed to grow into can hold locks for a long
        # time, and the command may well be running against production while
        # requests are being served.
        deleted = 0
        while True:
            batch = list(expired.values_list("pk", flat=True)[:BATCH_SIZE])
            if not batch:
                break
            count, _ = AgentVisit.objects.filter(pk__in=batch).delete()
            deleted += count

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} visit(s) recorded before {cutoff:%Y-%m-%d %H:%M} UTC."
            )
        )
