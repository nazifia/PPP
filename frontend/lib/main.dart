import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';
import 'screens/catalogue.dart';
import 'screens/dashboard.dart';
import 'screens/deliveries.dart';
import 'screens/login.dart';
import 'screens/more.dart';
import 'screens/requests.dart';
import 'ui.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final api = Api();
  await api.restore();
  await restoreThemeMode();
  runApp(PppApp(api: api));
}

const seedColor = Color(0xFF00A65A);

/// The user's light/dark choice, kept outside the widget tree so any screen can
/// change it without a state library.
final themeMode = ValueNotifier<ThemeMode>(ThemeMode.system);

Future<void> restoreThemeMode() async {
  final saved = (await SharedPreferences.getInstance()).getString('theme_mode');
  themeMode.value = ThemeMode.values.firstWhere(
    (mode) => mode.name == saved,
    orElse: () => ThemeMode.system,
  );
}

Future<void> setThemeMode(ThemeMode mode) async {
  themeMode.value = mode;
  await (await SharedPreferences.getInstance()).setString('theme_mode', mode.name);
}

/// Material 3 palette and type, painted on glass: every surface the user reads
/// through is translucent, and the [GlassBackdrop] at the root shows through.
///
/// Scaffolds and bars are transparent here rather than at each call site, so a
/// screen still just builds a [Scaffold] and gets the treatment for free.
ThemeData appTheme(Brightness brightness) {
  final scheme = ColorScheme.fromSeed(
    seedColor: seedColor,
    brightness: brightness,
    // The M3 knob for how much colour the palette carries. `tonalSpot` (the
    // default) is deliberately muted and leaves the glass reading grey;
    // `vibrant` keeps the same green but gives the secondary and tertiary
    // ramps real hue, which is what the backdrop and the stat tiles paint
    // with. `expressive` and `fruitSalad` go further if this is still tame.
    dynamicSchemeVariant: DynamicSchemeVariant.vibrant,
  );
  final glass = glassTint(scheme).withValues(alpha: glassOpacity);
  return ThemeData(
    colorScheme: scheme,
    scaffoldBackgroundColor: Colors.transparent,
    // Dropdown menus paint with canvasColor and have no blur behind them, so
    // they stay near-opaque; the scaffold gets its transparency above instead.
    canvasColor: scheme.surfaceContainerHigh.withValues(alpha: 0.98),
    inputDecorationTheme: InputDecorationTheme(
      border: const OutlineInputBorder(),
      filled: true,
      fillColor: scheme.surfaceContainerHighest.withValues(alpha: 0.4),
    ),
    cardTheme: CardThemeData(
      margin: const EdgeInsets.symmetric(vertical: 4),
      color: glass,
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: glassEdge(scheme),
      ),
    ),
    appBarTheme: const AppBarTheme(backgroundColor: Colors.transparent, elevation: 0),
    // The nav chrome is wrapped in a GlassLayer, which supplies the blur; these
    // only stop it painting an opaque background over that blur.
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: glass,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
    ),
    navigationRailTheme: NavigationRailThemeData(backgroundColor: glass, elevation: 0),
    dialogTheme: DialogThemeData(
      backgroundColor: scheme.surfaceContainerHigh.withValues(alpha: 0.92),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(28),
        side: glassEdge(scheme),
      ),
    ),
    dividerTheme: DividerThemeData(color: scheme.outlineVariant.withValues(alpha: 0.5)),
  );
}

/// Makes the session available to every screen without a state library.
class ApiScope extends InheritedNotifier<Api> {
  const ApiScope({super.key, required Api api, required super.child}) : super(notifier: api);

  static Api of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<ApiScope>()!.notifier!;
}

class PppApp extends StatelessWidget {
  const PppApp({super.key, required this.api});

  final Api api;

  @override
  Widget build(BuildContext context) {
    return ApiScope(
      api: api,
      child: ValueListenableBuilder<ThemeMode>(
        valueListenable: themeMode,
        builder: (context, mode, _) => MaterialApp(
          title: 'PPP Supply',
          debugShowCheckedModeBanner: false,
          theme: appTheme(Brightness.light),
          darkTheme: appTheme(Brightness.dark),
          themeMode: mode,
          // Painted once, under everything including dialogs and snack bars,
          // so no screen has to carry the background itself.
          // Wrapping here rather than around MaterialApp puts the watcher over
          // the navigator, so dialogs and pushed routes count as activity too.
          builder: (context, child) =>
              _IdleWatcher(api: api, child: GlassBackdrop(child: child ?? const SizedBox())),
          home: const _Gate(),
        ),
      ),
    );
  }
}

/// Feeds the session's idle countdown: any pointer or key event restarts it,
/// and time spent backgrounded is settled against the clock on the way back.
class _IdleWatcher extends StatefulWidget {
  const _IdleWatcher({required this.api, required this.child});

  final Api api;
  final Widget child;

  @override
  State<_IdleWatcher> createState() => _IdleWatcherState();
}

