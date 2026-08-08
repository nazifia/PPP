"""Domain models for the PPP supply platform.

Two kinds of tenant live in the same tables: hospitals (which raise requests)
and supplier companies (which fulfil them). Every row hangs off an
Organization, and the API only ever returns rows belonging to the caller's
organization or to a partner it trades with.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.db.models.functions import Coalesce
from django.utils import timezone


def new_reference():
    return uuid4().hex[:8].upper()


#: Quantities move in half units — half a pack or half a carton is real stock,
#: a quarter of one is not. One decimal place is therefore all any quantity
#: column needs, and the API refuses anything off that step.
HALF_UNIT = Decimal('0.5')
QTY_DECIMAL_PLACES = 1


def quantity_field(**kwargs):
    """A quantity column: decimal, because half units are dispensed and sold."""
    kwargs.setdefault('max_digits', 12)
    kwargs.setdefault('decimal_places', QTY_DECIMAL_PLACES)
    return models.DecimalField(**kwargs)


def normalize_phone(phone):
    """Keep digits and a leading +, so '0803 123-4567' == '08031234567'."""
    cleaned = ''.join(ch for ch in str(phone or '') if ch.isdigit() or ch == '+')
    return ('+' + cleaned.replace('+', '')) if cleaned.startswith('+') else cleaned.replace('+', '')


class OrgKind(models.TextChoices):
    HOSPITAL = 'HOSPITAL', 'Hospital / Organisation'
    SUPPLIER = 'SUPPLIER', 'Supplier Company'


class OrgCategory(models.TextChoices):
    PHARMACY = 'PHARMACY', 'Pharmacy / Medicines'
    LABORATORY = 'LABORATORY', 'Laboratory Reagents'
    CONSUMABLES = 'CONSUMABLES', 'Medical Consumables'
    MIXED = 'MIXED', 'Mixed'


class Role(models.TextChoices):
    ADMIN = 'ADMIN', 'Administrator'
    STAFF = 'STAFF', 'Staff'


class ReqStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft (wishlist)'
    SUBMITTED = 'SUBMITTED', 'Submitted'
    APPROVED = 'APPROVED', 'Approved'
    PARTIALLY_APPROVED = 'PARTIALLY_APPROVED', 'Partially approved'
    REJECTED = 'REJECTED', 'Rejected'
    DISPATCHED = 'DISPATCHED', 'Dispatched'
    DELIVERED = 'DELIVERED', 'Delivered (partly verified)'
    CLOSED = 'CLOSED', 'Closed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class LineStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    PARTIAL = 'PARTIAL', 'Partially approved'
    REJECTED = 'REJECTED', 'Rejected'


class Organization(models.Model):
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=16, choices=OrgKind.choices)
    category = models.CharField(
        max_length=16, choices=OrgCategory.choices, blank=True,
        help_text='What a supplier sells. Blank for hospitals.',
    )
    phone = models.CharField(max_length=24, unique=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    registration_no = models.CharField(max_length=64, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(
        default=30,
        help_text='How long a supplier gives a hospital to pay. Copied onto each invoice.',
    )
    idle_timeout_minutes = models.PositiveSmallIntegerField(
        default=30,
        validators=[MinValueValidator(2), MaxValueValidator(480)],
        help_text=(
            'Ceiling on how long a signed-in app may sit idle before it signs '
            'itself out. A device may choose a shorter time, never a longer one.'
        ),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        return super().save(*args, **kwargs)


class UserManager(BaseUserManager):
    def create_user(self, phone, password=None, **extra):
        phone = normalize_phone(phone)
        if not phone:
            raise ValueError('A phone number is required.')
        user = self.model(phone=phone, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, password=None, **extra):
        extra.setdefault('role', Role.ADMIN)
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        extra.setdefault('full_name', 'Platform admin')
        return self.create_user(phone, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    phone = models.CharField(max_length=24, unique=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    organization = models.ForeignKey(
        Organization, null=True, blank=True, on_delete=models.CASCADE, related_name='users',
    )
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.STAFF)
    job_title = models.CharField(max_length=80, blank=True)
    must_change_password = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    #: When this account last made an authenticated request. The token expires
    #: an organisation's idle timeout after it. Written at most once a minute.
    last_seen_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = UserManager()

    # Set for the length of one request when a superuser steps into a tenant.
    # Deliberately not a database field, so it can never reach a save().
    act_as_org = None

    USERNAME_FIELD = 'phone'
    REQUIRED_FIELDS = ['full_name']

    class Meta:
        ordering = ['full_name']

    def __str__(self):
        return f'{self.full_name} ({self.phone})'

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        return super().save(*args, **kwargs)

    @property
    def scope_org(self):
        """The organisation this request works inside.

        Its own, or the tenant a superuser asked to act as.
        """
        return self.act_as_org or self.organization

    @property
    def scope_org_id(self):
        return self.act_as_org.pk if self.act_as_org else self.organization_id

    @property
    def sees_all_tenants(self):
        """A superuser reads every tenant, until it steps inside one."""
        return self.is_superuser and self.act_as_org is None

    @property
    def is_org_admin(self):
        # A platform superuser outranks every organisation administrator.
        return self.is_superuser or self.role == Role.ADMIN

    @property
    def org_kind(self):
        org = self.scope_org
        return org.kind if org else None


class Partnership(models.Model):
    """A hospital is only allowed to trade with suppliers it has registered."""

    hospital = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='supplier_links',
    )
    supplier = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='hospital_links',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('hospital', 'supplier')]
        ordering = ['supplier__name']

    def __str__(self):
        return f'{self.hospital.name} -> {self.supplier.name}'


class Department(models.Model):
    """A division of a hospital: pharmacy, laboratory, theatre."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='departments',
    )
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        unique_together = [('organization', 'name')]

    def __str__(self):
        return self.name


