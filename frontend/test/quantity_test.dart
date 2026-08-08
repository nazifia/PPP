import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/ui.dart';

/// Quantities go in half units. The API sends them as JSON numbers, so a whole
/// one arrives as an int and a half as a double, and both must read the same.
void main() {
  test('reads a quantity whichever way the API sent it', () {
    expect(qty(3), 3);
    expect(qty(2.5), 2.5);
    expect(qty('1.5'), 1.5);
    expect(qty(null), 0);
  });

  test('prints whole units without a decimal and halves with one', () {
    expect(qtyText(3), '3');
    expect(qtyText(3.0), '3');
    expect(qtyText(2.5), '2.5');
    expect(qtyText(-1.5), '-1.5');
  });

  test('accepts half units and refuses anything off the step', () {
    expect(parseQty('0.5'), 0.5);
    expect(parseQty('2'), 2);
    expect(parseQty(' 1.5 '), 1.5);
    expect(parseQty('-3'), -3);
    expect(parseQty('1.25'), isNull);
    expect(parseQty('0.3'), isNull);
    expect(parseQty('two'), isNull);
    expect(parseQty(''), isNull);
    expect(parseQty(null), isNull);
  });
}
