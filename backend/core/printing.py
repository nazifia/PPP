"""Printable paper: the request, the delivery note, the invoice, the receipt,
and the two ledgers a procurement office is asked to produce — stock and audit.

One template, two outputs. Run through xhtml2pdf it is a PDF, which is what
`print/` answers with and what the app hands straight to the printer; rendered
as it stands it is an HTML sheet that opens the browser's own print dialog. The
template therefore lays everything out in tables and avoids flexbox and grid,
which xhtml2pdf does not understand.

`print/` is an ordinary authenticated route on the viewset, so the queryset has
already decided that this caller may read the row. A browser cannot send the
token header, so it gets `print-link/` instead: a short-lived signed URL that
the print view accepts in place of a session, and nothing else. For a ledger
the filters travel *inside* the signature, so the sheet cannot be widened by
editing the address bar.
"""

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl

from django.core.signing import BadSignature, TimestampSigner
from django.http import Http404, HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from xhtml2pdf import pisa

from .models import (
    Delivery,
    DeliveryStatus,
    Invoice,
    Payment,
    PaymentStatus,
    Product,
    Requisition,
    Transfer,
    TransferStatus,
    Unit,
    User,
)

#: Long enough to walk to the printer, short enough that a link copied out of a
#: browser's history is worthless by the time anyone tries it.
LINK_MAX_AGE = 600

#: A ledger is a report, not an archive. Past this the sheet is nobody's idea of
#: paper, and the filters are the answer.
#: ponytail: a flat cap, not paging. Page the document if anyone ever prints one
#: this long on purpose.
MAX_LIST_ROWS = 500

_signer = TimestampSigner(salt='core.printing')

DASH = '—'
NAIRA = '\u20a6'

#: reportlab's built-in fonts are WinAnsi and have no ₦ in them, so a PDF needs
#: a TrueType face that does. These are the usual places one sits. If none is
#: there the amounts are written `NGN` rather than printed as hollow boxes.
FONT_CANDIDATES = [
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
    ('/System/Library/Fonts/Supplemental/Arial.ttf',
     '/System/Library/Fonts/Supplemental/Arial Bold.ttf'),
]


def sign(kind, pk, user_id, query=''):
    """Authorise one document. `pk` is 0 for a ledger, whose filters are [query]."""
    return _signer.sign(f'{kind}:{pk}:{user_id}:{query}')


def money(value):
    return f'{NAIRA}{Decimal(value or 0):,.2f}'


def qty(value):
    """Quantities carry one decimal place, but nobody writes `40.0` on a form."""
    text = f'{Decimal(value or 0):.1f}'
    return text[:-2] if text.endswith('.0') else text


def when(value):
    if not value:
        return DASH
    if isinstance(value, datetime):
        # %I pads the hour: '01:05 PM' reads better as '1:05 PM'.
        return timezone.localtime(value).strftime('%d %b %Y, %I:%M %p').replace(', 0', ', ')
    return value.strftime('%d %b %Y')


def who(user):
    return user.full_name if user else DASH


def party(org):
    return {
        'name': org.name,
        'address': org.address,
        'phone': org.phone,
        'email': org.email,
        'registration_no': org.registration_no,
    }


# ---------------------------------------------------------------------------
# One document at a time
# ---------------------------------------------------------------------------

def requisition_document(obj):
    lines = obj.lines.select_related('product__unit').all()
    decided = obj.status not in ('DRAFT', 'SUBMITTED')
    return {
        'title': 'PURCHASE REQUEST',
        'reference': obj.reference,
        'from_label': 'From',
        'party_from': party(obj.hospital),
        'to_label': 'To',
        'party_to': party(obj.supplier),
        'meta': [
            ('Status', obj.get_status_display()),
            ('Requesting for', obj.department_label or DASH),
            ('Raised by', who(obj.created_by)),
            ('Raised on', when(obj.created_at)),
            ('Submitted', when(obj.submitted_at)),
            ('Decided by', who(obj.decided_by)),
            ('Decided on', when(obj.decided_at)),
        ],
        'columns': [
            ('#', 'right'), ('Item', 'left'), ('Unit', 'left'),
            ('Requested', 'right'), ('Approved', 'right'),
            ('Unit price', 'right'), ('Amount', 'right'),
        ],
        'rows': [
            [
                str(number), str(line.product), line.product.unit.name,
                qty(line.qty_requested),
                qty(line.qty_approved) if decided else DASH,
                money(line.unit_price),
                money(line.line_total_approved if decided else line.line_total_requested),
            ]
            for number, line in enumerate(lines, start=1)
        ],
        'totals': [
            ('Requested value', money(obj.requested_value)),
            *([('Approved value', money(obj.approved_value))] if decided else []),
        ],
        'notes': [
            ('Note', obj.note),
            ('Supplier note', obj.supplier_note),
        ],
        'signatures': ['Requested by', 'Authorised by'],
    }


