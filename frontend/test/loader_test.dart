import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/ui.dart';

void main() {
  testWidgets('a refetch keeps the rows it has instead of blanking them', (tester) async {
    final controller = LoaderController();
    var call = 0;
    // The second load never finishes, which is what the middle of a search
    // keystroke looks like.
    final pending = Completer<String>();

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Loader<String>(
            controller: controller,
            load: () => call++ == 0 ? Future.value('first page') : pending.future,
            builder: (context, data, reload) => ListView(children: [Text(data)]),
          ),
        ),
      ),
    );

    // Nothing loaded yet: a spinner is all there is to show.
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    await tester.pumpAndSettle();
    expect(find.text('first page'), findsOneWidget);

    controller.reload();
    await tester.pump();
    expect(find.text('first page'), findsOneWidget, reason: 'rows stay up while refetching');
    expect(find.byType(LinearProgressIndicator), findsOneWidget);

    pending.complete('second page');
    await tester.pumpAndSettle();
    expect(find.text('second page'), findsOneWidget);
    expect(find.byType(LinearProgressIndicator), findsNothing);
  });

  test('listAll follows the page cursor; list stops at the first page', () async {
    final asked = <String?>[];
    final api = Api(
      client: MockClient((request) async {
        asked.add(request.url.queryParameters['page']);
        final page = int.parse(request.url.queryParameters['page'] ?? '1');
        return http.Response(
          jsonEncode({
            'results': [
              {'id': page},
            ],
            'next': page < 3 ? 'http://x/?page=${page + 1}' : null,
          }),
          200,
        );
      }),
    )..token = 'test-token';

    expect((await api.list('/products/')).length, 1);

    asked.clear();
    final rows = await api.listAll('/products/');
    expect(rows.map((row) => row['id']), [1, 2, 3]);
    expect(asked, [null, '2', '3'], reason: 'stops as soon as the server has no next');
  });

  test('listAll gives up at maxPages rather than paging forever', () async {
    final api = Api(
      client: MockClient(
        (request) async => http.Response(
          jsonEncode({
            'results': [
              {'id': 1},
            ],
            'next': 'http://x/?page=99',
          }),
          200,
        ),
      ),
    )..token = 'test-token';

    expect((await api.listAll('/audit-logs/', null, 4)).length, 4);
  });
  test('a crash page is reported as a problem, not as a parse error', () async {
    final api = Api(
      client: MockClient(
        // What Django serves when a view throws: a page, not JSON.
        (request) async => http.Response('<html><body>Server Error</body></html>', 500),
      ),
    )..token = 'test-token';

    await expectLater(
      api.get('/dashboard/'),
      throwsA(
        isA<ApiException>()
            .having((error) => error.message, 'message', contains('had a problem'))
            .having((error) => error.statusCode, 'statusCode', 500),
      ),
    );
  });
}
