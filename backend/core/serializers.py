from decimal import Decimal

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.validators import FileExtensionValidator
from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from .models import (
    AuditLog,
    Delivery,
    DeliveryLine,
    Department,
    DispensingUnit,
    Formulation,
    HALF_UNIT,
    Invoice,
    OrgCategory,
    OrgKind,
    Organization,
    Partnership,
    RECEIPT_EXTENSIONS,
    RECEIPT_MAX_BYTES,
    Payment,
    PaymentMethod,
    Product,
    Requisition,
    RequisitionLine,
    Role,
    StockMovement,
    Unit,
    User,
    audit,
    normalize_phone,
)


class QuantityField(serializers.DecimalField):
    """A quantity: half units only, never a quarter.

    The one decimal place already refuses 1.25; the step check adds the other
    half of the rule, that 1.3 is not a quantity anybody can pick off a shelf.
    Quantities travel as JSON numbers rather than strings the way money does —
    a half is exact in binary, and the client does arithmetic on them.
    """

    default_error_messages = {
        'step': 'Quantities go in half units: 0.5, 1, 1.5 and so on.',
    }

    def __init__(self, **kwargs):
        kwargs.setdefault('max_digits', 12)
        kwargs.setdefault('decimal_places', 1)
        kwargs.setdefault('coerce_to_string', False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value % HALF_UNIT:
            self.fail('step')
        return value


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'kind', 'category', 'phone', 'email', 'address',
            'registration_no', 'payment_terms_days', 'idle_timeout_minutes',
            'is_active', 'created_at',
        ]
        read_only_fields = ['kind', 'created_at']


class UserSerializer(serializers.ModelSerializer):
    # scope_org rather than organization, so a superuser acting inside a tenant
    # is reported as that tenant. For everyone else the two are the same.
    organization = serializers.PrimaryKeyRelatedField(source='scope_org', read_only=True)
    organization_name = serializers.CharField(source='scope_org.name', read_only=True)
    organization_kind = serializers.CharField(source='scope_org.kind', read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'phone', 'full_name', 'email', 'role', 'job_title',
            'organization', 'organization_name', 'organization_kind',
            'is_superuser', 'must_change_password', 'is_active', 'date_joined',
        ]
        read_only_fields = [
            'organization', 'is_superuser', 'must_change_password', 'date_joined',
        ]

    def validate_phone(self, value):
        # The model normalises on the way into the database, so the clash has to
        # be looked for in the same form — otherwise `0803 123-4567` passes the
        # field's own uniqueness check and then collides on save.
        phone = normalize_phone(value)
        clash = User.objects.filter(phone=phone)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('That phone number is already registered.')
        return phone


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ['id', 'phone', 'full_name', 'email', 'role', 'job_title', 'password']

    def validate_phone(self, value):
        phone = normalize_phone(value)
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('That phone number is already registered.')
        return phone

    def create(self, validated):
        password = validated.pop('password')
        organization = self.context['request'].user.scope_org
        return User.objects.create_user(
            password=password, organization=organization, **validated
        )


class LoginSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=normalize_phone(attrs['phone']),
            password=attrs['password'],
        )
        if not user:
            raise serializers.ValidationError('Wrong phone number or password.')
        if not user.is_active:
            raise serializers.ValidationError('This account has been disabled.')
        if user.organization and not user.organization.is_active:
            raise serializers.ValidationError('Your organisation has been suspended.')
        attrs['user'] = user
        return attrs


