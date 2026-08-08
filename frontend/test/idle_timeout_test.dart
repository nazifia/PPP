import 'package:flutter_test/flutter_test.dart';
import 'package:ppp_app/api.dart';
import 'package:ppp_app/main.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The idle logout: whether the gap since the app was last seen outran
/// [Api.idleTimeout], and the warning that precedes the sign-out.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  Api signedInApi(Duration away) {
    SharedPreferences.setMockInitialValues({
      'token': 'abc',
      'last_seen': DateTime.now().subtract(away).millisecondsSinceEpoch,
    });
    return Api()..token = 'abc';
  }

  test('a gap longer than the timeout ends the session', () async {
    final api = signedInApi(const Duration(minutes: 31));
    await api.resumeFromAway();
    expect(api.signedIn, isFalse);
    expect(api.signedOutForIdle, isTrue);
  });

  test('a gap inside the timeout keeps the session', () async {
    final api = signedInApi(const Duration(minutes: 29));
    await api.resumeFromAway();
    expect(api.signedIn, isTrue);
    expect(api.signedOutForIdle, isFalse);
  });

  test('a signed-out app has nothing to expire', () async {
    SharedPreferences.setMockInitialValues({});
    final api = Api();
    await api.resumeFromAway();
    expect(api.signedOutForIdle, isFalse);
  });

  test('a saved timeout is picked up on the next launch', () async {
    SharedPreferences.setMockInitialValues({});
    final api = Api();
    await api.setIdleTimeout(const Duration(minutes: 5));

    final relaunched = Api();
    await relaunched.restore();
    expect(relaunched.deviceIdleTimeout, const Duration(minutes: 5));
  });

  test('the organisation policy caps the device, but cannot loosen it', () async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()
      ..token = 'abc'
      ..user = {
        'organization_detail': {'idle_timeout_minutes': 10},
      };
    await api.setIdleTimeout(const Duration(minutes: 60));
    expect(api.idleTimeout, const Duration(minutes: 10));

    await api.setIdleTimeout(const Duration(minutes: 5));
    expect(api.idleTimeout, const Duration(minutes: 5));

    await api.signOut();
  });

  test('overlapping sign-outs settle as one', () async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()..token = 'abc';
    api.touch();
    await Future.wait([api.signOut(idle: true), api.signOut()]);
    expect(api.signedIn, isFalse);
    expect(api.signedOutForIdle, isTrue);
  });

  testWidgets('the warning shows a minute out, and staying signed in clears it', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()..token = 'abc';
    api.touch();

    await tester.pump(api.idleTimeout - idleWarning - const Duration(seconds: 1));
    expect(api.idleWarningOn.value, isFalse);
    await tester.pump(const Duration(seconds: 2));
    expect(api.idleWarningOn.value, isTrue);

    // Activity alone must not dismiss it; only the explicit reprieve does.
    api.touch();
    expect(api.idleWarningOn.value, isTrue);
    api.staySignedIn();
    expect(api.idleWarningOn.value, isFalse);

    await api.signOut(); // the reprieve armed fresh timers; leave none pending
  });

  testWidgets('a shortened timeout takes effect at once', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()..token = 'abc';
    api.touch();
    await api.setIdleTimeout(const Duration(minutes: 5));

    await tester.pump(const Duration(minutes: 5, seconds: 1));
    expect(api.signedIn, isFalse);
  });

  testWidgets('the warning is painted over the running app', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()
      ..ready = true
      ..token = 'abc'
      ..user = {'full_name': 'Tester', 'role': 'ADMIN', 'organization_kind': 'HOSPITAL'};
    await tester.pumpWidget(PppApp(api: api));
    api.touch();
    await tester.pump(api.idleTimeout - idleWarning + const Duration(seconds: 1));
    expect(find.text('Still there?'), findsOneWidget);

    await tester.tap(find.text('Stay signed in'));
    await tester.pump();
    expect(find.text('Still there?'), findsNothing);
    expect(api.signedIn, isTrue);

    await api.signOut();
  });

  testWidgets('ignoring the warning ends the session', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final api = Api()..token = 'abc';
    api.touch();
    await tester.pump(api.idleTimeout + const Duration(seconds: 1));
    expect(api.signedIn, isFalse);
    expect(api.signedOutForIdle, isTrue);
  });
}
