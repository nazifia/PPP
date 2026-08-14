"""End-to-end check of the request lifecycle and of tenant isolation."""

import io
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import (
    AuditLog,
    Delivery,
    DeliveryStatus,
    Department,
    DispensingUnit,
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
    RequisitionLine,
    ReqStatus,
    Role,
    StockMovement,
    Unit,
    User,
)


class SupplyFlowTests(APITestCase):
    def setUp(self):
        # The login throttle counts attempts in the cache, which no test client
        # resets. A test that signs four people in is not an attack, and without
        # this the count carries into the next test. LoginThrottleTests below is
        # where the rate itself is checked.
        cache.clear()
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
        # Seeded as shared rows by migration 0014, so every test starts with the
        # standard units already there.
        self.carton = DispensingUnit.objects.get(supplier=None, name='CARTON')
        self.product = Product.objects.create(
            supplier=self.supplier, generic_name='10% Dextrose Water', unit=self.carton,
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

        payment = Payment.objects.get(pk=response.data['id'])
        self.assertTrue(payment.receipt.name.startswith('receipts/'))
        # The slip is offered as a route through the API, not as a path in
        # MEDIA_ROOT: the one asks who is calling, the other does not.
        self.assertTrue(response.data['receipt'].endswith(f'/api/payments/{payment.pk}/receipt/'))

        # The hospital that filed it reads it back. Closed rather than left to
        # the garbage collector: Windows will not delete a file still held open.
        fetched = self.client.get(f'/api/payments/{payment.pk}/receipt/')
        self.addCleanup(fetched.close)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(b''.join(fetched.streaming_content), b'jpegbytes')

        # The company sees the slip it has to check.
        self.login('08022222222')
        listed = self.client.get('/api/payments/').data['results']
        self.assertEqual(len(listed), 1)
        supplier_copy = self.client.get(f'/api/payments/{payment.pk}/receipt/')
        self.addCleanup(supplier_copy.close)
        self.assertEqual(supplier_copy.status_code, 200)

        # Nobody else does, even holding the payment's id.
        rival = Organization.objects.create(
            name='Rival Clinic', kind=OrgKind.HOSPITAL, phone='08099999999',
        )
        User.objects.create_user(
            phone='08088888888', password='Sup3rSecret!', full_name='Rival Admin',
            organization=rival, role=Role.ADMIN,
        )
        self.login('08088888888')
        self.assertEqual(self.client.get(f'/api/payments/{payment.pk}/receipt/').status_code, 404)

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

    def test_supplier_defines_and_sells_in_its_own_dispensing_unit(self):
        self.login('08022222222')
        created = self.client.post('/api/dispensing-units/', {'name': 'jar'}, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        # One spelling per unit, whatever case it was typed in.
        self.assertEqual(created.data['name'], 'JAR')
        product = self.client.post(
            '/api/products/',
            {'generic_name': 'Vaseline', 'unit': created.data['id'], 'unit_price': '900.00'},
            format='json',
        )
        self.assertEqual(product.status_code, 201, product.data)
        self.assertEqual(product.data['unit_name'], 'JAR')
        # A standard unit is already on offer, so redefining it is a duplicate.
        clash = self.client.post('/api/dispensing-units/', {'name': 'CARTON'}, format='json')
        self.assertEqual(clash.status_code, 400)

    def test_one_supplier_never_sells_in_another_companys_unit(self):
        rival = Organization.objects.create(
            name='Rival Supplies', kind=OrgKind.SUPPLIER, phone='08000000009',
        )
        User.objects.create_user(
            phone='08099999999', password='Sup3rSecret!', full_name='Rival Admin',
            organization=rival, role=Role.ADMIN,
        )
        self.login('08099999999')
        theirs = self.client.post(
            '/api/dispensing-units/', {'name': 'DRUM'}, format='json',
        ).data

        self.login('08022222222')
        listed = self.client.get('/api/dispensing-units/', {'search': 'DRUM'}).data
        self.assertEqual(listed, [])
        refused = self.client.post(
            '/api/products/',
            {'generic_name': 'Spirit', 'unit': theirs['id'], 'unit_price': '100.00'},
            format='json',
        )
        self.assertEqual(refused.status_code, 400)

    def test_standard_unit_is_fixed_and_a_unit_in_use_is_not_deleted(self):
        self.login('08022222222')
        shared = self.client.patch(
            f'/api/dispensing-units/{self.carton.id}/', {'name': 'CRATE'}, format='json',
        )
        self.assertEqual(shared.status_code, 403)
        mine = self.client.post(
            '/api/dispensing-units/', {'name': 'JAR'}, format='json',
        ).data
        # Its own it may rename, which is what the More screen offers.
        renamed = self.client.patch(
            f'/api/dispensing-units/{mine["id"]}/', {'name': 'tub'}, format='json',
        )
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertEqual(renamed.data['name'], 'TUB')
        moved = self.client.patch(
            f'/api/products/{self.product.id}/', {'unit': mine['id']}, format='json',
        )
        self.assertEqual(moved.status_code, 200, moved.data)
        in_use = self.client.delete(f'/api/dispensing-units/{mine["id"]}/')
        self.assertEqual(in_use.status_code, 400)

    def test_formulation_is_picked_defined_and_still_optional(self):
        self.login('08022222222')
        standard = self.client.get('/api/formulations/', {'search': 'SYRUP'}).data
        self.assertEqual([row['name'] for row in standard], ['SYRUP'])
        mine = self.client.post('/api/formulations/', {'name': 'lozenge'}, format='json')
        self.assertEqual(mine.status_code, 201, mine.data)

        described = self.client.post(
            '/api/products/',
            {
                'generic_name': 'Strepsils', 'formulation': mine.data['id'],
                'unit': self.carton.id, 'unit_price': '450.00',
            },
            format='json',
        )
        self.assertEqual(described.status_code, 201, described.data)
        self.assertEqual(described.data['formulation_name'], 'LOZENGE')

        # An item that comes in no particular form still goes in the catalogue.
        bare = self.client.post(
            '/api/products/',
            {'generic_name': 'Sterile Water', 'unit': self.carton.id, 'unit_price': '80.00'},
            format='json',
        )
        self.assertEqual(bare.status_code, 201, bare.data)
        self.assertIsNone(bare.data['formulation'])
        self.assertEqual(bare.data['formulation_name'], '')

        # And the rules the units follow hold here too.
        self.assertEqual(
            self.client.post('/api/formulations/', {'name': 'SYRUP'}, format='json').status_code,
            400,
        )
        self.assertEqual(
            self.client.delete(f'/api/formulations/{mine.data["id"]}/').status_code, 400,
        )

    def test_stock_ledger_filters_by_product_and_kind(self):
        other = Product.objects.create(
            supplier=self.supplier, generic_name='Paracetamol',
            unit=DispensingUnit.objects.get(supplier=None, name='PACK'),
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

    def test_hospital_writes_stock_off_and_puts_a_miscount_right(self):
        StockMovement.objects.create(
            organization=self.hospital, product=self.product,
            kind=StockMovement.RECEIPT, qty=20,
        )
        self.login('08011111111')
        url = reverse('stock-movement-adjust')
        balances = reverse('stock-movement-balances')

        # Six vials found broken in the store: off the books, with a reason.
        written_off = self.client.post(
            url,
            {'product': self.product.pk, 'qty': -6, 'reason': 'Broken in the store'},
            format='json',
        )
        self.assertEqual(written_off.status_code, 201, written_off.data)
        self.assertEqual(written_off.data['qty'], -6)
        self.assertEqual(written_off.data['kind'], StockMovement.ADJUST)
        self.assertEqual(self.client.get(balances).data[0]['balance'], 14)

        # A recount the other way is the same endpoint.
        self.client.post(
            url, {'product': self.product.pk, 'qty': 2, 'reason': 'Recount'}, format='json',
        )
        self.assertEqual(self.client.get(balances).data[0]['balance'], 16)

        # Never past zero, never silent, never nothing at all. A reason of
        # nothing but spaces is trimmed to blank and refused by the field, which
        # is why the service's own check for it never has to answer here.
        for payload, expected in (
            ({'qty': -20, 'reason': 'Too much'}, 'only 16 in stock'),
            ({'qty': -1, 'reason': '  '}, 'may not be blank'),
            ({'qty': 0, 'reason': 'Nothing'}, 'changes nothing'),
        ):
            response = self.client.post(
                url, {'product': self.product.pk, **payload}, format='json',
            )
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn(expected, str(response.data))
        self.assertEqual(self.client.get(balances).data[0]['balance'], 16)

        # It is an administrator's signature, like every other correction.
        staff = User.objects.create_user(
            phone='08033333333', password='Sup3rSecret!', full_name='Ward Staff',
            organization=self.hospital, role=Role.STAFF,
        )
        self.client.force_authenticate(staff)
        self.assertEqual(
            self.client.post(
                url, {'product': self.product.pk, 'qty': -1, 'reason': 'Mine now'},
                format='json',
            ).status_code,
            403,
        )

    def test_a_batch_and_its_expiry_date_follow_the_goods_onto_the_ledger(self):
        expires = timezone.localdate() + timedelta(days=30)
        self.login('08011111111')
        requisition = self.client.post(
            '/api/requisitions/', {'supplier': self.supplier.pk}, format='json',
        ).data
        self.client.post(
            '/api/requisition-lines/',
            {'requisition': requisition['id'], 'product': self.product.pk, 'qty_requested': 10},
            format='json',
        )
        self.client.post(f"/api/requisitions/{requisition['id']}/submit/")

        self.login('08022222222')
        line = self.client.get(f"/api/requisitions/{requisition['id']}/").data['lines'][0]
        self.client.post(
            f"/api/requisitions/{requisition['id']}/decide/",
            {'lines': [{'line': line['id'], 'qty_approved': 10}]}, format='json',
        )
        delivery = self.client.post(
            f"/api/requisitions/{requisition['id']}/dispatch/",
            {
                'items': [{
                    'line': line['id'], 'qty': 10,
                    'batch_no': 'BATCH-77', 'expiry_date': expires.isoformat(),
                }],
            },
            format='json',
        ).data

        self.login('08011111111')
        self.client.post(
            f"/api/deliveries/{delivery['id']}/verify/",
            {'lines': [{'line': delivery['lines'][0]['id'], 'qty_accepted': 10}]},
            format='json',
        )

        # The receipt on the hospital's ledger carries what the crate was marked with.
        receipt = StockMovement.objects.get(
            organization=self.hospital, kind=StockMovement.RECEIPT,
        )
        self.assertEqual(receipt.batch_no, 'BATCH-77')
        self.assertEqual(receipt.expiry_date, expires)

        # And the shelf check finds it, inside the window and not outside it.
        url = reverse('stock-movement-expiring')
        soon = self.client.get(url, {'days': 90}).data
        self.assertEqual(soon['count'], 1)
        self.assertEqual(soon['results'][0]['batch_no'], 'BATCH-77')
        self.assertEqual(self.client.get(url, {'days': 7}).data['count'], 0)
        # Default window, and a nonsense one falls back to it rather than failing.
        self.assertEqual(self.client.get(url).data['count'], 1)
        self.assertEqual(self.client.get(url, {'days': 'soon'}).data['count'], 1)

    def test_low_stock_answers_to_each_items_own_reorder_level(self):
        # 100 in stock: low for the item that wants 150 on the shelf, and fine
        # for the one that wants 20, which the flat figure could never tell apart.
        self.product.reorder_level = 150
        self.product.save(update_fields=['reorder_level'])
        comfortable = Product.objects.create(
            supplier=self.supplier, generic_name='Paracetamol', unit=self.carton,
            unit_price='100.00', stock_qty=100, reorder_level=20,
        )
        # Named nothing, so it falls back to the platform figure — and 4 is under it.
        silent = Product.objects.create(
            supplier=self.supplier, generic_name='Adrenaline', unit=self.carton,
            unit_price='500.00', stock_qty=4,
        )

        self.login('08022222222')
        low = {row['generic_name'] for row in self.client.get('/api/dashboard/').data['low_stock']}
        self.assertEqual(low, {'10% Dextrose Water', 'Adrenaline'})
        self.assertNotIn(comfortable.generic_name, low)

        # Reserved stock is promised elsewhere, so it does not count as available.
        comfortable.qty_reserved = 85
        comfortable.save(update_fields=['qty_reserved'])
        self.assertIn(
            'Paracetamol',
            {row['generic_name'] for row in self.client.get('/api/dashboard/').data['low_stock']},
        )
        self.assertTrue(Product.objects.get(pk=silent.pk).is_low_stock)

    def test_the_catalogue_refuses_a_unit_from_another_department(self):
        laboratory = Department.objects.create(organization=self.hospital, name='LABORATORY')
        theatre = Department.objects.create(organization=self.hospital, name='THEATRE')
        haematology = Unit.objects.create(department=laboratory, name='HAEMATOLOGY')

        self.login('08011111111')
        clash = self.client.post(
            '/api/requisitions/wishlist/add/',
            {
                'product': self.product.pk, 'qty': 1,
                'department': theatre.pk, 'unit': haematology.pk,
            },
            format='json',
        )
        self.assertEqual(clash.status_code, 400, clash.data)
        self.assertIn('different department', str(clash.data))
        self.assertFalse(Requisition.objects.exists())

        # The pair that agrees is taken.
        agreed = self.client.post(
            '/api/requisitions/wishlist/add/',
            {
                'product': self.product.pk, 'qty': 1,
                'department': laboratory.pk, 'unit': haematology.pk,
            },
            format='json',
        )
        self.assertEqual(agreed.status_code, 201, agreed.data)

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
        # Every supplier, and only suppliers: the hospital is a tenant, not a
        # company anybody trades with.
        companies = self.client.get('/api/companies/').data['results']
        self.assertEqual([row['name'] for row in companies], [self.supplier.name])
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

    def test_an_organisation_cannot_be_left_without_an_administrator(self):
        self.login('08011111111')

        # The only administrator can neither step down nor be disabled.
        for payload in ({'role': Role.STAFF}, {'is_active': False}):
            response = self.client.patch(
                f'/api/users/{self.hospital_admin.id}/', payload, format='json',
            )
            self.assertEqual(response.status_code, 400, response.data)
        self.hospital_admin.refresh_from_db()
        self.assertEqual(self.hospital_admin.role, Role.ADMIN)
        self.assertTrue(self.hospital_admin.is_active)

        # With a second administrator in place, the first may step down.
        deputy = User.objects.create_user(
            phone='08055555555', password='Sup3rSecret!', full_name='Deputy',
            organization=self.hospital, role=Role.ADMIN,
        )
        self.assertEqual(
            self.client.patch(
                f'/api/users/{self.hospital_admin.id}/', {'role': Role.STAFF}, format='json',
            ).status_code,
            200,
        )

        # And now the deputy is the last one standing, so DELETE refuses too.
        self.login(deputy.phone)
        self.assertEqual(self.client.delete(f'/api/users/{deputy.id}/').status_code, 400)

    def test_a_disabled_account_is_enabled_again_by_an_administrator(self):
        staff = User.objects.create_user(
            phone='08066666666', password='Sup3rSecret!', full_name='Ward Clerk',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08011111111')
        self.assertEqual(self.client.delete(f'/api/users/{staff.id}/').status_code, 204)
        staff.refresh_from_db()
        self.assertFalse(staff.is_active)

        # Disabled: the account cannot log in until it is turned back on.
        self.assertEqual(
            self.client.post(
                reverse('login'), {'phone': staff.phone, 'password': 'Sup3rSecret!'},
                format='json',
            ).status_code,
            400,
        )

        self.login('08011111111')
        response = self.client.patch(
            f'/api/users/{staff.id}/', {'is_active': True}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        staff.refresh_from_db()
        self.assertTrue(staff.is_active)
        self.login(staff.phone)

    def test_staff_read_the_directory_but_cannot_change_it(self):
        staff = User.objects.create_user(
            phone='08077777777', password='Sup3rSecret!', full_name='Ward Clerk',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login(staff.phone)
        self.assertEqual(self.client.get('/api/users/').status_code, 200)
        self.assertEqual(
            self.client.patch(
                f'/api/users/{staff.id}/', {'role': Role.ADMIN}, format='json',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f'/api/users/{self.hospital_admin.id}/reset_password/',
                {'new_password': 'An0therSecret!'}, format='json',
            ).status_code,
            403,
        )

    def test_an_administrator_edits_every_detail_of_a_staff_account(self):
        staff = User.objects.create_user(
            phone='08099999999', password='Sup3rSecret!', full_name='Wrong Name',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08011111111')
        response = self.client.patch(
            f'/api/users/{staff.id}/',
            {
                'full_name': 'Right Name', 'phone': '0808 888-8888',
                'email': 'right@example.com', 'job_title': 'Ward Pharmacist',
                'role': Role.ADMIN,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        staff.refresh_from_db()
        self.assertEqual(staff.full_name, 'Right Name')
        # Written in the normalised form, so the new number logs in as typed.
        self.assertEqual(staff.phone, '08088888888')
        self.assertEqual(staff.email, 'right@example.com')
        self.assertEqual(staff.job_title, 'Ward Pharmacist')
        self.assertEqual(staff.role, Role.ADMIN)
        self.login('0808 888-8888')

        # A number another account already holds is refused, spaced or not.
        self.login('08011111111')
        response = self.client.patch(
            f'/api/users/{staff.id}/', {'phone': '0801-111 1111'}, format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        staff.refresh_from_db()
        self.assertEqual(staff.phone, '08088888888')

    def test_editing_stops_at_the_tenant_boundary(self):
        outsider = User.objects.create_user(
            phone='08012121212', password='Sup3rSecret!', full_name='Supplier Clerk',
            organization=self.supplier, role=Role.STAFF,
        )
        self.login('08011111111')
        self.assertEqual(
            self.client.patch(
                f'/api/users/{outsider.id}/', {'full_name': 'Renamed'}, format='json',
            ).status_code,
            404,
        )

    def test_a_line_cannot_be_moved_onto_another_item(self):
        """The item on a line is fixed; the quantity is not.

        Swapping it walked past the check that a line's product belongs to the
        company the request was raised against.
        """
        other_supplier = Organization.objects.create(
            name='DCL Lab Products', kind=OrgKind.SUPPLIER, phone='08000000009',
        )
        theirs = Product.objects.create(
            supplier=other_supplier, generic_name='Rapid Test Kit', unit=self.carton,
            unit_price='500.00', stock_qty=50,
        )
        mine = Product.objects.create(
            supplier=self.supplier, generic_name='Normal Saline', unit=self.carton,
            unit_price='300.00', stock_qty=50,
        )

        self.login('08011111111')
        draft = self.client.post(
            '/api/requisitions/wishlist/add/', {'product': self.product.id, 'qty': 4},
            format='json',
        ).data
        line = draft['lines'][0]['id']

        for product in (theirs.id, mine.id):
            response = self.client.patch(
                f'/api/requisition-lines/{line}/', {'product': product}, format='json',
            )
            self.assertEqual(response.status_code, 400, response.data)

        # The quantity still moves, and the item is where it was.
        self.assertEqual(
            self.client.patch(
                f'/api/requisition-lines/{line}/', {'qty_requested': 6}, format='json',
            ).status_code,
            200,
        )
        row = RequisitionLine.objects.get(pk=line)
        self.assertEqual(row.product_id, self.product.id)
        self.assertEqual(row.qty_requested, Decimal('6.0'))

    def test_stock_moves_through_restock_and_not_through_the_catalogue_form(self):
        """A direct edit would write no ledger row and skip the reserved floor."""
        self.login('08022222222')
        movements = StockMovement.objects.filter(product=self.product).count()

        refused = self.client.patch(
            f'/api/products/{self.product.id}/', {'stock_qty': 999}, format='json',
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 100)

        # Everything else on the row still edits, including sending the
        # unchanged stock figure back with it.
        edited = self.client.patch(
            f'/api/products/{self.product.id}/',
            {'brand': 'Unicare', 'stock_qty': 100}, format='json',
        )
        self.assertEqual(edited.status_code, 200, edited.data)

        # Restock is the door, and it records what it did.
        restocked = self.client.post(
            f'/api/products/{self.product.id}/restock/', {'qty': 25}, format='json',
        )
        self.assertEqual(restocked.status_code, 200, restocked.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_qty, 125)
        self.assertEqual(StockMovement.objects.filter(product=self.product).count(), movements + 1)

    def test_a_credit_note_reverses_what_was_accepted_and_found_bad(self):
        """Verification catches what is visible at the door. This catches the rest."""
        invoice = self.raise_invoice(qty=10)  # 10 * 720 = 7200
        delivery_line = invoice.delivery.lines.get()

        self.login('08011111111')
        creditable = self.client.get(f'/api/invoices/{invoice.id}/creditable/')
        self.assertEqual(creditable.status_code, 200, creditable.data)
        self.assertEqual(qty := creditable.data[0]['qty_creditable'], Decimal('10.0'))

        # A note has to say what is wrong with the goods.
        self.assertEqual(
            self.client.post(
                f'/api/invoices/{invoice.id}/credit/',
                {'lines': [{'line': delivery_line.id, 'qty': 3}]}, format='json',
            ).status_code,
            400,
        )
        # And it cannot credit more than was accepted.
        self.assertEqual(
            self.client.post(
                f'/api/invoices/{invoice.id}/credit/',
                {'lines': [{'line': delivery_line.id, 'qty': qty + 1}], 'reason': 'bad batch'},
                format='json',
            ).status_code,
            400,
        )

        raised = self.client.post(
            f'/api/invoices/{invoice.id}/credit/',
            {'lines': [{'line': delivery_line.id, 'qty': 3}], 'reason': 'counterfeit batch'},
            format='json',
        )
        self.assertEqual(raised.status_code, 201, raised.data)
        self.assertEqual(raised.data['status'], 'PENDING')
        self.assertEqual(raised.data['amount'], '2160.00')  # 3 * 720

        # Nothing moves on the hospital's word alone.
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.amount), '7200.00')
        # And the same cartons cannot be credited twice over.
        self.assertEqual(
            self.client.get(f'/api/invoices/{invoice.id}/creditable/').data[0]['qty_creditable'],
            Decimal('7.0'),
        )

        # The hospital does not accept its own note.
        self.assertEqual(
            self.client.post(
                f"/api/credits/{raised.data['id']}/confirm/", format='json',
            ).status_code,
            403,
        )

        self.login('08022222222')
        confirmed = self.client.post(
            f"/api/credits/{raised.data['id']}/confirm/", format='json',
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data['status'], 'CONFIRMED')

        # The debt falls, and the goods leave the hospital's books with it.
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.amount), '5040.00')
        self.assertEqual(str(invoice.balance), '5040.00')
        held = StockMovement.objects.filter(
            organization=self.hospital, product=self.product,
        ).aggregate(Sum('qty'))['qty__sum']
        self.assertEqual(held, Decimal('7.0'))

        # Deciding it twice changes nothing.
        self.assertEqual(
            self.client.post(
                f"/api/credits/{raised.data['id']}/confirm/", format='json',
            ).status_code,
            400,
        )

        # A refusal needs a reason, and moves nothing.
        self.login('08011111111')
        second = self.client.post(
            f'/api/invoices/{invoice.id}/credit/',
            {'lines': [{'line': delivery_line.id, 'qty': 2}], 'reason': 'short dated'},
            format='json',
        ).data
        self.login('08022222222')
        self.assertEqual(
            self.client.post(f"/api/credits/{second['id']}/reject/", format='json').status_code,
            400,
        )
        refused = self.client.post(
            f"/api/credits/{second['id']}/reject/", {'reason': 'dates were on the note'},
            format='json',
        )
        self.assertEqual(refused.status_code, 200, refused.data)
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.amount), '5040.00')
        # A refused note frees the cartons it had spoken for.
        self.login('08011111111')
        self.assertEqual(
            self.client.get(f'/api/invoices/{invoice.id}/creditable/').data[0]['qty_creditable'],
            Decimal('7.0'),
        )

    def test_a_credit_note_stops_at_what_is_still_owed(self):
        """The platform moves no money, so it cannot hand any back."""
        invoice = self.raise_invoice(qty=10)
        delivery_line = invoice.delivery.lines.get()

        self.login('08011111111')
        payment = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '6000.00', 'method': PaymentMethod.CASH}, format='json',
        ).data
        self.login('08022222222')
        self.client.post(f"/api/payments/{payment['id']}/confirm/", format='json')

        # 1200 left owing, so a 2160 credit is a refund, not a credit.
        self.login('08011111111')
        too_much = self.client.post(
            f'/api/invoices/{invoice.id}/credit/',
            {'lines': [{'line': delivery_line.id, 'qty': 3}], 'reason': 'bad batch'},
            format='json',
        )
        self.assertEqual(too_much.status_code, 400, too_much.data)
        self.assertIn('refund', str(too_much.data))

        # What is still owed can be credited, and that settles the invoice.
        raised = self.client.post(
            f'/api/invoices/{invoice.id}/credit/',
            {'lines': [{'line': delivery_line.id, 'qty': 1}], 'reason': 'one bad carton'},
            format='json',
        )
        self.assertEqual(raised.status_code, 201, raised.data)
        self.login('08022222222')
        self.client.post(f"/api/credits/{raised.data['id']}/confirm/", format='json')
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.amount), '6480.00')
        self.assertEqual(str(invoice.balance), '480.00')
        self.assertEqual(invoice.status, InvoiceStatus.PART_PAID)

    def test_health_answers_without_a_token(self):
        """Whatever watches the process holds no credentials."""
        self.client.credentials()
        response = self.client.get('/api/health/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'ok')

    def test_an_organisation_phone_is_checked_in_the_form_it_is_stored_in(self):
        """Spaced or dashed, it is the same number — and a refusal, not a 500."""
        self.login('08011111111')
        response = self.client.patch(
            # The supplier's number, written the way it would be read aloud.
            '/api/organization/', {'phone': '0800 000-0002'}, format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.hospital.refresh_from_db()
        self.assertEqual(self.hospital.phone, '08000000001')

        # Its own number back again is not a clash with itself.
        self.assertEqual(
            self.client.patch(
                '/api/organization/', {'phone': '0800-000 0001'}, format='json',
            ).status_code,
            200,
        )

    def test_a_suspended_organisation_loses_its_open_sessions(self):
        """Login refuses one; a session opened before it must not outlive it."""
        self.login('08011111111')
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

        Organization.objects.filter(pk=self.hospital.pk).update(is_active=False)
        response = self.client.get('/api/auth/me/')
        self.assertEqual(response.status_code, 401, response.data)
        self.assertFalse(Token.objects.filter(user=self.hospital_admin).exists())

    def test_a_reset_password_meets_the_same_bar_as_every_other(self):
        staff = User.objects.create_user(
            phone='08033333333', password='Sup3rSecret!', full_name='Ward Staff',
            organization=self.hospital, role=Role.STAFF,
        )
        self.login('08011111111')
        for weak in ('12345678', 'password'):
            response = self.client.post(
                f'/api/users/{staff.id}/reset_password/', {'new_password': weak}, format='json',
            )
            self.assertEqual(response.status_code, 400, response.data)
        staff.refresh_from_db()
        self.assertTrue(staff.check_password('Sup3rSecret!'))

        good = self.client.post(
            f'/api/users/{staff.id}/reset_password/',
            {'new_password': 'An0therSecret!'}, format='json',
        )
        self.assertEqual(good.status_code, 200, good.data)
        staff.refresh_from_db()
        self.assertTrue(staff.must_change_password)

    def test_a_retired_department_takes_no_new_work(self):
        """Retiring one is how an organisation closes it, so it has to bite."""
        self.login('08011111111')
        department = Department.objects.create(organization=self.hospital, name='Theatre')
        unit = Unit.objects.create(department=department, name='Recovery')

        draft = self.client.post(
            '/api/requisitions/wishlist/add/',
            {'product': self.product.id, 'qty': 2, 'department': department.id},
            format='json',
        )
        self.assertEqual(draft.status_code, 201, draft.data)

        Department.objects.filter(pk=department.pk).update(is_active=False)
        for tag in ({'department': department.id}, {'unit': unit.id}):
            # The unit is still live in its own right; its department is not,
            # and a department stands for its units.
            response = self.client.post(
                '/api/requisitions/wishlist/add/',
                {'product': self.product.id, 'qty': 1, **tag}, format='json',
            )
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn('retired', str(response.data))

        # The draft raised while it was open is still editable, tag and all.
        edited = self.client.patch(
            f"/api/requisitions/{draft.data['id']}/",
            {'note': 'still ours', 'department': department.id}, format='json',
        )
        self.assertEqual(edited.status_code, 200, edited.data)

    def test_a_retired_term_describes_no_new_item(self):
        self.login('08022222222')
        retired = DispensingUnit.objects.create(supplier=self.supplier, name='JAR')
        listed = self.client.post(
            '/api/products/',
            {'generic_name': 'Zinc Oxide', 'unit': retired.id, 'unit_price': '80.00'},
            format='json',
        )
        self.assertEqual(listed.status_code, 201, listed.data)

        DispensingUnit.objects.filter(pk=retired.pk).update(is_active=False)
        refused = self.client.post(
            '/api/products/',
            {'generic_name': 'Calamine', 'unit': retired.id, 'unit_price': '90.00'},
            format='json',
        )
        self.assertEqual(refused.status_code, 400, refused.data)

        # The item already described by it is not stranded: its price still edits.
        priced = self.client.patch(
            f"/api/products/{listed.data['id']}/",
            {'unit_price': '95.00', 'unit': retired.id}, format='json',
        )
        self.assertEqual(priced.status_code, 200, priced.data)

    def test_an_account_cannot_close_itself(self):
        """/users/ refuses to close the last administrator; this is the way round."""
        self.login('08011111111')
        response = self.client.patch('/api/auth/me/', {'is_active': False}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.hospital_admin.refresh_from_db()
        self.assertTrue(self.hospital_admin.is_active)

    def test_a_suspended_link_stops_the_approval_too(self):
        requisition, line = self.submit_request('08011111111', 5)
        Partnership.objects.filter(
            hospital=self.hospital, supplier=self.supplier,
        ).update(is_active=False)

        self.login('08022222222')
        response = self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 5}]}, format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.qty_reserved, 0)  # nothing promised

    def test_adjust_cannot_invent_stock_for_an_item_never_received(self):
        """An adjustment corrects a ledger. It does not start one."""
        self.login('08011111111')
        response = self.client.post(
            '/api/stock-movements/adjust/',
            {'product': self.product.id, 'qty': 50, 'reason': 'found some'}, format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            StockMovement.objects.filter(organization=self.hospital).count(), 0,
        )

        # Once a delivery has actually landed, the same correction goes through.
        self.raise_invoice(qty=10)
        self.login('08011111111')
        corrected = self.client.post(
            '/api/stock-movements/adjust/',
            {'product': self.product.id, 'qty': -2, 'reason': 'broken in the store'},
            format='json',
        )
        self.assertEqual(corrected.status_code, 201, corrected.data)

    def test_the_same_item_is_not_listed_twice(self):
        self.login('08022222222')
        item = {
            'generic_name': 'Amoxicillin', 'brand': 'Amoxil', 'strength': '500mg',
            'unit': self.carton.id, 'unit_price': '1200.00',
        }
        self.assertEqual(self.client.post('/api/products/', item, format='json').status_code, 201)

        again = self.client.post('/api/products/', item, format='json')
        self.assertEqual(again.status_code, 400, again.data)

        # A different strength is a different item, and so is another company's.
        self.assertEqual(
            self.client.post(
                '/api/products/', {**item, 'strength': '250mg'}, format='json',
            ).status_code,
            201,
        )

        # Renaming one onto another is the same clash by another road.
        rows = self.client.get('/api/products/', {'search': 'Amoxicillin'}).data['results']
        moved = self.client.patch(
            f"/api/products/{rows[0]['id']}/", {'strength': rows[1]['strength']}, format='json',
        )
        self.assertEqual(moved.status_code, 400, moved.data)

    def test_stale_deliveries_names_what_nobody_verified(self):
        requisition, line = self.submit_request('08011111111', 6)
        self.login('08022222222')
        self.client.post(
            f'/api/requisitions/{requisition}/decide/',
            {'lines': [{'line': line, 'qty_approved': 6}]}, format='json',
        )
        delivery = self.client.post(
            f'/api/requisitions/{requisition}/dispatch/',
            {'items': [{'line': line, 'qty': 6}]}, format='json',
        ).data

        out = StringIO()
        call_command('stale_deliveries', '--days', '7', stdout=out)
        self.assertIn('0 consignment(s)', out.getvalue())

        # Age it past the window: it is named, and nothing is decided for anyone.
        Delivery.objects.filter(pk=delivery['id']).update(
            dispatched_at=timezone.now() - timedelta(days=9),
        )
        out = StringIO()
        call_command('stale_deliveries', '--days', '7', stdout=out)
        self.assertIn(delivery['reference'], out.getvalue())
        self.assertIn('1 consignment(s)', out.getvalue())
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(
            Delivery.objects.get(pk=delivery['id']).status, DeliveryStatus.IN_TRANSIT,
        )

    def test_an_item_added_with_stock_on_it_opens_the_ledger(self):
        """Otherwise the ledger starts short of the shelf and never catches up."""
        self.login('08022222222')
        created = self.client.post(
            '/api/products/',
            {
                'generic_name': 'Paracetamol', 'strength': '500mg',
                'unit': self.carton.id, 'unit_price': '150.00', 'stock_qty': '40',
            },
            format='json',
        )
        self.assertEqual(created.status_code, 201, created.data)
        opening = StockMovement.objects.get(product_id=created.data['id'])
        self.assertEqual(opening.qty, Decimal('40.0'))
        self.assertEqual(opening.kind, StockMovement.ADJUST)
        self.assertEqual(opening.organization_id, self.supplier.id)

        # The ledger and the shelf now say the same thing, and go on doing so.
        self.client.post(f"/api/products/{created.data['id']}/restock/", {'qty': 10}, format='json')
        product = Product.objects.get(pk=created.data['id'])
        self.assertEqual(product.stock_qty, Decimal('50.0'))
        self.assertEqual(
            StockMovement.objects.filter(product=product).aggregate(Sum('qty'))['qty__sum'],
            product.stock_qty,
        )

        # An item added with nothing on it opens no row.
        empty = self.client.post(
            '/api/products/',
            {'generic_name': 'Ibuprofen', 'unit': self.carton.id, 'unit_price': '90.00'},
            format='json',
        )
        self.assertEqual(empty.status_code, 201, empty.data)
        self.assertFalse(StockMovement.objects.filter(product_id=empty.data['id']).exists())

    def test_a_hospital_links_a_company_another_hospital_registered(self):
        """A supplier already on the platform is not the first hospital's alone."""
        second = Organization.objects.create(
            name='Cottage Hospital', kind=OrgKind.HOSPITAL, phone='08000000008',
        )
        their_admin = User.objects.create_user(
            phone='08055555555', password='Sup3rSecret!', full_name='Cottage Admin',
            organization=second, role=Role.ADMIN,
        )

        self.login(their_admin.phone)
        # Nothing to trade with, so nothing to read.
        self.assertEqual(self.client.get('/api/companies/').data['results'], [])
        self.assertEqual(self.client.get('/api/products/').data['results'], [])

        # Registering it again is refused — the phone number is taken — which is
        # the whole reason the link exists.
        taken = self.client.post(
            '/api/companies/',
            {
                'name': 'Valour Pharmaceuticals', 'phone': self.supplier.phone,
                'contact_full_name': 'Someone', 'contact_phone': '08066666666',
                'password': 'An0therSecret!',
            },
            format='json',
        )
        self.assertEqual(taken.status_code, 400, taken.data)

        self.assertEqual(
            self.client.post('/api/companies/link/', {'phone': '0800-000 0000'}, format='json')
            .status_code,
            400,
        )
        linked = self.client.post(
            # Spaced and dashed, the way it would be read off a card.
            '/api/companies/link/', {'phone': '0800 000-0002'}, format='json',
        )
        self.assertEqual(linked.status_code, 201, linked.data)
        self.assertEqual(linked.data['id'], self.supplier.id)
        self.assertTrue(linked.data['partnership_active'])

        # The catalogue is now readable, and no second account was created.
        self.assertEqual(len(self.client.get('/api/products/').data['results']), 1)
        self.assertEqual(User.objects.filter(organization=second).count(), 1)
        self.assertEqual(
            Partnership.objects.filter(supplier=self.supplier, is_active=True).count(), 2,
        )

        # Twice is a mistake, not a second link.
        self.assertEqual(
            self.client.post(
                '/api/companies/link/', {'phone': self.supplier.phone}, format='json',
            ).status_code,
            400,
        )

        # Suspending and linking again reopens the same row rather than adding one.
        self.client.delete(f'/api/companies/{self.supplier.id}/')
        self.assertEqual(
            self.client.post(
                '/api/companies/link/', {'phone': self.supplier.phone}, format='json',
            ).status_code,
            201,
        )
        self.assertEqual(
            Partnership.objects.filter(hospital=second, supplier=self.supplier).count(), 1,
        )

        # Ordinary staff do not redraw who the hospital trades with.
        staff = User.objects.create_user(
            phone='08077777777', password='Sup3rSecret!', full_name='Cottage Staff',
            organization=second, role=Role.STAFF,
        )
        self.login(staff.phone)
        self.assertEqual(
            self.client.post(
                '/api/companies/link/', {'phone': self.supplier.phone}, format='json',
            ).status_code,
            403,
        )

    def test_a_hospital_withdraws_a_payment_the_supplier_has_not_decided(self):
        """A keying mistake is the hospital's to take back, not the supplier's."""
        invoice = self.raise_invoice(qty=10)  # 7200.00
        self.login('08011111111')
        wrong = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '7200.00', 'method': PaymentMethod.TRANSFER, 'payer_reference': 'TRF-9'},
            format='json',
        ).data

        # While it stands, it holds down what else may be recorded.
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.amount_unclaimed), '0.00')

        # The supplier does not withdraw the hospital's entry; it rejects it.
        self.login('08022222222')
        self.assertEqual(
            self.client.post(f"/api/payments/{wrong['id']}/withdraw/", format='json').status_code,
            403,
        )

        self.login('08011111111')
        withdrawn = self.client.post(
            f"/api/payments/{wrong['id']}/withdraw/", {'reason': 'wrong invoice'}, format='json',
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.data)
        self.assertEqual(withdrawn.data['status'], PaymentStatus.WITHDRAWN)

        # No money moved, the invoice is free again, and the slip's reference
        # is free with it.
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.UNPAID)
        self.assertEqual(str(invoice.amount_paid), '0.00')
        self.assertEqual(str(invoice.amount_pending), '0.00')
        self.assertEqual(str(invoice.amount_unclaimed), '7200.00')
        again = self.client.post(
            f'/api/invoices/{invoice.id}/pay/',
            {'amount': '7200.00', 'method': PaymentMethod.TRANSFER, 'payer_reference': 'TRF-9'},
            format='json',
        )
        self.assertEqual(again.status_code, 201, again.data)

        # Withdrawing it twice, or withdrawing one already decided, is refused.
        self.assertEqual(
            self.client.post(f"/api/payments/{wrong['id']}/withdraw/", format='json').status_code,
            400,
        )
        self.login('08022222222')
        self.client.post(f"/api/payments/{again.data['id']}/confirm/", format='json')
        self.login('08011111111')
        self.assertEqual(
            self.client.post(
                f"/api/payments/{again.data['id']}/withdraw/", format='json',
            ).status_code,
            400,
        )


