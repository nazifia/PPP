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


def stock_balance(organization, product):
    """What the ledger says an organisation still holds of one item.

    A hospital owns no catalogue row, so its movements are the only record.
    """
    total = StockMovement.objects.filter(
        organization=organization, product=product,
    ).aggregate(total=Sum('qty'))['total']
    return total or ZERO


@transaction.atomic
def dispense(user, product, qty, note=''):
    """A hospital records what it handed out. No approval: the ward already used it."""
    require_hospital(user)
    if qty <= 0:
        raise ValidationError('Dispensed quantity must be greater than zero.')
    # The catalogue row is the lock, as it is for a supplier dispatch, so two
    # dispenses cannot both read the same balance and overdraw it.
    # ponytail: that serialises every hospital holding the item; lock per
    # organisation if contention ever shows up.
    Product.objects.select_for_update().get(pk=product.pk)
    held = stock_balance(user.scope_org, product)
    if qty > held:
        raise ValidationError(f'{product}: only {held} in stock.')

    movement = StockMovement.objects.create(
        organization=user.scope_org, product=product,
        kind=StockMovement.DISPENSE, qty=-qty, note=note,
    )
    audit(user, 'StockMovement', movement.pk, 'DISPENSED', product=str(product), qty=qty)
    return movement


@transaction.atomic
def adjust(user, product, qty, reason):
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

    # Same lock as a dispense, for the same reason: two adjustments must not
    # both read the balance before either has written.
    Product.objects.select_for_update().get(pk=product.pk)
    held = stock_balance(user.scope_org, product)
    if held + qty < 0:
        raise ValidationError(f'{product}: only {held} in stock, so {qty} would go below zero.')

    movement = StockMovement.objects.create(
        organization=user.scope_org, product=product,
        kind=StockMovement.ADJUST, qty=qty, note=reason.strip(),
    )
    audit(
        user, 'StockMovement', movement.pk, 'ADJUSTED',
        product=str(product), qty=qty, reason=reason.strip(),
    )
    return movement


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

        DeliveryLine.objects.create(
            delivery=delivery,
            requisition_line=line,
            qty_supplied=qty,
            batch_no=item.get('batch_no', '') or '',
            expiry_date=item.get('expiry_date') or None,
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
    ).exclude(status=PaymentStatus.REJECTED).exists():
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
