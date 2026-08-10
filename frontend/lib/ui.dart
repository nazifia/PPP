import 'dart:async';
import 'dart:ui' show ImageFilter;

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:printing/printing.dart';

import 'api.dart';

final money = NumberFormat.currency(symbol: '₦', decimalDigits: 2);
final shortDate = DateFormat('d MMM yyyy, h:mm a');

String formatDate(String? iso) =>
    iso == null ? '-' : shortDate.format(DateTime.parse(iso).toLocal());

String amount(dynamic value) => money.format(double.tryParse('$value') ?? 0);

final _monthName = DateFormat('MMMM yyyy');
final _monthShort = DateFormat('MMM');

/// A month from its first day, as the API sends it: 'August 2026', or 'Aug' on
/// a chart axis where the year is already implied by the bars beside it.
String monthLabel(String iso, {bool short = false}) =>
    (short ? _monthShort : _monthName).format(DateTime.parse(iso));

/// Quantities are half units — half a pack is real stock, a quarter is not —
/// so nothing may read one as a whole number.
const halfUnit = 0.5;

/// A quantity as it came off the API, which sends it as a JSON number.
double qty(dynamic value) => value is num ? value.toDouble() : double.tryParse('$value') ?? 0;

/// A quantity for reading: '2' for a whole one, '2.5' for a half.
String qtyText(dynamic value) {
  final number = qty(value);
  return number == number.roundToDouble() ? '${number.round()}' : '$number';
}

/// A quantity as the user typed it, or null if it is not one: the server takes
/// half units only, and the message it would send back is the same either way.
double? parseQty(String? text) {
  final value = double.tryParse((text ?? '').trim());
  if (value == null || (value / halfUnit) % 1 != 0) return null;
  return value;
}

/// The keyboard for a quantity box. [signed] for a stock adjustment, which may
/// take goods off the shelf as well as put them on.
TextInputType qtyKeyboard({bool signed = false}) =>
    TextInputType.numberWithOptions(decimal: true, signed: signed);

const badQtyMessage = 'Quantities go in half units: 0.5, 1, 1.5 and so on.';

/// Seeds for the tonal palettes below. Never painted directly: a raw material
/// colour is unreadable in one theme or the other, so it is run through
/// [statusScheme] first.
const statusColors = <String, Color>{
  'DRAFT': Colors.blueGrey,
  'SUBMITTED': Colors.orange,
  'APPROVED': Colors.green,
  'PARTIALLY_APPROVED': Colors.teal,
  'REJECTED': Colors.red,
  'DISPATCHED': Colors.indigo,
  'DELIVERED': Colors.purple,
  'CLOSED': Colors.grey,
  'CANCELLED': Colors.red,
  'IN_TRANSIT': Colors.indigo,
  'VERIFIED': Colors.green,
  'RETURNED': Colors.red,
  'UNPAID': Colors.red,
  'OVERDUE': Colors.deepOrange,
  'PART_PAID': Colors.orange,
  'PAID': Colors.green,
  'PENDING': Colors.orange,
  'CONFIRMED': Colors.green,
  'PARTIAL': Colors.teal,
  'AVL': Colors.green,
  'RV': Colors.blue,
};

final _schemeCache = <(Color, Brightness), ColorScheme>{};

/// A Material 3 tonal palette derived from an accent colour, so an accent keeps
/// its meaning in light and dark without hand-picked shades.
ColorScheme tonalScheme(BuildContext context, Color seed) {
  final brightness = Theme.of(context).brightness;
  return _schemeCache.putIfAbsent(
    (seed, brightness),
    // Same variant as the app theme, or a status chip would sit on the screen
    // looking washed out beside everything else.
    () => ColorScheme.fromSeed(
      seedColor: seed,
      brightness: brightness,
      dynamicSchemeVariant: DynamicSchemeVariant.vibrant,
    ),
  );
}

ColorScheme statusScheme(BuildContext context, String status) =>
    tonalScheme(context, statusColors[status] ?? Colors.blueGrey);

// ---------------------------------------------------------------------------
// Glass
//
// Material 3 already decides the colours; these only decide how solid a
// surface is painted. A glass surface is the scheme's own surface colour at
// less than full opacity, over a blur of whatever scrolls behind it — so the
// palette stays M3 and only the material changes.
// ---------------------------------------------------------------------------