class _IdleWatcherState extends State<_IdleWatcher> with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    HardwareKeyboard.instance.addHandler(_onKey);
    // No touch() here: the countdown is started by whatever produced the
    // session — Api.restore or a sign-in — so mounting is not activity.
  }

  @override
  void dispose() {
    HardwareKeyboard.instance.removeHandler(_onKey);
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  /// Typing counts as activity: filling a long form on desktop or web can go
  /// minutes without a click. Always false — this only listens.
  bool _onKey(KeyEvent event) {
    widget.api.touch();
    return false;
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      widget.api.resumeFromAway();
    } else {
      widget.api.markAway();
    }
  }

  @override
  Widget build(BuildContext context) {
    // Listener watches events on their way down without claiming them, so no
    // button, field or scrollable loses a gesture to this.
    return Listener(
      onPointerDown: (_) => widget.api.touch(),
      onPointerSignal: (_) => widget.api.touch(),
      child: ValueListenableBuilder<bool>(
        valueListenable: widget.api.idleWarningOn,
        // The app itself is passed through, so raising the warning does not
        // rebuild everything underneath it.
        child: widget.child,
        builder: (context, warning, child) => Stack(
          children: [
            child!,
            if (warning) _IdleWarning(api: widget.api),
          ],
        ),
      ),
    );
  }
}

/// The last minute of the session, offered back. Painted over the app rather
/// than pushed as a route, so nothing has to be popped when it goes away —
/// whichever timer wins simply takes the flag down.
class _IdleWarning extends StatelessWidget {
  const _IdleWarning({required this.api});

  final Api api;

  @override
  Widget build(BuildContext context) {
    return Positioned.fill(
      child: Stack(
        children: [
          // Swallows the taps the app must not receive while this is up.
          const ModalBarrier(dismissible: false, color: Colors.black54),
          Center(
            child: AlertDialog(
              icon: const Icon(Icons.timer_outlined),
              title: const Text('Still there?'),
              content: TweenAnimationBuilder<double>(
                tween: Tween(begin: idleWarning.inSeconds.toDouble(), end: 0),
                duration: idleWarning,
                builder: (context, seconds, _) => Text(
                  'This session ends in ${seconds.ceil()} seconds so an '
                  'unattended screen does not stay signed in.',
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () => api.signOut(),
                  child: const Text('Sign out now'),
                ),
                FilledButton(
                  onPressed: api.staySignedIn,
                  child: const Text('Stay signed in'),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Gate extends StatelessWidget {
  const _Gate();

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    if (!api.ready) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    return api.signedIn ? const HomeShell() : const LoginScreen();
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;
  bool _askedForNewPassword = false;

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final supplier = api.isSupplier;
    final pages = <Widget>[
      const DashboardScreen(),
      const CatalogueScreen(),
      const RequestsScreen(),
      const DeliveriesScreen(),
      const MoreScreen(),
    ];
    final tabs = <(IconData, String)>[
      (Icons.dashboard_outlined, 'Home'),
      (Icons.medication_outlined, supplier ? 'Catalogue' : 'Wishlist'),
      (Icons.assignment_outlined, 'Requests'),
      (Icons.local_shipping_outlined, 'Deliveries'),
      (Icons.more_horiz, 'More'),
    ];

    if (api.user?['must_change_password'] == true && !_askedForNewPassword) {
      _askedForNewPassword = true;
      WidgetsBinding.instance.addPostFrameCallback((_) => _forcePasswordChange(context));
    }

    void select(int value) => setState(() => _index = value);
    // Tablets and desktop get a side rail; phones keep the bottom bar.
    final width = MediaQuery.sizeOf(context).width;
    final wide = width >= 720;
    final extended = width >= 1200;

    return Scaffold(
      // Content runs under the bottom bar, which is what a translucent one is
      // for: something moving behind the glass. Scaffold pays for it by adding
      // the bar's height to the body's MediaQuery bottom padding, which the
      // lists then take as their own inset — see [listInset].
      extendBody: true,
      body: wide
          ? Row(
              children: [
                // A rail is taller than a landscape phone: let it scroll rather than overflow.
                GlassLayer(
                  child: LayoutBuilder(
                    builder: (context, constraints) => SingleChildScrollView(
                      child: ConstrainedBox(
                        constraints: BoxConstraints(minHeight: constraints.maxHeight),
                        child: IntrinsicHeight(
                          child: NavigationRail(
                            selectedIndex: _index,
                            onDestinationSelected: select,
                            extended: extended,
                            labelType: extended ? null : NavigationRailLabelType.all,
                            destinations: [
                              for (final (icon, label) in tabs)
                                NavigationRailDestination(icon: Icon(icon), label: Text(label)),
                            ],
                            // The rail has no "More" overflow the way the bottom bar
                            // does, so sign out gets its own spot under the tabs.
                            trailing: Padding(
                              padding: const EdgeInsets.only(top: 8),
                              // A ListTile would assert here: the rail gives its
                              // trailing unbounded width. Buttons shrink-wrap.
                              child: extended
                                  ? TextButton.icon(
                                      icon: const Icon(Icons.logout),
                                      label: const Text('Sign out'),
                                      style: TextButton.styleFrom(
                                        foregroundColor: Theme.of(context).colorScheme.error,
                                      ),
                                      onPressed: () => signOutFlow(context, api),
                                    )
                                  : IconButton(
                                      icon: const Icon(Icons.logout),
                                      color: Theme.of(context).colorScheme.error,
                                      tooltip: 'Sign out',
                                      onPressed: () => signOutFlow(context, api),
                                    ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                const VerticalDivider(width: 1),
                Expanded(child: pages[_index]),
              ],
            )
          : pages[_index],
      bottomNavigationBar: wide
          ? null
          : GlassLayer(
              child: NavigationBar(
                selectedIndex: _index,
                onDestinationSelected: select,
                destinations: [
                  for (final (icon, label) in tabs)
                    NavigationDestination(icon: Icon(icon), label: label),
                ],
              ),
            ),
    );
  }

  void _forcePasswordChange(BuildContext context) {
    if (!mounted) return;
    showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (context) => const ChangePasswordDialog(forced: true),
    );
  }
}
