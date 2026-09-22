import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/screens/unit_items.dart';

void main() {
  test('parses a header in any order and skips blanks', () {
    final rows = parseImport('qty, Name ,strength,ignored\n100,Paracetamol,500mg,x\n\n,Aspirin\n');
    expect(rows, [
      {'qty': 100.0, 'name': 'Paracetamol', 'strength': '500mg'},
      {'name': 'Aspirin'},
    ]);
  });

  test('refuses a file without a name column', () {
    expect(() => parseImport('qty\n1'), throwsFormatException);
    expect(parseImport(''), isEmpty);
  });
}