class HospitalRegisterSerializer(serializers.Serializer):
    """Public sign-up: creates the hospital tenant and its first administrator."""

    name = serializers.CharField(max_length=160)
    phone = serializers.CharField(max_length=24)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    registration_no = serializers.CharField(max_length=64, required=False, allow_blank=True)
    admin_full_name = serializers.CharField(max_length=120)
    admin_phone = serializers.CharField(max_length=24)
    password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_phone(self, value):
        phone = normalize_phone(value)
        if Organization.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('An organisation with that phone already exists.')
        return phone

    def validate_admin_phone(self, value):
        phone = normalize_phone(value)
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('That phone number is already registered.')
        return phone

    @transaction.atomic
    def create(self, validated):
        org = Organization.objects.create(
            name=validated['name'],
            kind=OrgKind.HOSPITAL,
            phone=validated['phone'],
            email=validated.get('email', ''),
            address=validated.get('address', ''),
            registration_no=validated.get('registration_no', ''),
        )
        user = User.objects.create_user(
            phone=validated['admin_phone'],
            password=validated['password'],
            full_name=validated['admin_full_name'],
            organization=org,
            role=Role.ADMIN,
        )
        audit(user, 'Organization', org.pk, 'HOSPITAL_REGISTERED', name=org.name)
        return user


class CompanySerializer(serializers.ModelSerializer):
    """A supplier as the hospital sees it, plus its partnership state."""

    partnership_active = serializers.BooleanField(read_only=True)
    product_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Organization
        fields = [
            'id', 'name', 'kind', 'category', 'phone', 'email', 'address',
            'registration_no', 'is_active', 'partnership_active',
            'product_count', 'created_at',
        ]
        # is_active is the company's own account, not the hospital's to close:
        # clearing it would lock the supplier out of the platform entirely.
        # Suspending trade is DELETE, which flips the partnership instead.
        read_only_fields = ['kind', 'is_active', 'created_at']


class CompanyCreateSerializer(serializers.Serializer):
    """A hospital registers a supplier company and its login account."""

    name = serializers.CharField(max_length=160)
    phone = serializers.CharField(max_length=24)
    category = serializers.ChoiceField(choices=OrgCategory.choices, default=OrgCategory.PHARMACY)
    email = serializers.EmailField(required=False, allow_blank=True)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    registration_no = serializers.CharField(max_length=64, required=False, allow_blank=True)
    contact_full_name = serializers.CharField(max_length=120)
    contact_phone = serializers.CharField(max_length=24)
    password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_phone(self, value):
        phone = normalize_phone(value)
        if Organization.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('A company with that phone already exists.')
        return phone

    def validate_contact_phone(self, value):
        phone = normalize_phone(value)
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('That phone number is already registered.')
        return phone

    @transaction.atomic
    def create(self, validated):
        actor = self.context['request'].user
        supplier = Organization.objects.create(
            name=validated['name'],
            kind=OrgKind.SUPPLIER,
            category=validated['category'],
            phone=validated['phone'],
            email=validated.get('email', ''),
            address=validated.get('address', ''),
            registration_no=validated.get('registration_no', ''),
        )
        User.objects.create_user(
            phone=validated['contact_phone'],
            password=validated['password'],
            full_name=validated['contact_full_name'],
            organization=supplier,
            role=Role.ADMIN,
            must_change_password=True,
        )
        Partnership.objects.create(hospital=actor.scope_org, supplier=supplier)
        audit(actor, 'Organization', supplier.pk, 'COMPANY_REGISTERED', name=supplier.name)
        return supplier


class DepartmentSerializer(serializers.ModelSerializer):
    unit_count = serializers.IntegerField(source='units.count', read_only=True)
    full_name = serializers.CharField(source='__str__', read_only=True)

    class Meta:
        model = Department
        fields = ['id', 'name', 'full_name', 'unit_count', 'is_active']

    def validate_name(self, name):
        # Checked here rather than by the unique_together, because organization
        # comes from the caller rather than the payload.
        clash = Department.objects.filter(
            organization=self.context['request'].user.scope_org, name=name,
        ).exclude(pk=getattr(self.instance, 'pk', None))
        if clash.exists():
            raise serializers.ValidationError('That department already exists.')
        return name


class UnitSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source='department.name', read_only=True)
    full_name = serializers.CharField(source='__str__', read_only=True)

    class Meta:
        model = Unit
        fields = ['id', 'name', 'full_name', 'department', 'department_name', 'is_active']


