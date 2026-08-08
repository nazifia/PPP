"""End-to-end check of the request lifecycle and of tenant isolation."""

import io
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import (
    Department,
    Invoice,
    InvoiceStatus,
    OrgKind,
    Organization,
    Partnership,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Product,
    Requisition,
    ReqStatus,
    Role,
    StockMovement,
    Unit,
    User,
)


class SupplyFlowTests(APITestCase):
    def setUp(self):
        self.hospital = Organization.objects.create(
            name='General Hospital', kind=OrgKind.HOSPITAL, phone='08000000001',
        )
        self.hospital_admin = User.objects.create_user(
            phone='08011111111', password='Sup3rSecret!', full_name='Hospital Admin',
            organization=self.hospital, role=Role.ADMIN,
        )
        self.supplier = Organization.objects.create(
            name='Valour Pharmaceuticals', kind=OrgKind.SUPPLIER, phone='08000000002',
        )
        self.supplier_admin = User.objects.create_user(
            phone='08022222222', password='Sup3rSecret!', full_name='Supplier Admin',
            organization=self.supplier, role=Role.ADMIN,
        )
        Partnership.objects.create(hospital=self.hospital, supplier=self.supplier)
        self.product = Product.objects.create(
            supplier=self.supplier, generic_name='10% Dextrose Water', unit='CARTON',
            unit_price='720.00', stock_qty=100,
        )

    def login(self, phone):
        response = self.client.post(
            reverse('login'), {'phone': phone, 'password': 'Sup3rSecret!'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + response.data['token'])
        return response.data

    def test_login_normalises_phone_and_rejects_bad_password(self):
        self.login('0801-111 1111')
        bad = self.client.post(
            reverse('login'), {'phone': '08011111111', 'password': 'wrong'}, format='json',
        )
        self.assertEqual(bad.status_code, 400)

    def test_idle_timeout_policy_is_set_by_an_admin_and_read_by_every_member(self):
        staff = User.objects.create_user(
            phone='08033333333', password='Sup3rSecret!', full_name='Ward Staff',
            organization=self.hospital, role=Role.STAFF,
        )

        self.login('08011111111')
        response = self.client.patch(
            '/api/organization/', {'idle_timeout_minutes': 10}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['idle_timeout_minutes'], 10)

        # Out of range on either side is refused.
        for minutes in (1, 481):
            self.assertEqual(
                self.client.patch(
                    '/api/organization/', {'idle_timeout_minutes': minutes}, format='json',
                ).status_code,
                400,
            )

        # Staff read the policy with their profile but cannot loosen it.
        self.login(staff.phone)
        profile = self.client.get('/api/auth/me/')
        self.assertEqual(profile.data['organization_detail']['idle_timeout_minutes'], 10)
        self.assertEqual(
            self.client.patch(
                '/api/organization/', {'idle_timeout_minutes': 240}, format='json',
            ).status_code,
            403,
        )
        self.hospital.refresh_from_db()
        self.assertEqual(self.hospital.idle_timeout_minutes, 10)

    def test_an_idle_token_expires_on_the_organisation_policy(self):
        self.hospital.idle_timeout_minutes = 10
        self.hospital.save(update_fields=['idle_timeout_minutes'])
        self.login('08011111111')
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

        # Nine minutes idle: still inside the policy, and the request itself
        # counts as activity.
        User.objects.filter(pk=self.hospital_admin.pk).update(
            last_seen_at=timezone.now() - timedelta(minutes=9),
        )
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)
        self.hospital_admin.refresh_from_db()
        self.assertGreater(
            self.hospital_admin.last_seen_at, timezone.now() - timedelta(minutes=1),
        )

        # Eleven minutes idle: the token is gone, not merely refused.
        User.objects.filter(pk=self.hospital_admin.pk).update(
            last_seen_at=timezone.now() - timedelta(minutes=11),
        )
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 401)
        self.assertFalse(Token.objects.filter(user=self.hospital_admin).exists())

        # Signing in again clears the idle clock the old token left behind.
        self.login('08011111111')
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

    def test_full_request_to_invoice_cycle(self):
        # Hospital builds a wishlist and submits it.
        self.login('08011111111')
        response = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 40}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        requisition_id = response.data['id']
        line_id = response.data['lines'][0]['id']

        response = self.client.post(f'/api/requisitions/{requisition_id}/submit/', format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], ReqStatus.SUBMITTED)

        # Supplier approves 30 of the 40 requested.
        self.login('08022222222')
        response = self.client.post(
            f'/api/requisitions/{requisition_id}/decide/',
            {'lines': [{'line': line_id, 'qty_approved': 30, 'note': 'partial stock'}]},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], ReqStatus.PARTIALLY_APPROVED)

        # Approving more than requested is refused.
        response = self.client.post(
            f'/api/requisitions/{requisition_id}/decide/',
            {'lines': [{'line': line_id, 'qty_approved': 999}]}, format='json',
        )
        self.assertEqual(response.status_code, 400)

        # Supplier dispatches the 30; stock leaves the shelf.
        response = self.client.post(
            f'/api/requisitions/{requisition_id}/dispatch/',
            {'items': [{'line': line_id, 'qty': 30, 'batch_no': 'B-1'}], 'waybill_no': 'WB-9'},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        delivery_id = response.data['id']
        delivery_line_id = response.data['lines'][0]['id']
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 70)

        # Hospital verifies: 25 good, 5 damaged.
        self.login('08011111111')
        response = self.client.post(
            f'/api/deliveries/{delivery_id}/verify/',
            {'lines': [{
                'line': delivery_line_id, 'qty_accepted': 25, 'qty_rejected': 5,
                'reason': 'seals broken',
            }]},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 75)  # the 5 rejected went back

        invoice = Invoice.objects.get(delivery_id=delivery_id)
        self.assertEqual(str(invoice.amount), '18000.00')  # 25 * 720

        response = self.client.get(f'/api/requisitions/{requisition_id}/')
        self.assertEqual(response.data['status'], ReqStatus.DELIVERED)  # 5 of 30 still owed

        # The spend lands on the department the unit belongs to.
        department = Department.objects.create(organization=self.hospital, name='LABORATORY')
        unit = Unit.objects.create(department=department, name='HAEMATOLOGY')
        Requisition.objects.filter(pk=requisition_id).update(unit=unit)
        spend = self.client.get('/api/dashboard/').data['spend_by_department']
        self.assertEqual(spend, [{
            'department_id': department.id, 'department_name': 'LABORATORY',
            'total': Decimal('18000'), 'paid': Decimal('0'),
        }])

    def test_dashboard_reports_six_months_of_requests_and_fills_the_empty_ones(self):
        self.login('08011111111')
        self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 3},
            format='json',
        )
        monthly = self.client.get('/api/dashboard/').data['requests_monthly']
        self.assertEqual(len(monthly), 6)
        # Oldest first, this month last, and the five quiet months still there.
        self.assertEqual([row['month'] for row in monthly], sorted(row['month'] for row in monthly))
        self.assertEqual(
            monthly[-1], {'month': timezone.localdate().replace(day=1).isoformat(), 'count': 1},
        )
        self.assertEqual([row['count'] for row in monthly[:-1]], [0] * 5)

    def test_dashboard_trend_window_and_the_month_a_bar_drills_into(self):
        requisition, _ = self.submit_request('08011111111', 3)
        this_month = timezone.localdate().replace(day=1)
        last_month = (this_month - timedelta(days=1)).replace(day=1)

        # A supplier counts a request from when it was submitted to it, not from
        # when the hospital started raising it a month earlier.
        Requisition.objects.filter(pk=requisition).update(
            created_at=(timezone.now().replace(day=1) - timedelta(days=1)).replace(hour=12),
        )
        self.login('08022222222')
        monthly = self.client.get('/api/dashboard/', {'months': 12}).data['requests_monthly']
        self.assertEqual(len(monthly), 12)
        self.assertEqual(monthly[-1], {'month': this_month.isoformat(), 'count': 1})
        # The hospital's own view counts the same request under the earlier month.
        self.login('08011111111')
        hospital_monthly = self.client.get('/api/dashboard/').data['requests_monthly']
        self.assertEqual(hospital_monthly[-1], {'month': this_month.isoformat(), 'count': 0})
        self.assertEqual(hospital_monthly[-2], {'month': last_month.isoformat(), 'count': 1})

        # Tapping a bar asks the list for that month, counted the same way.
        self.assertEqual(
            self.client.get('/api/requisitions/', {'month': last_month.isoformat()}).data['count'],
            1,
        )
        self.assertEqual(
            self.client.get('/api/requisitions/', {'month': this_month.isoformat()}).data['count'],
            0,
        )
        self.login('08022222222')
        self.assertEqual(
            self.client.get('/api/requisitions/', {'month': this_month.isoformat()}).data['count'],
            1,
        )
        # A window is capped and a junk month is refused rather than guessed at.
        self.assertEqual(
            len(self.client.get('/api/dashboard/', {'months': 999}).data['requests_monthly']), 24,
        )
        self.assertEqual(
            len(self.client.get('/api/dashboard/', {'months': 'lots'}).data['requests_monthly']), 6,
        )
        self.assertEqual(
            self.client.get('/api/requisitions/', {'month': 'August'}).status_code, 400,
        )

    def submit_request(self, phone, qty):
        """Log in as `phone`, wishlist `qty` of the test product and submit it."""
        self.login(phone)
        requisition = self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': qty},
            format='json',
        ).data
        submitted = self.client.post(
            f"/api/requisitions/{requisition['id']}/submit/", format='json',
        ).data
        return requisition['id'], submitted['lines'][0]['id']

    def raise_invoice(self, qty=10):
        """Run a request all the way to a verified delivery and return its invoice."""
        requisition, line = self.submit_request('08011111111', qty)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': qty}]}, format='json',
        )
        delivery = self.client.post(
            f'/api/requisitions/{requisition}/dispatch/',
            {'items': [{'line': line, 'qty': qty}]}, format='json',
        ).data
        self.login('08011111111')
        self.client.post(
            f"/api/deliveries/{delivery['id']}/verify/",
            {'lines': [{'line': delivery['lines'][0]['id'], 'qty_accepted': qty}]},
            format='json',
        )
        return Invoice.objects.get(delivery_id=delivery['id'])

    def test_payment_only_counts_once_the_supplier_confirms_it(self):
        invoice = self.raise_invoice(qty=10)  # 10 * 720 = 7200
        self.assertEqual(str(invoice.amount), '7200.00')

        # A transfer without its reference is not something anyone can check.
        self.login('08011111111')
        bare = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '7200.00', 'method': PaymentMethod.TRANSFER}, format='json',
        )
        self.assertEqual(bare.status_code, 400)

        recorded = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '3000.00', 'method': PaymentMethod.TRANSFER, 'payer_reference': 'TRF-1'},
            format='json',
        )
        self.assertEqual(recorded.status_code, 201, recorded.data)
        self.assertEqual(recorded.data['status'], PaymentStatus.PENDING)
        payment_id = recorded.data['id']

        # Declared money does not settle the invoice on the hospital's word alone.
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.UNPAID)
        self.assertEqual(str(invoice.amount_paid), '0.00')
        self.assertEqual(str(invoice.amount_pending), '3000.00')

        # The same slip cannot be entered twice, and the pending 3000 is held back.
        self.assertEqual(
            self.client.post(
                f'/api/invoices/{invoice.id}/pay/',
                {'amount': '100.00', 'method': PaymentMethod.TRANSFER, 'payer_reference': 'TRF-1'},
                format='json',
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f'/api/invoices/{invoice.id}/pay/',
                {'amount': '4300.00', 'method': PaymentMethod.CASH}, format='json',
            ).status_code,
            400,
        )

        # The hospital cannot confirm its own payment.
        self.assertEqual(
            self.client.post(f'/api/payments/{payment_id}/confirm/', format='json').status_code,
            403,
        )

        self.login('08022222222')
        confirmed = self.client.post(f'/api/payments/{payment_id}/confirm/', format='json')
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data['status'], PaymentStatus.CONFIRMED)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.PART_PAID)
        self.assertEqual(str(invoice.amount_paid), '3000.00')

        # Deciding it a second time changes nothing.
        self.assertEqual(
            self.client.post(f'/api/payments/{payment_id}/confirm/', format='json').status_code,
            400,
        )

        # The rest, in cash, settles it.
        self.login('08011111111')
        rest = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '4200.00', 'method': PaymentMethod.CASH}, format='json',
        )
        self.assertEqual(rest.status_code, 201, rest.data)
        self.login('08022222222')
        self.client.post(f"/api/payments/{rest.data['id']}/confirm/", format='json')

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.PAID)
        self.assertEqual(str(invoice.balance), '0.00')
        self.assertIsNotNone(invoice.paid_at)

    def test_rejected_payment_frees_its_reference_and_moves_no_money(self):
        invoice = self.raise_invoice(qty=10)
        self.login('08011111111')
        payment = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '7200.00', 'method': PaymentMethod.CHEQUE, 'payer_reference': 'CHQ-7'},
            format='json',
        ).data

        self.login('08022222222')
        # A rejection needs a reason.
        self.assertEqual(
            self.client.post(f"/api/payments/{payment['id']}/reject/", format='json').status_code,
            400,
        )
        rejected = self.client.post(
            f"/api/payments/{payment['id']}/reject/", {'reason': 'cheque bounced'}, format='json',
        )
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data['status'], PaymentStatus.REJECTED)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.UNPAID)
        self.assertEqual(str(invoice.amount_paid), '0.00')
        self.assertEqual(str(invoice.amount_unclaimed), '7200.00')

        # The rejected slip's reference is free to be recorded again.
        self.login('08011111111')
        again = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '7200.00', 'method': PaymentMethod.CHEQUE, 'payer_reference': 'CHQ-7'},
            format='json',
        )
        self.assertEqual(again.status_code, 201, again.data)

    def test_invoice_takes_its_due_date_from_the_suppliers_terms(self):
        Organization.objects.filter(pk=self.supplier.pk).update(payment_terms_days=7)
        invoice = self.raise_invoice(qty=10)
        self.assertEqual(invoice.terms_days, 7)
        self.assertEqual(invoice.due_date, timezone.localdate() + timedelta(days=7))
        self.assertFalse(invoice.is_overdue)

        # Changing the terms afterwards leaves an agreed date alone.
        Organization.objects.filter(pk=self.supplier.pk).update(payment_terms_days=60)
        invoice.refresh_from_db()
        self.assertEqual(invoice.due_date, timezone.localdate() + timedelta(days=7))

        # Age it past the due date: it is overdue and shows up in the filter.
        Invoice.objects.filter(pk=invoice.pk).update(
            due_date=timezone.localdate() - timedelta(days=3),
        )
        self.login('08011111111')
        listed = self.client.get('/api/invoices/', {'overdue': 'true'}).data['results']
        self.assertEqual([row['reference'] for row in listed], [invoice.reference])
        self.assertEqual(listed[0]['days_overdue'], 3)
        self.assertTrue(listed[0]['is_overdue'])
        self.assertEqual(self.client.get('/api/dashboard/').data['invoices_overdue'], 1)

        # Settling it takes it out of both.
        payment = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '7200.00', 'method': PaymentMethod.CASH}, format='json',
        ).data
        self.login('08022222222')
        self.client.post(f"/api/payments/{payment['id']}/confirm/", format='json')
        self.login('08011111111')
        self.assertEqual(self.client.get('/api/invoices/', {'overdue': 'true'}).data['results'], [])
        self.assertEqual(self.client.get('/api/dashboard/').data['invoices_overdue'], 0)
        invoice.refresh_from_db()
        self.assertEqual(invoice.days_overdue, 0)  # paid, however late it was

    def test_receipt_is_stored_with_the_payment_and_screened_on_the_way_in(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=media.name))

        invoice = self.raise_invoice(qty=10)
        self.login('08011111111')

        # An executable is not a receipt.
        rejected = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {
                'amount': '100.00', 'method': PaymentMethod.CASH,
                'receipt': SimpleUploadedFile('slip.exe', b'MZ', content_type='application/exe'),
            },
            format='multipart',
        )
        self.assertEqual(rejected.status_code, 400)

        # Nor is a 6 MB one.
        toobig = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {
                'amount': '100.00', 'method': PaymentMethod.CASH,
                'receipt': SimpleUploadedFile('slip.png', b'x' * (6 * 1024 * 1024)),
            },
            format='multipart',
        )
        self.assertEqual(toobig.status_code, 400)

        response = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {
                'amount': '7200.00', 'method': PaymentMethod.TRANSFER,
                'payer_reference': 'TRF-9',
                'receipt': SimpleUploadedFile('slip.jpg', b'jpegbytes', content_type='image/jpeg'),
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn('.jpg', response.data['receipt'])

        payment = Payment.objects.get(pk=response.data['id'])
        self.assertTrue(payment.receipt.name.startswith('receipts/'))

        # The company sees the slip it has to check.
        self.login('08022222222')
        listed = self.client.get('/api/payments/').data['results']
        self.assertEqual(len(listed), 1)
        self.assertIn('.jpg', listed[0]['receipt'])

    def test_a_stranger_cannot_see_or_decide_another_tenants_payments(self):
        invoice = self.raise_invoice(qty=10)
        self.login('08011111111')
        payment = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '720.00', 'method': PaymentMethod.CASH}, format='json',
        ).data

        rival = Organization.objects.create(
            name='Other Supplier', kind=OrgKind.SUPPLIER, phone='08000000009',
        )
        User.objects.create_user(
            phone='08099999999', password='Sup3rSecret!', full_name='Other Admin',
            organization=rival, role=Role.ADMIN,
        )
        self.login('08099999999')
        self.assertEqual(self.client.get('/api/payments/').data['results'], [])
        self.assertEqual(
            self.client.post(f"/api/payments/{payment['id']}/confirm/", format='json').status_code,
            404,
        )

    def test_approval_reserves_stock_against_a_second_hospital(self):
        rival = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000004',
        )
        User.objects.create_user(
            phone='08044444444', password='Sup3rSecret!', full_name='Rival Admin',
            organization=rival, role=Role.ADMIN,
        )
        Partnership.objects.create(hospital=rival, supplier=self.supplier)

        # Both hospitals ask for the whole shelf.
        first, first_line = self.submit_request('08011111111', 100)
        second, second_line = self.submit_request('08044444444', 100)

        self.login('08022222222')
        approved = self.client.post(
            f'/api/requisitions/{first}/decide/',
            {'lines': [{'line': first_line, 'qty_approved': 100}]}, format='json',
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 100)
        self.assertEqual(self.product.available_qty, 0)

        # The same cartons cannot be promised twice, even though stock_qty is still 100.
        clash = self.client.post(
            f'/api/requisitions/{second}/decide/',
            {'lines': [{'line': second_line, 'qty_approved': 1}]}, format='json',
        )
        self.assertEqual(clash.status_code, 400)

        # Dispatching converts the reservation into a real issue, it does not double count.
        self.client.post(
            f'/api/requisitions/{first}/dispatch/',
            {'items': [{'line': first_line, 'qty': 100}]}, format='json',
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 0)
        self.assertEqual(self.product.qty_reserved, 0)

    def test_supplier_releases_an_approval_it_never_dispatched(self):
        requisition, line = self.submit_request('08011111111', 40)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 40}]}, format='json',
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 40)

        # A release needs a reason.
        self.assertEqual(
            self.client.post(f'/api/requisitions/{requisition}/release/', format='json')
            .status_code,
            400,
        )
        response = self.client.post(
            f'/api/requisitions/{requisition}/release/', {'reason': 'recalled batch'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], ReqStatus.CANCELLED)

        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 0)
        self.assertEqual(self.product.stock_qty, 100)  # nothing shipped, nothing lost

        # Nothing left to give back the second time.
        again = self.client.post(
            f'/api/requisitions/{requisition}/release/', {'reason': 'again'}, format='json',
        )
        self.assertEqual(again.status_code, 400)

    def test_release_keeps_what_was_already_dispatched(self):
        requisition, line = self.submit_request('08011111111', 40)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 40}]}, format='json',
        )
        self.client.post(
            f'/api/requisitions/{requisition}/dispatch/',
            {'items': [{'line': line, 'qty': 15}]}, format='json',
        )
        response = self.client.post(
            f'/api/requisitions/{requisition}/release/', {'reason': 'out of stock'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], ReqStatus.DISPATCHED)  # verify still pending
        self.assertEqual(response.data['lines'][0]['qty_approved'], 15)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 85)  # the 15 that shipped
        self.assertEqual(self.product.qty_reserved, 0)  # the other 25 are free again

    def test_sweep_releases_only_approvals_past_the_cutoff(self):
        requisition, line = self.submit_request('08011111111', 40)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 40}]}, format='json',
        )

        # A fresh approval is left alone.
        call_command('release_stale_approvals', '--days', '14', stdout=StringIO())
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 40)

        # Age it past the cutoff.
        Requisition.objects.filter(pk=requisition).update(
            decided_at=timezone.now() - timedelta(days=20),
        )
        call_command('release_stale_approvals', '--days', '14', stdout=StringIO())

        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 0)
        self.assertEqual(self.product.stock_qty, 100)
        self.assertEqual(
            Requisition.objects.get(pk=requisition).status, ReqStatus.CANCELLED,
        )

    def test_sweep_dry_run_changes_nothing(self):
        requisition, line = self.submit_request('08011111111', 40)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 40}]}, format='json',
        )
        Requisition.objects.filter(pk=requisition).update(
            decided_at=timezone.now() - timedelta(days=20),
        )
        out = StringIO()
        call_command('release_stale_approvals', '--days', '14', '--dry-run', stdout=out)

        self.assertIn('would release 40', out.getvalue())
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 40)

    def test_hospital_cannot_release(self):
        requisition, line = self.submit_request('08011111111', 5)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 5}]}, format='json',
        )
        self.login('08011111111')
        response = self.client.post(
            f'/api/requisitions/{requisition}/release/', {'reason': 'changed our minds'},
            format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_restock_cannot_take_stock_below_what_is_reserved(self):
        requisition, line = self.submit_request('08011111111', 60)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 60}]}, format='json',
        )
        response = self.client.post(
            f'/api/products/{self.product.id}/restock/', {'qty': -50}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_stock_ledger_filters_by_product_and_kind(self):
        other = Product.objects.create(
            supplier=self.supplier, generic_name='Paracetamol', unit='PACK',
            unit_price='100.00', stock_qty=10,
        )
        StockMovement.objects.create(
            organization=self.supplier, product=self.product,
            kind=StockMovement.ADJUST, qty=100,
        )
        StockMovement.objects.create(
            organization=self.supplier, product=self.product,
            kind=StockMovement.ISSUE, qty=-5,
        )
        StockMovement.objects.create(
            organization=self.supplier, product=other,
            kind=StockMovement.ISSUE, qty=-1,
        )
        self.login('08022222222')
        url = reverse('stock-movement-list')

        self.assertEqual(self.client.get(url).data['count'], 3)
        self.assertEqual(self.client.get(url, {'kind': StockMovement.ISSUE}).data['count'], 2)
        self.assertEqual(self.client.get(url, {'product': self.product.pk}).data['count'], 2)
        both = self.client.get(url, {'product': self.product.pk, 'kind': StockMovement.ISSUE})
        self.assertEqual(both.data['count'], 1)
        self.assertEqual(both.data['results'][0]['qty'], -5)

    def test_stock_ledger_filters_by_date_and_totals_balances(self):
        recent = StockMovement.objects.create(
            organization=self.supplier, product=self.product,
            kind=StockMovement.ADJUST, qty=100,
        )
        old = StockMovement.objects.create(
            organization=self.supplier, product=self.product,
            kind=StockMovement.ISSUE, qty=-30,
        )
        # auto_now_add ignores an assigned value, so the date is pushed back after.
        long_ago = timezone.now() - timedelta(days=40)
        StockMovement.objects.filter(pk=old.pk).update(created_at=long_ago)
        self.login('08022222222')
        url = reverse('stock-movement-list')
        balances = reverse('stock-movement-balances')

        # Both ends inclusive: the range ending today still holds today's row.
        today = recent.created_at.date().isoformat()
        windowed = self.client.get(url, {'from': today, 'to': today})
        self.assertEqual(windowed.data['count'], 1)
        self.assertEqual(windowed.data['results'][0]['id'], recent.pk)
        self.assertEqual(
            self.client.get(url, {'to': long_ago.date().isoformat()}).data['count'], 1,
        )

        self.assertEqual(
            self.client.get(balances).data,
            [{'product': self.product.pk, 'product_name': str(self.product), 'balance': 70}],
        )
        # A balance answers the same filters the list does.
        self.assertEqual(
            self.client.get(balances, {'from': today}).data[0]['balance'], 100,
        )

    def test_hospital_dispenses_against_what_it_holds(self):
        StockMovement.objects.create(
            organization=self.hospital, product=self.product,
            kind=StockMovement.RECEIPT, qty=12,
        )
        self.login('08011111111')
        url = reverse('stock-movement-dispense')
        balances = reverse('stock-movement-balances')

        response = self.client.post(
            url, {'product': self.product.pk, 'qty': 5, 'note': 'Ward B'}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['qty'], -5)
        self.assertEqual(response.data['kind'], StockMovement.DISPENSE)
        self.assertEqual(self.client.get(balances).data[0]['balance'], 7)

        # Never below what the ledger says is left, and never a non-positive qty.
        over = self.client.post(url, {'product': self.product.pk, 'qty': 8}, format='json')
        self.assertEqual(over.status_code, 400)
        self.assertIn('only 7 in stock', str(over.data))
        self.assertEqual(
            self.client.post(url, {'product': self.product.pk, 'qty': 0}, format='json')
            .status_code,
            400,
        )
        self.assertEqual(self.client.get(balances).data[0]['balance'], 7)

    def test_supplier_cannot_dispense(self):
        self.login('08022222222')
        response = self.client.post(
            reverse('stock-movement-dispense'),
            {'product': self.product.pk, 'qty': 1}, format='json',
        )
        self.assertEqual(response.status_code, 403)
        # The supplier's own stock is untouched by the attempt.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 100)

    def test_stock_ledger_pages_without_dropping_a_row(self):
        # 30 rows written in the same instant: only the id tiebreaker keeps the
        # two pages from overlapping.
        StockMovement.objects.bulk_create(
            StockMovement(
                organization=self.supplier, product=self.product,
                kind=StockMovement.ADJUST, qty=index + 1,
            )
            for index in range(30)
        )
        self.login('08022222222')
        url = reverse('stock-movement-list')
        first = self.client.get(url).data
        second = self.client.get(url, {'page': 2}).data
        seen = [row['id'] for row in first['results'] + second['results']]

        self.assertEqual(first['count'], 30)
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)
        self.assertIsNone(second['next'])

    def test_rejected_quantity_needs_a_reason(self):
        self.login('08011111111')
        requisition_id = self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 2},
            format='json',
        ).data['id']
        line_id = self.client.post(
            f'/api/requisitions/{requisition_id}/submit/', format='json',
        ).data['lines'][0]['id']
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition_id}/decide/',
            {'lines': [{'line': line_id, 'qty_approved': 2}]}, format='json',
        )
        dispatch = self.client.post(
            f'/api/requisitions/{requisition_id}/dispatch/',
            {'items': [{'line': line_id, 'qty': 2}]}, format='json',
        ).data
        self.login('08011111111')
        response = self.client.post(
            f"/api/deliveries/{dispatch['id']}/verify/",
            {'lines': [{
                'line': dispatch['lines'][0]['id'], 'qty_accepted': 1, 'qty_rejected': 1,
            }]},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_tenants_cannot_see_each_other(self):
        other_hospital = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000003',
        )
        User.objects.create_user(
            phone='08033333333', password='Sup3rSecret!', full_name='Rival Admin',
            organization=other_hospital, role=Role.ADMIN,
        )
        self.login('08011111111')
        self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 5},
            format='json',
        )
        self.login('08033333333')
        # No partnership, so neither the catalogue nor the other hospital's requests show up.
        self.assertEqual(self.client.get('/api/products/').data['count'], 0)
        self.assertEqual(self.client.get('/api/requisitions/').data['count'], 0)

    def test_superuser_reads_every_tenant_but_writes_for_none(self):
        User.objects.create_superuser(phone='08044444444', password='Sup3rSecret!')
        self.login('08011111111')
        self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 3},
            format='json',
        )
        self.login('08044444444')
        # Both tenants' rows, including a hospital draft it has no partnership for.
        self.assertEqual(self.client.get('/api/products/').data['count'], 1)
        self.assertEqual(self.client.get('/api/requisitions/').data['count'], 1)
        self.assertEqual(self.client.get('/api/companies/').data['count'], 2)
        self.assertEqual(self.client.get('/api/audit-logs/').status_code, 200)
        self.assertEqual(self.client.get('/api/dashboard/').status_code, 200)
        # But it belongs to no organisation, so it cannot write on one's behalf.
        self.assertEqual(
            self.client.post('/api/departments/', {'name': 'Theatre'}, format='json').status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 1},
                format='json',
            ).status_code,
            403,
        )

    def test_superuser_acts_on_any_row_without_stepping_into_a_tenant(self):
        User.objects.create_superuser(phone='08044444444', password='Sup3rSecret!')
        requisition, line = self.submit_request('08011111111', 20)

        # No X-Act-As-Org header anywhere below: each call takes the tenant from
        # the row it touches, supplier side and hospital side alike.
        self.login('08044444444')
        response = self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 20}]}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)

        response = self.client.post(
            f'/api/requisitions/{requisition}/dispatch/',
            {'items': [{'line': line, 'qty': 20, 'batch_no': 'B-7'}]}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        delivery, delivery_line = response.data['id'], response.data['lines'][0]['id']

        self.login('08044444444')
        response = self.client.post(
            f'/api/deliveries/{delivery}/verify/',
            {'lines': [{'line': delivery_line, 'qty_accepted': 20, 'qty_rejected': 0}]},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.login('08044444444')
        response = self.client.post(
            f'/api/products/{self.product.id}/restock/', {'qty': 5}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Invoice.objects.filter(delivery_id=delivery).count(), 1)
        # The switch never touches the account itself.
        self.assertIsNone(User.objects.get(phone='08044444444').organization)

    def test_a_tenant_still_cannot_reach_another_tenants_rows(self):
        other = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000003',
        )
        User.objects.create_user(
            phone='08055555555', password='Sup3rSecret!', full_name='Rival Admin',
            organization=other, role=Role.ADMIN,
        )
        requisition, line = self.submit_request('08011111111', 5)
        self.login('08055555555')
        response = self.client.post(f'/api/requisitions/{requisition}/cancel/', format='json')
        self.assertIn(response.status_code, (403, 404))

    def act_as(self, token, org):
        self.client.credentials(
            HTTP_AUTHORIZATION='Token ' + token, HTTP_X_ACT_AS_ORG=str(org.id),
        )

    def test_superuser_steps_into_one_tenant_and_works_as_its_admin(self):
        User.objects.create_superuser(phone='08044444444', password='Sup3rSecret!')
        session = self.login('08044444444')
        self.act_as(session['token'], self.hospital)

        profile = self.client.get('/api/auth/me/').data
        self.assertEqual(profile['organization_kind'], OrgKind.HOSPITAL)
        self.assertEqual(profile['organization'], self.hospital.id)

        response = self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 2},
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        requisition = Requisition.objects.get(pk=response.data['id'])
        self.assertEqual(requisition.hospital, self.hospital)

        # Scoped to that tenant only: another hospital's view is empty, and the
        # switch is in memory, so the real account still has no organisation.
        other = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000003',
        )
        self.act_as(session['token'], other)
        self.assertEqual(self.client.get('/api/requisitions/').data['count'], 0)
        self.assertIsNone(User.objects.get(phone='08044444444').organization)

    def test_superuser_names_the_tenant_in_the_payload_of_a_create(self):
        User.objects.create_superuser(phone='08044444444', password='Sup3rSecret!')
        self.login('08044444444')

        # No header: the create says which tenant it belongs to itself.
        response = self.client.post(
            '/api/departments/',
            {'name': 'Theatre', 'organization': self.hospital.id}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Department.objects.get(pk=response.data['id']).organization, self.hospital,
        )

        response = self.client.post(
            '/api/departments/', {'name': 'Ward', 'organization': 99999}, format='json',
        )
        self.assertEqual(response.status_code, 401, response.data)

    def test_superuser_names_the_tenant_in_the_query_string_of_a_bodyless_write(self):
        User.objects.create_superuser(phone='08044444444', password='Sup3rSecret!')
        self.login('08044444444')

        response = self.client.delete(
            f'/api/companies/{self.supplier.id}/?organization={self.hospital.id}',
        )
        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(
            Partnership.objects.get(hospital=self.hospital, supplier=self.supplier).is_active,
        )

    def test_superuser_lists_every_organisation_from_inside_a_tenant(self):
        User.objects.create_superuser(phone='08044444444', password='Sup3rSecret!')
        session = self.login('08044444444')

        # Stepped into the supplier, so the hospital-only company list is shut:
        # the switcher reads /organizations/ and can still step back out.
        self.act_as(session['token'], self.supplier)
        self.assertEqual(self.client.get('/api/companies/').status_code, 403)
        response = self.client.get('/api/organizations/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            {row['id'] for row in response.data}, {self.hospital.id, self.supplier.id},
        )

        self.login('08022222222')
        self.assertEqual(self.client.get('/api/organizations/').status_code, 403)

    def test_payload_organization_does_nothing_for_a_normal_user(self):
        self.login('08022222222')
        profile = self.client.get('/api/auth/me/').data
        self.assertEqual(profile['organization'], self.supplier.id)

    def test_act_as_header_does_nothing_for_a_normal_user(self):
        session = self.login('08022222222')
        self.act_as(session['token'], self.hospital)
        profile = self.client.get('/api/auth/me/').data
        self.assertEqual(profile['organization'], self.supplier.id)

    def test_supplier_cannot_be_reached_without_a_partnership(self):
        Partnership.objects.filter(hospital=self.hospital, supplier=self.supplier).update(
            is_active=False,
        )
        self.login('08011111111')
        response = self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 1},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_hospital_registers_a_company_and_that_company_can_log_in(self):
        self.login('08011111111')
        response = self.client.post('/api/companies/', {
            'name': 'DCL Lab Products', 'phone': '08166717574', 'category': 'LABORATORY',
            'contact_full_name': 'Lab Contact', 'contact_phone': '08099999999',
            'password': 'Sup3rSecret!',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.client.credentials()
        profile = self.login('08099999999')
        self.assertEqual(profile['user']['organization_kind'], OrgKind.SUPPLIER)
        self.assertTrue(profile['user']['must_change_password'])

    def test_units_live_under_a_department_and_tag_a_request(self):
        self.login('08011111111')
        department = self.client.post(
            '/api/departments/', {'name': 'MAIN LABORATORY'}, format='json',
        ).data
        unit = self.client.post(
            '/api/units/', {'name': 'HAEMATOLOGY', 'department': department['id']},
            format='json',
        )
        self.assertEqual(unit.status_code, 201, unit.data)
        self.assertEqual(unit.data['full_name'], 'MAIN LABORATORY / HAEMATOLOGY')

        # The same unit name may repeat under another department, but not twice
        # under the same one.
        other = self.client.post('/api/departments/', {'name': 'A&E'}, format='json').data
        twin = self.client.post(
            '/api/units/', {'name': 'HAEMATOLOGY', 'department': other['id']}, format='json',
        )
        self.assertEqual(twin.status_code, 201, twin.data)
        duplicate = self.client.post(
            '/api/units/', {'name': 'HAEMATOLOGY', 'department': department['id']},
            format='json',
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            self.client.post('/api/departments/', {'name': 'A&E'}, format='json').status_code,
            400,
        )

        self.assertEqual(self.client.get('/api/departments/').data['count'], 2)
        listed = self.client.get(f'/api/units/?department={department["id"]}').data
        self.assertEqual([row['name'] for row in listed['results']], ['HAEMATOLOGY'])

        requisition = self.client.post(
            '/api/requisitions/',
            {'supplier': self.supplier.id, 'unit': unit.data['id']}, format='json',
        )
        self.assertEqual(requisition.status_code, 201, requisition.data)
        self.assertEqual(requisition.data['department_name'], 'MAIN LABORATORY / HAEMATOLOGY')

        # A unit cannot be paired with a department it does not belong to.
        mismatch = self.client.post(
            '/api/requisitions/',
            {'supplier': self.supplier.id, 'unit': unit.data['id'], 'department': other['id']},
            format='json',
        )
        self.assertEqual(mismatch.status_code, 400)

        # Filtering by a department answers with its units' requests as well.
        self.client.post(
            '/api/requisitions/',
            {'supplier': self.supplier.id, 'department': other['id']}, format='json',
        )
        by_department = self.client.get(f'/api/requisitions/?department={department["id"]}').data
        self.assertEqual([row['id'] for row in by_department['results']], [requisition.data['id']])
        by_unit = self.client.get(f'/api/requisitions/?unit={unit.data["id"]}').data
        self.assertEqual(by_unit['count'], 1)
        self.assertEqual(self.client.get('/api/requisitions/').data['count'], 2)

        # Another tenant's department is never a valid home for a unit or a tag.
        rival = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000009',
        )
        foreign = Department.objects.create(organization=rival, name='THEIR LAB')
        response = self.client.post(
            '/api/units/', {'name': 'SMUGGLED', 'department': foreign.id}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        foreign_unit = Unit.objects.create(department=foreign, name='THEIR BENCH')
        response = self.client.post(
            '/api/requisitions/',
            {'supplier': self.supplier.id, 'unit': foreign_unit.id}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_staff_cannot_promote_themselves_or_touch_a_partner_company(self):
        staff = User.objects.create_user(
            phone='08077777777', password='Sup3rSecret!', full_name='Ward Clerk',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08077777777')

        response = self.client.patch(
            reverse('me'), {'role': Role.ADMIN, 'job_title': 'Clerk'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        staff.refresh_from_db()
        self.assertEqual(staff.role, Role.STAFF)
        self.assertEqual(staff.job_title, 'Clerk')

        # Suspending, editing or registering a company is the administrator's.
        self.assertEqual(
            self.client.delete(f'/api/companies/{self.supplier.id}/').status_code, 403,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/companies/{self.supplier.id}/', {'name': 'Hijacked'}, format='json',
            ).status_code,
            403,
        )

        # Even an administrator cannot close the company's own account from here.
        self.login('08011111111')
        response = self.client.patch(
            f'/api/companies/{self.supplier.id}/', {'is_active': False}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.supplier.refresh_from_db()
        self.assertTrue(self.supplier.is_active)

    def test_supplier_staff_read_the_catalogue_but_do_not_price_approve_or_release(self):
        User.objects.create_user(
            phone='08088888888', password='Sup3rSecret!', full_name='Storekeeper',
            organization=self.supplier, role=Role.STAFF,
        )
        self.login('08011111111')
        self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 4}, format='json',
        )
        requisition = Requisition.objects.get()
        self.client.post(f'/api/requisitions/{requisition.id}/submit/', format='json')

        self.login('08088888888')
        self.assertEqual(self.client.get('/api/products/').status_code, 200)
        for path, payload in (
            ('/api/products/', {'generic_name': 'Saline', 'unit_price': '100.00'}),
            (f'/api/products/{self.product.id}/restock/', {'qty': 50}),
            (f'/api/requisitions/{requisition.id}/decide/', {
                'lines': [{'line': requisition.lines.get().id, 'qty_approved': 4}],
            }),
        ):
            self.assertEqual(
                self.client.post(path, payload, format='json').status_code, 403, path,
            )
        self.assertEqual(
            self.client.patch(
                f'/api/products/{self.product.id}/', {'unit_price': '1.00'}, format='json',
            ).status_code,
            403,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 100)
        self.assertEqual(self.product.unit_price, Decimal('720.00'))
        requisition.refresh_from_db()
        self.assertEqual(requisition.status, ReqStatus.SUBMITTED)

        self.login('08022222222')
        self.assertEqual(
            self.client.post(
                f'/api/requisitions/{requisition.id}/decide/',
                {'lines': [{'line': requisition.lines.get().id, 'qty_approved': 4}]},
                format='json',
            ).status_code,
            200,
        )

        # Giving the approval back is the same call, so it answers to the same rank.
        self.login('08088888888')
        self.assertEqual(
            self.client.post(
                f'/api/requisitions/{requisition.id}/release/',
                {'reason': 'out of stock'}, format='json',
            ).status_code,
            403,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 4)

        # Dispatching what is already approved is warehouse work, not the admin's.
        self.assertEqual(
            self.client.post(
                f'/api/requisitions/{requisition.id}/dispatch/',
                {'items': [{'line': requisition.lines.get().id, 'qty': 4}]}, format='json',
            ).status_code,
            201,
        )

    def test_only_a_hospital_administrator_redraws_the_organisation(self):
        department = Department.objects.create(organization=self.hospital, name='PHARMACY')
        User.objects.create_user(
            phone='08077777777', password='Sup3rSecret!', full_name='Ward Clerk',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08077777777')

        # Staff still read the list: raising a request means picking a department.
        self.assertEqual(self.client.get('/api/departments/').status_code, 200)
        for path, payload in (
            ('/api/departments/', {'name': 'THEATRE'}),
            ('/api/units/', {'name': 'HAEMATOLOGY', 'department': department.id}),
        ):
            self.assertEqual(self.client.post(path, payload, format='json').status_code, 403)
        self.assertEqual(
            self.client.patch(
                f'/api/departments/{department.id}/', {'name': 'RENAMED'}, format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f'/api/departments/{department.id}/').status_code, 403,
        )

        # A supplier has no departments to draw at all, administrator or not.
        self.login('08022222222')
        self.assertEqual(
            self.client.post('/api/departments/', {'name': 'THEATRE'}, format='json').status_code,
            403,
        )

        self.login('08011111111')
        self.assertEqual(
            self.client.post('/api/departments/', {'name': 'THEATRE'}, format='json').status_code,
            201,
        )
        self.assertEqual(
            self.client.post(
                '/api/units/', {'name': 'HAEMATOLOGY', 'department': department.id}, format='json',
            ).status_code,
            201,
        )

    def test_suspending_a_company_is_reported_and_reversible(self):
        self.login('08011111111')
        listed = self.client.get('/api/companies/').data['results'][0]
        self.assertTrue(listed['partnership_active'])

        self.assertEqual(
            self.client.delete(f'/api/companies/{self.supplier.id}/').status_code, 204,
        )
        listed = self.client.get('/api/companies/').data['results'][0]
        self.assertFalse(listed['partnership_active'])
        # Suspended means no new business.
        blocked = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 1}, format='json',
        )
        self.assertEqual(blocked.status_code, 400)

        response = self.client.post(f'/api/companies/{self.supplier.id}/reactivate/', format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.client.get('/api/companies/').data['results'][0]['partnership_active'])
        self.assertEqual(
            self.client.post(
                '/api/requisitions/wishlist/add/',
                {'product': self.product.id, 'qty': 1}, format='json',
            ).status_code,
            201,
        )

    def test_only_one_open_draft_per_company_and_department(self):
        self.login('08011111111')
        first = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.id}, format='json',
        )
        self.assertEqual(first.status_code, 201, first.data)

        # A second untagged draft for the same company would be a basket the
        # catalogue never reaches.
        twin = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.id}, format='json',
        )
        self.assertEqual(twin.status_code, 400)

        # A different department keeps its own, and so does a unit of it.
        department = self.client.post(
            '/api/departments/', {'name': 'THEATRE'}, format='json',
        ).data
        tagged = self.client.post(
            '/api/requisitions/',
            {'supplier': self.supplier.id, 'department': department['id']}, format='json',
        )
        self.assertEqual(tagged.status_code, 201, tagged.data)
        self.assertEqual(
            self.client.post(
                '/api/requisitions/',
                {'supplier': self.supplier.id, 'department': department['id']}, format='json',
            ).status_code,
            400,
        )

        # Catalogue additions land in one of them rather than crashing.
        added = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 5}, format='json',
        )
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual(added.data['lines'][0]['qty_requested'], 5)

        # Submitting frees the slot for the next request to that company.
        self.client.post(f'/api/requisitions/{added.data["id"]}/submit/', format='json')
        again = self.client.post(
            '/api/requisitions/',
            {
                'supplier': self.supplier.id,
                **({'department': department['id']} if added.data['department'] else {}),
            },
            format='json',
        )
        self.assertEqual(again.status_code, 201, again.data)

    def test_catalogue_add_keeps_each_unit_in_its_own_draft(self):
        self.login('08011111111')
        department = self.client.post(
            '/api/departments/', {'name': 'LABORATORY'}, format='json',
        ).data
        unit = self.client.post(
            '/api/units/', {'department': department['id'], 'name': 'HAEMATOLOGY'}, format='json',
        ).data
        untagged = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 2}, format='json',
        ).data

        tagged = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 3, 'unit': unit['id']}, format='json',
        )
        self.assertEqual(tagged.status_code, 201, tagged.data)
        # A separate basket, tagged, and the untagged one untouched.
        self.assertNotEqual(tagged.data['id'], untagged['id'])
        self.assertEqual(tagged.data['unit'], unit['id'])
        self.assertEqual(tagged.data['lines'][0]['qty_requested'], 3)

        # A second add for the same unit tops up that same draft.
        again = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 4, 'unit': unit['id']}, format='json',
        )
        self.assertEqual(again.data['id'], tagged.data['id'])
        self.assertEqual(again.data['lines'][0]['qty_requested'], 7)

    def test_items_are_added_to_a_named_draft_and_the_same_item_tops_it_up(self):
        self.login('08011111111')
        draft = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.id}, format='json',
        ).data

        added = self.client.post(
            '/api/requisition-lines/',
            {'requisition': draft['id'], 'product': self.product.id, 'qty_requested': 4},
            format='json',
        )
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual(added.data['unit_price'], '720.00')

        # The same item again is more of the one line, not a second row.
        again = self.client.post(
            '/api/requisition-lines/',
            {'requisition': draft['id'], 'product': self.product.id, 'qty_requested': 3},
            format='json',
        )
        self.assertEqual(again.status_code, 201, again.data)
        lines = self.client.get(f'/api/requisitions/{draft["id"]}/').data['lines']
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['qty_requested'], 7)

        # Once submitted the request is closed to editing.
        self.client.post(f'/api/requisitions/{draft["id"]}/submit/', format='json')
        late = self.client.post(
            '/api/requisition-lines/',
            {'requisition': draft['id'], 'product': self.product.id, 'qty_requested': 1},
            format='json',
        )
        self.assertEqual(late.status_code, 400)

    def test_half_units_travel_the_whole_chain(self):
        self.login('08011111111')
        draft = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.id}, format='json',
        ).data
        line = self.client.post(
            '/api/requisition-lines/',
            {'requisition': draft['id'], 'product': self.product.id, 'qty_requested': '2.5'},
            format='json',
        )
        self.assertEqual(line.status_code, 201, line.data)
        self.assertEqual(line.data['qty_requested'], 2.5)

        # Anything off the half step is refused, however it is written.
        for bad in ('1.3', '0.25', '0.4'):
            refused = self.client.post(
                '/api/requisition-lines/',
                {'requisition': draft['id'], 'product': self.product.id, 'qty_requested': bad},
                format='json',
            )
            self.assertEqual(refused.status_code, 400, bad)

        self.client.post(f'/api/requisitions/{draft["id"]}/submit/', format='json')
        self.login('08022222222')
        decided = self.client.post(
            f'/api/requisitions/{draft["id"]}/decide/',
            {'lines': [{'line': line.data['id'], 'qty_approved': '1.5'}]}, format='json',
        )
        self.assertEqual(decided.status_code, 200, decided.data)
        self.assertEqual(decided.data['status'], ReqStatus.PARTIALLY_APPROVED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, Decimal('1.5'))

        dispatched = self.client.post(
            f'/api/requisitions/{draft["id"]}/dispatch/',
            {'items': [{'line': line.data['id'], 'qty': '1.5'}]}, format='json',
        )
        self.assertEqual(dispatched.status_code, 201, dispatched.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, Decimal('98.5'))
        self.assertEqual(self.product.qty_reserved, Decimal('0'))

        # A half unit is accepted and a half sent back, so the invoice is for one.
        delivery_line = dispatched.data['lines'][0]
        self.login('08011111111')
        verified = self.client.post(
            f'/api/deliveries/{dispatched.data["id"]}/verify/',
            {
                'lines': [
                    {
                        'line': delivery_line['id'], 'qty_accepted': '1',
                        'qty_rejected': '0.5', 'reason': 'One pack leaking.',
                    },
                ],
            },
            format='json',
        )
        self.assertEqual(verified.status_code, 200, verified.data)
        invoice = Invoice.objects.get(delivery_id=dispatched.data['id'])
        self.assertEqual(invoice.amount, Decimal('720.00'))
        self.assertEqual(
            StockMovement.objects.get(
                organization=self.hospital, kind=StockMovement.RECEIPT,
            ).qty,
            Decimal('1'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, Decimal('99.0'))

    def test_draft_can_be_retagged_but_not_onto_an_occupied_tag(self):
        self.login('08011111111')
        department = self.client.post(
            '/api/departments/', {'name': 'LABORATORY'}, format='json',
        ).data
        unit = self.client.post(
            '/api/units/', {'department': department['id'], 'name': 'HAEMATOLOGY'}, format='json',
        ).data
        draft = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.id}, format='json',
        ).data

        moved = self.client.patch(
            f'/api/requisitions/{draft["id"]}/',
            {'department': None, 'unit': unit['id']}, format='json',
        )
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(moved.data['unit'], unit['id'])

        # The slot it left is free, and moving back onto an occupied one is refused.
        other = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.id}, format='json',
        )
        self.assertEqual(other.status_code, 201, other.data)
        clash = self.client.patch(
            f'/api/requisitions/{draft["id"]}/',
            {'department': None, 'unit': None}, format='json',
        )
        self.assertEqual(clash.status_code, 400, clash.data)

        # And no borrowing another hospital's department.
        rival = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000005',
        )
        theirs = Department.objects.create(organization=rival, name='THEATRE')
        stolen = self.client.patch(
            f'/api/requisitions/{draft["id"]}/',
            {'unit': None, 'department': theirs.id}, format='json',
        )
        self.assertEqual(stolen.status_code, 400, stolen.data)

    def test_public_hospital_signup(self):
        response = self.client.post(reverse('register'), {
            'name': 'New Hospital', 'phone': '08055555555',
            'admin_full_name': 'Chief', 'admin_phone': '08066666666',
            'password': 'Sup3rSecret!',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn('token', response.data)

    def test_every_document_prints_from_a_signed_link(self):
        invoice = self.raise_invoice(qty=10)  # 10 * 720 = 7200
        delivery = invoice.delivery
        requisition = delivery.requisition

        self.login('08011111111')
        links = {}
        for marker, path in (
            ('PURCHASE REQUEST', f'/api/requisitions/{requisition.id}/print-link/'),
            ('DELIVERY NOTE', f'/api/deliveries/{delivery.id}/print-link/'),
            ('INVOICE', f'/api/invoices/{invoice.id}/print-link/'),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.data)
            links[marker] = response.data['url']

        # The signature carries the authorisation, so the printable page itself
        # needs no token: a browser cannot send one.
        self.client.credentials()
        for marker, url in links.items():
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            body = page.content.decode()
            self.assertIn(marker, body)
            self.assertIn(self.hospital.name, body)
            self.assertIn(self.supplier.name, body)
        self.assertIn('7,200.00', self.client.get(links['INVOICE']).content.decode())
        self.assertIn(requisition.reference, self.client.get(links['DELIVERY NOTE']).content.decode())

        # A forged signature, and a good one pointed at another document.
        invoice_url = links['INVOICE']
        self.assertEqual(self.client.get(f"{invoice_url.split('?')[0]}?s=forged").status_code, 404)
        self.assertEqual(
            self.client.get(invoice_url.replace('/print/invoice/', '/print/requisition/'))
            .status_code,
            404,
        )

    def test_only_a_confirmed_payment_prints_as_a_receipt(self):
        invoice = self.raise_invoice(qty=5)
        self.login('08011111111')
        payment = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '1000.00', 'method': PaymentMethod.TRANSFER, 'payer_reference': 'TRF-7'},
            format='json',
        ).data
        link = f"/api/payments/{payment['id']}/print-link/"

        url = self.client.get(link).data['url']
        self.client.credentials()
        pending = self.client.get(url).content.decode()
        self.assertIn('PAYMENT ADVICE', pending)
        self.assertIn('NOT CONFIRMED', pending)

        self.login('08022222222')
        self.client.post(f"/api/payments/{payment['id']}/confirm/", format='json')
        url = self.client.get(link).data['url']
        self.client.credentials()
        confirmed = self.client.get(url).content.decode()
        self.assertIn('PAYMENT RECEIPT', confirmed)
        self.assertNotIn('NOT CONFIRMED', confirmed)

    def test_print_link_stops_at_the_tenant_boundary(self):
        invoice = self.raise_invoice(qty=1)
        rival = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000009',
        )
        User.objects.create_user(
            phone='08099999999', password='Sup3rSecret!', full_name='Rival Admin',
            organization=rival, role=Role.ADMIN,
        )
        self.login('08099999999')
        self.assertEqual(
            self.client.get(f'/api/invoices/{invoice.id}/print-link/').status_code, 404,
        )

    def test_a_document_also_prints_as_a_pdf(self):
        invoice = self.raise_invoice(qty=2)
        self.login('08011111111')
        url = self.client.get(f'/api/invoices/{invoice.id}/print-link/').data['url']

        self.client.credentials()
        response = self.client.get(f'{url}&format=pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn(invoice.reference, response['Content-Disposition'])

        page = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
        self.assertIn('INVOICE', page)
        self.assertIn(invoice.reference, page)

    def test_stock_ledger_prints_the_rows_the_screen_shows(self):
        for kind, qty, note in (
            (StockMovement.RECEIPT, 12, 'ward store'),
            (StockMovement.DISPENSE, -2, 'handed out'),
        ):
            StockMovement.objects.create(
                organization=self.hospital, product=self.product, kind=kind, qty=qty, note=note,
            )
        StockMovement.objects.create(
            organization=self.supplier, product=self.product,
            kind=StockMovement.ADJUST, qty=50, note='supplier only',
        )

        self.login('08011111111')
        url = self.client.get(
            '/api/stock-movements/print-link/', {'kind': StockMovement.RECEIPT},
        ).data['url']
        self.client.credentials()
        body = self.client.get(url).content.decode()

        self.assertIn('STOCK LEDGER', body)
        self.assertIn('ward store', body)
        self.assertNotIn('handed out', body)  # the filter the screen had
        self.assertNotIn('supplier only', body)  # another tenant's ledger

    def test_a_ledger_says_so_when_it_is_cut_short(self):
        StockMovement.objects.bulk_create(
            StockMovement(
                organization=self.hospital, product=self.product,
                kind=StockMovement.RECEIPT, qty=1, note=f'row {index}',
            )
            for index in range(8)
        )
        self.login('08011111111')
        url = self.client.get('/api/stock-movements/print-link/').data['url']
        self.client.credentials()
        with patch('core.printing.MAX_LIST_ROWS', 3):
            body = self.client.get(url).content.decode()
        self.assertIn('3 of 8', body)
        self.assertIn('Only the first 3 rows are printed', body)

    def test_a_long_ledger_pdf_repeats_its_header_and_numbers_its_pages(self):
        StockMovement.objects.bulk_create(
            StockMovement(
                organization=self.hospital, product=self.product,
                kind=StockMovement.RECEIPT, qty=1, note=f'row {index}',
            )
            for index in range(90)
        )
        self.login('08011111111')
        url = self.client.get('/api/stock-movements/print-link/').data['url']
        self.client.credentials()
        pdf = PdfReader(io.BytesIO(self.client.get(f'{url}&format=pdf').content))

        self.assertGreater(len(pdf.pages), 1)
        second = pdf.pages[1].extract_text()
        self.assertIn('page 2 of', second)
        self.assertIn('Movement', second)  # the table header, repeated

    def test_audit_trail_prints_for_an_administrator_only(self):
        self.raise_invoice(qty=1)
        User.objects.create_user(
            phone='08033333333', password='Sup3rSecret!', full_name='Ward Clerk',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08033333333')
        self.assertEqual(self.client.get('/api/audit-logs/print-link/').status_code, 403)

        self.login('08011111111')
        url = self.client.get('/api/audit-logs/print-link/').data['url']
        self.client.credentials()
        body = self.client.get(url).content.decode()
        self.assertIn('AUDIT TRAIL', body)
        self.assertIn('SUBMITTED', body)
        self.assertIn('Hospital Admin', body)

        # And the link goes dead if the administrator who made it is demoted
        # before it is opened.
        User.objects.filter(phone='08011111111').update(role=Role.STAFF)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_app_prints_a_pdf_over_the_ordinary_api(self):
        """No link, no browser: the token fetches the PDF and the app prints it."""
        invoice = self.raise_invoice(qty=3)
        self.login('08011111111')
        response = self.client.get(f'/api/invoices/{invoice.id}/print/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        page = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
        self.assertIn(invoice.reference, page)

        # Signed out, it is nobody's document.
        self.client.credentials()
        self.assertEqual(self.client.get(f'/api/invoices/{invoice.id}/print/').status_code, 401)

    def test_printing_a_document_stops_at_the_tenant_boundary(self):
        invoice = self.raise_invoice(qty=1)
        rival = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08000000008',
        )
        User.objects.create_user(
            phone='08088888888', password='Sup3rSecret!', full_name='Rival Admin',
            organization=rival, role=Role.ADMIN,
        )
        self.login('08088888888')
        self.assertEqual(self.client.get(f'/api/invoices/{invoice.id}/print/').status_code, 404)

    def test_printing_a_ledger_keeps_the_filters_the_screen_had(self):
        for kind, qty, note in (
            (StockMovement.RECEIPT, 12, 'ward store'),
            (StockMovement.DISPENSE, -2, 'handed out'),
        ):
            StockMovement.objects.create(
                organization=self.hospital, product=self.product, kind=kind, qty=qty, note=note,
            )
        self.login('08011111111')
        response = self.client.get('/api/stock-movements/print/', {'kind': StockMovement.RECEIPT})

        self.assertEqual(response['Content-Type'], 'application/pdf')
        page = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
        self.assertIn('ward store', page)
        self.assertNotIn('handed out', page)

    def test_printing_the_audit_trail_is_for_administrators(self):
        self.raise_invoice(qty=1)
        User.objects.create_user(
            phone='08044444444', password='Sup3rSecret!', full_name='Ward Clerk',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08044444444')
        self.assertEqual(self.client.get('/api/audit-logs/print/').status_code, 403)

        self.login('08011111111')
        self.assertEqual(self.client.get('/api/audit-logs/print/').status_code, 200)
