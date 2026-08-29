import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../main.dart';
import '../ui.dart';
import 'requests.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  final _controller = LoaderController();

  /// How many months of trend to ask for. The server caps this.
  int _months = 6;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(api.organization?['name'] ?? 'Dashboard'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh),
            onPressed: () {
              // The profile behind the title and the figures below it are two
              // fetches; the button says refresh, so it does both.
              api.refreshProfile();
              _controller.reload();
            },
          ),
        ],
      ),
      body: Loader<Map<String, dynamic>>(
        controller: _controller,
        load: () async =>
            await api.get('/dashboard/', {'months': _months}) as Map<String, dynamic>,
        builder: (context, data, reload) {
          final byStatus = (data['requests_by_status'] as Map).cast<String, dynamic>();
          final recent = (data['recent'] as List).cast<Map<String, dynamic>>();
          final lowStock = (data['low_stock'] as List).cast<Map<String, dynamic>>();
          final spend = (data['spend_by_department'] as List? ?? []).cast<Map<String, dynamic>>();
          final monthly = (data['requests_monthly'] as List? ?? []).cast<Map<String, dynamic>>();
          return ListView(
            // Explicit padding, so the bar's inset has to be added by hand.
            padding: listInset(context, const EdgeInsets.all(12)),
            children: [
              // As many tiles per row as fit at a readable size, and never
              // fewer than two: one column wastes a phone, and the fixed 168
              // this used to be left a ragged tail of dead space on a monitor.
              LayoutBuilder(
                builder: (context, constraints) {
                  const spacing = 10.0;
                  final columns = (constraints.maxWidth / 190).floor().clamp(2, 5).toInt();
                  final tile = (constraints.maxWidth - spacing * (columns - 1)) / columns;
                  return Wrap(
                    spacing: spacing,
                    runSpacing: spacing,
                    children: [
                      _Stat(
                        width: tile,
                        label: api.isSupplier
                            ? 'Awaiting your decision'
                            : 'Awaiting verification',
                        value: '${data['awaiting_action']}',
                        seed: Colors.orange,
                        icon: Icons.pending_actions,
                      ),
                      _Stat(
                        width: tile,
                        label: 'Requests total',
                        value: '${data['requests_total']}',
                        seed: Colors.blue,
                        icon: Icons.assignment,
                      ),
                      _Stat(
                        width: tile,
                        label: 'In transit',
                        value: '${data['deliveries_in_transit']}',
                        seed: Colors.indigo,
                        icon: Icons.local_shipping,
                      ),
                      _Stat(
                        width: tile,
                        label: api.isSupplier ? 'Receivable' : 'Payable',
                        value: amount(data['invoices_outstanding']),
                        seed: Colors.red,
                        icon: Icons.receipt_long,
                      ),
                      if ((data['invoices_overdue'] as int? ?? 0) > 0)
                        _Stat(
                          width: tile,
                          label: '${data['invoices_overdue']} overdue',
                          value: amount(data['invoices_overdue_amount']),
                          seed: Colors.deepOrange,
                          icon: Icons.event_busy,
                        ),
                      if ((data['payments_pending'] as int? ?? 0) > 0)
                        _Stat(
                          width: tile,
                          label: api.isSupplier
                              ? 'Payments to confirm'
                              : 'Payments awaiting confirmation',
                          value: '${data['payments_pending']}',
                          seed: Colors.amber,
                          icon: Icons.payments_outlined,
                        ),
                      _Stat(
                        width: tile,
                        label: api.isSupplier ? 'Hospitals' : 'Companies',
                        value: '${data['partners']}',
                        seed: Colors.teal,
                        icon: Icons.handshake,
                      ),
                      if (data['catalogue_items'] != null)
                        _Stat(
                          width: tile,
                          label: 'Catalogue items',
                          value: '${data['catalogue_items']}',
                          seed: Colors.purple,
                          icon: Icons.medication,
                        ),
                    ],
                  );
                },
              ),
              const SizedBox(height: 16),
              if (byStatus.isNotEmpty) ...[
                const _SectionTitle('Requests by status'),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: _StatusDonut(byStatus: byStatus),
                  ),
                ),
              ],
              if (monthly.isNotEmpty) ...[
                const _SectionTitle('Requests per month'),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(12, 12, 12, 12),
                    child: Column(
                      children: [
                        Align(
                          alignment: Alignment.centerRight,
                          child: SegmentedButton<int>(
                            showSelectedIcon: false,
                            style: const ButtonStyle(visualDensity: VisualDensity.compact),
                            segments: const [
                              ButtonSegment(value: 3, label: Text('3m')),
                              ButtonSegment(value: 6, label: Text('6m')),
                              ButtonSegment(value: 12, label: Text('12m')),
                            ],
                            selected: {_months},
                            onSelectionChanged: (choice) {
                              setState(() => _months = choice.first);
                              _controller.reload();
                            },
                          ),
                        ),
                        const SizedBox(height: 12),
                        _MonthlyBars(
                          rows: monthly,
                          onTap: (month) => Navigator.push(
                            context,
                            MaterialPageRoute<void>(builder: (_) => RequestsScreen(month: month)),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ],
              if (spend.isNotEmpty) ...[
                const _SectionTitle('Invoiced by department'),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    child: Column(
                      children: [
                        for (final row in spend)
                          _DepartmentBar(
                            name: '${row['department_name']}',
                            total: qty(row['total']),
                            paid: qty(row['paid']),
                            // Bars share one scale, so the widest is the biggest
                            // department rather than every row reading as full.
                            scale: spend
                                .map((r) => qty(r['total']))
                                .reduce((a, b) => a > b ? a : b),
                            onTap: () => Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                builder: (_) =>
                                    RequestsScreen(department: row['department_id'] as int),
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                ),
              ],
              if (lowStock.isNotEmpty) ...[
                const _SectionTitle('Low stock (under 10)'),
                Card(
                  child: Column(
                    children: [
                      for (final item in lowStock)
                        ListTile(
                          dense: true,
                          leading: Icon(
                            Icons.warning_amber,
                            color: Theme.of(context).colorScheme.error,
                          ),
                          title: Text('${item['generic_name']}'),
                          trailing: Text('${qtyText(item['stock_qty'])} left'),
                        ),
                    ],
                  ),
                ),
              ],
              const _SectionTitle('Recent requests'),
              if (recent.isEmpty)
                Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text(
                    'Nothing yet.',
                    style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant),
                  ),
                ),
              for (final row in recent)
                Card(
                  child: ListTile(
                    title: Text('${row['reference']} · ${row['item_count']} item(s)'),
                    subtitle: Text(
                      api.isSupplier
                          ? '${row['hospital_name']} · ${formatDate(row['submitted_at'])}'
                          : '${row['supplier_name']} · ${formatDate(row['created_at'])}',
                    ),
                    trailing: StatusChip('${row['status']}'),
                    onTap: () async {
                      await Navigator.push(
                        context,
                        MaterialPageRoute<void>(
                          builder: (_) => RequestDetailScreen(requisitionId: row['id'] as int),
                        ),
                      );
                      reload();
                    },
                  ),
                ),
            ],
          );
        },
      ),
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({
    required this.width,
    required this.label,
    required this.value,
    required this.seed,
    required this.icon,
  });

  /// Decided by the row, which is the only thing that knows how many tiles
  /// have to share the space.
  final double width;

  final String label;
  final String value;
  final Color seed;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final scheme = tonalScheme(context, seed);
    final textTheme = Theme.of(context).textTheme;
    // Glass rather than a filled card: this row is the top of the screen and
    // stays there, so it is worth the blur that the list rows below are not.
    // The tint keeps each tile's tonal colour; only the material changes.
    // One reading, not two: a screen reader announcing '4' and then 'Awaiting
    // your decision' as separate nodes leaves the number floating.
    return Semantics(
      label: '$label: $value',
      excludeSemantics: true,
      child: Glass(
        width: width,
        tint: scheme.primaryContainer,
        // Opaque enough that the numbers stay readable over a moving backdrop.
        opacity: 0.82,
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, color: scheme.onPrimaryContainer, size: 20),
            const SizedBox(height: 6),
            FittedBox(
              fit: BoxFit.scaleDown,
              alignment: Alignment.centerLeft,
              child: Text(
                value,
                style: textTheme.headlineSmall?.copyWith(color: scheme.onPrimaryContainer),
              ),
            ),
            Text(label, style: textTheme.labelMedium?.copyWith(color: scheme.onPrimaryContainer)),
          ],
        ),
      ),
    );
  }
}