def visible_terms(user, model):
    """The standard rows of [model] plus whatever this caller's company defined."""
    if user.sees_all_tenants:
        return model.objects.all()
    return model.objects.filter(
        Q(supplier__isnull=True) | Q(supplier=user.scope_org),
    )


class CatalogueTermSerializer(serializers.ModelSerializer):
    """Shared by the dispensing units and the formulations: same shape, same rules."""

    is_shared = serializers.SerializerMethodField()

    class Meta:
        fields = ['id', 'name', 'supplier', 'is_shared', 'is_active']
        read_only_fields = ['supplier']

    def get_is_shared(self, term):
        """A standard term, which no company may rename or retire."""
        return term.supplier_id is None

    def validate_name(self, value):
        # The model's constraints cannot see past NULL, and a clash with a
        # standard term would otherwise slip through as a duplicate offering.
        name = value.strip().upper()
        if not name:
            raise serializers.ValidationError('This needs a name.')
        clash = visible_terms(
            self.context['request'].user, self.Meta.model,
        ).filter(name=name)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('That one already exists.')
        return name


class DispensingUnitSerializer(CatalogueTermSerializer):
    class Meta(CatalogueTermSerializer.Meta):
        model = DispensingUnit


class FormulationSerializer(CatalogueTermSerializer):
    class Meta(CatalogueTermSerializer.Meta):
        model = Formulation


class ProductSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    formulation_name = serializers.SerializerMethodField()
    availability = serializers.CharField(read_only=True)
    available_qty = QuantityField(read_only=True)
    stock_qty = QuantityField(min_value=Decimal('0'), required=False)
    qty_reserved = QuantityField(read_only=True)
    max_order_qty = QuantityField(min_value=HALF_UNIT, required=False, allow_null=True)

    class Meta:
        model = Product
        fields = [
            'id', 'supplier', 'supplier_name', 'generic_name', 'brand', 'strength',
            'formulation', 'formulation_name', 'unit', 'unit_name', 'unit_price',
            'stock_qty', 'qty_reserved', 'available_qty', 'max_order_qty',
            'is_active', 'availability', 'updated_at',
        ]
        read_only_fields = ['supplier', 'qty_reserved']

    def get_formulation_name(self, product):
        """Empty rather than absent, since an item may name no form at all."""
        return product.formulation.name if product.formulation_id else ''

    def _check_own(self, term):
        """Nobody describes an item with a term another company defined."""
        user = self.context['request'].user
        if not visible_terms(user, type(term)).filter(pk=term.pk).exists():
            raise serializers.ValidationError('That belongs to another company.')
        return term

    def validate_unit(self, value):
        return self._check_own(value)

    def validate_formulation(self, value):
        return value if value is None else self._check_own(value)


class RequisitionLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.__str__', read_only=True)
    brand = serializers.CharField(source='product.brand', read_only=True)
    unit = serializers.CharField(source='product.unit.name', read_only=True)
    stock_qty = QuantityField(source='product.stock_qty', read_only=True)
    available_qty = QuantityField(source='product.available_qty', read_only=True)
    qty_supplied = QuantityField(read_only=True)
    qty_accepted = QuantityField(read_only=True)
    qty_requested = QuantityField(min_value=HALF_UNIT, required=False)
    qty_approved = QuantityField(read_only=True)

    class Meta:
        model = RequisitionLine
        fields = [
            'id', 'product', 'product_name', 'brand', 'unit', 'stock_qty', 'available_qty',
            'qty_requested', 'qty_approved', 'qty_supplied', 'qty_accepted',
            'unit_price', 'status', 'supplier_note',
        ]
        read_only_fields = ['qty_approved', 'unit_price', 'status', 'supplier_note']


class DeliveryLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(
        source='requisition_line.product.__str__', read_only=True,
    )
    unit_price = serializers.DecimalField(
        source='requisition_line.unit_price', max_digits=12, decimal_places=2, read_only=True,
    )
    qty_supplied = QuantityField(read_only=True)
    qty_accepted = QuantityField(read_only=True)
    qty_rejected = QuantityField(read_only=True)

    class Meta:
        model = DeliveryLine
        fields = [
            'id', 'requisition_line', 'product_name', 'unit_price', 'qty_supplied',
            'batch_no', 'expiry_date', 'qty_accepted', 'qty_rejected', 'reject_reason',
        ]


