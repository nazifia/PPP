from django.contrib import admin

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