class ListQueryCountTests(APITestCase):
    """An index page must not ask one more question per row it shows.

    requested_value, approved_value and item_count each walk a requisition's
    lines, and amount_pending walks an invoice's payments — so without the
    prefetches in the viewsets these lists cost a query per row and nobody
    notices until a real tenant has a few hundred of them.
    """

    def setUp(self):
        cache.clear()
        self.hospital = Organization.objects.create(
            name='General Hospital', kind=OrgKind.HOSPITAL, phone='08000000001',
        )
        self.supplier = Organization.objects.create(
            name='Valour Pharmaceuticals', kind=OrgKind.SUPPLIER, phone='08000000002',
        )
        Partnership.objects.create(hospital=self.hospital, supplier=self.supplier)
        self.admin = User.objects.create_user(
            phone='08011111111', password='Sup3rSecret!', full_name='Hospital Admin',
            organization=self.hospital, role=Role.ADMIN,
        )
        self.product = Product.objects.create(
            supplier=self.supplier, generic_name='10% Dextrose Water',
            unit=DispensingUnit.objects.get(supplier=None, name='CARTON'),
            unit_price='720.00', stock_qty=1000,
        )
        self.client.force_authenticate(self.admin)

    def make_requisitions(self, count):
        for _ in range(count):
            requisition = Requisition.objects.create(
                hospital=self.hospital, supplier=self.supplier, status=ReqStatus.SUBMITTED,
            )
            requisition.lines.create(
                product=self.product, qty_requested=2, unit_price='720.00',
            )

    def make_invoices(self, count):
        for _ in range(count):
            requisition = Requisition.objects.create(
                hospital=self.hospital, supplier=self.supplier, status=ReqStatus.DELIVERED,
            )
            invoice = Invoice.objects.create(
                delivery=Delivery.objects.create(requisition=requisition),
                hospital=self.hospital, supplier=self.supplier, amount='720.00',
            )
            Payment.objects.create(invoice=invoice, amount='100.00', method=PaymentMethod.CASH)

    def assert_flat(self, url, add_rows):
        """One row and ten rows must cost the same number of queries."""
        add_rows(1)
        with CaptureQueriesContext(connection) as first:
            self.client.get(url)
        add_rows(9)
        with self.assertNumQueries(len(first)):
            response = self.client.get(url)
        self.assertEqual(len(response.data['results']), 10)

    def test_the_requisition_list_costs_the_same_for_one_row_or_many(self):
        self.assert_flat('/api/requisitions/', self.make_requisitions)

    def test_the_invoice_list_costs_the_same_for_one_row_or_many(self):
        self.assert_flat('/api/invoices/', self.make_invoices)