/// How solid a glass surface is. The one knob to turn: lower is more
/// see-through, and text over it gets harder to read well before it looks bad.
const glassOpacity = 0.72;

/// Blur radius behind a glass surface. Blur is the expensive half of this
/// effect, so it is spent on the few surfaces content actually passes under.
const glassBlur = 18.0;

/// The border that gives glass an edge. Without it a translucent surface has
/// no visible boundary against a busy background.
BorderSide glassEdge(ColorScheme scheme) =>
    BorderSide(color: scheme.primary.withValues(alpha: 0.22));

/// The colour a glass surface is before opacity: the M3 container colour pulled
/// a few percent toward the brand, so surfaces read tinted instead of grey.
Color glassTint(ColorScheme scheme) =>
    Color.alphaBlend(scheme.primary.withValues(alpha: 0.07), scheme.surfaceContainer);

/// A surface painted as glass: blurred backdrop, translucent M3 surface colour,
/// hairline edge.
///
/// Opt-in, because every blur is a saved layer. A [Card] already picks up the
/// translucent colour from the theme; reach for this only where the blur earns
/// its cost — chrome that content scrolls under, a sheet over a list.
class Glass extends StatelessWidget {
  const Glass({
    super.key,
    required this.child,
    this.radius = 16,
    this.padding,
    this.blur = glassBlur,
    this.opacity = glassOpacity,
    this.tint,
    this.width,
  });

  final Widget child;
  final double radius;
  final EdgeInsetsGeometry? padding;
  final double blur;
  final double opacity;

  /// The surface colour before opacity. Defaults to the scheme's own container
  /// colour; pass a tonal container to tint the glass without leaving M3.
  final Color? tint;

  final double? width;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final corners = BorderRadius.circular(radius);
    return ClipRRect(
      borderRadius: corners,
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: blur, sigmaY: blur),
        child: Container(
          width: width,
          padding: padding,
          decoration: BoxDecoration(
            color: (tint ?? glassTint(scheme)).withValues(alpha: opacity),
            borderRadius: corners,
            border: Border.fromBorderSide(glassEdge(scheme)),
          ),
          child: child,
        ),
      ),
    );
  }
}

/// A scroll view's own padding plus whatever the bottom bar covers, so the last
/// row clears the glass instead of hiding under it.
///
/// Only needed by a list that sets [padding] explicitly: a list that leaves it
/// null already takes this inset from the ambient [MediaQuery] by itself.
EdgeInsets listInset(BuildContext context, [EdgeInsets base = EdgeInsets.zero]) =>
    base + EdgeInsets.only(bottom: MediaQuery.paddingOf(context).bottom);

/// Blurs whatever is behind [child] without rounding or tinting it. For chrome
/// that already paints its own background — a nav bar, a rail — where a second
/// container would only double the tint.
class GlassLayer extends StatelessWidget {
  const GlassLayer({super.key, required this.child, this.blur = glassBlur});

  final Widget child;
  final double blur;

  @override
  Widget build(BuildContext context) => ClipRect(
    child: BackdropFilter(
      filter: ImageFilter.blur(sigmaX: blur, sigmaY: blur),
      child: child,
    ),
  );
}

/// The colour wash glass refracts. Without something behind it, a blurred
/// translucent surface is just flat grey — the effect needs a background with
/// variation in it to be visible at all.
///
/// Painted once at the root, under every screen, so scaffolds can stay
/// transparent and let it through.
class GlassBackdrop extends StatelessWidget {
  const GlassBackdrop({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    // Three corners, three ramps of the M3 palette. Two washes leave the middle
    // of a tall screen grey; a third from the opposite corner keeps colour in
    // the frame wherever the user has scrolled to.
    return ColoredBox(
      color: scheme.surface,
      child: Stack(
        children: [
          Positioned.fill(child: _wash(const Alignment(-1, -1), scheme.primary, 0.34)),
          Positioned.fill(child: _wash(const Alignment(1.1, 0.2), scheme.tertiary, 0.28)),
          Positioned.fill(child: _wash(const Alignment(-0.6, 1.1), scheme.secondary, 0.26)),
          child,
        ],
      ),
    );
  }

  Widget _wash(Alignment center, Color color, double strength) => DecoratedBox(
    decoration: BoxDecoration(
      gradient: RadialGradient(
        center: center,
        radius: 1.1,
        colors: [
          color.withValues(alpha: strength),
          color.withValues(alpha: 0),
        ],
      ),
    ),
  );
}

class StatusChip extends StatelessWidget {
  const StatusChip(this.status, {super.key});