class Unit(models.Model):
    """A subdivision of one department. Units do not nest any further.

    Names are unique within their department, so two departments may each keep
    their own HAEMATOLOGY.
    """

    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name='units',
    )
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        unique_together = [('department', 'name')]

    def __str__(self):
        return f'{self.department.name} / {self.name}'

    @property
    def organization_id(self):
        return self.department.organization_id


class Product(models.Model):
    """An item a supplier offers: drug, reagent or consumable."""

    supplier = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='products',
    )
    generic_name = models.CharField(max_length=160)
    brand = models.CharField(max_length=120, blank=True)
    strength = models.CharField(max_length=60, blank=True)
    formulation = models.CharField(max_length=60, blank=True)
    unit = models.CharField(max_length=40, default='UNIT')
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    stock_qty = quantity_field(default=Decimal('0'))
    qty_reserved = quantity_field(
        default=Decimal('0'),
        help_text='Approved but not yet dispatched. Held for those requests.',
    )
    max_order_qty = quantity_field(
        null=True, blank=True, help_text='Cap per single request. Blank = no cap.',
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['generic_name', 'brand']
        indexes = [models.Index(fields=['supplier', 'generic_name'])]

    def __str__(self):
        return f'{self.generic_name} {self.strength}'.strip()

    @property
    def available_qty(self):
        """On the shelf and not already promised to an approved request."""
        return max(self.stock_qty - self.qty_reserved, Decimal('0'))

    @property
    def availability(self):
        return 'AVL' if self.is_active and self.available_qty > 0 else 'RV'


class Requisition(models.Model):
    """One request from one hospital to one supplier.

    A DRAFT requisition doubles as the hospital's wishlist for that supplier.
    """

    reference = models.CharField(max_length=16, unique=True, default=new_reference)
    hospital = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='requisitions',
    )
    supplier = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='incoming_requisitions',
    )
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name='requisitions',
    )
    unit = models.ForeignKey(
        Unit, null=True, blank=True, on_delete=models.SET_NULL, related_name='requisitions',
        help_text='Set instead of department when the request comes from a unit.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='requisitions',
    )
    status = models.CharField(max_length=20, choices=ReqStatus.choices, default=ReqStatus.DRAFT)
    note = models.TextField(blank=True)
    supplier_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='decided_requisitions',
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['hospital', 'status']),
            models.Index(fields=['supplier', 'status']),
        ]
        constraints = [
            # One open wishlist per person, company and department. A second
            # identical draft would be a basket nobody can reach, since adding
            # from the catalogue only ever finds one of them. Two departments
            # may still each keep their own, and the constraint is lifted once
            # the draft is submitted. Coalesce because an untagged draft holds
            # NULL, and SQL counts two NULLs as different values.
            models.UniqueConstraint(
                'hospital',
                'supplier',
                'created_by',
                Coalesce('department', models.Value(0)),
                Coalesce('unit', models.Value(0)),
                condition=models.Q(status=ReqStatus.DRAFT),
                name='uniq_open_draft_per_company',
            ),
        ]

    def __str__(self):
        return f'{self.reference} {self.hospital.name} -> {self.supplier.name}'

    @property
    def department_label(self):
        """Whichever of the two tags the request carries, unit winning."""
        return str(self.unit or self.department or '')

    @property
    def requested_value(self):
        return sum((line.line_total_requested for line in self.lines.all()), Decimal('0.00'))

    @property
    def approved_value(self):
        return sum((line.line_total_approved for line in self.lines.all()), Decimal('0.00'))

    @property
    def is_editable(self):
        return self.status == ReqStatus.DRAFT