class LoginThrottleTests(APITestCase):
    """Guessing at a phone number and a password is rationed."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User.objects.create_user(
            phone='08011111111', password='Sup3rSecret!', full_name='Hospital Admin',
        )

    def attempt(self, password):
        return self.client.post(
            reverse('login'), {'phone': '08011111111', 'password': password}, format='json',
        ).status_code

    def test_a_run_of_guesses_is_cut_off(self):
        # Ten a minute: wrong ones are answered 400 and still counted, or a
        # guesser would get an unlimited run of them.
        for _ in range(10):
            self.assertEqual(self.attempt('wrong'), 400)
        self.assertEqual(self.attempt('wrong'), 429)
        # The right password is refused too — the door is shut, not the guess.
        self.assertEqual(self.attempt('Sup3rSecret!'), 429)

    def test_signing_in_normally_is_never_throttled(self):
        for _ in range(5):
            self.assertEqual(self.attempt('Sup3rSecret!'), 200)


class RuntimeModeTests(TestCase):
    """The development/production switch on the admin site."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / '.django_env'
        self.superuser = User.objects.create_superuser(
            phone='08099999999', password='Sup3rSecret!',
        )

    def switch(self, mode):
        with override_settings(ENV_FILE=self.env_file):
            return self.client.post('/admin/env/', {'mode': mode}, follow=True)

    def saved(self):
        return self.env_file.read_text() if self.env_file.exists() else None

    def test_only_a_superuser_reaches_the_page(self):
        # Signed out: the admin login stands in the way.
        self.assertEqual(self.client.get('/admin/env/').status_code, 302)

        staff = User.objects.create_user(
            phone='08088888888', password='Sup3rSecret!', full_name='Desk Staff',
            is_staff=True,
        )
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/admin/env/').status_code, 403)

        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get('/admin/env/').status_code, 200)
        # And the admin header carries the way in.
        self.assertContains(self.client.get('/admin/'), '/admin/env/')

    @patch.dict(os.environ, {'DJANGO_SECRET_KEY': '', 'DJANGO_ALLOWED_HOSTS': ''})
    def test_production_is_refused_until_the_environment_is_ready(self):
        os.environ.pop('DJANGO_ENV', None)
        self.client.force_login(self.superuser)

        self.switch('prod')
        # Nothing written: the next start would have died on the missing secret.
        self.assertIsNone(self.saved())

        os.environ['DJANGO_SECRET_KEY'] = 'a-real-one'
        os.environ['DJANGO_ALLOWED_HOSTS'] = 'example.com'
        self.switch('prod')
        self.assertEqual(self.saved(), 'prod')

        self.switch('dev')
        self.assertEqual(self.saved(), 'dev')

    @patch.dict(os.environ, {'DJANGO_ENV': 'dev'})
    def test_the_environment_wins_over_the_page(self):
        self.client.force_login(self.superuser)
        self.switch('prod')
        self.assertIsNone(self.saved())


