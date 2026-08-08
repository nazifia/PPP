import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Thrown for anything the server refused. [message] is already readable.
class ApiException implements Exception {
  ApiException(this.message, [this.statusCode = 0]);

  final String message;
  final int statusCode;

  @override
  String toString() => message;
}

String _defaultBaseUrl() {
  const fromEnv = String.fromEnvironment('API_BASE');
  if (fromEnv.isNotEmpty) return fromEnv;
  if (kIsWeb) return 'http://localhost:8000/api';
  // 10.0.2.2 is how the Android emulator reaches the host machine.
  return Platform.isAndroid ? 'http://10.0.2.2:8000/api' : 'http://localhost:8000/api';
}

/// How much of [Api.idleTimeout] is spent warning before the session goes.
const idleWarning = Duration(seconds: 60);

/// What the More screen offers for [Api.idleTimeout].
const idleTimeoutChoices = [
  Duration(minutes: 5),
  Duration(minutes: 15),
  Duration(minutes: 30),
  Duration(minutes: 60),
];

/// The idle timeout a fresh install starts on, overridable per build with
/// `--dart-define=IDLE_MINUTES=15` for a site with its own policy.
Duration _defaultIdleTimeout() {
  const minutes = int.fromEnvironment('IDLE_MINUTES', defaultValue: 30);
  // Below two minutes the warning would eat the whole session.
  return Duration(minutes: minutes < 2 ? 2 : minutes);
}

/// Session + thin REST client. One instance lives at the root of the app.
class Api extends ChangeNotifier {
  String baseUrl = _defaultBaseUrl();
  String? token;
  Map<String, dynamic>? user;
  bool ready = false;

  /// This device's own choice of idle timeout, from the More screen.
  Duration deviceIdleTimeout = _defaultIdleTimeout();

  /// The ceiling the organisation's administrator set, once a profile has been
  /// loaded. Null until then, and for an account with no organisation.
  Duration? get idlePolicy {
    final minutes = organization?['idle_timeout_minutes'];
    return minutes is int ? Duration(minutes: minutes) : null;
  }

  /// How long a session survives with nobody touching it. A shared ward
  /// terminal left open is the case this closes. The device may be stricter
  /// than its organisation asks, never looser.
  Duration get idleTimeout {
    final policy = idlePolicy;
    return policy != null && policy < deviceIdleTimeout ? policy : deviceIdleTimeout;
  }

  /// Set when the session ended by itself, so the login screen can say why.
  bool signedOutForIdle = false;

  /// True while the last-minute countdown is on screen. Ordinary activity does
  /// not clear it — only [staySignedIn] does, so a nudged mouse cannot quietly
  /// hold a ward terminal open on somebody's behalf.
  final idleWarningOn = ValueNotifier<bool>(false);

  Timer? _idleTimer;
  Timer? _warningTimer;
  bool _signingOut = false;

  /// The tenant a superuser is working inside. Null means it sees them all.
  int? actAsOrg;

  bool get signedIn => token != null;
  Map<String, dynamic>? get organization => user?['organization_detail'] as Map<String, dynamic>?;
  String get orgKind => (user?['organization_kind'] ?? '') as String;
  bool get isHospital => orgKind == 'HOSPITAL';
  bool get isSupplier => orgKind == 'SUPPLIER';
  bool get isAdmin => user?['role'] == 'ADMIN';
  bool get isSuperuser => user?['is_superuser'] == true;

  /// A superuser reading across tenants acts on either side of any row it
  /// opens, because the server takes the tenant from the row itself. Creating a
  /// row from nothing still needs a tenant, so `isHospital` and `isSupplier`
  /// keep gating the new-record buttons.
  bool get actsForAnyTenant => isSuperuser && actAsOrg == null;
  bool get canActAsHospital => isHospital || actsForAnyTenant;
  bool get canActAsSupplier => isSupplier || actsForAnyTenant;

  Future<void> restore() async {
    final prefs = await SharedPreferences.getInstance();
    baseUrl = prefs.getString('base_url') ?? baseUrl;
    final savedIdle = prefs.getInt('idle_minutes');
    if (savedIdle != null) deviceIdleTimeout = Duration(minutes: savedIdle);
    token = prefs.getString('token');
    actAsOrg = prefs.getInt('act_as_org');
    final cached = prefs.getString('user');
    if (cached != null) user = jsonDecode(cached) as Map<String, dynamic>;
    ready = true;
    notifyListeners();
    if (token != null) {
      // The app may have been shut for longer than the timeout.
      await resumeFromAway();
      if (token == null) return;
      try {
        await refreshProfile();
      } on ApiException {
        await signOut();
      }
    }
  }