  final String status;

  @override
  Widget build(BuildContext context) {
    final scheme = statusScheme(context, status);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: scheme.primaryContainer,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        status.replaceAll('_', ' '),
        style: Theme.of(context).textTheme.labelSmall?.copyWith(
          color: scheme.onPrimaryContainer,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

void showError(BuildContext context, Object error) {
  final message = error is ApiException ? error.message : '$error';
  final scheme = Theme.of(context).colorScheme;
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Text(message, style: TextStyle(color: scheme.onErrorContainer)),
      backgroundColor: scheme.errorContainer,
    ),
  );
}

/// Keeps a column of content readable on a monitor. A no-op on a phone.
class Bounded extends StatelessWidget {
  const Bounded({super.key, required this.child, this.maxWidth = 900});

  final Widget child;
  final double maxWidth;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) => Align(
      alignment: Alignment.topCenter,
      // A tight width, not a cap: children that stretch (form fields, list rows)
      // must still fill the column instead of shrinking to their own content.
      child: SizedBox(
        width: constraints.maxWidth < maxWidth ? constraints.maxWidth : maxWidth,
        child: child,
      ),
    ),
  );
}

/// As wide as it wants on a tablet, as wide as the phone allows on a phone.
/// The 80 is the horizontal inset [AlertDialog] keeps around its content.
double dialogWidth(BuildContext context, [double preferred = 460]) {
  final available = MediaQuery.sizeOf(context).width - 80;
  return available < preferred ? available : preferred;
}

void showDone(BuildContext context, String message) {
  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
}

/// A search box that filters as the user types. [onChanged] fires once the
/// typing pauses, not once per keystroke, so a fast typist costs one request.
class DebouncedSearch extends StatelessWidget {
  const DebouncedSearch({
    super.key,
    required this.controller,
    required this.onChanged,
    this.hintText = 'Search',
    this.trailing = const [],
  });

  final TextEditingController controller;
  final VoidCallback onChanged;
  final String hintText;

  /// Filters that belong beside the box rather than under it.
  final List<Widget> trailing;

  @override
  Widget build(BuildContext context) {
    return Bounded(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 6),
        child: Row(
          children: [
            Expanded(child: _DebouncedSearchBar(this)),
            ...trailing,
          ],
        ),
      ),
    );
  }
}

class _DebouncedSearchBar extends StatefulWidget {
  const _DebouncedSearchBar(this.parent);

  final DebouncedSearch parent;

  @override
  State<_DebouncedSearchBar> createState() => _DebouncedSearchBarState();
}

class _DebouncedSearchBarState extends State<_DebouncedSearchBar> {
  Timer? _timer;

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  void _typed(String _) {
    // The rebuild is for the clear button; only the timer reaches the API.
    setState(() {});
    _timer?.cancel();
    _timer = Timer(const Duration(milliseconds: 300), widget.parent.onChanged);
  }

  void _now() {
    _timer?.cancel();
    widget.parent.onChanged();
  }

  @override
  Widget build(BuildContext context) {
    return SearchBar(
      controller: widget.parent.controller,
      hintText: widget.parent.hintText,
      leading: const Icon(Icons.search),
      onChanged: _typed,
      onSubmitted: (_) => _now(),
      trailing: [
        if (widget.parent.controller.text.isNotEmpty)
          IconButton(
            tooltip: 'Clear',
            icon: const Icon(Icons.close),
            onPressed: () {
              widget.parent.controller.clear();
              setState(_now);
            },
          ),
      ],
    );
  }
}

/// Loads once, rebuilds on pull-to-refresh, shows errors instead of a blank screen.
class Loader<T> extends StatefulWidget {
  const Loader({
    super.key,
    required this.load,
    required this.builder,
    this.controller,
    this.onReload,
  });

  final Future<T> Function() load;
  final Widget Function(BuildContext context, T data, VoidCallback reload) builder;
  final LoaderController? controller;

