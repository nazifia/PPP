"""Consignments that arrived and were never checked in.

    python manage.py stale_deliveries [--days 7]

The counterpart to `release_stale_approvals`, and deliberately the quieter one.
An approval that is never shipped holds stock the supplier could sell to
somebody else, so that command frees it. A delivery that is never verified holds
nothing — the goods have already left the supplier's shelf — but no invoice is
raised until somebody says what arrived, so the supplier is owed money it cannot
ask for, and the hospital is holding stock its own ledger has never heard of.

Nothing is decided here. Accepting goods on a hospital's behalf would raise a
real debt on nobody's word, so this only names the consignments somebody needs
to walk down the corridor about. Run it daily beside the other one; the output
is what the cron mail carries.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Delivery, DeliveryStatus


class Command(BaseCommand):
    help = 'List consignments in transit for longer than --days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7)

    def handle(self, *args, **options):
        days = options['days']
        cutoff = timezone.now() - timedelta(days=days)
        stale = Delivery.objects.filter(
            status=DeliveryStatus.IN_TRANSIT, dispatched_at__lt=cutoff,
        ).select_related(
            'requisition__hospital', 'requisition__supplier',
        ).order_by('dispatched_at')

        for delivery in stale:
            requisition = delivery.requisition
            waiting = (timezone.now() - delivery.dispatched_at).days
            self.stdout.write(
                f'{delivery.reference}: {requisition.supplier.name} -> '
                f'{requisition.hospital.name}, request {requisition.reference}, '
                f'{waiting} days unverified'
            )

        total = len(stale)
        line = f'{total} consignment(s) unverified after {days} days.'
        self.stdout.write(
            self.style.WARNING(line) if total else self.style.SUCCESS(line)
        )