def delivery_document(obj):
    lines = obj.lines.select_related('requisition_line__product__unit').all()
    verified = obj.status != DeliveryStatus.IN_TRANSIT
    return {
        'title': 'DELIVERY NOTE',
        'reference': obj.reference,
        'from_label': 'Supplied by',
        'party_from': party(obj.requisition.supplier),
        'to_label': 'Delivered to',
        'party_to': party(obj.requisition.hospital),
        'meta': [
            ('Against request', obj.requisition.reference),
            ('Requesting for', obj.requisition.department_label or DASH),
            ('Waybill no.', obj.waybill_no or DASH),
            ('Status', obj.get_status_display()),
            ('Dispatched by', who(obj.dispatched_by)),
            ('Dispatched on', when(obj.dispatched_at)),
            ('Verified by', who(obj.verified_by)),
            ('Verified on', when(obj.verified_at)),
        ],
        'columns': [
            ('#', 'right'), ('Item', 'left'), ('Batch', 'left'), ('Expiry', 'left'),
            ('Supplied', 'right'), ('Accepted', 'right'), ('Rejected', 'right'),
            ('Reason', 'left'),
        ],
        'rows': [
            [
                str(number), str(line.requisition_line.product),
                line.batch_no or DASH, when(line.expiry_date),
                qty(line.qty_supplied),
                qty(line.qty_accepted) if verified else DASH,
                qty(line.qty_rejected) if verified else DASH,
                line.reject_reason or '',
            ]
            for number, line in enumerate(lines, start=1)
        ],
        'totals': [('Accepted value', money(obj.accepted_value))] if verified else [],
        'notes': [('Remark', obj.remark)],
        'signatures': ['Dispatched by', 'Received by'],
    }


def invoice_document(obj):
    # An invoice is raised for what the hospital accepted, so those are the only
    # lines that belong on it.
    lines = [
        line for line in obj.delivery.lines.select_related('requisition_line__product__unit')
        if line.qty_accepted
    ]
    return {
        'title': 'INVOICE',
        'reference': obj.reference,
        'from_label': 'From',
        'party_from': party(obj.supplier),
        'to_label': 'Bill to',
        'party_to': party(obj.hospital),
        'meta': [
            ('Status', obj.get_status_display()),
            ('Against delivery', obj.delivery.reference),
            ('Against request', obj.delivery.requisition.reference),
            ('Requesting for', obj.delivery.requisition.department_label or DASH),
            ('Issued', when(obj.issued_at)),
            ('Terms', f'{obj.terms_days} days'),
            ('Due', when(obj.due_date)),
            *([('Overdue by', f'{obj.days_overdue} days')] if obj.is_overdue else []),
            ('Paid on', when(obj.paid_at)),
        ],
        'columns': [
            ('#', 'right'), ('Item', 'left'), ('Unit', 'left'),
            ('Quantity', 'right'), ('Unit price', 'right'), ('Amount', 'right'),
        ],
        'rows': [
            [
                str(number), str(line.requisition_line.product),
                line.requisition_line.product.unit.name,
                qty(line.qty_accepted), money(line.requisition_line.unit_price),
                money(line.accepted_value),
            ]
            for number, line in enumerate(lines, start=1)
        ],
        'totals': [
            ('Total', money(obj.amount)),
            ('Paid (confirmed)', money(obj.amount_paid)),
            ('Awaiting confirmation', money(obj.amount_pending)),
            ('Balance', money(obj.balance)),
        ],
        'notes': [],
        'signatures': ['Raised by', 'Received by'],
    }


