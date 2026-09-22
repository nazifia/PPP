import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/main.dart';
import 'package:ppp_app/screens/unit_items.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// One item on this person's own shelf, one on the unit next door.
const _rows = [
  {
    'id': 1, 'unit': 7, 'unit_name': 'LABORATORY / HAEMATOLOGY', 'name': 'Giemsa stain',
    'brand': '', 'strength': '500ml', 'formulation_name': 'SOLUTION',
    'dispensing_unit_name': 'BOTTLE', 'qty': 4, 'is_low_stock': true, 'is_active': true,
  },
  {
    'id': 2, 'unit': 8, 'unit_name': 'LABORATORY / CHEMISTRY', 'name': 'Glucose kit',
    'brand': '', 'strength': '', 'formulation_name': '', 'dispensing_unit_name': 'KIT',
    'qty': 30, 'is_low_stock': false, 'is_active': true,
  },
];

const _units = [
  {'id': 7, 'name': 'HAEMATOLOGY', 'full_name': 'LABORATORY / HAEMATOLOGY'},
  {'id': 8, 'name': 'CHEMISTRY', 'full_name': 'LABORATORY / CHEMISTRY'},
];

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('unit staff may only touch their own shelf', (tester) async {
    final queries = <String>[];
    final api = Api(
      client: MockClient((request) async {
        if (request.url.path.endsWith('/units/')) {
          return http.Response(jsonEncode(_units), 200);
        }
        queries.add(request.url.query);
        return http.Response(jsonEncode(_rows), 200);
      }),
    )
      ..ready = true
      ..token = 'test-token'
      ..user = {
        'full_name': 'Haematology Staff',
        'role': 'STAFF',
        'organization_kind': 'HOSPITAL',
        'unit': 7,
      };
    await tester.pumpWidget(
      ApiScope(api: api, child: const MaterialApp(home: UnitItemsScreen())),
    );
    await tester.pumpAndSettle();

    // Opens on the caller's own shelf.
    expect(queries.first, contains('unit=7'));
    expect(find.text('Giemsa stain 500ml'), findsOneWidget);
    expect(find.text('4 BOTTLE'), findsOneWidget);
    expect(find.text('LOW'), findsOneWidget);
    // One delete button: the neighbour's row offers none.
    expect(find.byIcon(Icons.delete_outline), findsOneWidget);
    expect(find.byIcon(Icons.add), findsOneWidget);
  });
}
