import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/screens/requests.dart';

/// One request can be billed on several deliveries, so its payment status is a
/// roll-up rather than a field. The amounts arrive from the API as strings.
Map<String, dynamic> delivery({
  String? amount,
  String paid = '0.00',
  String pending = '0.00',
  int daysOverdue = 0,
}) => {
  'invoice': amount == null
      ? null
      : {
          'id': 1,
          'amount': amount,
          'amount_paid': paid,
          'amount_pending': pending,
          'days_overdue': daysOverdue,
        },
};

void main() {
  test('a delivery with no invoice yet contributes nothing', () {
    final money = RequisitionMoney([delivery(), delivery(amount: '100.00')]);
    expect(money.invoices.length, 1);
    expect(money.invoiced, 100);
  });

  test('sums across invoices and only counts confirmed money as paid', () {
    final money = RequisitionMoney([
      delivery(amount: '100.00', paid: '100.00'),
      delivery(amount: '50.00', pending: '50.00'),
    ]);
    expect(money.invoiced, 150);
    expect(money.paid, 100);
    expect(money.pending, 50);
    expect(money.balance, 50);
    expect(money.status, 'PART_PAID');
  });

  test('declared but unconfirmed money leaves the request unpaid', () {
    final money = RequisitionMoney([delivery(amount: '100.00', pending: '100.00')]);
    expect(money.status, 'UNPAID');
  });

  test('paid only once every invoice is settled', () {
    final money = RequisitionMoney([
      delivery(amount: '100.00', paid: '100.00'),
      delivery(amount: '0.01', paid: '0.01'),
    ]);
    expect(money.status, 'PAID');
    expect(money.isOverdue, isFalse);
  });

  test('one overdue invoice makes the request overdue', () {
    final money = RequisitionMoney([
      delivery(amount: '100.00', paid: '100.00'),
      delivery(amount: '50.00', daysOverdue: 3),
    ]);
    expect(money.isOverdue, isTrue);
  });
}
