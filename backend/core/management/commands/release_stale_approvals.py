"""Free stock held by approvals nobody ever shipped.

    python manage.py release_stale_approvals --days 14 [--dry-run]

Run it daily (cron, Task Scheduler, whatever the server already has). Each stale
request goes through the ordinary supplier release, so the audit trail, the line
statuses and the reservation arithmetic are exactly as if a person had pressed
the button. Anything already dispatched is left alone.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.models import ReqStatus, Requisition, Role, User
from core.services import release

OPEN_STATUSES = (
    ReqStatus.APPROVED,
    ReqStatus.PARTIALLY_APPROVED,
    ReqStatus.DISPATCHED,
    ReqStatus.DELIVERED,
)


class Command(BaseCommand):
    help = 'Release approved-but-undispatched quantities older than --days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=14)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        days = options['days']
        cutoff = timezone.now() - timedelta(days=days)
        stale = Requisition.objects.filter(
            status__in=OPEN_STATUSES, decided_at__lt=cutoff,
        ).select_related('supplier', 'decided_by')

        released = 0
        for requisition in stale:
            actor = self._actor(requisition)
            if actor is None:
                self.stderr.write(
                    f'{requisition.reference}: no user left at {requisition.supplier.name}, '
                    f'skipped.'
                )
                continue
            if options['dry_run']:
                outstanding = sum(
                    max(line.qty_approved - line.qty_supplied, 0)
                    for line in requisition.lines.all()
                )
                if outstanding:
                    self.stdout.write(f'{requisition.reference}: would release {outstanding}')
                    released += 1
                continue
            try:
                release(requisition, actor, f'not dispatched within {days} days')
            except ValidationError:
                continue  # Everything approved already went out.
            released += 1
            self.stdout.write(f'{requisition.reference}: released')

        verb = 'would release' if options['dry_run'] else 'released'
        self.stdout.write(self.style.SUCCESS(f'{verb} {released} request(s).'))

    def _actor(self, requisition):
        """Act as whoever decided the request, or any admin still at that company."""
        if requisition.decided_by_id and requisition.decided_by.is_active:
            return requisition.decided_by
        return (
            User.objects.filter(
                organization=requisition.supplier, role=Role.ADMIN, is_active=True,
            ).first()
            or User.objects.filter(organization=requisition.supplier, is_active=True).first()
        )
