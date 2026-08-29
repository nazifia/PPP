import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/ui.dart';

/// Builds [child] under a chosen system font size and hands back its context.
Future<BuildContext> _at(WidgetTester tester, double factor) async {
  late BuildContext captured;
  await tester.pumpWidget(
    MediaQuery(
      data: MediaQueryData(textScaler: TextScaler.linear(factor)),
      child: MaterialApp(
        home: Builder(
          builder: (context) {
            captured = context;
            return const SizedBox.shrink();
          },
        ),
      ),
    ),
  );
  return captured;
}

void main() {
  testWidgets('textScale follows the font size up, but only so far', (tester) async {
    expect(textScale(await _at(tester, 1)), 1);
    expect(textScale(await _at(tester, 1.3)), closeTo(1.3, 0.001));
    // Smaller-than-default text must not shrink a box below what it was drawn for.
    expect(textScale(await _at(tester, 0.8)), 1);
    // And the largest accessibility setting must not eat the screen.
    expect(textScale(await _at(tester, 3)), 1.6);
  });

  testWidgets('a status chip says which status by shape as well as colour', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(
          body: Column(children: [StatusChip('APPROVED'), StatusChip('REJECTED')]),
        ),
      ),
    );
    // The pair a red/green confusion would otherwise collapse into one.
    expect(find.byIcon(statusIcon('APPROVED')), findsOneWidget);
    expect(find.byIcon(statusIcon('REJECTED')), findsOneWidget);
    expect(statusIcon('APPROVED'), isNot(statusIcon('REJECTED')));
  });

  testWidgets('undo runs what it was given, and says so when that fails', (tester) async {
    var undone = 0;
    late BuildContext context;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (inner) {
              context = inner;
              return const SizedBox.shrink();
            },
          ),
        ),
      ),
    );

    showUndo(context, 'Removed a line.', () async => undone++);
    await tester.pumpAndSettle();
    expect(find.text('Removed a line.'), findsOneWidget);
    await tester.tap(find.text('Undo'));
    await tester.pumpAndSettle();
    expect(undone, 1);

    showUndo(context, 'Removed another.', () async => throw Exception('gone'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Undo'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Could not undo'), findsOneWidget);
  });
}