  /// Fires on every refetch but not the first load. A detail screen uses this to
  /// tell the list it sits beside that the row it is showing has changed.
  final VoidCallback? onReload;

  @override
  State<Loader<T>> createState() => _LoaderState<T>();
}

/// Lets a parent (a FAB, a dialog) ask a [Loader] to fetch again.
class LoaderController extends ChangeNotifier {
  void reload() => notifyListeners();
}

class _LoaderState<T> extends State<Loader<T>> {
  late Future<T> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.load();
    widget.controller?.addListener(_reload);
  }

  @override
  void dispose() {
    widget.controller?.removeListener(_reload);
    super.dispose();
  }

  void _reload() {
    if (!mounted) return;
    // A block, not an arrow: an arrow returns the future it assigns and
    // setState asserts on a callback that returns one.
    setState(() {
      _future = widget.load();
    });
    widget.onReload?.call();
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<T>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    Icons.cloud_off,
                    size: 40,
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                  const SizedBox(height: 12),
                  Text('${snapshot.error}', textAlign: TextAlign.center),
                  const SizedBox(height: 12),
                  FilledButton(onPressed: _reload, child: const Text('Try again')),
                ],
              ),
            ),
          );
        }
        return RefreshIndicator(
          onRefresh: () async => _reload(),
          child: Bounded(child: widget.builder(context, snapshot.data as T, _reload)),
        );
      },
    );
  }
}

class EmptyState extends StatelessWidget {
  const EmptyState(this.message, {super.key, this.icon = Icons.inbox_outlined});

  final String message;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListView(
      children: [
        const SizedBox(height: 120),
        Icon(icon, size: 48, color: theme.colorScheme.onSurfaceVariant),
        const SizedBox(height: 12),
        Text(
          message,
          textAlign: TextAlign.center,
          style: theme.textTheme.bodyMedium?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
      ],
    );
  }
}

/// A table where there is room for one, and a stack of labelled values where
/// there is not. Sized off the space the table actually gets, not the screen,
/// so it also does the right thing inside a narrow [TwoPane] detail pane.
class WideTable extends StatelessWidget {
  const WideTable({super.key, required this.columns, required this.rows});

  final List<String> columns;
  final List<DataRow> rows;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) => constraints.maxWidth < 600
          ? _stacked(context)
          : SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: DataTable(
                columnSpacing: 20,
                headingRowHeight: 40,
                columns: [for (final c in columns) DataColumn(label: Text(c))],
                rows: rows,
              ),
            ),
    );
  }

  /// One block per row, header text moved beside each value, so a phone reads
  /// down the screen instead of dragging the table sideways. Blocks and not
  /// cards: every caller already wraps this in a [Card] and nesting two reads
  /// as a mistake.
  Widget _stacked(BuildContext context) {
    final theme = Theme.of(context);
    final labelStyle = theme.textTheme.labelMedium?.copyWith(
      color: theme.colorScheme.onSurfaceVariant,
    );
    return Column(
      children: [
        for (final (rowIndex, row) in rows.indexed) ...[
          if (rowIndex > 0) const Divider(height: 20),
          for (final (cellIndex, cell) in row.cells.indexed)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // An unnamed column (an actions column) gets the full width.
                  if (cellIndex < columns.length && columns[cellIndex].isNotEmpty)
                    SizedBox(width: 92, child: Text(columns[cellIndex], style: labelStyle)),
                  Expanded(
                    child: Align(alignment: Alignment.centerLeft, child: cell.child),
                  ),
                ],
              ),
            ),
        ],
      ],
    );
  }
}

/// A list beside its detail on a monitor, the list alone on a phone — where
/// tapping a row pushes the detail as its own page, as it always did.
///
/// The list keeps track of what is selected; this only decides where the detail
/// is painted.
class TwoPane extends StatelessWidget {
  const TwoPane({super.key, required this.list, required this.detail, required this.placeholder});

  final Widget list;

  /// Null when nothing is selected yet.
  final Widget? detail;
  final String placeholder;

  /// Below this, one pane does not leave the other a usable column.
  static bool of(BuildContext context) => MediaQuery.sizeOf(context).width >= 1000;