class DeliverySerializer(serializers.ModelSerializer):
    lines = DeliveryLineSerializer(many=True, read_only=True)
    requisition_reference = serializers.CharField(source='requisition.reference', read_only=True)
    hospital_name = serializers.CharField(source='requisition.hospital.name', read_only=True)
    supplier_name = serializers.CharField(source='requisition.supplier.name', read_only=True)
    accepted_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    invoice = serializers.SerializerMethodField()

    class Meta:
        model = Delivery
        fields = [
            'id', 'reference', 'requisition', 'requisition_reference', 'hospital_name',
            'supplier_name', 'waybill_no', 'status', 'dispatched_at', 'verified_at',
            'remark', 'accepted_value', 'invoice', 'lines',
        ]

    def get_invoice(self, delivery):
        """Null until the delivery is verified and an invoice is raised."""
        invoice = getattr(delivery, 'invoice', None)
        return InvoiceDetailSerializer(invoice, context=self.context).data if invoice else None


class RequisitionSerializer(serializers.ModelSerializer):
    lines = RequisitionLineSerializer(many=True, read_only=True)
    deliveries = DeliverySerializer(many=True, read_only=True)
    hospital_name = serializers.CharField(source='hospital.name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    department_name = serializers.CharField(source='department_label', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    requested_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    approved_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    item_count = serializers.IntegerField(source='lines.count', read_only=True)

    class Meta:
        model = Requisition
        fields = [
            'id', 'reference', 'hospital', 'hospital_name', 'supplier', 'supplier_name',
            'department', 'unit', 'department_name', 'created_by', 'created_by_name', 'status',
            'note', 'supplier_note', 'requested_value', 'approved_value', 'item_count',
            'created_at', 'submitted_at', 'decided_at', 'closed_at', 'lines', 'deliveries',
        ]
        read_only_fields = [
            'reference', 'hospital', 'created_by', 'status', 'supplier_note',
            'submitted_at', 'decided_at', 'closed_at',
        ]

    def validate(self, attrs):
        instance = self.instance
        unit = attrs.get('unit', getattr(instance, 'unit', None))
        department = attrs.get('department', getattr(instance, 'department', None))
        if unit and department and unit.department_id != department.pk:
            raise serializers.ValidationError(
                {'unit': 'That unit belongs to a different department.'},
            )
        return attrs


class RequisitionListSerializer(RequisitionSerializer):
    """Same shape without the nested lines, for index screens."""

    class Meta(RequisitionSerializer.Meta):
        fields = [f for f in RequisitionSerializer.Meta.fields if f not in ('lines', 'deliveries')]


class WishlistAddSerializer(serializers.Serializer):
    """Add an item to the open draft for whichever supplier owns it."""

    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.filter(is_active=True))
    qty = QuantityField(min_value=HALF_UNIT, default=Decimal('1'))
    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.all(), required=False, allow_null=True,
    )
    unit = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.all(), required=False, allow_null=True,
    )


class DecisionSerializer(serializers.Serializer):
    line = serializers.IntegerField()
    qty_approved = QuantityField(min_value=Decimal('0'))
    note = serializers.CharField(required=False, allow_blank=True)


class DecideSerializer(serializers.Serializer):
    lines = DecisionSerializer(many=True)
    note = serializers.CharField(required=False, allow_blank=True)


class DispatchItemSerializer(serializers.Serializer):
    line = serializers.IntegerField()
    qty = QuantityField(min_value=Decimal('0'))
    batch_no = serializers.CharField(required=False, allow_blank=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)


class DispatchSerializer(serializers.Serializer):
    items = DispatchItemSerializer(many=True)
    waybill_no = serializers.CharField(required=False, allow_blank=True)