def payment_document(obj):
    # Only a confirmed payment is a receipt. Anything else is the hospital's own
    # word that it paid, and printing that as a receipt would be a forgery
    # waiting to be filed.
    confirmed = obj.status == PaymentStatus.CONFIRMED
    invoice = obj.invoice
    return {
        'title': 'PAYMENT RECEIPT' if confirmed else 'PAYMENT ADVICE',
        'reference': obj.reference,
        'banner': '' if confirmed else
                  'NOT CONFIRMED — the supplier has not acknowledged this money.',
        'from_label': 'Paid by',
        'party_from': party(invoice.hospital),
        'to_label': 'Paid to',
        'party_to': party(invoice.supplier),
        'meta': [
            ('Status', obj.get_status_display()),
            ('Against invoice', invoice.reference),
            ('Amount', money(obj.amount)),
            ('Method', obj.get_method_display()),
            ('Payer reference', obj.payer_reference or DASH),
            ('Recorded by', who(obj.recorded_by)),
            ('Recorded on', when(obj.recorded_at)),
            ('Confirmed by' if confirmed else 'Decided by', who(obj.decided_by)),
            ('Confirmed on' if confirmed else 'Decided on', when(obj.decided_at)),
        ],
        'columns': [],
        'rows': [],
        'totals': [
            ('This payment', money(obj.amount)),
            ('Invoice total', money(invoice.amount)),
            ('Invoice paid to date', money(invoice.amount_paid)),
            ('Invoice balance', money(invoice.balance)),
        ],
        'notes': [
            ('Note', obj.note),
            ('Rejected because', obj.reject_reason),
        ],
        'signatures': ['Paid by', 'Received by'],
    }


def unit_party(unit, hospital):
    """A unit as a party on paper: its own name over its department's.

    Not [party], which describes an organisation — both sides of a transfer are
    inside one, and what tells them apart is which unit and which department.
    """
    return {
        'name': str(unit.name) if unit else DASH,
        'address': f'{unit.department.name}, {hospital.name}' if unit else hospital.name,
        'phone': hospital.phone,
        'email': hospital.email,
    }


def transfer_document(obj):
    """The note that travels with the cartons, and is signed at both ends.

    No money on it: nothing was bought, and the two sides are units of one
    hospital. What it has instead is the four quantities — asked, agreed, given,
    received — because the shortfall between the last two is the whole reason
    anybody keeps the paper.
    """
    lines = obj.lines.select_related('product__unit').all()
    issued = obj.status in (TransferStatus.ISSUED, TransferStatus.RECEIVED)
    received = obj.status == TransferStatus.RECEIVED
    return {
        'title': 'STOCK TRANSFER NOTE',
        'reference': obj.reference,
        'banner': '' if issued else
                  'NOTHING HAS MOVED YET — this is the request, not a hand-over.',
        'from_label': 'Issued by',
        'party_from': unit_party(obj.from_unit, obj.hospital),
        'to_label': 'Received by',
        'party_to': unit_party(obj.to_unit, obj.hospital),
        'meta': [
            ('Status', obj.get_status_display()),
            ('Department', obj.department_name or DASH),
            ('Asked by', who(obj.requested_by)),
            ('Asked on', when(obj.created_at)),
            ('Agreed by', who(obj.decided_by)),
            ('Agreed on', when(obj.decided_at)),
            ('Handed over by', who(obj.issued_by)),
            ('Handed over on', when(obj.issued_at)),
            ('Signed for by', who(obj.received_by)),
            ('Signed for on', when(obj.received_at)),
        ],
        'columns': [
            ('#', 'right'), ('Item', 'left'), ('Unit', 'left'), ('Asked', 'right'),
            ('Agreed', 'right'), ('Given', 'right'), ('Received', 'right'),
            ('Short because', 'left'),
        ],
        'rows': [
            [
                str(number), str(line.product), line.product.unit.name,
                qty(line.qty_requested), qty(line.qty_approved),
                qty(line.qty_issued) if issued else DASH,
                qty(line.qty_received) if received else DASH,
                line.shortfall_reason or '',
            ]
            for number, line in enumerate(lines, start=1)
        ],
        'totals': [
            ('Items', str(len(lines))),
            *([('Quantity handed over', qty(sum(line.qty_issued for line in lines)))]
              if issued else []),
        ],
        'notes': [
            ('Note', obj.note),
            ('Said on deciding', obj.decision_note),
            ('Said on issue', obj.issue_note),
            ('Said on receipt', obj.receipt_note),
        ],
        'signatures': ['Issued by', 'Received by'],
    }