  @override
  Widget build(BuildContext context) {
    if (!of(context)) return list;
    return Row(
      children: [
        SizedBox(width: 380, child: list),
        const VerticalDivider(width: 1),
        Expanded(child: detail ?? EmptyState(placeholder, icon: Icons.ads_click_outlined)),
      ],
    );
  }
}

Future<String?> promptText(
  BuildContext context, {
  required String title,
  String? hint,
  String initial = '',
  bool obscure = false,
  TextInputType? keyboard,
}) {
  final controller = TextEditingController(text: initial);
  return showDialog<String>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(title),
      content: TextField(
        controller: controller,
        autofocus: true,
        obscureText: obscure,
        keyboardType: keyboard,
        decoration: InputDecoration(hintText: hint),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: () => Navigator.pop(context, controller.text),
          child: const Text('Save'),
        ),
      ],
    ),
  );
}

/// The tenant a new row belongs to, as an extra payload field.
///
/// Creating from nothing needs an organisation, and a superuser reading across
/// every tenant has none of its own. Rather than make it step into one first,
/// the create names the tenant in its own payload. Everyone else adds nothing:
/// the server takes the tenant from the account. Null means the superuser
/// dismissed the picker, so the caller should stop.
Future<Map<String, dynamic>?> orgField(BuildContext context, Api api, {String? kind}) async {
  if (!api.actsForAnyTenant) return const <String, dynamic>{};
  final List<Map<String, dynamic>> rows;
  try {
    rows = (await api.list(
      '/organizations/',
    )).where((row) => kind == null || row['kind'] == kind).toList();
  } catch (error) {
    if (context.mounted) showError(context, error);
    return null;
  }
  if (!context.mounted) return null;
  final id = await showDialog<int>(
    context: context,
    builder: (context) => SimpleDialog(
      title: const Text('Which organisation?'),
      children: [
        if (rows.isEmpty)
          const Padding(
            padding: EdgeInsets.fromLTRB(24, 0, 24, 16),
            child: Text('No organisation to create this for.'),
          ),
        for (final row in rows)
          ListTile(
            leading: const Icon(Icons.apartment),
            title: Text('${row['name']}'),
            subtitle: Text('${row['kind']} · ${row['phone']}'),
            onTap: () => Navigator.pop(context, row['id'] as int),
          ),
      ],
    ),
  );
  return id == null ? null : {'organization': id};
}

/// Prints a document: one press, the platform's print dialog, paper.
///
/// The app draws no paper of its own. It fetches the server's PDF of the sheet
/// over the ordinary API — token and all — and hands the bytes to the printer,
/// so nothing detours through a browser tab or a link the user has to open.
/// The same dialog is where a device offers "save as PDF", so there is no
/// second button for that.
///
/// [path] is the row's own detail path, e.g. `/invoices/12/`, or a list path
/// for a ledger, e.g. `/stock-movements/`. [query] is what narrows that ledger,
/// so the sheet covers what the screen was showing.
class PrintButton extends StatefulWidget {
  const PrintButton({
    super.key,
    required this.api,
    required this.path,
    this.query,
    this.tooltip = 'Print',
    this.label,
    this.name = 'Document',
  });

  final Api api;
  final String path;
  final Map<String, dynamic>? query;
  final String tooltip;

  /// Set where the button sits among other actions rather than in an app bar,
  /// and an icon alone would not say what it prints.
  final String? label;

  /// What the print job — and any file saved from it — is called.
  final String name;

  @override
  State<PrintButton> createState() => _PrintButtonState();
}

class _PrintButtonState extends State<PrintButton> {
  bool _busy = false;

  Future<void> _print() async {
    setState(() => _busy = true);
    try {
      await Printing.layoutPdf(
        name: widget.name,
        onLayout: (_) => widget.api.bytes('${widget.path}print/', widget.query),
      );
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final icon = _busy
        ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
        : const Icon(Icons.print_outlined);
    final onPressed = _busy ? null : _print;
    return widget.label == null
        ? IconButton(tooltip: widget.tooltip, onPressed: onPressed, icon: icon)
        : FilledButton.tonalIcon(onPressed: onPressed, icon: icon, label: Text(widget.label!));
  }
}

Future<bool> confirm(BuildContext context, String title, String message) async {
  final answer = await showDialog<bool>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(title),
      content: Text(message),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('No')),
        FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Yes')),
      ],
    ),
  );
  return answer ?? false;
}
