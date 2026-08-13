from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.db.utils import OperationalError
from django.db.models import (
    BooleanField,
    Count,
    DecimalField,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import filters, mixins, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    action,
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import (
    AllowAny,
    BasePermission,
    IsAuthenticated,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import (
    DEFAULT_REORDER_LEVEL,
    AuditLog,
    CreditNote,
    Delivery,
    Department,
    DispensingUnit,
    Formulation,
    Invoice,
    InvoiceStatus,
    OrgKind,
    Organization,
    Partnership,
    Payment,
    PaymentStatus,
    Product,
    ReqStatus,
    Requisition,
    RequisitionLine,
    Role,
    StockMovement,
    Unit,
    User,
    audit,
    normalize_phone,
)
from .printing import PrintListMixin, PrintMixin
from .serializers import (
    AdjustSerializer,
    AuditLogSerializer,
    ChangePasswordSerializer,
    CompanyCreateSerializer,
    CompanySerializer,
    CreditNoteSerializer,
    DecideSerializer,
    DispenseSerializer,
    DeliverySerializer,
    DepartmentSerializer,
    DispensingUnitSerializer,
    FormulationSerializer,
    DispatchSerializer,
    HospitalRegisterSerializer,
    InvoiceSerializer,
    LoginSerializer,
    OrganizationSerializer,
    PaymentSerializer,
    ProductSerializer,
    QuantityField,
    RaiseCreditSerializer,
    RecordPaymentSerializer,
    RejectCreditSerializer,
    RejectPaymentSerializer,
    RequisitionLineSerializer,
    RequisitionListSerializer,
    RequisitionSerializer,
    StockMovementSerializer,
    UnitSerializer,
    UserCreateSerializer,
    UserSerializer,
    VerifySerializer,
    WishlistAddSerializer,
    visible_terms,
)


def org_scope(user, **filters):
    """The tenant clause for a normal user, and nothing at all for a superuser.

    A platform superuser belongs to no organisation and reads every tenant's
    rows, so its scoping clause is dropped while the view's other filters
    (status, department, search) still apply.
    """
    return {} if user.sees_all_tenants else filters


#: How far back the dashboard trend may be asked to go.
MAX_TREND_MONTHS = 24

#: A quarter ahead is the usual shelf check, and two years is further out than
#: any of this stock is dated, so asking beyond it is asking for the whole list.
DEFAULT_EXPIRY_WINDOW_DAYS = 90
MAX_EXPIRY_WINDOW_DAYS = 730


def requisition_month_field(user):
    """Which date a request is counted under, from this caller's side of it.

    A supplier's month is when the request reached it; a hospital's is when it
    was raised. The dashboard trend and the list it drills into share this, or
    tapping a bar would answer with a different set of rows.
    """
    return 'submitted_at' if user.org_kind == OrgKind.SUPPLIER else 'created_at'


def month_range(value):
    """The half-open [start, next month) range for an ISO date, for filtering."""
    try:
        start = date.fromisoformat(value).replace(day=1)
    except ValueError:
        raise ValidationError({'month': 'Expected a date like 2026-08-01.'})
    return start, (start + timedelta(days=31)).replace(day=1)


def require_org(user):
    """Guard writes that hang off an organisation the caller does not have."""
    if not user.scope_org_id:
        raise PermissionDenied('This account is not attached to an organisation.')


class IsOrgAdmin(BasePermission):
    message = 'Only an organisation administrator can do this.'

    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.is_org_admin)


class IsHospital(BasePermission):
    message = 'Only hospital users can do this.'

    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        # A superuser reads every hospital's data and edits any of it: the row
        # it acts on says which tenant that is (see services.require_owner).
        # Creating one from nothing still needs a tenant named up front, which
        # `require_org` below asks for.
        if user.sees_all_tenants:
            return True
        return user.org_kind == OrgKind.HOSPITAL


# The router actions that change data, as opposed to reading it.
WRITE_ACTIONS = ('create', 'update', 'partial_update', 'destroy')


def token_payload(user):
    token, _ = Token.objects.get_or_create(user=user)
    # Signing in is activity. Without this the reused token would still carry
    # the idle clock of the session before it.
    User.objects.filter(pk=user.pk).update(last_seen_at=timezone.now())
    return {'token': token.key, 'user': UserSerializer(user).data}


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    """Is the process up, and can it still reach its database?

    Unauthenticated on purpose: whatever watches this — a load balancer, an
    uptime check, a container's readiness probe — holds no token, and the answer
    tells a stranger nothing they could not learn by knocking on the login door.
    Django keeps connections open between requests, so a database that went away
    is only found by asking it something.
    """
    try:
        connection.ensure_connection()
    except OperationalError:
        return Response(
            {'status': 'error', 'database': 'unreachable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({'status': 'ok'})


class RegisterHospitalView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    # Nothing but the throttle stands between this and a table full of tenants
    # nobody asked for.
    throttle_scope = 'register'

    def post(self, request):
        serializer = HospitalRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(token_payload(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    # A client still holding the token that just timed out must be able to sign
    # in again; authenticating first would refuse it before it got the chance.
    authentication_classes = []
    # A phone number and a password is the whole door, so guessing at it is
    # rationed by address. See DEFAULT_THROTTLE_RATES.
    throttle_scope = 'login'

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        audit(user, 'User', user.pk, 'LOGIN')
        return Response(token_payload(user))


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        data = UserSerializer(request.user).data
        data['organization_detail'] = (
            OrganizationSerializer(request.user.scope_org).data
            if request.user.scope_org_id else None
        )
        return Response(data)

    def patch(self, request):
        # Role is granted by an administrator through /users/, never by the
        # account itself, or any member of staff could promote themselves.
        # is_active goes the same way: closing an account is an administrator's
        # call, and /users/ refuses to close the last one an organisation has.
        # Left open here, that check is walked round by the last administrator
        # closing their own account and locking the tenant out of itself.
        data = {
            key: value for key, value in request.data.items()
            if key not in ('role', 'is_active')
        }
        serializer = UserSerializer(request.user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ChangePasswordView(APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])
        Token.objects.filter(user=user).delete()
        audit(user, 'User', user.pk, 'PASSWORD_CHANGED')
        return Response(token_payload(user))


class OrganizationView(APIView):
    """The caller's own organisation profile."""

    def get(self, request):
        if not request.user.scope_org_id:
            raise PermissionDenied('No organisation attached to this account.')
        return Response(OrganizationSerializer(request.user.scope_org).data)

    def patch(self, request):
        if not request.user.is_org_admin:
            raise PermissionDenied('Only an administrator can edit the organisation.')
        serializer = OrganizationSerializer(
            request.user.scope_org, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@api_view(['GET'])
def organizations(request):
    """Every tenant, for the superuser's switcher and for a create that names
    the organisation it belongs to.

    Read straight from the table. Unlike `/companies/`, which is the hospital's
    list of the suppliers it trades with, this does not care which tenant the
    superuser is standing in, so it is still there to step back out of one.
    """
    if not request.user.is_superuser:
        raise PermissionDenied('Only a platform superuser can list every organisation.')
    return Response(
        OrganizationSerializer(Organization.objects.order_by('name'), many=True).data,
    )


class CompanyViewSet(viewsets.ModelViewSet):
    """Supplier companies as managed by a hospital."""

    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, IsHospital]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'phone', 'category']
    ordering_fields = ['name', 'created_at']
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        # This list is the supplier companies a hospital trades with, so a
        # hospital has no place on it — least of all as something to register a
        # request against.
        queryset = Organization.objects.filter(kind=OrgKind.SUPPLIER)
        if user.sees_all_tenants:
            # No hospital of its own, so nothing is suspended for it.
            queryset = queryset.annotate(
                partnership_active=Value(True, output_field=BooleanField()),
            )
        else:
            links = Partnership.objects.filter(hospital=user.scope_org)
            queryset = queryset.filter(id__in=links.values('supplier')).annotate(
                # Suspended companies stay on the list, marked, so the hospital
                # can see its history and trade with them again.
                partnership_active=Subquery(
                    links.filter(supplier=OuterRef('pk')).values('is_active')[:1],
                ),
            )
        return queryset.annotate(
            product_count=Count('products', filter=Q(products__is_active=True)),
        ).order_by('name')

    def get_permissions(self):
        # Registering a company, editing it or suspending the trading link is
        # an administrator's call; every member of staff may read the list.
        if self.action in WRITE_ACTIONS + ('reactivate', 'link'):
            return [IsAuthenticated(), IsHospital(), IsOrgAdmin()]
        return [IsAuthenticated(), IsHospital()]

    def get_serializer_class(self):
        return CompanyCreateSerializer if self.action == 'create' else CompanySerializer

    def create(self, request, *args, **kwargs):
        serializer = CompanyCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        supplier = serializer.save()
        return Response(CompanySerializer(supplier).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        """Suspend the trading link. The company and its history stay."""
        # A link joins two organisations, so suspending one means saying which
        # hospital is dropping the company. A superuser names it by stepping in.
        require_org(self.request.user)
        Partnership.objects.filter(
            hospital=self.request.user.scope_org, supplier=instance,
        ).update(is_active=False)
        audit(self.request.user, 'Partnership', instance.pk, 'SUSPENDED', company=instance.name)

    @action(detail=False, methods=['post'])
    def link(self, request):
        """Start trading with a company already on the platform.

        `create` above puts a new company on the platform and opens its login
        account. This opens the trading link to one that is already there,
        because another hospital registered it first — without which a supplier
        can only ever serve the hospital that typed it in.

        The company is named by its phone number, which is what it hands out and
        what the platform already treats as its identity. Nothing is created:
        the company keeps its own account, its own catalogue and its own staff.
        """
        require_org(request.user)
        phone = normalize_phone(request.data.get('phone'))
        supplier = (
            Organization.objects.filter(phone=phone, kind=OrgKind.SUPPLIER).first()
            if phone else None
        )
        if supplier is None:
            raise ValidationError({'phone': 'No supplier company with that phone number.'})
        # get_or_create rather than create: a link suspended long ago is the
        # same link, and reopening it is what `reactivate` does by another road.
        link, created = Partnership.objects.get_or_create(
            hospital=request.user.scope_org, supplier=supplier,
        )
        if not created and link.is_active:
            raise ValidationError('You already trade with that company.')
        if not link.is_active:
            link.is_active = True
            link.save(update_fields=['is_active'])
        audit(request.user, 'Partnership', supplier.pk, 'LINKED', company=supplier.name)
        # Read back through the queryset, which carries the partnership state and
        # the item count the list shows.
        return Response(
            CompanySerializer(self.get_queryset().get(pk=supplier.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def reactivate(self, request, pk=None):
        company = self.get_object()
        require_org(request.user)
        Partnership.objects.filter(
            hospital=request.user.scope_org, supplier=company,
        ).update(is_active=True)
        audit(request.user, 'Partnership', company.pk, 'REACTIVATED', company=company.name)
        return Response(CompanySerializer(company).data)


class UserViewSet(viewsets.ModelViewSet):
    """Staff accounts inside the caller's own organisation."""

    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['full_name', 'phone', 'job_title']

    def get_queryset(self):
        user = self.request.user
        return User.objects.filter(**org_scope(user, organization=user.scope_org))

    def get_serializer_class(self):
        return UserCreateSerializer if self.action == 'create' else UserSerializer

    def perform_create(self, serializer):
        require_org(self.request.user)
        serializer.save()

    def get_permissions(self):
        # This override replaces the one the @action carries, so reset_password
        # has to be named here or it falls back to any authenticated member of
        # staff — who could then reset an administrator's password and log in
        # as them.
        if self.action in WRITE_ACTIONS + ('reset_password',):
            return [IsAuthenticated(), IsOrgAdmin()]
        return [IsAuthenticated()]

    @staticmethod
    def _is_last_admin(instance):
        """Is this the only administrator the organisation has left?

        An organisation with no active administrator is locked out of its own
        account management: nobody can add a user, change a role or reset a
        password again, and only the platform can put it right.
        """
        if instance.role != Role.ADMIN or not instance.is_active:
            return False
        return not User.objects.filter(
            organization_id=instance.organization_id, role=Role.ADMIN, is_active=True,
        ).exclude(pk=instance.pk).exists()

    def perform_update(self, serializer):
        instance = serializer.instance
        data = serializer.validated_data
        # Checked before the save, so a refusal leaves the row untouched.
        steps_down = (
            data.get('role', instance.role) != Role.ADMIN
            or not data.get('is_active', instance.is_active)
        )
        if steps_down and self._is_last_admin(instance):
            raise ValidationError(
                'This is the only administrator left. Promote someone else first.'
            )
        was_active = instance.is_active
        user = serializer.save()
        if user.is_active and not was_active:
            Token.objects.filter(user=user).delete()
            audit(self.request.user, 'User', user.pk, 'ENABLED')
        elif was_active and not user.is_active:
            Token.objects.filter(user=user).delete()
            audit(self.request.user, 'User', user.pk, 'DISABLED')

    def perform_destroy(self, instance):
        if instance == self.request.user:
            raise ValidationError('You cannot disable your own account.')
        if self._is_last_admin(instance):
            raise ValidationError(
                'This is the only administrator left. Promote someone else first.'
            )
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        Token.objects.filter(user=instance).delete()
        audit(self.request.user, 'User', instance.pk, 'DISABLED')

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def reset_password(self, request, pk=None):
        user = self.get_object()
        new_password = request.data.get('new_password') or ''
        # The same validators sign-up and change-password run. A reset is the
        # one password path an administrator picks for somebody else, which is
        # the last place a weak one should be easier to set than a strong one.
        try:
            validate_password(new_password, user)
        except DjangoValidationError as error:
            raise ValidationError({'new_password': error.messages})
        user.set_password(new_password)
        user.must_change_password = True
        user.save(update_fields=['password', 'must_change_password'])
        Token.objects.filter(user=user).delete()
        audit(request.user, 'User', user.pk, 'PASSWORD_RESET')
        return Response({'detail': 'Password reset. The user must change it at next login.'})


class DepartmentViewSet(viewsets.ModelViewSet):
    """The caller's own departments."""

    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']

    def get_queryset(self):
        user = self.request.user
        return Department.objects.filter(**org_scope(user, organization=user.scope_org))

    def get_permissions(self):
        # Every member of staff reads the list, because raising a request means
        # picking a department. Redrawing the organisation is an administrator's
        # call, like registering a company or opening a staff account.
        if self.action in WRITE_ACTIONS:
            return [IsAuthenticated(), IsHospital(), IsOrgAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        require_org(self.request.user)
        serializer.save(organization=self.request.user.scope_org)


class UnitViewSet(viewsets.ModelViewSet):
    """Units of the caller's departments. `?department=<id>` narrows to one."""

    serializer_class = UnitSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'department__name']

    def get_queryset(self):
        user = self.request.user
        queryset = Unit.objects.filter(
            **org_scope(user, department__organization=user.scope_org),
        ).select_related('department')
        department = self.request.query_params.get('department')
        return queryset.filter(department_id=department) if department else queryset

    def get_permissions(self):
        if self.action in WRITE_ACTIONS:
            return [IsAuthenticated(), IsHospital(), IsOrgAdmin()]
        return [IsAuthenticated()]

    def _check_department(self, serializer):
        department = serializer.validated_data.get('department')
        if department and department.organization_id != self.request.user.scope_org_id:
            raise ValidationError('That department belongs to another organisation.')

    def perform_create(self, serializer):
        require_org(self.request.user)
        self._check_department(serializer)
        serializer.save()

    def perform_update(self, serializer):
        services.require_owner(self.request.user, serializer.instance.department.organization)
        require_org(self.request.user)
        self._check_department(serializer)
        serializer.save()


class CatalogueTermViewSet(viewsets.ModelViewSet):
    """The standard terms of one kind, plus the ones this company defined itself.

    Subclassed once per vocabulary; `model` and `serializer_class` are all a
    subclass carries, since the rules are the same for every one of them.
    """

    model = None
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']
    # A picker needs every row at once, and there are only ever a few dozen.
    pagination_class = None

    def get_queryset(self):
        return visible_terms(self.request.user, self.model)

    def get_permissions(self):
        # Every account reads the list, because a hospital reading a catalogue
        # is reading the terms each item is described by. Defining one is a
        # supplier administrator's call, like the catalogue itself.
        if self.action in WRITE_ACTIONS:
            return [IsAuthenticated(), IsOrgAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        user = self.request.user
        if user.org_kind != OrgKind.SUPPLIER:
            raise PermissionDenied('Only a supplier can define one of these.')
        require_org(user)
        term = serializer.save(supplier=user.scope_org)
        audit(user, self.model.__name__, term.pk, 'CREATED', name=term.name)

    def _require_own(self, term):
        """A standard term is nobody's to edit; another company's is not mine."""
        if term.supplier_id is None:
            raise PermissionDenied('A standard entry cannot be changed.')
        services.require_owner(
            self.request.user, term.supplier,
            'Only the company that defined this can change it.',
        )

    def perform_update(self, serializer):
        self._require_own(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._require_own(instance)
        # The column protects itself, but a database error is not an answer.
        if instance.products.exists():
            raise ValidationError('Items in the catalogue still use this.')
        instance.delete()


class DispensingUnitViewSet(CatalogueTermViewSet):
    model = DispensingUnit
    serializer_class = DispensingUnitSerializer


class FormulationViewSet(CatalogueTermViewSet):
    model = Formulation
    serializer_class = FormulationSerializer


class ProductViewSet(viewsets.ModelViewSet):
    """Suppliers manage their own catalogue; hospitals read their partners' catalogues."""

    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'generic_name', 'brand', 'formulation__name', 'strength', 'supplier__name',
    ]
    ordering_fields = ['generic_name', 'unit_price', 'stock_qty', 'updated_at']

    def get_queryset(self):
        user = self.request.user
        if user.org_kind == OrgKind.SUPPLIER:
            return Product.objects.filter(supplier=user.scope_org)
        if user.sees_all_tenants:
            queryset = Product.objects.all()
        else:
            partners = Partnership.objects.filter(
                hospital=user.scope_org, is_active=True,
            ).values('supplier')
            queryset = Product.objects.filter(supplier__in=partners, is_active=True)
        supplier_id = self.request.query_params.get('supplier')
        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)
        if self.request.query_params.get('available') == 'true':
            queryset = queryset.filter(stock_qty__gt=F('qty_reserved'))
        return queryset.select_related('supplier', 'unit', 'formulation')

    def get_permissions(self):
        # Prices and stock levels are what the company sells on, so every member
        # of staff reads the catalogue and only an administrator moves it.
        if self.action in WRITE_ACTIONS + ('restock',):
            return [IsAuthenticated(), IsOrgAdmin()]
        return [IsAuthenticated()]

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        if user.org_kind != OrgKind.SUPPLIER:
            raise PermissionDenied('Only a supplier can add catalogue items.')
        product = serializer.save(supplier=user.scope_org)
        # An item that arrives with stock on it has to say where that stock came
        # from, or the ledger starts life owing the shelf its opening figure and
        # never catches up: every later movement is recorded, only this one was
        # not. `restock` writes the same row for every move after this one.
        if product.stock_qty:
            StockMovement.objects.create(
                organization=product.supplier, product=product,
                kind=StockMovement.ADJUST, qty=product.stock_qty, note='Opening stock',
            )
        audit(
            user, 'Product', product.pk, 'CREATED',
            name=str(product), opening_stock=product.stock_qty,
        )

    def perform_update(self, serializer):
        services.require_owner(
            self.request.user, serializer.instance.supplier,
            'Only the owning supplier can edit an item.',
        )
        product = serializer.save()
        audit(self.request.user, 'Product', product.pk, 'UPDATED', stock=product.stock_qty)

    def perform_destroy(self, instance):
        services.require_owner(
            self.request.user, instance.supplier,
            'Only the owning supplier can remove an item.',
        )
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        audit(self.request.user, 'Product', instance.pk, 'DEACTIVATED')

    @action(detail=True, methods=['post'])
    def restock(self, request, pk=None):
        """Add (or subtract, with a negative value) supplier stock."""
        product = self.get_object()
        services.require_owner(
            request.user, product.supplier, 'Only the owning supplier can restock.',
        )
        # Signed, so no minimum: a negative figure takes stock back off.
        qty = QuantityField().run_validation(request.data.get('qty'))
        if product.stock_qty + qty < product.qty_reserved:
            raise ValidationError(
                f'That would take stock below the {product.qty_reserved} already '
                f'promised to approved requests.'
            )
        product.stock_qty += qty
        product.save(update_fields=['stock_qty'])
        StockMovement.objects.create(
            organization=product.supplier, product=product,
            kind=StockMovement.ADJUST, qty=qty, note=request.data.get('note', ''),
        )
        audit(request.user, 'Product', product.pk, 'RESTOCKED', qty=qty)
        return Response(ProductSerializer(product).data)


def _check_own_tags(user, data, instance=None):
    """A request may only be tagged with the caller's own live department or unit.

    Retiring one is how an organisation stops new work being filed under it. The
    requests already tagged with it stay where they are — `instance` is the one
    being edited, and a tag it already carries is left alone — and it keeps its
    place in the list, so it can be brought back.
    """
    for field in ('department', 'unit'):
        tag = data.get(field)
        if not tag:
            continue
        if tag.organization_id != user.scope_org_id:
            raise ValidationError(f'That {field} belongs to another organisation.')
        if instance is not None and getattr(instance, f'{field}_id') == tag.pk:
            continue
        # A department stands for its units here as it does everywhere else, so
        # retiring one closes the units under it too — otherwise the work goes
        # on being filed one level down.
        if not tag.is_active or (field == 'unit' and not tag.department.is_active):
            raise ValidationError(f'That {field} has been retired.')


class RequisitionViewSet(PrintMixin, viewsets.ModelViewSet):
    print_kind = 'requisition'
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'reference', 'supplier__name', 'hospital__name', 'note',
        'department__name', 'unit__name',
    ]
    ordering_fields = ['created_at', 'submitted_at', 'status']

    def get_queryset(self):
        user = self.request.user
        if user.sees_all_tenants:
            queryset = Requisition.objects.all()
        elif user.org_kind == OrgKind.SUPPLIER:
            # A supplier never sees another tenant's drafts.
            queryset = Requisition.objects.filter(
                supplier=user.scope_org,
            ).exclude(status=ReqStatus.DRAFT)
        else:
            queryset = Requisition.objects.filter(hospital=user.scope_org)
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status__in=status_param.split(','))
        department = self.request.query_params.get('department')
        if department:
            # A department stands for itself and its units, so asking for the
            # laboratory answers with haematology's requests too.
            queryset = queryset.filter(
                Q(department_id=department) | Q(unit__department_id=department),
            )
        unit = self.request.query_params.get('unit')
        if unit:
            queryset = queryset.filter(unit_id=unit)
        month = self.request.query_params.get('month')
        if month:
            # The dashboard's trend drills in here, so it counts the same date.
            start, end = month_range(month)
            dated = requisition_month_field(user)
            queryset = queryset.filter(**{f'{dated}__date__gte': start, f'{dated}__date__lt': end})
        queryset = queryset.select_related(
            'hospital', 'supplier', 'department', 'unit__department', 'created_by',
        )
        # Only the two read actions prefetch. `submit`, `decide` and `release`
        # all edit the lines and then serialise the same requisition object, and
        # a prefetch filled in before the edit would answer with what the lines
        # said beforehand.
        #
        # requested_value, approved_value and item_count each walk the lines, so
        # an index page of 25 asks 75 questions without this. The list serializer
        # does not print the lines; it only adds them up.
        if self.action == 'list':
            return queryset.prefetch_related('lines')
        if self.action == 'retrieve':
            return queryset.prefetch_related(
                'lines__product__unit',
                'lines__delivery_lines',
                'deliveries__lines__requisition_line__product',
                'deliveries__invoice__payments',
            )
        return queryset

    def get_serializer_class(self):
        return RequisitionListSerializer if self.action == 'list' else RequisitionSerializer

    def perform_create(self, serializer):
        user = self.request.user
        if user.org_kind != OrgKind.HOSPITAL:
            raise PermissionDenied('Only a hospital can raise a request.')
        supplier = serializer.validated_data['supplier']
        services.require_partner(user.scope_org, supplier)
        _check_own_tags(user, serializer.validated_data)
        # A second draft for the same company and department would be a basket
        # nobody can reach: adding from the catalogue only ever finds one.
        tags = {
            'department': serializer.validated_data.get('department'),
            'unit': serializer.validated_data.get('unit'),
        }
        if self._open_drafts(user, supplier).filter(**tags).exists():
            raise ValidationError(
                f'You already have an open request for {supplier.name} against that '
                f'department. Add to it, or submit it first.'
            )
        serializer.save(hospital=user.scope_org, created_by=user)

    @staticmethod
    def _open_drafts(user, supplier):
        return Requisition.objects.filter(
            hospital=user.scope_org, supplier=supplier,
            status=ReqStatus.DRAFT, created_by=user,
        )

    def perform_update(self, serializer):
        instance = serializer.instance
        services.require_owner(self.request.user, instance.hospital, 'Not your requisition.')
        if instance.status != ReqStatus.DRAFT:
            raise ValidationError('Only a draft request can be edited.')
        _check_own_tags(self.request.user, serializer.validated_data, instance)
        # Retagging must not collide with the draft that tag already owns,
        # which the unique constraint would otherwise refuse mid-save.
        tags = {
            field: serializer.validated_data.get(field, getattr(instance, field))
            for field in ('department', 'unit')
        }
        clash = (
            self._open_drafts(self.request.user, instance.supplier)
            .filter(**tags)
            .exclude(pk=instance.pk)
        )
        if clash.exists():
            raise ValidationError(
                'You already have an open request for that company against that department. '
                'Add to it, or submit it first.'
            )
        serializer.save()

    def perform_destroy(self, instance):
        services.require_owner(self.request.user, instance.hospital, 'Not your requisition.')
        if instance.status != ReqStatus.DRAFT:
            raise ValidationError('Only a draft request can be deleted.')
        instance.delete()

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        requisition = services.submit(self.get_object(), request.user)
        return Response(RequisitionSerializer(requisition).data)

    # Approving commits stock and money, so it is the administrator's signature.
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def decide(self, request, pk=None):
        serializer = DecideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requisition = services.decide(
            self.get_object(), request.user,
            serializer.validated_data['lines'],
            serializer.validated_data.get('note', ''),
        )
        return Response(RequisitionSerializer(requisition).data)

    # Named dispatch_consignment because `dispatch` is Django's request entry point.
    @action(detail=True, methods=['post'], url_path='dispatch')
    def dispatch_consignment(self, request, pk=None):
        serializer = DispatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        delivery = services.dispatch(
            self.get_object(), request.user,
            serializer.validated_data['items'],
            serializer.validated_data.get('waybill_no', ''),
        )
        return Response(DeliverySerializer(delivery).data, status=status.HTTP_201_CREATED)

    # Cuts back an approval, so it answers to whoever could have made it.
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def release(self, request, pk=None):
        """Supplier hands back approved stock it is not going to ship."""
        requisition = services.release(
            self.get_object(), request.user, request.data.get('reason', ''),
        )
        return Response(RequisitionSerializer(requisition).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        requisition = services.cancel(
            self.get_object(), request.user, request.data.get('reason', ''),
        )
        return Response(RequisitionSerializer(requisition).data)

    @action(detail=False, methods=['get'])
    def wishlist(self, request):
        """Open drafts, one per supplier."""
        drafts = self.get_queryset().filter(status=ReqStatus.DRAFT)
        return Response(RequisitionSerializer(drafts, many=True).data)

    @action(detail=False, methods=['post'], url_path='wishlist/add')
    def wishlist_add(self, request):
        """Add a catalogue item to the open draft for its supplier."""
        user = request.user
        if user.org_kind != OrgKind.HOSPITAL:
            raise PermissionDenied('Only a hospital can build a wishlist.')
        serializer = WishlistAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = serializer.validated_data['product']
        qty = serializer.validated_data['qty']
        _check_own_tags(user, serializer.validated_data)
        services.require_partner(user.scope_org, product.supplier)

        # The open draft for that company carrying the same tag: a laboratory
        # basket must not swallow an item picked for haematology.
        tags = {
            'department': serializer.validated_data.get('department'),
            'unit': serializer.validated_data.get('unit'),
        }
        requisition = self._open_drafts(user, product.supplier).filter(**tags).first()
        if requisition is None:
            requisition = Requisition.objects.create(
                hospital=user.scope_org,
                supplier=product.supplier,
                status=ReqStatus.DRAFT,
                created_by=user,
                **tags,
            )
        line, created = RequisitionLine.objects.get_or_create(
            requisition=requisition, product=product,
            defaults={'qty_requested': qty, 'unit_price': product.unit_price},
        )
        if not created:
            line.qty_requested += qty
            line.save(update_fields=['qty_requested'])
        return Response(RequisitionSerializer(requisition).data, status=status.HTTP_201_CREATED)


class RequisitionLineViewSet(
    mixins.UpdateModelMixin, mixins.DestroyModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    """Line editing, allowed only while the request is still a draft."""

    serializer_class = RequisitionLineSerializer
    permission_classes = [IsAuthenticated, IsHospital]

    def get_queryset(self):
        user = self.request.user
        return RequisitionLine.objects.filter(
            **org_scope(user, requisition__hospital=user.scope_org),
        ).select_related('requisition', 'product__unit')

    def _check_draft(self, requisition):
        if requisition.status != ReqStatus.DRAFT:
            raise ValidationError('This request has been submitted and can no longer be edited.')

    def perform_create(self, serializer):
        user = self.request.user
        requisition = Requisition.objects.filter(
            pk=self.request.data.get('requisition'),
            **org_scope(user, hospital=user.scope_org),
        ).first()
        if requisition is None:
            raise ValidationError('Unknown request.')
        services.require_owner(user, requisition.hospital, 'Not your requisition.')
        self._check_draft(requisition)
        product = serializer.validated_data['product']
        if product.supplier_id != requisition.supplier_id:
            raise ValidationError('That item is not sold by this company.')
        # The same item asked for twice is one line of more of it, as it is
        # from the wishlist. A second row would break the unique constraint.
        line = requisition.lines.filter(product=product).first()
        if line is not None:
            line.qty_requested += serializer.validated_data.get('qty_requested', Decimal('1'))
            line.save(update_fields=['qty_requested'])
            serializer.instance = line
            return
        serializer.save(requisition=requisition, unit_price=product.unit_price)

    def perform_update(self, serializer):
        line = serializer.instance
        self._check_draft(line.requisition)
        # A line is a quantity of one item. Swapping the item is asking for a
        # different line: it would walk past the supplier check `perform_create`
        # makes, putting another company's product on the request, and land on
        # the (requisition, product) unique constraint if the request already
        # carries the item. Remove the line and add the other one.
        product = serializer.validated_data.get('product')
        if product is not None and product.pk != line.product_id:
            raise ValidationError(
                'A line cannot change its item. Remove it and add the other one.'
            )
        serializer.save()

    def perform_destroy(self, instance):
        self._check_draft(instance.requisition)
        instance.delete()


class DeliveryViewSet(PrintMixin, viewsets.ReadOnlyModelViewSet):
    print_kind = 'delivery'
    serializer_class = DeliverySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = [
        'reference', 'waybill_no', 'requisition__reference',
        'requisition__department__name', 'requisition__unit__name',
    ]

    def get_queryset(self):
        user = self.request.user
        field = (
            'requisition__supplier' if user.org_kind == OrgKind.SUPPLIER
            else 'requisition__hospital'
        )
        queryset = Delivery.objects.filter(
            **org_scope(user, **{field: user.scope_org}),
        ).select_related('requisition__hospital', 'requisition__supplier')
        if self.action not in ('list', 'retrieve'):
            # `verify` writes the accepted and rejected quantities onto these
            # lines and then serialises this same delivery. A cache filled in
            # beforehand would report the consignment as it arrived.
            return queryset
        return queryset.prefetch_related(
            # accepted_value adds up the lines, and each line names its product.
            'lines__requisition_line__product',
            'invoice__payments',
        )

    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        serializer = VerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        delivery = services.verify(
            self.get_object(), request.user,
            serializer.validated_data['lines'],
            serializer.validated_data.get('remark', ''),
        )
        return Response(DeliverySerializer(delivery).data)


class InvoiceViewSet(PrintMixin, viewsets.ReadOnlyModelViewSet):
    print_kind = 'invoice'
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = [
        'reference', 'delivery__reference',
        'delivery__requisition__department__name',
        'delivery__requisition__unit__name',
    ]

    def get_queryset(self):
        user = self.request.user
        field = 'supplier' if user.org_kind == OrgKind.SUPPLIER else 'hospital'
        queryset = Invoice.objects.filter(
            **org_scope(user, **{field: user.scope_org}),
        ).select_related(
            'hospital', 'supplier', 'delivery__requisition',
        ).prefetch_related(
            # amount_pending and amount_unclaimed both read the ledger.
            'payments',
        )
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        # Past its due date and still owing. Oldest debt first, since that is
        # the one being chased.
        if self.request.query_params.get('overdue') in ('1', 'true', 'True'):
            queryset = queryset.exclude(status=InvoiceStatus.PAID).filter(
                due_date__lt=timezone.localdate(),
            ).order_by('due_date')
        return queryset

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def pay(self, request, pk=None):
        """Hospital records a payment it has made. The supplier confirms it next.

        JSON, or multipart when a receipt image or PDF comes with it.
        """
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = services.record_payment(
            self.get_object(), request.user, **serializer.validated_data,
        )
        return Response(
            PaymentSerializer(payment, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['get'])
    def payments(self, request, pk=None):
        """Everything recorded against one invoice, decided or not."""
        invoice = self.get_object()
        return Response(
            PaymentSerializer(
                invoice.payments.all(), many=True, context={'request': request},
            ).data,
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def credit(self, request, pk=None):
        """Hospital raises a credit for goods it accepted and then found bad.

        Nothing moves until the supplier accepts it — see `/credits/`.
        """
        serializer = RaiseCreditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        credit = services.raise_credit(
            self.get_object(), request.user,
            serializer.validated_data['lines'],
            serializer.validated_data['reason'],
        )
        return Response(CreditNoteSerializer(credit).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def creditable(self, request, pk=None):
        """What is still open to credit on this invoice, line by line.

        The screen that raises one needs to know what is left after earlier
        notes, and working that out client-side is how two of them come to
        disagree.
        """
        invoice = self.get_object()
        lines = invoice.delivery.lines.select_related(
            'requisition_line__product',
        ).prefetch_related('credit_lines__credit_note')
        return Response([
            {
                'line': line.id,
                'product_name': str(line.requisition_line.product),
                'unit_price': line.requisition_line.unit_price,
                'qty_accepted': line.qty_accepted,
                'qty_creditable': services.creditable_qty(line),
            }
            for line in lines
        ])


class PaymentViewSet(PrintMixin, viewsets.ReadOnlyModelViewSet):
    """The payment ledger. Recorded through `/invoices/{id}/pay/`, decided here."""

    print_kind = 'payment'
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = [
        'reference', 'payer_reference', 'invoice__reference',
        'invoice__delivery__requisition__department__name',
        'invoice__delivery__requisition__unit__name',
    ]

    def get_queryset(self):
        user = self.request.user
        field = (
            'invoice__supplier' if user.org_kind == OrgKind.SUPPLIER else 'invoice__hospital'
        )
        queryset = Payment.objects.filter(
            **org_scope(user, **{field: user.scope_org}),
        ).select_related('invoice__hospital', 'invoice__supplier', 'recorded_by', 'decided_by')
        for param, column in (('status', 'status'), ('invoice', 'invoice_id')):
            value = self.request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{column: value})
        return queryset

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def confirm(self, request, pk=None):
        payment = services.confirm_payment(self.get_object(), request.user)
        return Response(PaymentSerializer(payment, context={'request': request}).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def withdraw(self, request, pk=None):
        """Hospital takes back an entry the supplier has not decided yet."""
        payment = services.withdraw_payment(
            self.get_object(), request.user, request.data.get('reason', ''),
        )
        return Response(PaymentSerializer(payment, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def receipt(self, request, pk=None):
        """The teller slip attached to a payment.

        Served through the viewset because the queryset above has already
        decided whose payment this is, and that is the same question as who may
        look at its slip. Handing the file out on a MEDIA_URL path instead
        answers it with nothing but an address that is hard to guess.
        """
        payment = self.get_object()
        if not payment.receipt:
            raise Http404('No receipt was attached to this payment.')
        return FileResponse(
            payment.receipt.open('rb'), filename=Path(payment.receipt.name).name,
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def reject(self, request, pk=None):
        serializer = RejectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = services.reject_payment(
            self.get_object(), request.user, serializer.validated_data['reason'],
        )
        return Response(PaymentSerializer(payment, context={'request': request}).data)


class CreditNoteViewSet(viewsets.ReadOnlyModelViewSet):
    """Credit notes. Raised through `/invoices/{id}/credit/`, decided here.

    The same shape as the payment ledger, because it is the same kind of thing:
    one side says what it thinks it owes, the other says whether it agrees, and
    only agreement moves the invoice.
    """

    serializer_class = CreditNoteSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['reference', 'reason', 'invoice__reference']

    def get_queryset(self):
        user = self.request.user
        field = (
            'invoice__supplier' if user.org_kind == OrgKind.SUPPLIER else 'invoice__hospital'
        )
        queryset = CreditNote.objects.filter(
            **org_scope(user, **{field: user.scope_org}),
        ).select_related(
            'invoice__hospital', 'invoice__supplier', 'raised_by', 'decided_by',
        ).prefetch_related('lines__delivery_line__requisition_line__product')
        for param, column in (('status', 'status'), ('invoice', 'invoice_id')):
            value = self.request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{column: value})
        return queryset

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def confirm(self, request, pk=None):
        credit = services.confirm_credit(self.get_object(), request.user)
        return Response(CreditNoteSerializer(credit).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def reject(self, request, pk=None):
        serializer = RejectCreditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        credit = services.reject_credit(
            self.get_object(), request.user, serializer.validated_data['reason'],
        )
        return Response(CreditNoteSerializer(credit).data)


class StockMovementViewSet(PrintListMixin, viewsets.ReadOnlyModelViewSet):
    print_kind = 'stock-ledger'
    serializer_class = StockMovementSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['product__generic_name', 'product__brand', 'kind', 'note']

    def get_queryset(self):
        user = self.request.user
        queryset = StockMovement.objects.filter(
            **org_scope(user, organization=user.scope_org),
        ).select_related('product', 'organization', 'delivery')
        params = self.request.query_params
        for param, clause in (
            ('product', 'product_id'),
            ('kind', 'kind'),
            # Inclusive on both ends, because a user picking 1-31 March means March.
            ('from', 'created_at__date__gte'),
            ('to', 'created_at__date__lte'),
        ):
            value = params.get(param)
            if value:
                queryset = queryset.filter(**{clause: value})
        return queryset

    @action(detail=False, methods=['post'])
    def dispense(self, request):
        """A hospital records what it handed out. It needs nobody's approval."""
        serializer = DispenseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        movement = services.dispense(request.user, **serializer.validated_data)
        return Response(StockMovementSerializer(movement).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated, IsOrgAdmin])
    def adjust(self, request):
        """A hospital writes stock off, or puts a miscount right. Signed."""
        serializer = AdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        movement = services.adjust(request.user, **serializer.validated_data)
        return Response(StockMovementSerializer(movement).data, status=status.HTTP_201_CREATED)

    @action(detail=False)
    def expiring(self, request):
        """Goods received with an expiry date inside the window, soonest first.

        Receipts rather than balances: a dispense names no batch, so the ledger
        cannot say how much of one particular batch is left. This is the
        shelf-check list — what came in and when it goes out of date — and the
        pharmacist counts the rest.

        ponytail: a real per-batch balance needs the batch named on the way out
        too. Add it when somebody asks to be told the quantity rather than where
        to go and look.
        """
        raw = request.query_params.get('days', '')
        days = int(raw) if raw.isdigit() else DEFAULT_EXPIRY_WINDOW_DAYS
        # Already expired is the most urgent case, so the window opens backwards
        # with no floor: everything still on the books that has passed its date.
        cutoff = timezone.localdate() + timedelta(days=min(days, MAX_EXPIRY_WINDOW_DAYS))
        rows = self.get_queryset().filter(
            kind=StockMovement.RECEIPT, expiry_date__isnull=False, expiry_date__lte=cutoff,
        ).order_by('expiry_date', 'id')
        page = self.paginate_queryset(rows)
        serializer = StockMovementSerializer(page if page is not None else rows, many=True)
        return (
            self.get_paginated_response(serializer.data) if page is not None
            else Response(serializer.data)
        )

    @action(detail=False)
    def balances(self, request):
        """What the ledger adds up to per item, under the same filters as the list.

        The only record of what a hospital holds: it owns no catalogue row, so
        there is no `stock_qty` to read on its side.
        """
        totals = (
            self.filter_queryset(self.get_queryset())
            .values('product')
            .annotate(balance=Sum('qty'))
        )
        products = Product.objects.in_bulk([row['product'] for row in totals])
        rows = [
            {
                'product': row['product'],
                'product_name': str(products[row['product']]),
                'balance': row['balance'],
            }
            for row in totals
        ]
        rows.sort(key=lambda row: row['product_name'])
        return Response(rows)


class AuditLogViewSet(PrintListMixin, viewsets.ReadOnlyModelViewSet):
    print_kind = 'audit-trail'
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, IsOrgAdmin]

    def get_queryset(self):
        user = self.request.user
        queryset = AuditLog.objects.filter(**org_scope(user, organization=user.scope_org))
        entity = self.request.query_params.get('entity')
        return queryset.filter(entity=entity) if entity else queryset


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard(request):
    user = request.user
    org = user.scope_org
    if org is None and not user.sees_all_tenants:
        return Response({'detail': 'No organisation attached to this account.'}, status=400)

    # A superuser has no organisation, so every count below is platform-wide.
    is_supplier = org is not None and org.kind == OrgKind.SUPPLIER
    requisitions = (
        Requisition.objects.filter(**org_scope(user, supplier=org)).exclude(status=ReqStatus.DRAFT)
        if is_supplier else Requisition.objects.filter(**org_scope(user, hospital=org))
    )
    deliveries = Delivery.objects.filter(
        **org_scope(user, **{
            ('requisition__supplier' if is_supplier else 'requisition__hospital'): org,
        })
    )
    invoices = Invoice.objects.filter(
        **org_scope(user, **{('supplier' if is_supplier else 'hospital'): org})
    )

    by_status = {row['status']: row['n'] for row in requisitions.values('status').annotate(n=Count('id'))}
    # What each department has been invoiced, a unit counting towards its own.
    tagged = 'delivery__requisition'
    spend_by_department = [] if is_supplier else list(
        invoices.annotate(
            department_id=Coalesce(
                f'{tagged}__unit__department_id', f'{tagged}__department_id',
            ),
            department_name=Coalesce(
                f'{tagged}__unit__department__name', f'{tagged}__department__name',
            ),
        )
        .filter(department_id__isnull=False)
        .values('department_id', 'department_name')
        .annotate(total=Sum('amount'), paid=Sum('amount_paid'))
        .order_by('-total')[:10]
    )
    # A window of months, oldest first. Empty months are filled in here, so a
    # quiet month reads as a gap in the trend rather than disappearing.
    raw = request.query_params.get('months', '')
    window = min(int(raw), MAX_TREND_MONTHS) if raw.isdigit() and int(raw) > 0 else 6
    month = timezone.localdate().replace(day=1)
    months = []
    for _ in range(window):
        months.append(month)
        month = (month - timedelta(days=1)).replace(day=1)
    months.reverse()
    dated = requisition_month_field(user)
    counted = {
        row['month'].date().replace(day=1) if hasattr(row['month'], 'date') else row['month']: row['n']
        for row in requisitions.filter(**{f'{dated}__date__gte': months[0]})
        .annotate(month=TruncMonth(dated))
        .values('month')
        .annotate(n=Count('id'))
    }
    requests_monthly = [
        {'month': m.isoformat(), 'count': counted.get(m, 0)} for m in months
    ]
    owing = invoices.exclude(status=InvoiceStatus.PAID)
    outstanding = owing.aggregate(total=Sum('amount'), paid=Sum('amount_paid'))
    overdue = owing.filter(due_date__lt=timezone.localdate()).aggregate(
        n=Count('id'), total=Sum('amount'), paid=Sum('amount_paid'),
    )
    catalogue = Product.objects.filter(**org_scope(user, supplier=org), is_active=True)
    return Response({
        'organization': OrganizationSerializer(org).data if org else None,
        'requests_by_status': by_status,
        'requests_monthly': requests_monthly,
        'spend_by_department': spend_by_department,
        'requests_total': requisitions.count(),
        'awaiting_action': requisitions.filter(
            status=ReqStatus.SUBMITTED if is_supplier else ReqStatus.DISPATCHED,
        ).count(),
        'deliveries_in_transit': deliveries.filter(status='IN_TRANSIT').count(),
        'invoices_outstanding': (outstanding['total'] or Decimal('0.00'))
        - (outstanding['paid'] or Decimal('0.00')),
        'invoices_overdue': overdue['n'],
        'invoices_overdue_amount': (overdue['total'] or Decimal('0.00'))
        - (overdue['paid'] or Decimal('0.00')),
        # A supplier has these to confirm; a hospital is waiting on them.
        'payments_pending': Payment.objects.filter(
            invoice__in=invoices, status=PaymentStatus.PENDING,
        ).count(),
        'partners': Partnership.objects.filter(
            **org_scope(user, **{('supplier' if is_supplier else 'hospital'): org}),
            is_active=True,
        ).count(),
        'catalogue_items': catalogue.count() if is_supplier or user.sees_all_tenants else None,
        # Against each item's own reorder level, and against what is *available*
        # rather than what is on the shelf: stock already promised to an approved
        # request cannot be sold to anybody else.
        'low_stock': list(
            catalogue.annotate(
                available=F('stock_qty') - F('qty_reserved'),
                level=Coalesce(
                    'reorder_level',
                    Value(DEFAULT_REORDER_LEVEL, output_field=DecimalField()),
                ),
            )
            .filter(available__lt=F('level'))
            .order_by('available')
            .values('id', 'generic_name', 'stock_qty', 'available', 'level')[:10]
        ) if is_supplier or user.sees_all_tenants else [],
        'recent': RequisitionListSerializer(
            requisitions.select_related('hospital', 'supplier')[:10], many=True,
        ).data,
    })
