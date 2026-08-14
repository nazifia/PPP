import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/main.dart';
import 'package:ppp_app/screens/transfers.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// One transfer: haematology (unit 7) has been asked for four cartons by
/// chemistry (unit 8), and both sit in the laboratory.
Map<String, dynamic> _transfer({String status = 'REQUESTED', double issued = 0}) => {
  'id': 1,
  'reference': 'AB12CD34',
  'from_unit': 7,
  'from_unit_name': 'HAEMATOLOGY',
  'to_unit': 8,
  'to_unit_name': 'CHEMISTRY',
  'department_name': 'LABORATORY',
  'status': status,
  'note': 'Run out before the round',
  'requested_by_name': 'Chemistry Staff',
  'created_at': '2026-08-14T09:00:00Z',
  'item_count': 1,
  'lines': [
    {
      'id': 5,
      'product': 3,
      'product_name': '10% Dextrose Water',
      'qty_requested': 4,
      'qty_approved': issued > 0 ? issued : 0,
      'qty_issued': issued,
      'qty_received': 0,
      'shortfall_reason': '',
    },
  ],
};

/// An account on one unit or another. The unit is what says which half of the
/// trade this person may act on — see `require_unit_member` in core/services.py.
Api _staffOn(int? unit, MockClient client) => Api(client: client)
  ..ready = true
  ..token = 'test-token'
  ..user = {
    'full_name': 'Ward Staff',
    'role': 'STAFF',
    'unit': unit,
    'organization_kind': 'HOSPITAL',
    'organization_name': 'General Hospital',
  };

MockClient _server(
  Map<String, dynamic> detail, {
  List<String>? bodies,
  Map<String, dynamic>? after,
}) => MockClient((request) async {
  final path = request.url.path;
  if (request.method == 'POST') {
    bodies?.add('$path ${request.body}');
    return http.Response(jsonEncode(after ?? detail), 200);
  }
  if (path.endsWith('/balances/')) return http.Response(jsonEncode([]), 200);
  return http.Response(jsonEncode(detail), 200);
});

Future<void> _pump(WidgetTester tester, Api api) async {
  await tester.pumpWidget(
    ApiScope(
      api: api,
      child: const MaterialApp(home: TransferDetailScreen(transferId: 1)),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('the unit being asked agrees; the one asking cannot', (tester) async {
    await _pump(tester, _staffOn(7, _server(_transfer())));
    expect(find.text('HAEMATOLOGY'), findsOneWidget);
    expect(find.text('Agree quantities'), findsOneWidget);
    expect(find.text('Refuse'), findsOneWidget);
    // Agreeing is not withdrawing: that half belongs to the other unit.
    expect(find.text('Withdraw'), findsNothing);

    await _pump(tester, _staffOn(8, _server(_transfer())));
    expect(find.text('Agree quantities'), findsNothing);
    expect(find.text('Refuse'), findsNothing);
    expect(find.text('Withdraw'), findsOneWidget);
  });

  testWidgets('nobody placed on a unit gets neither side of it', (tester) async {
    await _pump(tester, _staffOn(null, _server(_transfer())));
    expect(find.text('Agree quantities'), findsNothing);
    expect(find.text('Withdraw'), findsNothing);
  });

  testWidgets('agreeing sends what was typed, defaulted to what was asked', (tester) async {
    final bodies = <String>[];
    await _pump(
      tester,
      _staffOn(7, _server(_transfer(), bodies: bodies, after: _transfer(status: 'APPROVED'))),
    );

    await tester.tap(find.text('Agree quantities'));
    await tester.pumpAndSettle();
    // The box opens on the quantity that was asked for.
    expect(find.widgetWithText(TextField, '4'), findsOneWidget);
    await tester.enterText(find.widgetWithText(TextField, '4'), '2.5');
    await tester.tap(find.widgetWithText(FilledButton, 'Agree quantities').last);
    await tester.pumpAndSettle();

    expect(bodies, [
      '/api/transfers/1/approve/ '
          '${jsonEncode({
            'items': [
              {'line': 5, 'qty_approved': 2.5},
            ],
            'note': '',
          })}',
    ]);
  });

  testWidgets('a short receipt has to say what happened to the rest', (tester) async {
    final bodies = <String>[];
    await _pump(
      tester,
      _staffOn(
        8,
        _server(
          _transfer(status: 'ISSUED', issued: 4),
          bodies: bodies,
          after: _transfer(status: 'RECEIVED', issued: 4),
        ),
      ),
    );

    await tester.tap(find.text('Confirm what arrived'));
    await tester.pumpAndSettle();
    await tester.enterText(find.widgetWithText(TextField, '4'), '1');
    await tester.tap(find.widgetWithText(FilledButton, 'Confirm what arrived').last);
    await tester.pumpAndSettle();

    // Nothing sent: three cartons are missing and nobody has said why.
    expect(bodies, isEmpty);
    expect(
      find.text('10% Dextrose Water: say what happened to the missing quantity.'),
      findsOneWidget,
    );

    await tester.enterText(find.byType(TextField).at(1), 'never came over');
    await tester.tap(find.widgetWithText(FilledButton, 'Confirm what arrived').last);
    await tester.pumpAndSettle();

    expect(bodies, [
      '/api/transfers/1/receive/ '
          '${jsonEncode({
            'items': [
              {'line': 5, 'qty_received': 1.0, 'reason': 'never came over'},
            ],
            'note': '',
          })}',
    ]);
  });
}
