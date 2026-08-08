import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/ui.dart';

void main() {
  testWidgets('fires once after a burst of typing, not once per keystroke', (tester) async {
    final controller = TextEditingController();
    var calls = 0;

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: DebouncedSearch(controller: controller, onChanged: () => calls++),
        ),
      ),
    );

    for (final text in ['p', 'pa', 'par']) {
      await tester.enterText(find.byType(SearchBar), text);
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(calls, 0, reason: 'still typing');

    await tester.pump(const Duration(milliseconds: 400));
    expect(calls, 1);

    // The clear button appears with the text and searches immediately.
    await tester.tap(find.byIcon(Icons.close));
    await tester.pump();
    expect(controller.text, '');
    expect(calls, 2);
  });
}