# ---------------------------------------------------------------------------
# Ledgers, which are a filtered list rather than one row
# ---------------------------------------------------------------------------

def scoped_rows(viewset_name, user, filters):
    """Exactly the rows the screen itself would show, under the same filters.

    The print borrows the viewset rather than writing a second idea of what a
    user may see: tenant scoping, filters and search stay in one place, and a
    change to the screen's rules reaches the paper with it. The import is
    deferred because views.py imports this module.
    """
    from . import views

    drf_request = Request(RequestFactory().get('/', filters))
    drf_request.user = user
    view = getattr(views, viewset_name)(request=drf_request, action='list', args=(), kwargs={})
    return view.filter_queryset(view.get_queryset())


def filters_shown(filters, labels):
    """The filters that were actually set, for the sheet to say what it covers."""
    return [(label, filters[key]) for key, label in labels if filters.get(key)]


def ledger_context(user, title, filters, labels, columns, rows, count):
    org = user.scope_org
    return {
        'title': title,
        'reference': timezone.localdate().strftime('%d %b %Y'),
        'from_label': 'Organisation',
        'party_from': party(org) if org else None,
        'to_label': '',
        'party_to': None,
        'meta': [
            *filters_shown(filters, labels),
            ('Rows', f'{len(rows)} of {count}' if count > len(rows) else str(count)),
        ],
        'columns': columns,
        'rows': rows,
        'totals': [],
        'notes': [
            ('Cut short', f'Only the first {MAX_LIST_ROWS} rows are printed. Narrow the '
                          f'filters to see the rest.') if count > len(rows) else ('', ''),
        ],
        'signatures': ['Prepared by', 'Checked by'],
    }


STOCK_LEDGER_FILTERS = [
    ('product', 'Item'), ('kind', 'Movement'), ('unit', 'Shelf'), ('from', 'From'),
    ('to', 'To'), ('search', 'Search'),
]

#: What the ledger calls the organisation's own store, which belongs to no unit.
STORE = 'Organisation store'


def stock_ledger_document(user, filters):
    found = scoped_rows('StockMovementViewSet', user, filters)
    rows = list(found[:MAX_LIST_ROWS])
    # The screen filters by id; the sheet has to say which item and which shelf
    # those were, or it reads as a ledger of everything.
    # `.items()` rather than `dict(filters)`: the filters arrive as a QueryDict
    # from `print/` and as a plain dict from a signed link, and only this reads
    # one value per key out of both.
    shown = dict(filters.items())
    if shown.get('product'):
        product = Product.objects.filter(pk=shown['product']).first()
        shown['product'] = str(product) if product else shown['product']
    if shown.get('unit'):
        unit = Unit.objects.filter(pk=shown['unit']).first() if shown['unit'] != 'store' else None
        shown['unit'] = str(unit) if unit else STORE
    return ledger_context(
        user, 'STOCK LEDGER', shown, STOCK_LEDGER_FILTERS,
        columns=[
            ('When', 'left'), ('Item', 'left'), ('Shelf', 'left'), ('Movement', 'left'),
            ('Quantity', 'right'), ('Delivery', 'left'), ('Note', 'left'),
        ],
        rows=[
            [
                when(row.created_at), str(row.product),
                str(row.unit) if row.unit_id else STORE,
                row.get_kind_display(),
                qty(row.qty), row.delivery.reference if row.delivery else DASH,
                row.note or '',
            ]
            for row in rows
        ],
        count=found.count(),
    )


