"""Workflow transitions.

All state changes for a requisition live here so the rules (stock, quantities,
who may act) are stated once and the views stay thin.
"""

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    PAYMENT_DEAD_STATUSES,
    TRANSFER_OPEN_STATUSES,
    CreditNote,
    CreditNoteLine,
    CreditStatus,
    Delivery,
    DeliveryLine,
    DeliveryStatus,
    Invoice,
    InvoiceStatus,
    LineStatus,
    OrgKind,
    Partnership,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Product,
    ReqStatus,
    StockMovement,
    Transfer,
    TransferLine,
    TransferStatus,
    audit,
)


#: Quantities are decimals now (half units are real stock), so every running
#: total starts from a Decimal rather than an int.
ZERO = Decimal('0')


def require_hospital(user):
    if user.org_kind != OrgKind.HOSPITAL:
        raise PermissionDenied('Only hospital users can do this.')


def require_supplier(user):
    if user.org_kind != OrgKind.SUPPLIER:
        raise PermissionDenied('Only supplier users can do this.')


def require_owner(user, org, message='This row belongs to another organisation.'):
    """The row's own tenant, or a platform superuser, which steps into that tenant.

    A superuser adopts the organisation of whatever row it is acting on, for
    the length of the request only, so every later check (hospital or supplier,
    partnership, stock, audit trail) reads exactly as it would for that
    tenant's own administrator. That is what lets it work on any row anywhere
    without naming a tenant first. Creating a row from nothing still needs one,
    because no row is there to say which tenant it belongs to.
    """
    if user.sees_all_tenants:
        user.act_as_org = org  # request-scoped, never saved: see User.act_as_org
        return
    if org.pk != user.scope_org_id:
        raise PermissionDenied(message)


def stock_balance(organization, product, unit=None, store=False):
    """What the ledger says an organisation still holds of one item.

    A hospital owns no catalogue row, so its movements are the only record.
    Name a `unit` and the answer narrows to that unit's shelf, which is what a
    transfer between two of them is checked against. `store` narrows it the
    other way, to the organisation's own store — everything held by no unit at
    all, which is where a hospital that keeps no units holds all of it.

    Neither is the same as the plain total, and taking one for the other is how
    a central dispense comes to eat stock a ward is standing next to.
    """
    rows = StockMovement.objects.filter(organization=organization, product=product)
    if unit is not None:
        rows = rows.filter(unit=unit)
    elif store:
        rows = rows.filter(unit__isnull=True)
    return rows.aggregate(total=Sum('qty'))['total'] or ZERO


def soonest_batch(organization, product, unit=None):
    """The batch and expiry date to send on with goods leaving a shelf.

    A dispense names no batch, so the ledger cannot say which batch is left;
    the shelf check reads receipts and the pharmacist counts. Goods changing
    shelf carry the soonest date of what has ever arrived on the one they
    leave — the batch the pharmacist would hand over first — so the receiving
    unit's count is not blind to it. ponytail: soonest received, not soonest
    still there; per-batch balances need the batch named on the way out too.
    """
    rows = StockMovement.objects.filter(
        organization=organization, product=product, expiry_date__isnull=False,
        unit=unit, kind__in=[StockMovement.RECEIPT, StockMovement.TRANSFER], qty__gt=0,
    ).order_by('expiry_date', 'id').values_list('batch_no', 'expiry_date').first()
    return rows or ('', None)


def require_own_unit(user, unit):
    """A unit of the caller's own hospital, still open for business."""
    if unit.department.organization_id != user.scope_org_id:
        raise ValidationError('That unit belongs to another organisation.')
    # A department stands for its units, as it does everywhere else, so retiring
    # one closes what sits under it too.
    if not unit.is_active or not unit.department.is_active:
        raise ValidationError(f'{unit} has been retired.')


@transaction.atomic
def dispense(user, product, qty, note='', unit=None):
    """A hospital records what it handed out. No approval: the ward already used it.

    A unit may name itself, and then the goods come off that unit's shelf rather
    than off the organisation's own store.
    """
    require_hospital(user)
    if qty <= 0:
        raise ValidationError('Dispensed quantity must be greater than zero.')
    if unit is not None:
        require_own_unit(user, unit)
        # A unit's shelf is handed off by the people standing at it, as it is
        # when the unit lends to the one next door; the store is everybody's.
        require_unit_member(user, unit, 'hand out its stock')
    # The catalogue row is the lock, as it is for a supplier dispatch, so two
    # dispenses cannot both read the same balance and overdraw it.
    # ponytail: that serialises every hospital holding the item; lock per
    # organisation if contention ever shows up.
    Product.objects.select_for_update().get(pk=product.pk)
    # Naming no unit means the organisation's own store, not the sum of every
    # shelf in the building: what a ward is holding is not there to be handed
    # out at the central counter.
    held = stock_balance(user.scope_org, product, unit=unit, store=True)
    if qty > held:
        where = f' on {unit}' if unit is not None else ' in the store'
        raise ValidationError(f'{product}: only {held} in stock{where}.')

    movement = StockMovement.objects.create(
        organization=user.scope_org, product=product, unit=unit,
        kind=StockMovement.DISPENSE, qty=-qty, note=note,
    )
    audit(
        user, 'StockMovement', movement.pk, 'DISPENSED',
        product=str(product), qty=qty, unit=str(unit) if unit else None,
    )
    return movement


