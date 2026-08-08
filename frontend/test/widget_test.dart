import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/main.dart';
import 'package:ppp_app/ui.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('signed-out app shows the login screen', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()..ready = true;
    await tester.pumpWidget(PppApp(api: api));
    expect(find.text('Sign in'), findsOneWidget);
    expect(find.text('Register a hospital / organisation'), findsOneWidget);
  });

  testWidgets('signed-in app shows the shell tabs', (tester) async {
    SharedPreferences.setMockInitialValues({});
    tester.view.physicalSize = const Size(400, 800); // phone: bottom bar, not the rail
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final api = Api()
      ..ready = true
      ..token = 'test-token'
      ..user = {
        'full_name': 'Tester',
        'role': 'ADMIN',
        'organization_kind': 'HOSPITAL',
        'organization_name': 'Test Hospital',
      };
    await tester.pumpWidget(PppApp(api: api));
    await tester.pump();
    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.text('Wishlist'), findsOneWidget);
  });

  testWidgets('a wide window swaps the bottom bar for a rail', (tester) async {
    SharedPreferences.setMockInitialValues({});
    tester.view.physicalSize = const Size(1400, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final api = Api()
      ..ready = true
      ..token = 'test-token'
      ..user = {'full_name': 'Tester', 'role': 'ADMIN', 'organization_kind': 'HOSPITAL'};
    await tester.pumpWidget(PppApp(api: api));
    await tester.pump();
    expect(find.byType(NavigationRail), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
  });

  testWidgets('Loader.onReload fires on a refetch but not the first load', (tester) async {
    final controller = LoaderController();
    addTearDown(controller.dispose);
    var reloads = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: Loader<int>(
          controller: controller,
          onReload: () => reloads++,
          load: () async => 1,
          builder: (context, data, reload) => Text('$data'),
        ),
      ),
    );
    await tester.pump();
    expect(reloads, 0);

    controller.reload();
    await tester.pump();
    expect(reloads, 1);
  });

  testWidgets('the theme choice survives a restart', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await setThemeMode(ThemeMode.dark);
    themeMode.value = ThemeMode.system; // as if the app had just launched
    await restoreThemeMode();
    expect(themeMode.value, ThemeMode.dark);
    addTearDown(() => themeMode.value = ThemeMode.system);
  });
}