  /// Restart the idle countdown. Called on every pointer and key event, and
  /// ignored once the warning is up — see [idleWarningOn].
  void touch() {
    if (token == null || idleWarningOn.value) return;
    _idleTimer?.cancel();
    _warningTimer?.cancel();
    _warningTimer = Timer(idleTimeout - idleWarning, () => idleWarningOn.value = true);
    _idleTimer = Timer(idleTimeout, () => signOut(idle: true));
  }

  /// Change this device's timeout and start counting again on the new one.
  Future<void> setIdleTimeout(Duration value) async {
    deviceIdleTimeout = value;
    touch();
    notifyListeners();
    await (await SharedPreferences.getInstance()).setInt('idle_minutes', value.inMinutes);
  }

  /// The reprieve offered by the warning: put the full timeout back.
  void staySignedIn() {
    idleWarningOn.value = false;
    touch();
  }

  void _stopIdleTimers() {
    _idleTimer?.cancel();
    _warningTimer?.cancel();
    _idleTimer = null;
    _warningTimer = null;
  }

  /// Record when the app went away. A Dart timer does not run while the app is
  /// backgrounded or killed, so that gap is measured against the wall clock
  /// instead — otherwise closing the app would pause the timeout indefinitely.
  Future<void> markAway() async {
    _stopIdleTimers();
    if (token == null) return;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt('last_seen', DateTime.now().millisecondsSinceEpoch);
  }

  /// Sign out if the away gap outran the timeout; otherwise start counting again.
  Future<void> resumeFromAway() async {
    if (token == null) return;
    final prefs = await SharedPreferences.getInstance();
    final lastSeen = prefs.getInt('last_seen');
    final away = lastSeen == null
        ? Duration.zero
        : Duration(milliseconds: DateTime.now().millisecondsSinceEpoch - lastSeen);
    if (away > idleTimeout) {
      await signOut(idle: true);
    } else {
      // Coming back to the app is activity, so any warning left over from
      // before it was put away is spent.
      idleWarningOn.value = false;
      touch();
    }
  }

  Future<void> setBaseUrl(String value) async {
    baseUrl = value.trim().replaceAll(RegExp(r'/+$'), '');
    if (!baseUrl.endsWith('/api')) baseUrl = '$baseUrl/api';
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('base_url', baseUrl);
    notifyListeners();
  }

  Uri _uri(String path, [Map<String, dynamic>? query]) {
    final cleaned = query?.map((k, v) => MapEntry(k, '$v'))?..removeWhere((_, v) => v.isEmpty);
    return Uri.parse('$baseUrl$path').replace(queryParameters: cleaned);
  }

  Map<String, String> get _headers => {
    'Content-Type': 'application/json',
    if (token != null) 'Authorization': 'Token $token',
    if (actAsOrg != null) 'X-Act-As-Org': '$actAsOrg',
  };

  /// Step a superuser into one organisation, or back out again with null.
  Future<void> setActAsOrg(int? id) async {
    actAsOrg = id;
    final prefs = await SharedPreferences.getInstance();
    if (id == null) {
      await prefs.remove('act_as_org');
    } else {
      await prefs.setInt('act_as_org', id);
    }
    await refreshProfile();
  }

  Future<dynamic> _send(Future<http.Response> Function() call) async {
    http.Response response;
    try {
      response = await call().timeout(const Duration(seconds: 30));
    } catch (error) {
      throw ApiException('Cannot reach the server.\n$error');
    }
    if (response.statusCode == 401 && token != null) {
      // The server dropped the session — it expires an idle token on the
      // organisation's policy, whatever this device's own countdown says.
      unawaited(signOut(idle: true));
    }
    if (response.statusCode == 204 || response.body.isEmpty) return null;
    final body = jsonDecode(utf8.decode(response.bodyBytes));
    if (response.statusCode >= 400) {
      throw ApiException(_readError(body), response.statusCode);
    }
    return body;
  }

  String _readError(dynamic body) {
    if (body is String) return body;
    if (body is List) return body.map(_readError).join('\n');
    if (body is Map) {
      final parts = <String>[];
      body.forEach((key, value) {
        final text = _readError(value);
        parts.add(key == 'detail' || key == 'non_field_errors' ? text : '$key: $text');
      });
      return parts.join('\n');
    }
    return 'Something went wrong.';
  }

  Future<dynamic> get(String path, [Map<String, dynamic>? query]) =>
      _send(() => http.get(_uri(path, query), headers: _headers));