class UnitTransferTests(APITestCase):
    """One unit borrowing from another in its own department.

    Nothing is bought here, so there is no invoice to check: what matters is
    that the goods leave one shelf and land on the other, that the hospital's
    own total does not move, and that each of the four steps is signed by
    somebody who was entitled to take it.
    """

    def setUp(self):
        cache.clear()
        self.hospital = Organization.objects.create(
            name='General Hospital', kind=OrgKind.HOSPITAL, phone='08000000001',
        )
        self.admin = User.objects.create_user(
            phone='08011111111', password='Sup3rSecret!', full_name='Hospital Admin',
            organization=self.hospital, role=Role.ADMIN,
        )
        self.supplier = Organization.objects.create(
            name='Valour Pharmaceuticals', kind=OrgKind.SUPPLIER, phone='08000000002',
        )
        Partnership.objects.create(hospital=self.hospital, supplier=self.supplier)
        self.product = Product.objects.create(
            supplier=self.supplier, generic_name='10% Dextrose Water',
            unit=DispensingUnit.objects.get(supplier=None, name='CARTON'),
            unit_price='720.00', stock_qty=100,
        )
        self.laboratory = Department.objects.create(
            organization=self.hospital, name='LABORATORY',
        )
        self.haematology = Unit.objects.create(
            department=self.laboratory, name='HAEMATOLOGY',
        )
        self.chemistry = Unit.objects.create(department=self.laboratory, name='CHEMISTRY')
        self.pharmacy = Department.objects.create(organization=self.hospital, name='PHARMACY')
        self.dispensary = Unit.objects.create(department=self.pharmacy, name='DISPENSARY')

        self.holder = User.objects.create_user(
            phone='08033333333', password='Sup3rSecret!', full_name='Haematology Staff',
            organization=self.hospital, role=Role.STAFF, unit=self.haematology,
        )
        self.asker = User.objects.create_user(
            phone='08055555555', password='Sup3rSecret!', full_name='Chemistry Staff',
            organization=self.hospital, role=Role.STAFF, unit=self.chemistry,
        )
        # Somebody nobody has placed on a unit yet.
        self.unplaced = User.objects.create_user(
            phone='08066666666', password='Sup3rSecret!', full_name='Ward Staff',
            organization=self.hospital, role=Role.STAFF,
        )

    def login(self, user):
        phone = getattr(user, 'phone', user)
        response = self.client.post(
            reverse('login'), {'phone': phone, 'password': 'Sup3rSecret!'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + response.data['token'])
        return response.data

    def stock(self, unit, qty):
        """Put stock on a unit's shelf the way a verified delivery would."""
        return StockMovement.objects.create(
            organization=self.hospital, product=self.product, unit=unit,
            kind=StockMovement.RECEIPT, qty=qty, note='Opening',
        )

    def balance(self, unit=None):
        query = f'?unit={unit.id}' if unit is not None else ''
        rows = self.client.get(f'/api/stock-movements/balances/{query}').data
        return next((row['balance'] for row in rows if row['product'] == self.product.id), None)

    def ask(self, qty=8, from_unit=None, to_unit=None):
        return self.client.post('/api/transfers/', {
            'from_unit': (from_unit or self.haematology).id,
            'to_unit': (to_unit or self.chemistry).id,
            'items': [{'product': self.product.id, 'qty': qty}],
            'note': 'Run out before the round',
        }, format='json')

    def step(self, transfer_id, name, payload):
        return self.client.post(f'/api/transfers/{transfer_id}/{name}/', payload, format='json')

    def requested(self, qty=8):
        """A transfer sitting at REQUESTED, and the id of its only line."""
        self.login(self.asker)
        asked = self.ask(qty)
        self.assertEqual(asked.status_code, 201, asked.data)
        return asked.data['id'], asked.data['lines'][0]['id']

    def test_the_four_steps_move_stock_and_each_one_is_signed(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested(8)

        # Asking alone moves nothing.
        self.assertEqual(self.balance(self.haematology), Decimal('20.0'))

        self.login(self.holder)
        approved = self.step(transfer_id, 'approve', {
            'items': [{'line': line_id, 'qty_approved': 6}], 'note': 'Six we can spare',
        })
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data['status'], 'APPROVED')
        self.assertEqual(approved.data['decided_by_name'], 'Haematology Staff')
        self.assertEqual(Decimal(str(approved.data['lines'][0]['qty_approved'])), Decimal('6.0'))
        # Agreement is not delivery: the shelf has not moved yet.
        self.assertEqual(self.balance(self.haematology), Decimal('20.0'))

        issued = self.step(transfer_id, 'issue', {
            'items': [{'line': line_id, 'qty': 5}], 'note': 'Five counted out',
        })
        self.assertEqual(issued.status_code, 200, issued.data)
        self.assertEqual(issued.data['status'], 'ISSUED')
        self.assertEqual(Decimal(str(issued.data['lines'][0]['qty_issued'])), Decimal('5.0'))
        self.assertEqual(self.balance(self.haematology), Decimal('15.0'))

        # An empty confirmation is the ordinary one: all of it turned up.
        self.login(self.asker)
        received = self.step(transfer_id, 'receive', {})
        self.assertEqual(received.status_code, 200, received.data)
        self.assertEqual(received.data['status'], 'RECEIVED')
        self.assertEqual(received.data['received_by_name'], 'Chemistry Staff')
        self.assertEqual(Decimal(str(received.data['lines'][0]['qty_received'])), Decimal('5.0'))

        self.assertEqual(self.balance(self.haematology), Decimal('15.0'))
        self.assertEqual(self.balance(self.chemistry), Decimal('5.0'))
        # The hospital still holds every carton it held before.
        self.assertEqual(self.balance(), Decimal('20.0'))

        moves = StockMovement.objects.filter(kind=StockMovement.TRANSFER)
        self.assertEqual(moves.count(), 2)
        self.assertEqual(moves.aggregate(total=Sum('qty'))['total'], Decimal('0'))
        self.assertEqual(
            set(AuditLog.objects.filter(entity='Transfer').values_list('action', flat=True)),
            {'REQUESTED', 'APPROVED', 'ISSUED', 'RECEIVED'},
        )

    def test_what_never_arrived_is_written_off_the_asking_units_shelf(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested(8)
        self.login(self.holder)
        self.step(transfer_id, 'approve', {'items': [{'line': line_id, 'qty_approved': 5}]})
        self.step(transfer_id, 'issue', {'items': [{'line': line_id, 'qty': 5}]})

        self.login(self.asker)
        # A short count has to say what happened to the rest.
        silent = self.step(transfer_id, 'receive', {
            'items': [{'line': line_id, 'qty_received': 3}],
        })
        self.assertEqual(silent.status_code, 400)
        # Nor can more arrive than was ever sent.
        self.assertEqual(
            self.step(transfer_id, 'receive', {
                'items': [{'line': line_id, 'qty_received': 9, 'reason': 'found extra'}],
            }).status_code,
            400,
        )

        received = self.step(transfer_id, 'receive', {
            'items': [{
                'line': line_id, 'qty_received': 3, 'reason': 'two cartons never came over',
            }],
        })
        self.assertEqual(received.status_code, 200, received.data)
        self.assertEqual(
            received.data['lines'][0]['shortfall_reason'], 'two cartons never came over',
        )

        self.assertEqual(self.balance(self.haematology), Decimal('15.0'))
        self.assertEqual(self.balance(self.chemistry), Decimal('3.0'))
        # The two that went missing are off the hospital's books, not sitting on
        # a shelf nobody can find them on.
        self.assertEqual(self.balance(), Decimal('18.0'))
        written_off = StockMovement.objects.get(kind=StockMovement.ADJUST)
        self.assertEqual(written_off.qty, Decimal('-2.0'))
        self.assertEqual(written_off.unit_id, self.chemistry.pk)

    def test_each_unit_answers_for_its_own_half_of_the_trade(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested(8)

        # The unit being asked does not get to raise the request for the other.
        self.login(self.holder)
        self.assertEqual(self.ask().status_code, 403)
        # Nor does anybody nobody has placed on a unit.
        self.login(self.unplaced)
        self.assertEqual(self.ask().status_code, 403)
        self.assertEqual(
            self.step(transfer_id, 'approve', {
                'items': [{'line': line_id, 'qty_approved': 1}],
            }).status_code,
            403,
        )

        # The asking unit cannot agree to the other's stock, or hand it over.
        self.login(self.asker)
        for name, payload in (
            ('approve', {'items': [{'line': line_id, 'qty_approved': 6}]}),
            ('reject', {'reason': 'no'}),
        ):
            self.assertEqual(self.step(transfer_id, name, payload).status_code, 403)

        self.login(self.holder)
        self.step(transfer_id, 'approve', {'items': [{'line': line_id, 'qty_approved': 6}]})
        self.login(self.asker)
        self.assertEqual(
            self.step(transfer_id, 'issue', {
                'items': [{'line': line_id, 'qty': 6}],
            }).status_code,
            403,
        )
        # And the holding unit does not sign for what the other received.
        self.login(self.holder)
        self.step(transfer_id, 'issue', {'items': [{'line': line_id, 'qty': 6}]})
        self.assertEqual(self.step(transfer_id, 'receive', {}).status_code, 403)

        # An administrator stands in for either side.
        self.login(self.admin)
        self.assertEqual(self.step(transfer_id, 'receive', {}).status_code, 200)

    def test_the_steps_are_taken_in_order_and_only_once(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested(8)

        self.login(self.holder)
        # Nothing is issued before it is agreed, and nothing is received before
        # it is issued.
        self.assertEqual(
            self.step(transfer_id, 'issue', {
                'items': [{'line': line_id, 'qty': 1}],
            }).status_code,
            400,
        )
        self.step(transfer_id, 'approve', {'items': [{'line': line_id, 'qty_approved': 6}]})
        self.login(self.asker)
        self.assertEqual(self.step(transfer_id, 'receive', {}).status_code, 400)

        # Agreeing twice, and agreeing to nothing at all, are both refused.
        self.login(self.holder)
        self.assertEqual(
            self.step(transfer_id, 'approve', {
                'items': [{'line': line_id, 'qty_approved': 2}],
            }).status_code,
            400,
        )
        fresh_id, fresh_line = self.requested(4)
        self.login(self.holder)
        self.assertEqual(
            self.step(fresh_id, 'approve', {
                'items': [{'line': fresh_line, 'qty_approved': 0}],
            }).status_code,
            400,
        )
        self.assertEqual(
            self.step(fresh_id, 'approve', {
                'items': [{'line': fresh_line, 'qty_approved': 9}],
            }).status_code,
            400,
        )

        # An agreed transfer may still be withdrawn; an issued one may not.
        self.login(self.asker)
        cancelled = self.step(transfer_id, 'cancel', {'reason': 'Found some in the cupboard'})
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data['status'], 'CANCELLED')

        self.login(self.holder)
        self.step(fresh_id, 'approve', {'items': [{'line': fresh_line, 'qty_approved': 4}]})
        self.step(fresh_id, 'issue', {'items': [{'line': fresh_line, 'qty': 4}]})
        self.login(self.asker)
        self.assertEqual(self.step(fresh_id, 'cancel', {'reason': 'too late'}).status_code, 400)
        self.assertEqual(self.balance(self.haematology), Decimal('16.0'))

    def test_only_units_of_one_department_trade_with_each_other(self):
        self.stock(self.haematology, 20)
        self.login(self.asker)

        # Another department, itself, and a retired unit are all refused.
        self.assertEqual(self.ask(to_unit=self.dispensary).status_code, 400)
        self.assertEqual(self.ask(to_unit=self.haematology).status_code, 400)
        Unit.objects.filter(pk=self.chemistry.pk).update(is_active=False)
        self.assertEqual(self.ask().status_code, 400)
        Unit.objects.filter(pk=self.chemistry.pk).update(is_active=True)
        self.assertEqual(self.ask().status_code, 201)

    def test_a_unit_cannot_issue_more_than_it_holds(self):
        self.stock(self.haematology, 3)
        # The hospital holds plenty; the shelf being asked does not.
        self.stock(self.dispensary, 500)
        transfer_id, line_id = self.requested(5)

        self.login(self.holder)
        self.step(transfer_id, 'approve', {'items': [{'line': line_id, 'qty_approved': 5}]})
        refused = self.step(transfer_id, 'issue', {'items': [{'line': line_id, 'qty': 5}]})
        self.assertEqual(refused.status_code, 400)
        self.assertIn('holds only 3', str(refused.data))

        # More than was agreed, and nothing at all, are both refused.
        for qty in (6, 0):
            self.assertEqual(
                self.step(transfer_id, 'issue', {
                    'items': [{'line': line_id, 'qty': qty}],
                }).status_code,
                400,
            )
        self.assertEqual(self.balance(self.haematology), Decimal('3.0'))

    def test_a_request_can_be_refused_and_nothing_moves(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested()

        self.login(self.holder)
        # A refusal has to say why.
        self.assertEqual(self.step(transfer_id, 'reject', {}).status_code, 400)
        rejected = self.step(transfer_id, 'reject', {'reason': 'We are short ourselves'})
        self.assertEqual(rejected.status_code, 200, rejected.data)
        self.assertEqual(rejected.data['status'], 'REJECTED')
        self.assertEqual(rejected.data['decision_note'], 'We are short ourselves')
        self.assertEqual(
            self.step(transfer_id, 'approve', {
                'items': [{'line': line_id, 'qty_approved': 1}],
            }).status_code,
            400,
        )

        self.assertEqual(self.balance(self.haematology), Decimal('20.0'))
        self.assertFalse(StockMovement.objects.filter(kind=StockMovement.TRANSFER).exists())

        # It stays on the list, which is the record of what was asked.
        self.assertEqual(self.client.get('/api/transfers/pending/').data['count'], 0)
        self.assertEqual(self.client.get('/api/transfers/').data['count'], 1)

    def test_a_transfer_belongs_to_its_own_hospital(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested()

        other = Organization.objects.create(
            name='Cottage Hospital', kind=OrgKind.HOSPITAL, phone='08000000009',
        )
        User.objects.create_user(
            phone='08044444444', password='Sup3rSecret!', full_name='Other Admin',
            organization=other, role=Role.ADMIN,
        )
        self.login('08044444444')
        self.assertEqual(self.client.get('/api/transfers/').data['count'], 0)
        self.assertEqual(
            self.step(transfer_id, 'approve', {
                'items': [{'line': line_id, 'qty_approved': 1}],
            }).status_code,
            404,
        )
        # Nor may it name another hospital's units on one of its own.
        self.assertEqual(self.ask().status_code, 403)

    def test_a_member_of_staff_cannot_move_themselves_to_another_unit(self):
        self.login(self.asker)
        response = self.client.patch(
            '/api/auth/me/', {'unit': self.haematology.id, 'job_title': 'Chief'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['job_title'], 'Chief')
        self.asker.refresh_from_db()
        self.assertEqual(self.asker.unit_id, self.chemistry.pk)

        # The administrator places people, and only inside its own hospital.
        self.login(self.admin)
        moved = self.client.patch(
            f'/api/users/{self.asker.pk}/', {'unit': self.haematology.id}, format='json',
        )
        self.assertEqual(moved.status_code, 200, moved.data)
        self.asker.refresh_from_db()
        self.assertEqual(self.asker.unit_id, self.haematology.pk)

        other = Organization.objects.create(
            name='Cottage Hospital', kind=OrgKind.HOSPITAL, phone='08000000009',
        )
        elsewhere = Unit.objects.create(
            department=Department.objects.create(organization=other, name='THEATRE'),
            name='RECOVERY',
        )
        self.assertEqual(
            self.client.patch(
                f'/api/users/{self.asker.pk}/', {'unit': elsewhere.id}, format='json',
            ).status_code,
            400,
        )

    def test_goods_land_on_the_shelf_of_the_unit_that_asked_for_them(self):
        self.login(self.admin)
        asked = self.client.post('/api/requisitions/wishlist/add/', {
            'product': self.product.id, 'qty': 10,
            'department': self.laboratory.id, 'unit': self.haematology.id,
        }, format='json')
        self.assertEqual(asked.status_code, 201, asked.data)
        requisition_id, line_id = asked.data['id'], asked.data['lines'][0]['id']
        self.client.post(f'/api/requisitions/{requisition_id}/submit/', format='json')

        supplier_admin = User.objects.create_user(
            phone='08022222222', password='Sup3rSecret!', full_name='Supplier Admin',
            organization=self.supplier, role=Role.ADMIN,
        )
        self.login(supplier_admin)
        self.client.post(
            f'/api/requisitions/{requisition_id}/decide/',
            {'lines': [{'line': line_id, 'qty_approved': 10}]}, format='json',
        )
        dispatched = self.client.post(
            f'/api/requisitions/{requisition_id}/dispatch/',
            {'items': [{'line': line_id, 'qty': 10, 'batch_no': 'B-1'}]}, format='json',
        )
        self.login(self.admin)
        verified = self.client.post(
            f"/api/deliveries/{dispatched.data['id']}/verify/",
            {'lines': [{'line': dispatched.data['lines'][0]['id'], 'qty_accepted': 10}]},
            format='json',
        )
        self.assertEqual(verified.status_code, 200, verified.data)

        receipt = StockMovement.objects.get(kind=StockMovement.RECEIPT)
        self.assertEqual(receipt.unit_id, self.haematology.pk)
        self.assertEqual(self.balance(self.haematology), Decimal('10.0'))

        # A dispense off that shelf comes off it, not off the hospital at large.
        self.client.post('/api/stock-movements/dispense/', {
            'product': self.product.id, 'qty': 4, 'unit': self.haematology.id,
        }, format='json')
        self.assertEqual(self.balance(self.haematology), Decimal('6.0'))
        self.assertEqual(
            self.client.post('/api/stock-movements/dispense/', {
                'product': self.product.id, 'qty': 99, 'unit': self.haematology.id,
            }, format='json').status_code,
            400,
        )

    def test_a_unit_holding_stock_cannot_be_deleted_out_from_under_it(self):
        self.stock(self.haematology, 20)
        self.login(self.admin.phone)

        # Its shelf would land in the organisation's store with nothing to say
        # it ever moved, so the row is kept and retiring is offered instead.
        refused = self.client.delete(f'/api/units/{self.haematology.pk}/')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('Retire it instead', str(refused.data))
        # And the department cannot take it down the back way either.
        self.assertEqual(
            self.client.delete(f'/api/departments/{self.laboratory.pk}/').status_code, 400,
        )

        # A unit that has never held anything is nobody's record, and still goes.
        empty = Unit.objects.create(department=self.laboratory, name='SEROLOGY')
        self.assertEqual(self.client.delete(f'/api/units/{empty.pk}/').status_code, 204)

        # Retiring is what closes a unit to new work, and it still answers.
        retired = self.client.patch(
            f'/api/units/{self.haematology.pk}/', {'is_active': False}, format='json',
        )
        self.assertEqual(retired.status_code, 200, retired.data)
        self.assertTrue(Unit.objects.filter(pk=self.haematology.pk).exists())

    def test_a_unit_with_an_open_transfer_cannot_be_deleted(self):
        # No stock anywhere, so only the transfer stands in the way.
        self.login(self.asker)
        self.ask(1)
        self.login(self.admin.phone)
        self.assertEqual(
            self.client.delete(f'/api/units/{self.chemistry.pk}/').status_code, 400,
        )

    def test_the_store_is_a_shelf_of_its_own_when_stock_is_dispensed(self):
        self.stock(self.haematology, 20)
        self.login(self.admin.phone)

        # Twenty cartons in the building, none of them at the central counter.
        refused = self.client.post('/api/stock-movements/dispense/', {
            'product': self.product.id, 'qty': 1,
        }, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('in the store', str(refused.data))

        # Off the shelf that is actually holding them, it goes through.
        self.assertEqual(
            self.client.post('/api/stock-movements/dispense/', {
                'product': self.product.id, 'qty': 1, 'unit': self.haematology.id,
            }, format='json').status_code,
            201,
        )
        self.assertEqual(self.balance(self.haematology), Decimal('19.0'))

    def test_the_dashboard_counts_what_is_still_waiting_on_somebody(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested(8)

        self.login(self.admin.phone)
        self.assertEqual(self.client.get('/api/dashboard/').data['transfers_pending'], 1)

        self.step(transfer_id, 'approve', {'items': [{'line': line_id, 'qty_approved': 5}]})
        self.step(transfer_id, 'issue', {'items': [{'line': line_id, 'qty': 5}]})
        # Handed over is still waiting: nobody has signed for it.
        self.assertEqual(self.client.get('/api/dashboard/').data['transfers_pending'], 1)

        self.step(transfer_id, 'receive', {})
        self.assertEqual(self.client.get('/api/dashboard/').data['transfers_pending'], 0)

    def test_a_transfer_note_prints_what_both_ends_sign(self):
        self.stock(self.haematology, 20)
        transfer_id, line_id = self.requested(8)
        self.login(self.holder)
        self.step(transfer_id, 'approve', {'items': [{'line': line_id, 'qty_approved': 5}]})
        self.step(transfer_id, 'issue', {'items': [{'line': line_id, 'qty': 5}]})

        sheet = self.client.get(f'/api/transfers/{transfer_id}/print/')
        self.assertEqual(sheet.status_code, 200)
        self.assertEqual(sheet['Content-Type'], 'application/pdf')
        text = ' '.join(
            page.extract_text() or '' for page in PdfReader(io.BytesIO(sheet.content)).pages
        )
        # The item name wraps inside its column, so match a word of it rather
        # than the whole label.
        for expected in ('STOCK TRANSFER NOTE', 'HAEMATOLOGY', 'CHEMISTRY', 'Dextrose'):
            self.assertIn(expected, text)

        # The ledger sheet says which shelf it covers, so a filtered one cannot
        # read as the whole hospital's.
        ledger = self.client.get(
            '/api/stock-movements/print/', {'unit': self.haematology.id},
        )
        self.assertEqual(ledger.status_code, 200)
        ledger_text = ' '.join(
            page.extract_text() or '' for page in PdfReader(io.BytesIO(ledger.content)).pages
        )
        self.assertIn('Shelf', ledger_text)
        self.assertIn('HAEMATOLOGY', ledger_text)
