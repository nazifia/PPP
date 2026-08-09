import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/main.dart';
import 'package:ppp_app/screens/more.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The two rows every case below starts from: one standard entry nobody may
/// change, one the signed-in company defined itself.
const _rows = [
  {'id': 1, 'name': 'CARTON', 'is_shared': true, 'is_active': true},
  {'id': 2, 'name': 'JAR', 'is_shared': false, 'is_active': true},
];

Api _supplierAdmin(MockClient client) => Api(client: client)
  ..ready = true
  ..token = 'test-token'
  ..user = {
    'full_name': 'Supplier Admin',
    'role': 'ADMIN',
    'organization_kind': 'SUPPLIER',
    'organization_name': 'Valour',
  };

Future<void> _pumpScreen(WidgetTester tester, Api api) async {
  await tester.pumpWidget(
    ApiScope(
      api: api,
      child: const MaterialApp(
        home: CatalogueTermsScreen(
          path: '/dispensing-units/',
          title: 'Dispensing units',
          label: 'dispensing unit',
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('a standard entry is locked and a company\'s own is editable', (tester) async {
    final api = _supplierAdmin(
      MockClient((request) async => http.Response(jsonEncode(_rows), 200)),
    );
    await _pumpScreen(tester, api);

    expect(find.text('CARTON'), findsOneWidget);
    expect(find.text('Standard — the same for every company'), findsOneWidget);
    expect(find.text('JAR'), findsOneWidget);
    expect(find.text('Yours'), findsOneWidget);
    // One delete button, on the row that is this company's to delete.
    expect(find.byIcon(Icons.delete_outline), findsOneWidget);
  });

  testWidgets('deleting an entry still in use shows what the server said', (tester) async {
    final calls = <String>[];
    final api = _supplierAdmin(
      MockClient((request) async {
        calls.add('${request.method} ${request.url.path}');
        if (request.method == 'DELETE') {
          return http.Response(
            jsonEncode({'detail': 'Items in the catalogue still use this.'}),
            400,
          );
        }
        return http.Response(jsonEncode(_rows), 200);
      }),
    );
    await _pumpScreen(tester, api);

    await tester.tap(find.byIcon(Icons.delete_outline));
    await tester.pumpAndSettle();
    expect(find.text('Remove JAR?'), findsOneWidget);
    await tester.tap(find.text('Yes'));
    await tester.pumpAndSettle();

    expect(calls, contains('DELETE /api/dispensing-units/2/'));
    expect(find.text('Items in the catalogue still use this.'), findsOneWidget);
    // Refused, so the row is still there.
    expect(find.text('JAR'), findsOneWidget);
  });

  testWidgets('renaming sends the new name and reloads the list', (tester) async {
    final bodies = <String>[];
    final api = _supplierAdmin(
      MockClient((request) async {
        if (request.method == 'PATCH') {
          bodies.add(request.body);
          return http.Response(
            jsonEncode({'id': 2, 'name': 'TUB', 'is_shared': false}),
            200,
          );
        }
        return http.Response(
          jsonEncode(bodies.isEmpty
              ? _rows
              : [_rows.first, {'id': 2, 'name': 'TUB', 'is_shared': false}]),
          200,
        );
      }),
    );
    await _pumpScreen(tester, api);

    await tester.tap(find.text('JAR'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField).last, 'tub');
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(bodies, [jsonEncode({'name': 'tub'})]);
    expect(find.text('TUB'), findsOneWidget);
  });

  testWidgets('a hospital account gets no add button', (tester) async {
    final api = Api(client: MockClient((_) async => http.Response(jsonEncode(_rows), 200)))
      ..ready = true
      ..token = 'test-token'
      ..user = {'role': 'ADMIN', 'organization_kind': 'HOSPITAL'};
    await _pumpScreen(tester, api);

    expect(find.byType(FloatingActionButton), findsNothing);
    expect(find.byIcon(Icons.delete_outline), findsNothing);
  });
}
