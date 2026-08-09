import os

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render

from .models import (
    AuditLog,
    Delivery,
    DeliveryLine,
    Department,
    DispensingUnit,
    Formulation,
    Invoice,
    Organization,
    Partnership,
    Payment,
    Product,
    Requisition,
    RequisitionLine,
    StockMovement,
    Unit,
    User,
)


class RequisitionLineInline(admin.TabularInline):
    model = RequisitionLine
    extra = 0


class DeliveryLineInline(admin.TabularInline):
    model = DeliveryLine
    extra = 0


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'kind', 'category', 'phone', 'is_active']
    list_filter = ['kind', 'category', 'is_active']
    search_fields = ['name', 'phone']


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ['phone', 'full_name', 'organization', 'role', 'is_active']
    list_filter = ['role', 'is_active', 'organization__kind']
    search_fields = ['phone', 'full_name']


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['generic_name', 'brand', 'supplier', 'unit_price', 'stock_qty', 'is_active']
    list_filter = ['supplier', 'is_active']
    search_fields = ['generic_name', 'brand']


@admin.register(Requisition)
class RequisitionAdmin(admin.ModelAdmin):
    list_display = ['reference', 'hospital', 'supplier', 'status', 'created_at']
    list_filter = ['status', 'supplier']
    search_fields = ['reference']
    inlines = [RequisitionLineInline]


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ['reference', 'requisition', 'status', 'dispatched_at', 'verified_at']
    list_filter = ['status']
    inlines = [DeliveryLineInline]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = [
        'reference', 'hospital', 'supplier', 'amount', 'amount_paid', 'status', 'due_date',
    ]
    list_filter = ['status', 'due_date']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['reference', 'invoice', 'amount', 'method', 'status', 'recorded_at']
    list_filter = ['status', 'method']
    search_fields = ['reference', 'payer_reference', 'invoice__reference']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'organization', 'actor', 'action', 'entity', 'entity_id']
    list_filter = ['action', 'entity']


admin.site.register(
    [Partnership, Department, DispensingUnit, Formulation, Unit, StockMovement],
)


# The development/production switch, for a superuser who has the admin site but
# no shell on the box. Wired up in config/urls.py, linked from the admin header.


def _saved_mode():
    """The word sitting in the file the settings read at startup, if any."""
    try:
        return settings.ENV_FILE.read_text().strip().lower()
    except OSError:
        return ''


def _prod_blockers():
    """What production mode is still missing from the environment.

    The same two things config/settings.py refuses to start production without.
    Checked here so a switch that would leave the site raising
    ImproperlyConfigured on its next start — admin included, with no way back in
    through a browser — is stopped while there is still an admin to stop it.
    """
    missing = []
    key = os.environ.get('DJANGO_SECRET_KEY', '')
    if not key or key.startswith('django-insecure-'):
        missing.append('DJANGO_SECRET_KEY')
    if not [v for v in os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',') if v.strip()]:
        missing.append('DJANGO_ALLOWED_HOSTS')
    return missing


def env_switch(request):
    """Move the site between development and production mode.

    Writes the mode to a file rather than to the database: the settings need it
    long before the database is open. It is read once, at startup, so the page
    saves the choice and says plainly that a restart is what applies it.
    """
    if not request.user.is_superuser:
        raise PermissionDenied
    # A deployment that exports DJANGO_ENV has made its choice; the file is dead
    # weight there and the page says so rather than pretending to save.
    pinned = os.environ.get('DJANGO_ENV')
    if request.method == 'POST':
        wanted_prod = request.POST.get('mode') == 'prod'
        blockers = _prod_blockers() if wanted_prod else []
        if pinned is not None:
            messages.error(
                request,
                f'DJANGO_ENV is set to "{pinned}" where the server starts, and that wins '
                'over this page. Change it there instead.',
            )
        elif blockers:
            messages.error(
                request,
                'Production mode needs ' + ' and '.join(blockers) + ' in the environment '
                'first, or the server will not start again.',
            )
        else:
            settings.ENV_FILE.write_text('prod' if wanted_prod else 'dev')
            messages.success(
                request,
                ('Production' if wanted_prod else 'Development')
                + ' mode saved. Restart the server to apply it.',
            )
        return redirect(request.path)
    saved = _saved_mode()
    return render(request, 'admin/env.html', {
        **admin.site.each_context(request),
        'title': 'Runtime mode',
        'prod': settings.PROD,
        'pinned': pinned,
        'blockers': _prod_blockers(),
        'saved': 'production' if saved in ('prod', 'production') else 'development',
        # A save with no restart behind it yet.
        'pending': (
            bool(saved)
            and pinned is None
            and (saved in ('prod', 'production')) != settings.PROD
        ),
    })
