import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/ui.dart';

Widget _host(List<String> options, {void Function(String?)? onSelected, int searchFrom = 6}) =>
    MaterialApp(
      home: Scaffold(
        body: PickerField<String?>(
          label: 'Item',
          searchFrom: searchFrom,
          entries: [
            for (final option in options) DropdownMenuEntry(value: option, label: option),
          ],
          onSelected: onSelected ?? (_) {},
        ),
      ),
    );

void main() {
  testWidgets('typing part of a label narrows the list to it', (tester) async {
    String? picked;
    await tester.pumpWidget(
      _host([
        'Paracetamol 500mg',
        'Amoxicillin 250mg',
        'Ibuprofen 400mg',
        'Metformin 500mg',
        'Ciprofloxacin 500mg',
        'Omeprazole 20mg',
      ], onSelected: (value) => picked = value),
    );

    await tester.tap(find.byType(TextField));
    await tester.pumpAndSettle();

    // Mid-label, not a prefix: the point of the search box.
    await tester.enterText(find.byType(TextField), 'floxacin');
    await tester.pumpAndSettle();

    expect(find.widgetWithText(MenuItemButton, 'Ciprofloxacin 500mg'), findsOneWidget);
    expect(find.widgetWithText(MenuItemButton, 'Paracetamol 500mg'), findsNothing);

    await tester.tap(find.widgetWithText(MenuItemButton, 'Ciprofloxacin 500mg'));
    await tester.pumpAndSettle();
    expect(picked, 'Ciprofloxacin 500mg');
  });

  testWidgets('with a search callback the server, not the loaded page, answers', (tester) async {
    final asked = <String>[];
    // What the first page carried, and what only a query can reach.
    const firstPage = ['Accident & Emergency', 'Antenatal'];

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: PickerField<String?>(
            label: 'Department / unit',
            entries: [
              for (final row in firstPage) DropdownMenuEntry(value: row, label: row),
            ],
            search: (query) async {
              asked.add(query);
              return [DropdownMenuEntry(value: 'Theatre', label: 'Theatre $query')];
            },
            onSelected: (_) {},
          ),
        ),
      ),
    );

    await tester.tap(find.byType(TextField));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(MenuItemButton, 'Antenatal'), findsOneWidget);

    // A burst of typing is one request, for the last of it.
    await tester.enterText(find.byType(TextField), 'the');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.enterText(find.byType(TextField), 'theat');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    expect(asked, ['theat']);
    expect(find.widgetWithText(MenuItemButton, 'Theatre theat'), findsOneWidget);
    expect(find.widgetWithText(MenuItemButton, 'Antenatal'), findsNothing);
  });

  testWidgets('a short list opens without asking for the keyboard', (tester) async {
    await tester.pumpWidget(_host(['Staff', 'Administrator']));

    final field = tester.widget<TextField>(find.byType(TextField));
    expect(field.canRequestFocus, isFalse);

    await tester.tap(find.byType(TextField));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(MenuItemButton, 'Administrator'), findsOneWidget);
  });
}
