import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/ui.dart';

/// The two primitives every screen leans on for its layout: content that stops
/// widening on a monitor, and dialogs that stop widening on a phone.
void main() {
  testWidgets('Bounded fills a phone and caps on a desktop', (tester) async {
    Future<double> widthAt(Size screen) async {
      tester.view.physicalSize = screen;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: Bounded(child: Placeholder(key: Key('inner'))),
          ),
        ),
      );
      return tester.getSize(find.byKey(const Key('inner'))).width;
    }

    expect(await widthAt(const Size(360, 800)), 360);
    expect(await widthAt(const Size(1920, 1080)), 900);
  });

  testWidgets('dialogWidth never exceeds the screen', (tester) async {
    Future<double> widthAt(Size screen) async {
      tester.view.physicalSize = screen;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      late double result;
      await tester.pumpWidget(
        MaterialApp(
          home: Builder(
            builder: (context) {
              result = dialogWidth(context);
              return const SizedBox.shrink();
            },
          ),
        ),
      );
      return result;
    }

    expect(await widthAt(const Size(320, 640)), lessThan(320));
    expect(await widthAt(const Size(1920, 1080)), 460);
  });

  testWidgets('WideTable is a table on a monitor and a stack on a phone', (tester) async {
    Future<void> pumpAt(Size screen) async {
      tester.view.physicalSize = screen;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: WideTable(
              columns: ['Item', 'Sent'],
              rows: [
                DataRow(cells: [DataCell(Text('Paracetamol')), DataCell(Text('12'))]),
              ],
            ),
          ),
        ),
      );
    }

    await pumpAt(const Size(1200, 800));
    expect(find.byType(DataTable), findsOneWidget);
    // A header appears once, next to nothing.
    expect(find.text('Item'), findsOneWidget);

    await pumpAt(const Size(360, 800));
    expect(find.byType(DataTable), findsNothing);
    // The header is now a label beside its value, and the value is still there.
    expect(find.text('Item'), findsOneWidget);
    expect(find.text('Paracetamol'), findsOneWidget);
  });

  testWidgets('TwoPane hides the detail on a phone and shows it on a monitor', (tester) async {
    Future<void> pumpAt(Size screen) async {
      tester.view.physicalSize = screen;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(
        const MaterialApp(
          home: Scaffold(
            body: TwoPane(list: Text('list'), detail: Text('detail'), placeholder: 'pick one'),
          ),
        ),
      );
    }

    await pumpAt(const Size(360, 800));
    expect(find.text('list'), findsOneWidget);
    expect(find.text('detail'), findsNothing);

    await pumpAt(const Size(1400, 900));
    expect(find.text('list'), findsOneWidget);
    expect(find.text('detail'), findsOneWidget);
  });
}