@transaction.atomic
def adjust(user, product, qty, reason, unit=None):
    """A hospital corrects its own ledger. Signed: negative writes stock off.

    Dispensing is the only other way goods leave a hospital, and it means a ward
    used them. Everything else that takes something off the shelf — a drug past
    its date, a broken vial, a theft, a count that never matched the book — has
    no other way out, and without one the ledger drifts away from the shelf and
    never comes back. The supplier's half of this is `restock` on the catalogue.
    """
    require_hospital(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can adjust the ledger.')
    if qty == 0:
        raise ValidationError('An adjustment of zero changes nothing.')
    if not (reason or '').strip():
        raise ValidationError('Say why the ledger is being adjusted.')
    if unit is not None:
        require_own_unit(user, unit)

    # Same lock as a dispense, for the same reason: two adjustments must not
    # both read the balance before either has written.
    Product.objects.select_for_update().get(pk=product.pk)
    # An adjustment corrects a ledger; it does not start one. Everything a
    # hospital holds arrived on a verified delivery, which wrote a RECEIPT, so
    # an item with no history here is one this hospital never had — and a
    # positive adjustment against it would be stock conjured out of nothing,
    # for any item on the platform, partner or not.
    if not StockMovement.objects.filter(
        organization=user.scope_org, product=product,
    ).exists():
        raise ValidationError(f'{product} has never been received here.')
    # The shelf being corrected, as for a dispense: one unit's, or the store.
    held = stock_balance(user.scope_org, product, unit=unit, store=True)
    if held + qty < 0:
        where = f' on {unit}' if unit is not None else ' in the store'
        raise ValidationError(
            f'{product}: only {held} in stock{where}, so {qty} would go below zero.'
        )

    movement = StockMovement.objects.create(
        organization=user.scope_org, product=product, unit=unit,
        kind=StockMovement.ADJUST, qty=qty, note=reason.strip(),
    )
    audit(
        user, 'StockMovement', movement.pk, 'ADJUSTED',
        product=str(product), qty=qty, reason=reason.strip(),
        unit=str(unit) if unit else None,
    )
    return movement


@transaction.atomic
def move_stock(user, product, qty, from_unit=None, to_unit=None, note=''):
    """Stock changes shelf between the hospital's own store and one of its units.

    A verified delivery lands where the request pointed: on a unit's shelf if
    it named one, in the store otherwise. This is the only road between the
    two afterwards — the store issuing to a ward, or a ward sending what it no
    longer needs back. Name one side as a unit and leave the other blank for
    the store. Nobody has to agree first: the store belongs to everybody, and
    the unit's own people (or an administrator) sign for its shelf, whichever
    way the goods go. Unit to unit is a transfer, which takes turns, and is
    refused here.
    """
    require_hospital(user)
    if qty <= 0:
        raise ValidationError('Moved quantity must be greater than zero.')
    if from_unit is None and to_unit is None:
        raise ValidationError('Name the unit the stock moves to or from.')
    if from_unit is not None and to_unit is not None:
        raise ValidationError('Between two units, raise a transfer instead.')
    unit = from_unit or to_unit
    require_own_unit(user, unit)
    require_unit_member(user, unit, 'move stock on or off its shelf')

    # The same lock a dispense takes, so two moves cannot both read one shelf full.
    Product.objects.select_for_update().get(pk=product.pk)
    held = stock_balance(user.scope_org, product, unit=from_unit, store=True)
    if qty > held:
        where = f' on {from_unit}' if from_unit is not None else ' in the store'
        raise ValidationError(f'{product}: only {held} in stock{where}.')

    # A pair, as a transfer writes: the hospital's own total is untouched.
    label = f'Store issue to {to_unit}' if to_unit else f'Returned to store from {from_unit}'
    if note:
        label = f'{label}: {note.strip()}'
    batch_no, expiry_date = soonest_batch(user.scope_org, product, unit=from_unit)
    out = StockMovement.objects.create(
        organization=user.scope_org, product=product, unit=from_unit,
        kind=StockMovement.TRANSFER, qty=-qty, note=label[:255],
    )
    StockMovement.objects.create(
        organization=user.scope_org, product=product, unit=to_unit,
        kind=StockMovement.TRANSFER, qty=qty, note=label[:255],
        batch_no=batch_no, expiry_date=expiry_date,
    )
    audit(
        user, 'StockMovement', out.pk, 'MOVED',
        product=str(product), qty=qty,
        from_unit=str(from_unit) if from_unit else None,
        to_unit=str(to_unit) if to_unit else None,
    )
    return out


def require_partner(hospital, supplier):
    linked = Partnership.objects.filter(
        hospital=hospital, supplier=supplier, is_active=True,
    ).exists()
    if not linked:
        raise ValidationError('That company is not an active partner of your organisation.')


@transaction.atomic
def submit(requisition, user):
    require_owner(user, requisition.hospital, 'Not your requisition.')
    require_hospital(user)
    if requisition.status != ReqStatus.DRAFT:
        raise ValidationError('Only a draft can be submitted.')
    require_partner(requisition.hospital, requisition.supplier)

    lines = list(requisition.lines.select_related('product'))
    if not lines:
        raise ValidationError('Add at least one item before submitting.')

    for line in lines:
        if line.product.supplier_id != requisition.supplier_id:
            raise ValidationError(f'{line.product} does not belong to {requisition.supplier.name}.')
        if not line.product.is_active:
            raise ValidationError(f'{line.product} is no longer offered.')
        cap = line.product.max_order_qty
        if cap and line.qty_requested > cap:
            raise ValidationError(f'{line.product}: maximum order quantity is {cap}.')
        # Price is frozen at submission so later catalogue edits cannot change the request.
        line.unit_price = line.product.unit_price
        line.status = LineStatus.PENDING
        line.qty_approved = 0
        line.save(update_fields=['unit_price', 'status', 'qty_approved'])

    requisition.status = ReqStatus.SUBMITTED
    requisition.submitted_at = timezone.now()
    requisition.save(update_fields=['status', 'submitted_at'])
    audit(user, 'Requisition', requisition.pk, 'SUBMITTED', lines=len(lines))
    return requisition


@transaction.atomic
def decide(requisition, user, decisions, note=''):
    """Supplier answers a submitted request.

    `decisions` is [{'line': <id>, 'qty_approved': int, 'note': str}]. Any line
    left out is treated as rejected, so a silent omission cannot be read as a
    promise to supply.

    An approval reserves the quantity, so the same carton cannot be promised to
    two hospitals. The reservation is released when the goods are dispatched.
    """
    require_owner(user, requisition.supplier, 'Not your requisition.')
    require_supplier(user)
    if requisition.status != ReqStatus.SUBMITTED:
        raise ValidationError('Only a submitted request can be decided.')
    # Checked at submission too, but a hospital may have suspended the link in
    # between, and an approval reserves stock and fixes a price. The hospital
    # cancels the request if it no longer wants it.
    require_partner(requisition.hospital, requisition.supplier)

    by_id = {d.get('line'): d for d in decisions}
    lines = list(requisition.lines.select_related('product'))
    unknown = set(by_id) - {line.id for line in lines}
    if unknown:
        raise ValidationError(f'Unknown line ids: {sorted(unknown)}')

    # Lock the catalogue rows for the length of this decision so two suppliers
    # deciding at the same moment cannot both reserve the last carton.
    products = {
        product.id: product
        for product in Product.objects.select_for_update().filter(
            id__in={line.product_id for line in lines},
        )
    }

    approved_total = ZERO
    reserved = defaultdict(lambda: ZERO)
    for line in lines:
        product = products[line.product_id]
        decision = by_id.get(line.id, {})
        qty = Decimal(decision.get('qty_approved') or 0)
        if qty < 0:
            raise ValidationError('Approved quantity cannot be negative.')
        if qty > line.qty_requested:
            raise ValidationError(f'{product}: cannot approve more than requested.')
        free = product.available_qty - reserved[product.id]
        if qty > free:
            raise ValidationError(f'{product}: only {free} available, {qty} requested.')
        reserved[product.id] += qty
        line.qty_approved = qty
        line.supplier_note = decision.get('note', '') or ''
        if qty == 0:
            line.status = LineStatus.REJECTED
        elif qty == line.qty_requested:
            line.status = LineStatus.APPROVED
        else:
            line.status = LineStatus.PARTIAL
        line.save(update_fields=['qty_approved', 'supplier_note', 'status'])
        approved_total += qty

    for product_id, qty in reserved.items():
        if qty:
            product = products[product_id]
            product.qty_reserved += qty
            product.save(update_fields=['qty_reserved'])

    if approved_total == 0:
        requisition.status = ReqStatus.REJECTED
    elif all(line.status == LineStatus.APPROVED for line in lines):
        requisition.status = ReqStatus.APPROVED
    else:
        requisition.status = ReqStatus.PARTIALLY_APPROVED

    requisition.supplier_note = note or requisition.supplier_note
    requisition.decided_at = timezone.now()
    requisition.decided_by = user
    requisition.save(update_fields=['status', 'supplier_note', 'decided_at', 'decided_by'])
    audit(user, 'Requisition', requisition.pk, requisition.status, approved_qty=approved_total)
    return requisition


@transaction.atomic
def dispatch(requisition, user, items, waybill_no=''):
    """Supplier ships part or all of what was approved.

    `items` is [{'line': <id>, 'qty': int, 'batch_no': str, 'expiry_date': date}].
    Stock leaves the supplier here; the reservation taken at approval is
    released by the same amount, so the goods are never counted twice.
    """
    require_owner(user, requisition.supplier, 'Not your requisition.')
    require_supplier(user)
    if requisition.status not in (
        ReqStatus.APPROVED, ReqStatus.PARTIALLY_APPROVED, ReqStatus.DISPATCHED, ReqStatus.DELIVERED,
    ):
        raise ValidationError('Nothing approved to dispatch.')
    if not items:
        raise ValidationError('Nothing to dispatch.')

    # qty_supplied on each line adds up its delivery lines, so they come along
    # rather than costing a query apiece.
    lines = {
        line.id: line
        for line in requisition.lines.select_related('product').prefetch_related('delivery_lines')
    }
    delivery = Delivery.objects.create(
        requisition=requisition, dispatched_by=user, waybill_no=waybill_no,
    )

    for item in items:
        line = lines.get(item.get('line'))
        if line is None:
            raise ValidationError(f"Unknown line id {item.get('line')}.")
        qty = Decimal(item.get('qty') or 0)
        if qty <= 0:
            continue
        outstanding = line.qty_approved - line.qty_supplied
        if qty > outstanding:
            raise ValidationError(
                f'{line.product}: only {outstanding} left to supply on this request.'
            )
        product = Product.objects.select_for_update().get(pk=line.product_id)
        if qty > product.stock_qty:
            raise ValidationError(f'{product}: only {product.stock_qty} in stock.')
        expiry = item.get('expiry_date') or None
        # Goods already out of date are not goods; the hospital would only
        # reject them at the door, and the van has been paid for by then.
        if expiry is not None and expiry < timezone.localdate():
            raise ValidationError(f'{product}: that batch expired on {expiry}.')

        DeliveryLine.objects.create(
            delivery=delivery,
            requisition_line=line,
            qty_supplied=qty,
            batch_no=item.get('batch_no', '') or '',
            expiry_date=expiry,
        )
        product.stock_qty -= qty
        product.qty_reserved = max(product.qty_reserved - qty, 0)
        product.save(update_fields=['stock_qty', 'qty_reserved'])
        StockMovement.objects.create(
            organization=requisition.supplier, product=product, kind=StockMovement.ISSUE,
            qty=-qty, delivery=delivery, note=f'Dispatch {delivery.reference}',
        )

    if not delivery.lines.exists():
        raise ValidationError('Nothing to dispatch.')

    requisition.status = ReqStatus.DISPATCHED
    requisition.save(update_fields=['status'])
    audit(user, 'Delivery', delivery.pk, 'DISPATCHED', requisition=requisition.reference)
    return delivery


@transaction.atomic
def verify(delivery, user, results, remark=''):
    """Hospital inspects the consignment and accepts or rejects each item.

    `results` is [{'line': <delivery line id>, 'qty_accepted': int,
    'qty_rejected': int, 'reason': str}]. Rejected goods go back to the
    supplier's stock; accepted goods land in the hospital's ledger and are
    invoiced.
    """
    requisition = delivery.requisition
    require_owner(user, requisition.hospital, 'Not your delivery.')
    require_hospital(user)
    if delivery.status != DeliveryStatus.IN_TRANSIT:
        raise ValidationError('This delivery has already been verified.')

    by_id = {r.get('line'): r for r in results}
    lines = list(delivery.lines.select_related('requisition_line__product'))
    unknown = set(by_id) - {line.id for line in lines}
    if unknown:
        raise ValidationError(f'Unknown delivery line ids: {sorted(unknown)}')

    accepted_total = ZERO
    amount = Decimal('0.00')
    for line in lines:
        result = by_id.get(line.id, {})
        accepted = Decimal(result.get('qty_accepted') or 0)
        rejected = Decimal(result.get('qty_rejected') or 0)
        if accepted < 0 or rejected < 0:
            raise ValidationError('Quantities cannot be negative.')
        if accepted + rejected != line.qty_supplied:
            raise ValidationError(
                f'{line.requisition_line.product}: accepted + rejected must equal the '
                f'{line.qty_supplied} supplied.'
            )
        if rejected and not (result.get('reason') or '').strip():
            raise ValidationError(
                f'{line.requisition_line.product}: give a reason for the rejected quantity.'
            )
        line.qty_accepted = accepted
        line.qty_rejected = rejected
        line.reject_reason = result.get('reason', '') or ''
        line.save(update_fields=['qty_accepted', 'qty_rejected', 'reject_reason'])

        product = Product.objects.select_for_update().get(
            pk=line.requisition_line.product_id,
        )
        # The batch and its expiry date travel with the goods onto whichever
        # ledger they land on. Without this they stop at the delivery note, and
        # the hospital holds no record of what expires when.
        batch = {'batch_no': line.batch_no, 'expiry_date': line.expiry_date}
        if accepted:
            StockMovement.objects.create(
                organization=requisition.hospital, product=product,
                # Goods land on the shelf of whichever unit asked for them, so a
                # unit can later say what it holds and lend some of it to the
                # unit next door. A request that named only a department — or
                # nothing at all — lands in the organisation's own store.
                unit_id=requisition.unit_id,
                kind=StockMovement.RECEIPT, qty=accepted, delivery=delivery,
                note=f'Receipt {delivery.reference}', **batch,
            )
        if rejected:
            # Back on the shelf, but still owed on this request, so still reserved.
            product.stock_qty += rejected
            product.qty_reserved += rejected
            product.save(update_fields=['stock_qty', 'qty_reserved'])
            StockMovement.objects.create(
                organization=requisition.supplier, product=product,
                kind=StockMovement.RETURN, qty=rejected, delivery=delivery,
                note=line.reject_reason, **batch,
            )
        accepted_total += accepted
        amount += line.requisition_line.unit_price * accepted

    delivery.status = DeliveryStatus.VERIFIED if accepted_total else DeliveryStatus.RETURNED
    delivery.verified_by = user
    delivery.verified_at = timezone.now()
    delivery.remark = remark
    delivery.save(update_fields=['status', 'verified_by', 'verified_at', 'remark'])

    invoice = None
    if amount > 0:
        # Half a unit at an odd price lands on a third decimal, and money is
        # billed to the kobo.
        invoice = Invoice.objects.create(
            delivery=delivery, hospital=requisition.hospital,
            supplier=requisition.supplier, amount=_money(amount),
        )

    # Read after the saves above, and with the delivery lines in hand: qty_accepted
    # adds them up, which is a query per line without the prefetch.
    settled = all(
        line.qty_accepted >= line.qty_approved
        for line in requisition.lines.prefetch_related('delivery_lines')
    )
    requisition.status = ReqStatus.CLOSED if settled else ReqStatus.DELIVERED
    requisition.closed_at = timezone.now() if settled else None
    requisition.save(update_fields=['status', 'closed_at'])

    audit(
        user, 'Delivery', delivery.pk, 'VERIFIED',
        accepted=accepted_total, amount=str(amount),
        invoice=invoice.reference if invoice else None,
    )
    return delivery


@transaction.atomic
def release(requisition, user, reason=''):
    """Supplier gives back what it approved but has not shipped.

    Cuts each line's approved quantity down to what actually went out and frees
    the reservation, so stock promised to a request nobody is going to fulfil
    does not sit on the shelf forever. What has already been dispatched is left
    alone; the hospital still verifies it as usual.
    """
    require_owner(user, requisition.supplier, 'Not your requisition.')
    require_supplier(user)
    if requisition.status not in (
        ReqStatus.APPROVED, ReqStatus.PARTIALLY_APPROVED,
        ReqStatus.DISPATCHED, ReqStatus.DELIVERED,
    ):
        raise ValidationError('There is nothing approved to release on this request.')
    if not reason.strip():
        raise ValidationError('Say why the approved quantity is being released.')

    # qty_supplied and qty_accepted below both add up the delivery lines.
    lines = list(requisition.lines.select_related('product').prefetch_related('delivery_lines'))
    products = {
        product.id: product
        for product in Product.objects.select_for_update().filter(
            id__in={line.product_id for line in lines},
        )
    }

    released_total = ZERO
    supplied_total = ZERO
    for line in lines:
        supplied = line.qty_supplied
        supplied_total += supplied
        outstanding = line.qty_approved - supplied
        if outstanding <= 0:
            continue
        product = products[line.product_id]
        product.qty_reserved = max(product.qty_reserved - outstanding, 0)
        product.save(update_fields=['qty_reserved'])
        line.qty_approved = supplied
        if supplied == 0:
            line.status = LineStatus.REJECTED
        elif supplied == line.qty_requested:
            line.status = LineStatus.APPROVED
        else:
            line.status = LineStatus.PARTIAL
        line.supplier_note = (f'{line.supplier_note} Released: {reason}').strip()
        line.save(update_fields=['qty_approved', 'status', 'supplier_note'])
        released_total += outstanding

    if released_total == 0:
        raise ValidationError('Everything approved has already been dispatched.')

    if supplied_total == 0:
        requisition.status = ReqStatus.CANCELLED
    elif requisition.status == ReqStatus.DELIVERED and all(
        line.qty_accepted >= line.qty_approved for line in lines
    ):
        requisition.status = ReqStatus.CLOSED
        requisition.closed_at = timezone.now()
    requisition.supplier_note = (f'{requisition.supplier_note}\nReleased: {reason}').strip()
    requisition.save(update_fields=['status', 'closed_at', 'supplier_note'])
    audit(user, 'Requisition', requisition.pk, 'RELEASED', qty=released_total, reason=reason)
    return requisition


@transaction.atomic
def cancel(requisition, user, reason=''):
    require_owner(user, requisition.hospital, 'Not your requisition.')
    require_hospital(user)
    if requisition.status not in (ReqStatus.DRAFT, ReqStatus.SUBMITTED):
        raise ValidationError('A request can only be cancelled before it is decided.')
    # Nothing is reserved before a decision, so there is nothing to release here.
    # After a decision it is the supplier's call: see `release`.
    requisition.status = ReqStatus.CANCELLED
    requisition.note = (requisition.note + f'\nCancelled: {reason}').strip()
    requisition.save(update_fields=['status', 'note'])
    audit(user, 'Requisition', requisition.pk, 'CANCELLED', reason=reason)
    return requisition


# ---------------------------------------------------------------------------
# Transfers between units
#
# Two units of one department, in one hospital. Nothing is bought and no invoice
# is raised: the goods are already the hospital's, and what moves is which shelf
# they sit on. So the ledger writes the move as a pair — off the holding unit,
# onto the asking one — and the hospital's own total does not change.
#
#   request ─▶ REQUESTED ─approve─▶ APPROVED ─issue─▶ ISSUED ─receive─▶ RECEIVED
#                  ├──reject──▶ REJECTED    (the holding unit says no)
#                  └──cancel──▶ CANCELLED   (the asking unit thought better)
#
# Agreeing and handing over are two acts by two people — a unit head says yes to
# a quantity, whoever is at the shelf carries out what is actually there — and
# the record keeps them apart. Each side answers for its own half: the holding
# unit approves, refuses and issues; the asking unit requests, withdraws and
# confirms what turned up.
# ---------------------------------------------------------------------------


def require_unit_member(user, unit, action):
    """Only the unit's own people, or an administrator of the hospital.

    An account that names no unit is one nobody has placed yet, and a transfer
    is exactly the decision that needs placing: it says whose shelf is being
    emptied. So it takes the unit's own staff, or the administrator who could
    have set the unit in the first place.
    """
    if user.is_org_admin:
        return
    if unit is None or user.unit_id != unit.pk:
        raise PermissionDenied(
            f'Only {unit} or an administrator can {action}.' if unit
            else f'Only an administrator can {action}.'
        )


def require_same_department(from_unit, to_unit):
    if from_unit.pk == to_unit.pk:
        raise ValidationError('A unit cannot request from itself.')
    if from_unit.department_id != to_unit.department_id:
        raise ValidationError(
            'Both units must belong to the same department. Ask your department '
            'head to raise it with the other department.'
        )


@transaction.atomic
def request_transfer(user, from_unit, to_unit, items, note=''):
    """One unit asks another in its department for stock.

    `items` is [{'product': <Product>, 'qty': <decimal>}]. Nothing moves yet:
    the holding unit has to agree to it, and then hand it over.
    """
    require_owner(
        user, from_unit.department.organization,
        'That unit belongs to another organisation.',
    )
    require_hospital(user)
    require_own_unit(user, from_unit)
    require_own_unit(user, to_unit)
    require_same_department(from_unit, to_unit)
    require_unit_member(user, to_unit, 'ask for stock on its behalf')
    if not items:
        raise ValidationError('Ask for at least one item.')

    # The same item named twice is one line of more of it, as it is on a
    # requisition, and a second row would land on the unique constraint.
    wanted = defaultdict(lambda: ZERO)
    for item in items:
        qty = Decimal(item.get('qty') or 0)
        if qty <= 0:
            raise ValidationError('Requested quantity must be greater than zero.')
        wanted[item['product']] += qty

    transfer = Transfer.objects.create(
        hospital=user.scope_org, from_unit=from_unit, to_unit=to_unit,
        requested_by=user, note=note or '',
    )
    for product, qty in wanted.items():
        TransferLine.objects.create(transfer=transfer, product=product, qty_requested=qty)

    audit(
        user, 'Transfer', transfer.pk, 'REQUESTED',
        reference=transfer.reference, from_unit=str(from_unit), to_unit=str(to_unit),
        lines=len(wanted),
    )
    return transfer


def _lock_transfer(transfer):
    """Re-read the row under a lock, so two answers to one transfer take turns.

    Every step first asks what state the transfer is in and then writes the
    next one. Without this, two people pressing "hand over" at the same moment
    both read APPROVED and the shelf is emptied twice. The invoice takes the
    same lock for the same reason.
    """
    return Transfer.objects.select_for_update().get(pk=transfer.pk)


def _transfer_lines(transfer, items, field):
    """The transfer's lines, and the quantity named against each of them.

    Shared by the three steps that answer line by line, which all read the same
    payload — [{'line': <id>, '<field>': <decimal>}] — and all have to refuse an
    id belonging to somebody else's transfer.
    """
    by_id = {item.get('line'): item for item in items}
    lines = list(transfer.lines.select_related('product'))
    unknown = set(by_id) - {line.id for line in lines}
    if unknown:
        raise ValidationError(f'Unknown line ids: {sorted(unknown)}')
    return [(line, Decimal(by_id.get(line.id, {}).get(field) or 0)) for line in lines]


@transaction.atomic
def approve_transfer(transfer, user, items, note=''):
    """The holding unit agrees to a quantity. Nothing moves yet.

    `items` is [{'line': <id>, 'qty_approved': <decimal>}]. A line left out is
    a line agreed at nothing, so a silence cannot be read as a promise — the
    same rule a supplier's decision follows. Agreeing to everything and then
    finding the shelf empty is what the issue step is for; nothing is held back
    here, because a unit that has agreed to lend something still dispenses off
    that shelf until the moment it hands it over.
    """
    require_owner(user, transfer.hospital, 'Not your transfer.')
    require_hospital(user)
    transfer = _lock_transfer(transfer)
    if transfer.status != TransferStatus.REQUESTED:
        raise ValidationError('This transfer has already been decided.')
    if transfer.from_unit is None or transfer.to_unit is None:
        raise ValidationError('A unit on this transfer no longer exists.')
    require_unit_member(user, transfer.from_unit, 'agree to give its stock away')

    approved_total = ZERO
    for line, qty in _transfer_lines(transfer, items, 'qty_approved'):
        if qty < 0:
            raise ValidationError('Approved quantity cannot be negative.')
        if qty > line.qty_requested:
            raise ValidationError(f'{line.product}: cannot agree to more than was asked for.')
        line.qty_approved = qty
        line.save(update_fields=['qty_approved'])
        approved_total += qty

    if approved_total == 0:
        raise ValidationError('Nothing was agreed to. Refuse the request instead.')

    transfer.status = TransferStatus.APPROVED
    transfer.decided_by = user
    transfer.decided_at = timezone.now()
    transfer.decision_note = (note or '').strip()[:255]
    transfer.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
    audit(
        user, 'Transfer', transfer.pk, 'APPROVED',
        reference=transfer.reference, qty=approved_total,
        from_unit=str(transfer.from_unit), to_unit=str(transfer.to_unit),
    )
    return transfer


@transaction.atomic
def issue_transfer(transfer, user, items, note=''):
    """The holding unit hands over part or all of what it agreed to.

    `items` is [{'line': <id>, 'qty': <decimal>}]. Less than was agreed is the
    ordinary case — the shelf is counted again when somebody is standing at it —
    and what does not go is not owed: the asking unit raises another request if
    it still wants it. This is the step that moves stock.
    """
    require_owner(user, transfer.hospital, 'Not your transfer.')
    require_hospital(user)
    transfer = _lock_transfer(transfer)
    if transfer.status != TransferStatus.APPROVED:
        raise ValidationError(
            'Only a transfer the holding unit has agreed to can be issued.'
        )
    if transfer.from_unit is None or transfer.to_unit is None:
        raise ValidationError('A unit on this transfer no longer exists.')
    require_unit_member(user, transfer.from_unit, 'hand its stock over')

    issued_total = ZERO
    # Locked in one order across every transfer, so two issues that share two
    # items cannot each wait on the other's lock.
    lines = sorted(_transfer_lines(transfer, items, 'qty'), key=lambda pair: pair[0].product_id)
    for line, qty in lines:
        if qty < 0:
            raise ValidationError('Issued quantity cannot be negative.')
        if qty > line.qty_approved:
            raise ValidationError(f'{line.product}: cannot issue more than was agreed.')
        if qty == 0:
            continue
        # The same lock a dispense takes, and for the same reason: two people
        # emptying one shelf must not both read it full.
        Product.objects.select_for_update().get(pk=line.product_id)
        held = stock_balance(transfer.hospital, line.product, unit=transfer.from_unit)
        if qty > held:
            raise ValidationError(f'{line.product}: {transfer.from_unit} holds only {held}.')

        line.qty_issued = qty
        line.save(update_fields=['qty_issued'])
        # A pair, so the hospital's own total is untouched: the goods were always
        # its own, and only the shelf they sit on has changed.
        StockMovement.objects.create(
            organization=transfer.hospital, product=line.product, unit=transfer.from_unit,
            kind=StockMovement.TRANSFER, qty=-qty,
            note=f'Transfer {transfer.reference} to {transfer.to_unit}'[:255],
        )
        batch_no, expiry_date = soonest_batch(
            transfer.hospital, line.product, unit=transfer.from_unit,
        )
        StockMovement.objects.create(
            organization=transfer.hospital, product=line.product, unit=transfer.to_unit,
            kind=StockMovement.TRANSFER, qty=qty,
            note=f'Transfer {transfer.reference} from {transfer.from_unit}'[:255],
            batch_no=batch_no, expiry_date=expiry_date,
        )
        issued_total += qty

    if issued_total == 0:
        raise ValidationError('Nothing was issued. Refuse the request instead.')

    transfer.status = TransferStatus.ISSUED
    transfer.issued_by = user
    transfer.issued_at = timezone.now()
    transfer.issue_note = (note or '').strip()[:255]
    transfer.save(update_fields=['status', 'issued_by', 'issued_at', 'issue_note'])
    audit(
        user, 'Transfer', transfer.pk, 'ISSUED',
        reference=transfer.reference, qty=issued_total,
        from_unit=str(transfer.from_unit), to_unit=str(transfer.to_unit),
    )
    return transfer


@transaction.atomic
def receive_transfer(transfer, user, items, note=''):
    """The asking unit says what actually turned up.

    `items` is [{'line': <id>, 'qty_received': <decimal>, 'reason': str}], and a
    line left out is taken as everything issued having arrived — the ordinary
    case, and the one nobody should have to type.

    The stock moved onto this unit's shelf when it was issued, so a short
    delivery is not a transfer that did not happen: it is goods the hospital
    thought it had and does not. The difference is written off the asking unit's
    ledger here, with the reason on it, rather than left sitting there as stock
    nobody can find.
    """
    require_owner(user, transfer.hospital, 'Not your transfer.')
    require_hospital(user)
    transfer = _lock_transfer(transfer)
    if transfer.status != TransferStatus.ISSUED:
        raise ValidationError('Only stock that has been issued can be confirmed received.')
    if transfer.to_unit is None:
        # The shelf the goods landed on is gone, so a shortfall has nowhere to be
        # written off. Correct the ledger with an adjustment instead.
        raise ValidationError('The unit that asked for this no longer exists.')
    require_unit_member(user, transfer.to_unit, 'confirm what it received')

    received_total = ZERO
    short_total = ZERO
    by_id = {item.get('line'): item for item in items}
    lines = list(transfer.lines.select_related('product'))
    unknown = set(by_id) - {line.id for line in lines}
    if unknown:
        raise ValidationError(f'Unknown line ids: {sorted(unknown)}')

    for line in lines:
        result = by_id.get(line.id)
        # Nothing said about a line is the line arriving as it was sent.
        if result is None or result.get('qty_received') is None:
            qty = line.qty_issued
        else:
            qty = Decimal(result.get('qty_received') or 0)
        if qty < 0:
            raise ValidationError('Received quantity cannot be negative.')
        if qty > line.qty_issued:
            raise ValidationError(
                f'{line.product}: only {line.qty_issued} was issued, so {qty} cannot '
                f'have arrived. Correct the ledger with an adjustment instead.'
            )
        short = line.qty_issued - qty
        reason = ((result or {}).get('reason') or '').strip()
        if short and not reason:
            raise ValidationError(f'{line.product}: say what happened to the missing {short}.')

        line.qty_received = qty
        line.shortfall_reason = reason[:255]
        line.save(update_fields=['qty_received', 'shortfall_reason'])
        if short:
            StockMovement.objects.create(
                organization=transfer.hospital, product=line.product, unit=transfer.to_unit,
                kind=StockMovement.ADJUST, qty=-short,
                note=f'Transfer {transfer.reference} short: {line.shortfall_reason}'[:255],
            )
            short_total += short
        received_total += qty

    transfer.status = TransferStatus.RECEIVED
    transfer.received_by = user
    transfer.received_at = timezone.now()
    transfer.receipt_note = (note or '').strip()[:255]
    transfer.save(update_fields=['status', 'received_by', 'received_at', 'receipt_note'])
    audit(
        user, 'Transfer', transfer.pk, 'RECEIVED',
        reference=transfer.reference, qty=received_total, short=short_total,
        from_unit=str(transfer.from_unit), to_unit=str(transfer.to_unit),
    )
    return transfer


@transaction.atomic
def reject_transfer(transfer, user, reason=''):
    """The holding unit says no. Nothing moves."""
    require_owner(user, transfer.hospital, 'Not your transfer.')
    require_hospital(user)
    transfer = _lock_transfer(transfer)
    if transfer.status != TransferStatus.REQUESTED:
        raise ValidationError('This transfer has already been decided.')
    require_unit_member(user, transfer.from_unit, 'refuse a request for its stock')
    if not (reason or '').strip():
        raise ValidationError('Say why the request is being refused.')

    transfer.status = TransferStatus.REJECTED
    transfer.decided_by = user
    transfer.decided_at = timezone.now()
    transfer.decision_note = reason.strip()[:255]
    transfer.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
    audit(
        user, 'Transfer', transfer.pk, 'REJECTED',
        reference=transfer.reference, reason=transfer.decision_note,
    )
    return transfer


@transaction.atomic
def cancel_transfer(transfer, user, reason=''):
    """The asking unit withdraws the request before the goods leave the shelf.

    An agreed transfer may still be withdrawn — nothing has moved, and a unit
    that has found what it needed elsewhere should not have to take stock it no
    longer wants. Once it is issued the goods are already on its shelf, and what
    it does with them is a transfer back the other way.
    """
    require_owner(user, transfer.hospital, 'Not your transfer.')
    require_hospital(user)
    transfer = _lock_transfer(transfer)
    if transfer.status not in TRANSFER_OPEN_STATUSES:
        raise ValidationError('A transfer can only be withdrawn before it is issued.')
    require_unit_member(user, transfer.to_unit, 'withdraw its request')

    transfer.status = TransferStatus.CANCELLED
    transfer.decided_by = user
    transfer.decided_at = timezone.now()
    transfer.decision_note = (reason or '').strip()[:255]
    transfer.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])
    audit(
        user, 'Transfer', transfer.pk, 'CANCELLED',
        reference=transfer.reference, reason=transfer.decision_note,
    )
    return transfer