class VerifyResultSerializer(serializers.Serializer):
    line = serializers.IntegerField()
    qty_accepted = QuantityField(min_value=Decimal('0'))
    qty_rejected = QuantityField(min_value=Decimal('0'), default=Decimal('0'))
    reason = serializers.CharField(required=False, allow_blank=True)


class VerifySerializer(serializers.Serializer):
    lines = VerifyResultSerializer(many=True)
    remark = serializers.CharField(required=False, allow_blank=True)


class InvoiceSerializer(serializers.ModelSerializer):
    hospital_name = serializers.CharField(source='hospital.name', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    delivery_reference = serializers.CharField(source='delivery.reference', read_only=True)
    requisition_reference = serializers.CharField(
        source='delivery.requisition.reference', read_only=True,
    )
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    amount_pending = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    amount_unclaimed = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'reference', 'delivery', 'delivery_reference', 'requisition_reference',
            'hospital', 'hospital_name', 'supplier', 'supplier_name', 'amount',
            'amount_paid', 'balance', 'amount_pending', 'amount_unclaimed',
            'status', 'terms_days', 'due_date', 'days_overdue', 'is_overdue',
            'issued_at', 'paid_at',
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    invoice_reference = serializers.CharField(source='invoice.reference', read_only=True)
    invoice_amount = serializers.DecimalField(
        source='invoice.amount', max_digits=14, decimal_places=2, read_only=True,
    )
    hospital_name = serializers.CharField(source='invoice.hospital.name', read_only=True)
    supplier_name = serializers.CharField(source='invoice.supplier.name', read_only=True)
    method_display = serializers.CharField(source='get_method_display', read_only=True)
    recorded_by_name = serializers.CharField(source='recorded_by.full_name', read_only=True)
    decided_by_name = serializers.CharField(source='decided_by.full_name', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'reference', 'invoice', 'invoice_reference', 'invoice_amount',
            'hospital_name', 'supplier_name', 'amount', 'method', 'method_display',
            'payer_reference', 'status', 'note', 'receipt', 'recorded_by', 'recorded_by_name',
            'recorded_at', 'decided_by', 'decided_by_name', 'decided_at', 'reject_reason',
        ]
        read_only_fields = fields


class InvoiceDetailSerializer(InvoiceSerializer):
    """The invoice with its ledger attached, for a screen that shows both.

    Kept off the invoice list, where one query per row would pay for a ledger
    nobody is reading.
    """

    payments = PaymentSerializer(many=True, read_only=True)

    class Meta(InvoiceSerializer.Meta):
        fields = [*InvoiceSerializer.Meta.fields, 'payments']
        read_only_fields = fields


class RecordPaymentSerializer(serializers.Serializer):
    """What the hospital declares. The money itself moved at a bank, not here.

    Sent as JSON, or as multipart when a receipt is attached.
    """

    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=PaymentMethod.choices)
    payer_reference = serializers.CharField(max_length=64, required=False, allow_blank=True)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)
    receipt = serializers.FileField(
        required=False, allow_null=True,
        validators=[FileExtensionValidator(RECEIPT_EXTENSIONS)],
    )

    def validate_receipt(self, value):
        if value and value.size > RECEIPT_MAX_BYTES:
            raise serializers.ValidationError(
                f'Receipt must be under {RECEIPT_MAX_BYTES // (1024 * 1024)} MB.'
            )
        return value


class RejectPaymentSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class DispenseSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    qty = QuantityField(min_value=HALF_UNIT)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')


class StockMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.__str__', read_only=True)
    delivery_reference = serializers.CharField(source='delivery.reference', read_only=True)
    # Only ever differs between rows for a superuser, who reads every tenant.
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    qty = QuantityField(read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            'id', 'product', 'product_name', 'organization_name', 'kind', 'qty',
            'delivery', 'delivery_reference', 'note', 'created_at',
        ]


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source='actor.full_name', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id', 'actor', 'actor_name', 'entity', 'entity_id', 'action', 'detail', 'created_at',
        ]


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Current password is wrong.')
        return value