  /// Raw bytes, with the session's token: a PDF the app hands to the printer.
  /// Longer than the usual patience, because the server renders it on the spot.
  Future<Uint8List> bytes(String path, [Map<String, dynamic>? query]) async {
    http.Response response;
    try {
      response = await http
          .get(_uri(path, query), headers: _headers)
          .timeout(const Duration(seconds: 60));
    } catch (error) {
      throw ApiException('Cannot reach the server.\n$error');
    }
    if (response.statusCode >= 400) {
      throw ApiException(_readBytesError(response), response.statusCode);
    }
    return response.bodyBytes;
  }

  /// A refusal comes back as JSON; a crash comes back as a page of HTML.
  String _readBytesError(http.Response response) {
    try {
      return _readError(jsonDecode(utf8.decode(response.bodyBytes)));
    } catch (_) {
      return 'The server could not produce this document (${response.statusCode}).';
    }
  }

  Future<dynamic> post(String path, [Map<String, dynamic>? data]) =>
      _send(() => http.post(_uri(path), headers: _headers, body: jsonEncode(data ?? {})));

  /// Same as [post], but with one file attached. Everything else travels as
  /// text, so the server reads it exactly like the JSON form.
  Future<dynamic> postWithFile(
    String path,
    Map<String, dynamic> fields, {
    required String field,
    required String filename,
    required Uint8List bytes,
  }) => _send(() async {
    final request = http.MultipartRequest('POST', _uri(path))
      ..headers.addAll({..._headers}..remove('Content-Type'))
      ..fields.addAll(fields.map((key, value) => MapEntry(key, '$value')))
      ..files.add(http.MultipartFile.fromBytes(field, bytes, filename: filename));
    return http.Response.fromStream(await request.send());
  });

  Future<dynamic> patch(String path, Map<String, dynamic> data) =>
      _send(() => http.patch(_uri(path), headers: _headers, body: jsonEncode(data)));

  /// [query] carries what a request with no body cannot: a superuser naming
  /// the tenant a two-sided row such as a trading link belongs to.
  Future<dynamic> delete(String path, [Map<String, dynamic>? query]) =>
      _send(() => http.delete(_uri(path, query), headers: _headers));

  /// Unwraps a paginated list response into plain rows, dropping the page
  /// cursor. Enough for a list short enough to read in one screenful.
  Future<List<Map<String, dynamic>>> list(String path, [Map<String, dynamic>? query]) async =>
      (await listPage(path, query)).$1;

  /// One page of rows, plus whether the server has another after it. For a list
  /// that outgrows a single page, such as the stock ledger.
  Future<(List<Map<String, dynamic>>, bool)> listPage(
    String path, [
    Map<String, dynamic>? query,
  ]) async {
    final body = await get(path, query);
    if (body is! Map) return ((body as List).cast<Map<String, dynamic>>(), false);
    return ((body['results'] as List).cast<Map<String, dynamic>>(), body['next'] != null);
  }

  Future<void> _storeSession(Map<String, dynamic> payload) async {
    token = payload['token'] as String;
    user = payload['user'] as Map<String, dynamic>;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('token', token!);
    await prefs.setString('user', jsonEncode(user));
    signedOutForIdle = false;
    touch();
    notifyListeners();
    await refreshProfile();
  }

  Future<void> signIn(String phone, String password) async {
    final payload = await post('/auth/login/', {'phone': phone, 'password': password});
    await _storeSession(payload as Map<String, dynamic>);
  }

  Future<void> registerHospital(Map<String, dynamic> data) async {
    final payload = await post('/auth/register/', data);
    await _storeSession(payload as Map<String, dynamic>);
  }

  Future<void> changePassword(String current, String next) async {
    final payload = await post('/auth/change-password/', {
      'current_password': current,
      'new_password': next,
    });
    await _storeSession(payload as Map<String, dynamic>);
  }

  Future<void> refreshProfile() async {
    user = await get('/auth/me/') as Map<String, dynamic>;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('user', jsonEncode(user));
    // The profile carries the organisation's idle policy, so a tightened one
    // binds from here rather than at the next tap.
    touch();
    notifyListeners();
  }

  /// [idle] marks the session as timed out rather than ended on purpose.
  Future<void> signOut({bool idle = false}) async {
    // The logout call below can itself come back 401, which asks for a sign-out
    // again. Without this the two would chase each other.
    if (_signingOut) return;
    _signingOut = true;
    _stopIdleTimers();
    idleWarningOn.value = false;
    try {
      if (token != null) await post('/auth/logout/');
    } on ApiException {
      // ponytail: a dead token is already signed out; nothing to recover.
    }
    token = null;
    user = null;
    actAsOrg = null;
    signedOutForIdle = idle;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('token');
    await prefs.remove('user');
    await prefs.remove('act_as_org');
    await prefs.remove('last_seen');
    _signingOut = false;
    notifyListeners();
  }
}