def audit_trail_document(user, filters):
    # Checked again here, not only when the link was signed: ten minutes is long
    # enough for an administrator to have been demoted.
    if not user.is_org_admin:
        raise Http404('The audit trail is for administrators.')
    found = scoped_rows('AuditLogViewSet', user, filters)
    rows = list(found.select_related('actor')[:MAX_LIST_ROWS])
    return ledger_context(
        user, 'AUDIT TRAIL', filters, [('entity', 'Entity')],
        columns=[
            ('When', 'left'), ('Who', 'left'), ('Entity', 'left'),
            ('Action', 'left'), ('Detail', 'left'),
        ],
        rows=[
            [
                when(row.created_at), who(row.actor), f'{row.entity} #{row.entity_id}',
                row.action,
                ', '.join(f'{key} {value}' for key, value in sorted(row.detail.items())),
            ]
            for row in rows
        ],
        count=found.count(),
    )


#: What the URL and the signature both name. `DOCUMENTS` takes one row;
#: `LEDGERS` takes the caller and the filters it signed.
DOCUMENTS = {
    'requisition': (Requisition, requisition_document),
    'delivery': (Delivery, delivery_document),
    'invoice': (Invoice, invoice_document),
    'payment': (Payment, payment_document),
    'transfer': (Transfer, transfer_document),
}

LEDGERS = {
    'stock-ledger': stock_ledger_document,
    'audit-trail': audit_trail_document,
}


# ---------------------------------------------------------------------------
# What a viewset offers
#
# Two ways to the same sheet. `print/` answers with the PDF over the ordinary
# authenticated API, which is what the app hands straight to the printer.
# `print-link/` hands back a signed URL instead, for a browser — which cannot
# send the token header and so cannot ask for the PDF itself.
# ---------------------------------------------------------------------------

class PrintMixin:
    """Adds `GET {detail}/print/` and `GET {detail}/print-link/`.

    The row is fetched with `get_object()`, so the tenant scoping and
    permissions are the ones already written for reading it — a hospital can
    never print, or sign a link to, another hospital's invoice.
    """

    print_kind = ''

    @action(detail=True, methods=['get'], url_path='print')
    def print_sheet(self, request, pk=None):
        obj = self.get_object()
        return render_sheet(self.print_kind, request.user, pk=obj.pk, wants_pdf=True)

    @action(detail=True, methods=['get'], url_path='print-link')
    def print_link(self, request, pk=None):
        obj = self.get_object()
        path = reverse('print-document', args=[self.print_kind, obj.pk])
        return _link(request, path, sign(self.print_kind, obj.pk, request.user.pk))


class PrintListMixin:
    """The same two, for a ledger, carrying whatever narrowed the screen.

    A signed link keeps its filters *inside* the signature: passed on the
    address they could be widened to rows the screen never offered.
    """

    print_kind = ''

    @action(detail=False, methods=['get'], url_path='print')
    def print_sheet(self, request):
        return render_sheet(
            self.print_kind, request.user, filters=_ledger_filters(request), wants_pdf=True,
        )

    @action(detail=False, methods=['get'], url_path='print-link')
    def print_list_link(self, request):
        path = reverse('print-ledger', args=[self.print_kind])
        signature = sign(
            self.print_kind, 0, request.user.pk, _ledger_filters(request).urlencode(),
        )
        return _link(request, path, signature)


def _ledger_filters(request):
    query = request.GET.copy()
    query.pop('page', None)  # a printed ledger is not paged
    return query


def _link(request, path, signature):
    return Response({
        'url': request.build_absolute_uri(f'{path}?s={signature}'),
        'expires_in': LINK_MAX_AGE,
    })


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def pdf_font():
    """Register the first installed TrueType pair that can draw ₦.

    Returns the CSS family name to ask for, or '' when no such font is
    installed and the amounts have to fall back to `NGN`.

    The pair goes into reportlab and then into xhtml2pdf's own font table.
    Not through `@font-face`, which is the documented route but copies the file
    into a `NamedTemporaryFile` that Windows will not let reportlab reopen.
    """
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFError, TTFont
    from xhtml2pdf.default import DEFAULT_FONT

    for regular, bold in FONT_CANDIDATES:
        if not (Path(regular).exists() and Path(bold).exists()):
            continue
        try:
            face = TTFont('DocSans', regular)
            if ord(NAIRA) not in face.face.charToGlyph:
                continue
            pdfmetrics.registerFont(face)
            pdfmetrics.registerFont(TTFont('DocSans-Bold', bold))
        except TTFError:
            continue
        for is_bold in (0, 1):
            for is_italic in (0, 1):
                addMapping('DocSans', is_bold, is_italic, 'DocSans-Bold' if is_bold else 'DocSans')
        DEFAULT_FONT['docsans'] = 'DocSans'
        return 'docsans'
    return ''


