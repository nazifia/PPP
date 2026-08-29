import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/main.dart';
import 'package:ppp_app/screens/dashboard.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// A dashboard whose headline figure changes between fetches, so a screen that
/// never refetches is visible in what it still says.
MockClient _client(List<int> awaiting) {
  var call = 0;
  return MockClient((request) async {
    if (request.url.path.endsWith('/profile/')) {
      return http.Response(jsonEncode({'full_name': 'Tester', 'role': 'ADMIN'}), 200);
    }
    final value = awaiting[call < awaiting.length ? call : awaiting.length - 1];
    call++;
    return http.Response(
      jsonEncode({
        'awaiting_action': value,
        'requests_total': 0,
        'deliveries_in_transit': 0,
        'invoices_outstanding': '0',
        'partners': 0,
        'requests_by_status': <String, dynamic>{},
        'recent': <dynamic>[],
        'low_stock': <dynamic>[],
      }),
      200,
    );
  });
}

void main() {
  testWidgets('the refresh button refetches the figures, not just the profile', (tester) async {
    SharedPreferences.setMockInitialValues({});
    tester.view.physicalSize = const Size(500, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);

    final api = Api(client: _client([3, 7]))
      ..ready = true
      ..token = 'test-token'
      ..user = {
        'full_name': 'Tester',
        'role': 'ADMIN',
        'organization_kind': 'HOSPITAL',
        'organization_name': 'Test Hospital',
      };

    await tester.pumpWidget(
      ApiScope(
        api: api,
        child: const MaterialApp(home: DashboardScreen()),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('3'), findsOneWidget);

    await tester.tap(find.byTooltip('Refresh'));
    await tester.pumpAndSettle();
    expect(find.text('7'), findsOneWidget, reason: 'the second fetch reached the screen');
    expect(find.text('3'), findsNothing);

    // Refreshing restarted the idle countdown; the test ends here, so end it too.
    api.dispose();
  });
}