class RequisitionLine(models.Model):
    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='requisition_lines')
    qty_requested = quantity_field(default=Decimal('1'))
    qty_approved = quantity_field(default=Decimal('0'))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=10, choices=LineStatus.choices, default=LineStatus.PENDING)
    supplier_note = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = [('requisition', 'product')]
        ordering = ['id']

    def __str__(self):
        return f'{self.product} x{self.qty_requested}'

    @property
    def line_total_requested(self):
        return self.unit_price * self.qty_requested

    @property
    def line_total_approved(self):
        return self.unit_price * self.qty_approved

    @property
    def qty_supplied(self):
        return sum((dl.qty_supplied for dl in self.delivery_lines.all()), Decimal('0'))

    @property
    def qty_accepted(self):
        return sum((dl.qty_accepted for dl in self.delivery_lines.all()), Decimal('0'))


class DeliveryStatus(models.TextChoices):
    IN_TRANSIT = 'IN_TRANSIT', 'In transit / awaiting verification'
    VERIFIED = 'VERIFIED', 'Verified and accepted'
    RETURNED = 'RETURNED', 'Rejected on verification'


class Delivery(models.Model):
    """A physical consignment against a requisition. A request may need several."""

    reference = models.CharField(max_length=16, unique=True, default=new_reference)
    requisition = models.ForeignKey(
        Requisition, on_delete=models.CASCADE, related_name='deliveries',
    )
    waybill_no = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.IN_TRANSIT,
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='dispatches',
    )
    dispatched_at = models.DateTimeField(auto_now_add=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='verifications',
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    remark = models.TextField(blank=True)

    class Meta:
        ordering = ['-dispatched_at']

    def __str__(self):
        return f'{self.reference} for {self.requisition.reference}'

    @property
    def accepted_value(self):
        return sum((dl.accepted_value for dl in self.lines.all()), Decimal('0.00'))


class DeliveryLine(models.Model):
    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name='lines')
    requisition_line = models.ForeignKey(
        RequisitionLine, on_delete=models.CASCADE, related_name='delivery_lines',
    )
    qty_supplied = quantity_field(default=Decimal('0'))
    batch_no = models.CharField(max_length=64, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    qty_accepted = quantity_field(default=Decimal('0'))
    qty_rejected = quantity_field(default=Decimal('0'))
    reject_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.requisition_line.product} x{self.qty_supplied}'

    @property
    def accepted_value(self):
        return self.requisition_line.unit_price * self.qty_accepted


class InvoiceStatus(models.TextChoices):
    UNPAID = 'UNPAID', 'Unpaid'
    PART_PAID = 'PART_PAID', 'Part paid'
    PAID = 'PAID', 'Paid'