# ---------------------------------------------------------------------------
# Payments
#
# The platform holds no money and talks to no bank. It records the two halves
# of a payment that happened elsewhere: the hospital declares what it sent and
# how, the supplier says whether it arrived. An invoice only moves towards PAID
# on the supplier's confirmation, so neither side can settle a debt alone.
#
#   record ──▶ PENDING ──confirm──▶ CONFIRMED  (invoice.amount_paid grows)
#                    └──reject───▶ REJECTED   (nothing moves; may be re-recorded)
# ---------------------------------------------------------------------------

CENTS = Decimal('0.01')


def _money(amount):
    try:
        return Decimal(str(amount)).quantize(CENTS)
    except (ArithmeticError, TypeError, ValueError):
        raise ValidationError('amount must be a number.')


@transaction.atomic
def record_payment(invoice, user, amount, method, payer_reference='', note='', receipt=None):
    """Hospital declares a payment it has made against an invoice.

    Nothing lands on the invoice yet — the supplier still has to confirm it.
    """
    require_owner(user, invoice.hospital, 'Not your invoice.')
    require_hospital(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can record a payment.')

    amount = _money(amount)
    if amount <= 0:
        raise ValidationError('Payment must be greater than zero.')
    if method not in PaymentMethod.values:
        raise ValidationError(f'Unknown payment method. Use one of {PaymentMethod.values}.')

    payer_reference = (payer_reference or '').strip()
    # Cash is handed over in person and has no slip; everything else leaves a
    # trace the supplier can look up, and without it there is nothing to confirm.
    if method != PaymentMethod.CASH and not payer_reference:
        raise ValidationError('Give the transaction reference for this payment.')

    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == InvoiceStatus.PAID:
        raise ValidationError('This invoice is already settled.')
    unclaimed = invoice.amount_unclaimed
    if amount > unclaimed:
        raise ValidationError(
            f'That is more than the {unclaimed} still unclaimed on this invoice '
            f'(payments awaiting confirmation are already counted).'
        )
    if payer_reference and invoice.payments.filter(
        payer_reference__iexact=payer_reference,
    ).exclude(status__in=PAYMENT_DEAD_STATUSES).exists():
        raise ValidationError(f'Reference {payer_reference} is already recorded on this invoice.')

    payment = Payment.objects.create(
        invoice=invoice, amount=amount, method=method,
        payer_reference=payer_reference, note=note or '', receipt=receipt or '',
        recorded_by=user,
    )
    audit(
        user, 'Payment', payment.pk, 'RECORDED',
        invoice=invoice.reference, amount=str(amount), method=method,
        payer_reference=payer_reference, receipt=bool(receipt),
    )
    return payment


@transaction.atomic
def confirm_payment(payment, user):
    """Supplier says the money arrived. This is what moves the invoice."""
    require_owner(user, payment.invoice.supplier, 'Not your invoice.')
    require_supplier(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can confirm a payment.')
    if payment.status != PaymentStatus.PENDING:
        raise ValidationError('This payment has already been decided.')

    invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id)
    if payment.amount > invoice.balance:
        # Another payment was confirmed first and covered it.
        raise ValidationError(
            f'Only {invoice.balance} is outstanding on this invoice. '
            f'Reject this payment and ask for it to be recorded again.'
        )

    payment.status = PaymentStatus.CONFIRMED
    payment.decided_by = user
    payment.decided_at = timezone.now()
    payment.save(update_fields=['status', 'decided_by', 'decided_at'])
    invoice.register_payment(payment.amount)

    audit(
        user, 'Payment', payment.pk, 'CONFIRMED',
        invoice=invoice.reference, amount=str(payment.amount),
        invoice_status=invoice.status, balance=str(invoice.balance),
    )
    payment.invoice = invoice
    return payment


# ---------------------------------------------------------------------------
# Credit notes
#
# Verification catches what is visible at the door. A batch that turns out to be
# counterfeit, or that was already nearly out of date when it arrived, is found
# weeks later, and until this existed the invoice was final from the moment it
# was raised. Like a payment, a credit moves what one side owes the other, so
# like a payment it takes both of them:
#
#   raise ──▶ PENDING ──confirm──▶ CONFIRMED  (invoice.amount falls, stock written down)
#                    └──reject───▶ REJECTED   (nothing moves)
# ---------------------------------------------------------------------------


def creditable_qty(delivery_line):
    """How much of an accepted line has not already been credited.

    Counts the pending credits too: two notes for the same carton would each
    look affordable on their own and take the invoice down twice between them.
    """
    spoken_for = sum(
        (
            line.qty for line in delivery_line.credit_lines.all()
            if line.credit_note.status != CreditStatus.REJECTED
        ),
        ZERO,
    )
    return max(delivery_line.qty_accepted - spoken_for, ZERO)


@transaction.atomic
def raise_credit(invoice, user, items, reason=''):
    """Hospital says some of what it accepted was not what it paid for.

    `items` is [{'line': <delivery line id>, 'qty': <decimal>}]. Nothing moves
    until the supplier confirms it.
    """
    require_owner(user, invoice.hospital, 'Not your invoice.')
    require_hospital(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can raise a credit note.')
    if not (reason or '').strip():
        raise ValidationError('Say what is wrong with the goods.')
    if not items:
        raise ValidationError('Name at least one item to credit.')

    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    lines = {
        line.id: line
        for line in invoice.delivery.lines.select_related(
            'requisition_line__product',
        ).prefetch_related('credit_lines__credit_note')
    }

    credit = CreditNote.objects.create(invoice=invoice, reason=reason.strip(), raised_by=user)
    amount = Decimal('0.00')
    for item in items:
        line = lines.get(item.get('line'))
        if line is None:
            raise ValidationError(f"Line {item.get('line')} is not on this invoice's delivery.")
        qty = Decimal(item.get('qty') or 0)
        if qty <= 0:
            continue
        free = creditable_qty(line)
        if qty > free:
            raise ValidationError(
                f'{line.requisition_line.product}: only {free} of what was accepted is '
                f'still open to credit.'
            )
        CreditNoteLine.objects.create(credit_note=credit, delivery_line=line, qty=qty)
        amount += line.requisition_line.unit_price * qty

    if not credit.lines.exists():
        raise ValidationError('Name at least one item to credit.')

    amount = _money(amount)
    # The platform moves no money, so it cannot hand any back. A credit larger
    # than the debt is a refund the two sides arrange between themselves.
    if amount > invoice.balance:
        raise ValidationError(
            f'That credits {amount} against a {invoice.balance} balance. Credit what is '
            f'still owed; anything already paid is a refund to settle between you.'
        )
    credit.amount = amount
    credit.save(update_fields=['amount'])
    audit(
        user, 'CreditNote', credit.pk, 'RAISED',
        invoice=invoice.reference, amount=str(amount), reason=credit.reason,
    )
    return credit


@transaction.atomic
def confirm_credit(credit, user):
    """Supplier accepts the credit. This is what moves the invoice."""
    invoice = credit.invoice
    require_owner(user, invoice.supplier, 'Not your invoice.')
    require_supplier(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can accept a credit note.')
    if credit.status != CreditStatus.PENDING:
        raise ValidationError('This credit note has already been decided.')

    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if credit.amount > invoice.balance:
        # A payment landed first and left less owing than this credits.
        raise ValidationError(
            f'Only {invoice.balance} is outstanding on this invoice now. Refuse this note '
            f'and ask for one against what is left.'
        )

    for line in credit.lines.select_related('delivery_line__requisition_line__product'):
        # The goods leave the hospital's books: it is not holding them as stock,
        # whatever it does with them next. They do not go back on the supplier's
        # shelf either — bad goods are not stock, and where they physically end
        # up is between the two of them.
        StockMovement.objects.create(
            organization=invoice.hospital,
            product=line.delivery_line.requisition_line.product,
            # Off the same shelf the receipt landed on, or the unit goes on
            # showing stock the hospital has just written off its books.
            unit_id=invoice.delivery.requisition.unit_id,
            kind=StockMovement.RETURN, qty=-line.qty,
            delivery=invoice.delivery,
            batch_no=line.delivery_line.batch_no,
            expiry_date=line.delivery_line.expiry_date,
            note=f'Credit {credit.reference}: {credit.reason}'[:255],
        )

    invoice.amount = _money(invoice.amount - credit.amount)
    # What is owed just fell, which may be all that was left of it.
    if invoice.amount_paid >= invoice.amount:
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = invoice.paid_at or timezone.now()
    elif invoice.amount_paid > 0:
        invoice.status = InvoiceStatus.PART_PAID
    invoice.save(update_fields=['amount', 'status', 'paid_at'])

    credit.status = CreditStatus.CONFIRMED
    credit.decided_by = user
    credit.decided_at = timezone.now()
    credit.save(update_fields=['status', 'decided_by', 'decided_at'])
    audit(
        user, 'CreditNote', credit.pk, 'CONFIRMED',
        invoice=invoice.reference, amount=str(credit.amount),
        invoice_amount=str(invoice.amount), balance=str(invoice.balance),
    )
    credit.invoice = invoice
    return credit


@transaction.atomic
def reject_credit(credit, user, reason=''):
    """Supplier refuses the credit. The invoice does not move."""
    require_owner(user, credit.invoice.supplier, 'Not your invoice.')
    require_supplier(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can refuse a credit note.')
    if credit.status != CreditStatus.PENDING:
        raise ValidationError('This credit note has already been decided.')
    if not (reason or '').strip():
        raise ValidationError('Say why the credit note is being refused.')

    credit.status = CreditStatus.REJECTED
    credit.decided_by = user
    credit.decided_at = timezone.now()
    credit.reject_reason = reason.strip()
    credit.save(update_fields=['status', 'decided_by', 'decided_at', 'reject_reason'])
    audit(
        user, 'CreditNote', credit.pk, 'REJECTED',
        invoice=credit.invoice.reference, amount=str(credit.amount),
        reason=credit.reject_reason,
    )
    return credit


@transaction.atomic
def withdraw_payment(payment, user, reason=''):
    """Hospital takes back an entry the supplier has not decided yet.

    A payment recorded against the wrong invoice, or for the wrong amount, is
    otherwise the supplier's to clear: until it is rejected the entry keeps
    counting towards `amount_pending`, and so holds down what a corrected entry
    may be recorded for. The invoice does not move either way — nothing was ever
    confirmed — and the withdrawn entry stays on the ledger, so the correction
    reads as a correction rather than as a payment that never happened.
    """
    require_owner(user, payment.invoice.hospital, 'Not your payment.')
    require_hospital(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can withdraw a payment.')
    if payment.status != PaymentStatus.PENDING:
        raise ValidationError('This payment has already been decided.')

    payment.status = PaymentStatus.WITHDRAWN
    payment.decided_by = user
    payment.decided_at = timezone.now()
    # The same column the supplier's refusal writes to, because it answers the
    # same question: why this entry came to nothing.
    payment.reject_reason = (reason or '').strip()
    payment.save(update_fields=['status', 'decided_by', 'decided_at', 'reject_reason'])
    audit(
        user, 'Payment', payment.pk, 'WITHDRAWN',
        invoice=payment.invoice.reference, amount=str(payment.amount),
        reason=payment.reject_reason,
    )
    return payment


@transaction.atomic
def reject_payment(payment, user, reason=''):
    """Supplier says the money never arrived. The invoice does not move."""
    require_owner(user, payment.invoice.supplier, 'Not your invoice.')
    require_supplier(user)
    if not user.is_org_admin:
        raise PermissionDenied('Only an organisation administrator can reject a payment.')
    if payment.status != PaymentStatus.PENDING:
        raise ValidationError('This payment has already been decided.')
    if not (reason or '').strip():
        raise ValidationError('Say why the payment is being rejected.')

    payment.status = PaymentStatus.REJECTED
    payment.decided_by = user
    payment.decided_at = timezone.now()
    payment.reject_reason = reason.strip()
    payment.save(update_fields=['status', 'decided_by', 'decided_at', 'reject_reason'])
    audit(
        user, 'Payment', payment.pk, 'REJECTED',
        invoice=payment.invoice.reference, amount=str(payment.amount), reason=payment.reject_reason,
    )
    return payment