def local_file(uri, rel):
    """Resolve a linked file to its path on disk.

    Only the font in [FONT_CANDIDATES] is ever linked — no address from the
    document reaches this — and left to itself xhtml2pdf copies the file into
    the temp directory and then loses it on Windows.
    """
    return uri


def as_pdf(html, filename):
    buffer = BytesIO()
    result = pisa.CreatePDF(src=html, dest=buffer, encoding='utf-8', link_callback=local_file)
    if result.err:
        # A template this server owns failed to render: that is a bug here, not
        # a bad request, so let it be a 500 and be seen.
        raise RuntimeError('xhtml2pdf could not render this document.')
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    # Inline: a phone and a desktop both show it, and both offer to save it.
    response['Content-Disposition'] = f'inline; filename="{filename}.pdf"'
    return response


def render_sheet(kind, user, pk=0, filters=None, wants_pdf=False):
    """One printable sheet, as a PDF or as the HTML that prints itself.

    Says nothing about who may see it: every caller has already decided that.
    """
    if kind in LEDGERS:
        context = LEDGERS[kind](user, filters or {})
    elif kind in DOCUMENTS:
        model, build = DOCUMENTS[kind]
        obj = model.objects.filter(pk=pk).first()
        if obj is None:
            raise Http404('That document no longer exists.')
        context = build(obj)
    else:
        raise Http404('Unknown document.')

    font = pdf_font() if wants_pdf else ''
    # Each cell carries its column's alignment, so the template can stay a table
    # printer that knows nothing about which document it is printing.
    aligns = [align for _, align in context['columns']]
    context['rows'] = [list(zip(row, aligns)) for row in context['rows']]
    meta = context['meta']
    context.update({
        # Two label/value pairs to a line, so the header block stays shallow.
        'meta_rows': [meta[index:index + 2] for index in range(0, len(meta), 2)],
        'printed_by': who(user),
        'printed_at': when(timezone.now()),
        'pdf': wants_pdf,
        'pdf_font': font,
    })
    html = render_to_string('print/document.html', context)
    if not wants_pdf:
        return HttpResponse(html)
    if not font:
        # No installed face has the naira sign, and reportlab would print a
        # hollow box. The currency code says the same thing in ASCII.
        html = html.replace(NAIRA, 'NGN ')
    return as_pdf(html, f'{context["title"].title()} {context["reference"]}')


def document(request, kind, pk=0):
    """The browser's way in. Authorised by the signature, nothing else."""
    try:
        value = _signer.unsign(request.GET.get('s', ''), max_age=LINK_MAX_AGE)
    except BadSignature:
        # Expired and forged are the same answer: ask the app for a fresh link.
        raise Http404('This print link has expired. Open the document again.')
    signed_kind, signed_pk, user_id, query = value.split(':', 3)
    if (signed_kind, signed_pk) != (kind, str(pk)):
        raise Http404('This print link is for another document.')

    user = User.objects.select_related('organization').filter(pk=user_id).first()
    if user is None or not user.is_active:
        raise Http404('The account that made this link is gone.')
    # The signature is the whole of this door's authentication, so it answers
    # the same question core/auth.py asks of every token: is the organisation
    # behind this account still trading? A suspension has to shut this link too,
    # or a tenant cut off at the API goes on printing its rows for ten minutes.
    if user.organization is not None and not user.organization.is_active:
        raise Http404('That organisation has been suspended.')

    return render_sheet(
        kind, user, pk=pk, filters=dict(parse_qsl(query)),
        wants_pdf=request.GET.get('format') == 'pdf',
    )