/// Requests by status as a donut with a legend beside it: the ring shows the
/// mix, the legend keeps the exact counts the chips used to carry.
class _StatusDonut extends StatelessWidget {
  const _StatusDonut({required this.byStatus});

  final Map<String, dynamic> byStatus;

  @override
  Widget build(BuildContext context) {
    final textTheme = Theme.of(context).textTheme;
    final slices = [
      for (final entry in byStatus.entries)
        (
          label: entry.key.replaceAll('_', ' '),
          value: qty(entry.value),
          color: statusScheme(context, entry.key).primary,
          icon: statusIcon(entry.key),
        ),
    ]..sort((a, b) => b.value.compareTo(a.value));
    final total = slices.fold<double>(0, (sum, slice) => sum + slice.value);
    if (total <= 0) return const SizedBox.shrink();
    return Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        // The ring paints on a canvas, which carries no semantics of its own.
        // The legend beside it already reads the counts out, so this only has
        // to name what the picture is.
        Semantics(
          label: 'Requests by status, ${total.round()} in total',
          excludeSemantics: true,
          child: SizedBox(
            width: 120,
            height: 120,
            child: CustomPaint(
              painter: _DonutPainter(slices: slices, total: total),
              child: Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('${total.round()}', style: textTheme.headlineSmall),
                    Text('total', style: textTheme.labelSmall),
                  ],
                ),
              ),
            ),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final slice in slices)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 3),
                  child: Row(
                    children: [
                      // The status's own icon rather than a plain dot: the
                      // ring's slices are told apart by hue, which approved
                      // green and rejected red do not survive.
                      Icon(slice.icon, size: 14, color: slice.color),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          slice.label,
                          overflow: TextOverflow.ellipsis,
                          style: textTheme.bodySmall,
                        ),
                      ),
                      Text(
                        '${slice.value.round()} · ${(slice.value / total * 100).round()}%',
                        style: textTheme.labelMedium,
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

class _DonutPainter extends CustomPainter {
  _DonutPainter({required this.slices, required this.total});

  final List<({String label, double value, Color color, IconData icon})> slices;
  final double total;

  /// The hairline between two slices, in radians. Dropped on a slice too thin
  /// to survive it, which would otherwise paint backwards.
  static const gap = 0.03;

  @override
  void paint(Canvas canvas, Size size) {
    final stroke = size.shortestSide * 0.2;
    final rect = Rect.fromCircle(
      center: size.center(Offset.zero),
      radius: (size.shortestSide - stroke) / 2,
    );
    final paint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke;
    // Twelve o'clock, clockwise: where a ring is read from.
    var start = -math.pi / 2;
    for (final slice in slices) {
      final sweep = slice.value / total * 2 * math.pi;
      final inset = sweep > gap * 2 && slices.length > 1 ? gap : 0.0;
      canvas.drawArc(rect, start + inset / 2, sweep - inset, false, paint..color = slice.color);
      start += sweep;
    }
  }

  @override
  bool shouldRepaint(_DonutPainter old) => old.slices != slices || old.total != total;
}

/// Requests per month as a column chart. Six buckets, so they are drawn as
/// widgets rather than painted: a Row of bars sized against the busiest month.
class _MonthlyBars extends StatelessWidget {
  const _MonthlyBars({required this.rows, required this.onTap});

  final List<Map<String, dynamic>> rows;

  /// Opens the requests behind one bar, by the ISO date of that month.
  final void Function(String month) onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final textTheme = Theme.of(context).textTheme;
    final peak = rows.map((row) => qty(row['count'])).reduce(math.max);
    return SizedBox(
      height: 140 * textScale(context),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          for (final row in rows)
            Expanded(
              child: Semantics(
                button: true,
                label:
                    '${monthLabel('${row['month']}')}: '
                    '${qty(row['count']).round()} requests',
                excludeSemantics: true,
                child: InkWell(
                  // An empty month opens an empty list, which is an answer too.
                  onTap: () => onTap('${row['month']}'),
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      Text('${qty(row['count']).round()}', style: textTheme.labelSmall),
                      const SizedBox(height: 2),
                      Expanded(
                        child: FractionallySizedBox(
                          // The tallest bar fills the plot; an empty month keeps a
                          // sliver so the column still reads as a bar.
                          heightFactor: peak > 0 ? math.max(qty(row['count']) / peak, 0.02) : 0.02,
                          alignment: Alignment.bottomCenter,
                          widthFactor: 0.55,
                          child: DecoratedBox(
                            decoration: BoxDecoration(
                              color: scheme.primary,
                              borderRadius: const BorderRadius.vertical(top: Radius.circular(4)),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(monthLabel('${row['month']}', short: true), style: textTheme.labelSmall),
                    ],
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// One department's invoiced total as a bar, with the paid part filled in.
class _DepartmentBar extends StatelessWidget {
  const _DepartmentBar({
    required this.name,
    required this.total,
    required this.paid,
    required this.scale,
    required this.onTap,
  });

  final String name;
  final double total;
  final double paid;
  final double scale;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final textTheme = Theme.of(context).textTheme;
    return Semantics(
      button: true,
      label: '$name: ${amount(total)} invoiced, ${amount(paid)} paid',
      excludeSemantics: true,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(name, overflow: TextOverflow.ellipsis)),
                  const SizedBox(width: 8),
                  Text(amount(total), style: textTheme.titleSmall),
                ],
              ),
              const SizedBox(height: 6),
              ClipRRect(
                borderRadius: BorderRadius.circular(4),
                child: Container(
                  height: 8,
                  color: scheme.surfaceContainerHighest,
                  child: FractionallySizedBox(
                    alignment: Alignment.centerLeft,
                    widthFactor: scale > 0 ? (total / scale).clamp(0.0, 1.0) : 0,
                    child: Container(
                      color: scheme.primaryContainer,
                      child: FractionallySizedBox(
                        alignment: Alignment.centerLeft,
                        widthFactor: total > 0 ? (paid / total).clamp(0.0, 1.0) : 0,
                        child: Container(color: scheme.primary),
                      ),
                    ),
                  ),
                ),
              ),
              const SizedBox(height: 4),
              Text('${amount(paid)} paid', style: textTheme.bodySmall),
            ],
          ),
        ),
      ),
    );
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 16, 4, 8),
      child: Text(text, style: Theme.of(context).textTheme.titleMedium),
    );
  }
}