class Invoice(models.Model):
    """Raised automatically for what the hospital actually accepted.

    `amount_paid` counts confirmed money only. What the hospital has declared
    but the supplier has not yet confirmed sits in `amount_pending`.
    """

    reference = models.CharField(max_length=16, unique=True, default=new_reference)
    delivery = models.OneToOneField(Delivery, on_delete=models.CASCADE, related_name='invoice')
    hospital = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='payable_invoices',
    )
    supplier = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='receivable_invoices',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(
        max_length=10, choices=InvoiceStatus.choices, default=InvoiceStatus.UNPAID,
    )
    # Terms are copied off the supplier when the invoice is raised, so changing
    # them later cannot move a date the two sides have already agreed.
    terms_days = models.PositiveSmallIntegerField(default=30)
    due_date = models.DateField(null=True, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-issued_at']
        indexes = [models.Index(fields=['status', 'due_date'])]

    def __str__(self):
        return f'{self.reference} {self.amount}'

    def save(self, *args, **kwargs):
        # A new invoice takes its terms from the supplier and dates itself from
        # today. Both are then the invoice's own.
        if not self.pk and not self.due_date:
            self.terms_days = self.supplier.payment_terms_days
            self.due_date = timezone.localdate() + timedelta(days=self.terms_days)
        return super().save(*args, **kwargs)

    @property
    def balance(self):
        """Still owed, counting only money the supplier has confirmed."""
        return self.amount - self.amount_paid

    @property
    def days_overdue(self):
        """Days past the due date. Zero while there is still time, or once paid."""
        if self.status == InvoiceStatus.PAID or not self.due_date:
            return 0
        return max((timezone.localdate() - self.due_date).days, 0)

    @property
    def is_overdue(self):
        return self.days_overdue > 0

    @property
    def amount_pending(self):
        """Declared by the hospital, not yet confirmed by the supplier."""
        total = self.payments.filter(status=PaymentStatus.PENDING).aggregate(
            total=models.Sum('amount'),
        )['total']
        return (total or Decimal('0.00')).quantize(Decimal('0.01'))

    @property
    def amount_unclaimed(self):
        """What a new payment may still be recorded against."""
        return max(self.balance - self.amount_pending, Decimal('0.00'))

    def register_payment(self, amount):
        """Move confirmed money onto the invoice. Called by the confirm step."""
        self.amount_paid += Decimal(amount)
        if self.amount_paid >= self.amount:
            self.status = InvoiceStatus.PAID
            self.paid_at = timezone.now()
        elif self.amount_paid > 0:
            self.status = InvoiceStatus.PART_PAID
        self.save()
        return self


RECEIPT_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp', 'heic', 'pdf']
RECEIPT_MAX_BYTES = 5 * 1024 * 1024


def receipt_path(payment, filename):
    """Keep the extension, drop the phone's filename, file it under the payment."""
    suffix = Path(filename).suffix.lower()[:8]
    return f'receipts/{timezone.now():%Y/%m}/{payment.reference}{suffix}'


class PaymentMethod(models.TextChoices):
    TRANSFER = 'TRANSFER', 'Bank transfer'
    CASH = 'CASH', 'Cash'
    CHEQUE = 'CHEQUE', 'Cheque'
    POS = 'POS', 'Card / POS'


class PaymentStatus(models.TextChoices):
    PENDING = 'PENDING', 'Recorded, awaiting the supplier'
    CONFIRMED = 'CONFIRMED', 'Confirmed received'
    REJECTED = 'REJECTED', 'Not received'


class Payment(models.Model):
    """One attempt to settle part of an invoice.

    The platform moves no money. The hospital records what it sent and how, the
    supplier says whether it arrived, and only a confirmed payment counts
    against the invoice — so nothing reads as settled on one side's word alone.
    """

    reference = models.CharField(max_length=16, unique=True, default=new_reference)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    payer_reference = models.CharField(
        max_length=64, blank=True,
        help_text='Teller, transfer or cheque number. Required for everything but cash.',
    )
    status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.PENDING,
    )
    note = models.CharField(max_length=255, blank=True)
    receipt = models.FileField(
        upload_to=receipt_path, blank=True,
        validators=[FileExtensionValidator(RECEIPT_EXTENSIONS)],
        help_text='Photo or scan of the teller slip, transfer advice or cheque.',
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name='payments_recorded',
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='payments_decided',
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-recorded_at']
        indexes = [models.Index(fields=['invoice', 'status'])]
        constraints = [
            # The same teller slip entered twice against one invoice is a
            # keying mistake, not a second payment. Blank is only cash, and a
            # rejected entry frees its reference to be recorded again.
            models.UniqueConstraint(
                fields=['invoice', 'payer_reference'],
                condition=~models.Q(payer_reference='') & ~models.Q(status=PaymentStatus.REJECTED),
                name='uniq_payer_reference_per_invoice',
            ),
        ]

    def __str__(self):
        return f'{self.reference} {self.amount} ({self.get_status_display()})'


class StockMovement(models.Model):
    """Ledger of every stock change, on both sides of the trade."""

    ISSUE = 'ISSUE'
    RECEIPT = 'RECEIPT'
    RETURN = 'RETURN'
    ADJUST = 'ADJUST'
    DISPENSE = 'DISPENSE'
    KIND_CHOICES = [
        (ISSUE, 'Issued by supplier'),
        (RECEIPT, 'Received by hospital'),
        (RETURN, 'Returned to supplier'),
        (ADJUST, 'Manual adjustment'),
        (DISPENSE, 'Dispensed by hospital'),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='stock_movements',
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='movements')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    qty = quantity_field(help_text='Signed against the organization holding it.')
    delivery = models.ForeignKey(
        Delivery, null=True, blank=True, on_delete=models.SET_NULL, related_name='movements',
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # The id breaks the tie: one dispatch writes several rows in the same
        # instant, and a paged read needs a total order or rows shift between pages.
        ordering = ['-created_at', '-id']


class AuditLog(models.Model):
    """Append-only trail. Written by the workflow actions, never edited."""

    organization = models.ForeignKey(
        Organization, null=True, on_delete=models.SET_NULL, related_name='audit_logs',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='audit_logs',
    )
    entity = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=40)
    action = models.CharField(max_length=40)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['entity', 'entity_id'])]

    def __str__(self):
        return f'{self.action} {self.entity}#{self.entity_id}'


def audit(actor, entity, entity_id, action, **detail):
    return AuditLog.objects.create(
        organization=getattr(actor, 'scope_org', None),
        actor=actor if getattr(actor, 'pk', None) else None,
        entity=entity,
        entity_id=str(entity_id),
        action=action,
        # Quantities and money are Decimals, which JSON has no room for. The
        # string keeps the trail exact where a float would not.
        detail={
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in detail.items()
        },
    )
