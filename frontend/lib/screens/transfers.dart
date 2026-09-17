// Stock moving between two units of one department, inside one hospital.
//
// Nothing is bought here, so there is no company, no price and no invoice: the
// goods are already the hospital's and only the shelf they sit on changes. The
// four steps mirror the server's — see the transfer section of core/services.py
// — and each one belongs to one side of the trade.

import 'package:flutter/material.dart';

import '../api.dart';
import '../main.dart';
import '../ui.dart';

/// The units of the caller's own hospital. [department] narrows it to the one
/// department a transfer may cross, which is the only pairing the server takes.
Future<List<Map<String, dynamic>>> _units(Api api, {Object? department, String query = ''}) =>
    api.list('/units/', {
      if (department != null) 'department': '$department',
      if (query.isNotEmpty) 'search': query,
    });

List<DropdownMenuEntry<int?>> _unitEntries(List<Map<String, dynamic>> rows) => [
  for (final row in rows) DropdownMenuEntry(value: row['id'] as int, label: '${row['full_name']}'),
];

/// What one unit's shelf holds, item by item. The same balances the ledger
/// shows, asked for one unit — which is what a transfer may be issued against.
Future<List<Map<String, dynamic>>> _held(Api api, Object unit, {String query = ''}) async {
  final rows = await api.list('/stock-movements/balances/', {
    'unit': '$unit',
    if (query.isNotEmpty) 'search': query,
  });
  return [
    for (final row in rows)
      if (qty(row['balance']) > 0) row,
  ];
}

/// Product id to what the holding unit has of it, for the screens that decide a
/// quantity: agreeing to lend what is not there only postpones the refusal.
Future<Map<Object, double>> _heldByProduct(Api api, Object unit) async => {
  for (final row in await _held(api, unit)) row['product'] as Object: qty(row['balance']),
};

const _transferFilters = {
  '': 'All',
  'REQUESTED': 'Asked',
  'APPROVED': 'Agreed',
  'ISSUED': 'Handed over',
  'RECEIVED': 'Done',
  'REJECTED,CANCELLED': 'Refused',
};

class TransfersScreen extends StatefulWidget {
  const TransfersScreen({super.key});

  @override
  State<TransfersScreen> createState() => _TransfersScreenState();
}

class _TransfersScreenState extends State<TransfersScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();
  String _status = '';
  int? _selected;

  /// Narrow to the caller's own unit, both sides of its trade. Only offered to
  /// an account that has one; an administrator reads the whole hospital.
  bool _mineOnly = false;
  bool _startedOnMine = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Somebody who works on a unit is nearly always after their own transfers.
    if (_startedOnMine) return;
    _startedOnMine = true;
    _mineOnly = ApiScope.of(context).unitId != null;
  }

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  Future<List<Map<String, dynamic>>> _load() {
    final api = ApiScope.of(context);
    return api.listAll('/transfers/', {
      'search': _search.text,
      'status': _status,
      if (_mineOnly && api.unitId != null) 'unit': '${api.unitId}',
    });
  }

  Future<void> _ask() async {
    final created = await showDialog<int>(context: context, builder: (_) => const _AskDialog());
    if (created == null || !mounted) return;
    if (TwoPane.of(context)) {
      setState(() => _selected = created);
      _controller.reload();
      return;
    }
    await Navigator.push(
      context,
      MaterialPageRoute<void>(builder: (_) => TransferDetailScreen(transferId: created)),
    );
    _controller.reload();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final wide = TwoPane.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Unit transfers')),
      floatingActionButton: api.canActAsHospital
          ? FloatingActionButton.extended(
              onPressed: _ask,
              icon: const Icon(Icons.swap_horiz),
              label: const Text('Ask a unit'),
            )
          : null,
      body: TwoPane(
        placeholder: 'Pick a transfer to see what was asked for.',
        detail: _selected == null
            ? null
            : TransferDetailScreen(
                key: ValueKey(_selected),
                transferId: _selected!,
                onChanged: _controller,
              ),
        list: Column(
          children: [
            DebouncedSearch(
              controller: _search,
              onChanged: _controller.reload,
              hintText: 'Search reference, unit, note',
            ),
            Bounded(
              child: SizedBox(
                height: 48 * textScale(context),
                child: ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    if (api.unitId != null)
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: FilterChip(
                          avatar: const Icon(Icons.person_pin_circle_outlined, size: 18),
                          label: const Text('My unit'),
                          selected: _mineOnly,
                          onSelected: (value) {
                            setState(() => _mineOnly = value);
                            _controller.reload();
                          },
                        ),
                      ),
                    for (final entry in _transferFilters.entries)
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: FilterChip(
                          label: Text(entry.value),
                          selected: _status == entry.key,
                          onSelected: (_) {
                            setState(() => _status = entry.key);
                            _controller.reload();
                          },
                        ),
                      ),
                  ],
                ),
              ),
            ),
            Expanded(
              child: Loader<List<Map<String, dynamic>>>(
                controller: _controller,
                load: _load,
                builder: (context, rows, reload) {
                  if (rows.isEmpty) {
                    return const EmptyState(
                      'No transfer between units yet.',
                      icon: Icons.swap_horiz,
                    );
                  }
                  return ListView.separated(
                    padding: listInset(context),
                    itemCount: rows.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final row = rows[index];
                      final id = row['id'] as int;
                      return ListTile(
                        selected: wide && id == _selected,
                        leading: const Icon(Icons.swap_horiz),
                        title: Text('${row['from_unit_name']} → ${row['to_unit_name']}'),
                        subtitle: Text(
                          '${row['reference']} · ${row['department_name']} · '
                          '${row['item_count']} item(s)\n${formatDate(row['created_at'] as String?)}',
                        ),
                        isThreeLine: true,
                        trailing: StatusChip('${row['status']}'),
                        onTap: () async {
                          if (wide) {
                            setState(() => _selected = id);
                            return;
                          }
                          await Navigator.push(
                            context,
                            MaterialPageRoute<void>(
                              builder: (_) => TransferDetailScreen(transferId: id),
                            ),
                          );
                          reload();
                        },
                      );
                    },
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class TransferDetailScreen extends StatefulWidget {
  const TransferDetailScreen({super.key, required this.transferId, this.onChanged});

  final int transferId;

  /// The list this detail is pinned beside, so it refetches when a step taken
  /// here changes the row it is showing.
  final LoaderController? onChanged;

  @override
  State<TransferDetailScreen> createState() => _TransferDetailScreenState();
}

class _TransferDetailScreenState extends State<TransferDetailScreen> {
  final _controller = LoaderController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  String get _path => '/transfers/${widget.transferId}/';

  /// Whether this account may act for [unit]. The server decides for real; this
  /// only keeps buttons off a screen that would be refused for pressing them.
  bool _actsFor(Api api, Object? unit) =>
      api.isAdmin || api.isSuperuser || (unit != null && api.unitId == unit);

  Future<void> _step(String name, Map<String, dynamic> body, VoidCallback reload) async {
    try {
      await ApiScope.of(context).post('$_path$name/', body);
      reload();
    } catch (error) {
      if (mounted) showError(context, error);
    }
  }

  Future<void> _dialogStep(
    Widget Function(BuildContext) build,
    VoidCallback reload,
  ) async {
    final done = await showDialog<bool>(context: context, builder: build);
    if (done == true) reload();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Transfer'),
        actions: [
          // The note that travels with the cartons and is signed at both ends.
          PrintButton(
            api: api,
            path: _path,
            tooltip: 'Print this transfer note',
            name: 'Stock transfer note',
          ),
        ],
      ),
      body: Loader<Map<String, dynamic>>(
        controller: _controller,
        onReload: widget.onChanged?.reload,
        load: () async => await api.get(_path) as Map<String, dynamic>,
        builder: (context, data, reload) {
          final lines = (data['lines'] as List).cast<Map<String, dynamic>>();
          final status = '${data['status']}';
          final holds = _actsFor(api, data['from_unit']);
          final asks = _actsFor(api, data['to_unit']);
          return ListView(
            padding: listInset(context, const EdgeInsets.all(12)),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(
                            '${data['reference']}',
                            style: Theme.of(context).textTheme.titleLarge,
                          ),
                          StatusChip(status),
                        ],
                      ),
                      const SizedBox(height: 8),
                      _kv('Department', '${data['department_name']}'),
                      _kv('From', '${data['from_unit_name']}'),
                      _kv('To', '${data['to_unit_name']}'),
                      _kv(
                        'Asked',
                        '${formatDate(data['created_at'] as String?)}'
                        ' · ${data['requested_by_name'] ?? '-'}',
                      ),
                      if ('${data['note']}'.isNotEmpty) _kv('Note', '${data['note']}'),
                      if (data['decided_at'] != null)
                        _kv(
                          status == 'REJECTED'
                              ? 'Refused'
                              : status == 'CANCELLED'
                              ? 'Withdrawn'
                              : 'Agreed',
                          '${formatDate(data['decided_at'] as String?)}'
                          ' · ${data['decided_by_name'] ?? '-'}',
                        ),
                      if ('${data['decision_note']}'.isNotEmpty)
                        _kv('Said', '${data['decision_note']}'),
                      if (data['issued_at'] != null)
                        _kv(
                          'Handed over',
                          '${formatDate(data['issued_at'] as String?)}'
                          ' · ${data['issued_by_name'] ?? '-'}',
                        ),
                      if ('${data['issue_note']}'.isNotEmpty)
                        _kv('On issue', '${data['issue_note']}'),
                      if (data['received_at'] != null)
                        _kv(
                          'Received',
                          '${formatDate(data['received_at'] as String?)}'
                          ' · ${data['received_by_name'] ?? '-'}',
                        ),
                      if ('${data['receipt_note']}'.isNotEmpty)
                        _kv('On receipt', '${data['receipt_note']}'),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(8),
                  child: WideTable(
                    columns: const ['Item', 'Asked', 'Agreed', 'Given', 'Got'],
                    rows: [
                      for (final line in lines)
                        DataRow(
                          cells: [
                            DataCell(
                              SizedBox(
                                width: 180,
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Text(
                                      '${line['product_name']}',
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                    if ('${line['shortfall_reason']}'.isNotEmpty)
                                      Text(
                                        'Short: ${line['shortfall_reason']}',
                                        style: Theme.of(context).textTheme.labelSmall?.copyWith(
                                          color: Theme.of(context).colorScheme.error,
                                        ),
                                      ),
                                  ],
                                ),
                              ),
                            ),
                            DataCell(Text(qtyText(line['qty_requested']))),
                            DataCell(Text(qtyText(line['qty_approved']))),
                            DataCell(Text(qtyText(line['qty_issued']))),
                            DataCell(Text(qtyText(line['qty_received']))),
                          ],
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (status == 'REQUESTED' && holds) ...[
                    FilledButton.icon(
                      icon: const Icon(Icons.handshake_outlined),
                      label: const Text('Agree quantities'),
                      onPressed: () => _dialogStep(
                        (_) => _LineQuantityDialog.approve(
                          path: _path,
                          lines: lines,
                          fromUnit: data['from_unit'],
                        ),
                        reload,
                      ),
                    ),
                    OutlinedButton.icon(
                      icon: const Icon(Icons.block),
                      label: const Text('Refuse'),
                      onPressed: () async {
                        final reason = await promptText(
                          context,
                          title: 'Why is this refused?',
                          hint: 'We are short ourselves',
                        );
                        if (reason == null || !context.mounted) return;
                        await _step('reject', {'reason': reason}, reload);
                      },
                    ),
                  ],
                  if (status == 'APPROVED' && holds)
                    FilledButton.icon(
                      icon: const Icon(Icons.outbox_outlined),
                      label: const Text('Hand over'),
                      onPressed: () => _dialogStep(
                        (_) => _LineQuantityDialog.issue(
                          path: _path,
                          lines: lines,
                          fromUnit: data['from_unit'],
                        ),
                        reload,
                      ),
                    ),
                  if ((status == 'REQUESTED' || status == 'APPROVED') && asks)
                    OutlinedButton.icon(
                      icon: const Icon(Icons.undo),
                      label: const Text('Withdraw'),
                      onPressed: () async {
                        if (!await confirm(
                          context,
                          'Withdraw',
                          'Take this request back? Nothing has moved yet.',
                        )) {
                          return;
                        }
                        if (!context.mounted) return;
                        final reason = await promptText(
                          context,
                          title: 'Why? (optional)',
                          hint: 'Found some in the cupboard',
                        );
                        if (reason == null || !context.mounted) return;
                        await _step('cancel', {'reason': reason}, reload);
                      },
                    ),
                  if (status == 'ISSUED' && asks)
                    FilledButton.icon(
                      icon: const Icon(Icons.inventory_outlined),
                      label: const Text('Confirm what arrived'),
                      onPressed: () => _dialogStep(
                        (_) => _LineQuantityDialog.receive(path: _path, lines: lines),
                        reload,
                      ),
                    ),
                ],
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _kv(String key, String value) => Builder(
    builder: (context) {
      final theme = Theme.of(context);
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 110,
              child: Text(
                key,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
            ),
            Expanded(child: Text(value, style: theme.textTheme.bodyMedium)),
          ],
        ),
      );
    },
  );
}

/// One unit asking another in its department for stock.
///
/// The unit being asked is picked first, because what it holds is the whole
/// list of what may be asked for — a request for something that was never on
/// that shelf is one the other unit can only refuse.
class _AskDialog extends StatefulWidget {
  const _AskDialog();

  @override
  State<_AskDialog> createState() => _AskDialogState();
}

class _AskDialogState extends State<_AskDialog> {
  final _qty = TextEditingController();
  final _note = TextEditingController();
  Future<List<Map<String, dynamic>>>? _allUnits;

  int? _toUnit;
  int? _fromUnit;
  Object? _department;
  Object? _product;
  String _productLabel = '';
  bool _busy = false;

  /// What has been added so far: product id to (label, quantity).
  final _items = <Object, (String, double)>{};

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_allUnits != null) return;
    final api = ApiScope.of(context);
    _allUnits = _units(api);
    // An account that sits on a unit is asking on its behalf; anyone else says
    // which unit is asking.
    _toUnit = api.unitId;
  }

  @override
  void dispose() {
    _qty.dispose();
    _note.dispose();
    super.dispose();
  }

  /// The asking unit decides which department this transfer sits in, and so
  /// which units may be asked. Changing it drops anything already picked from
  /// the old one.
  void _pickAsker(List<Map<String, dynamic>> units, int? id) {
    final row = units.where((unit) => unit['id'] == id).firstOrNull;
    setState(() {
      _toUnit = id;
      _department = row?['department'];
      _fromUnit = null;
      _product = null;
      _items.clear();
    });
  }

  void _add() {
    final quantity = parseQty(_qty.text);
    if (_product == null || quantity == null || quantity < halfUnit) {
      showError(context, 'Pick an item and a quantity. $badQtyMessage');
      return;
    }
    setState(() {
      // The same item added twice is one line of more of it, as it is on a
      // request — which is also what the server would do with it.
      final existing = _items[_product!];
      _items[_product!] = (existing?.$1 ?? _productLabel, (existing?.$2 ?? 0) + quantity);
      _qty.clear();
      _product = null;
    });
  }

  Future<void> _send() async {
    if (_toUnit == null || _fromUnit == null || _items.isEmpty) {
      showError(context, 'Say which unit is asking, which is being asked, and for what.');
      return;
    }
    setState(() => _busy = true);
    try {
      final created = await ApiScope.of(context).post('/transfers/', {
        'from_unit': _fromUnit,
        'to_unit': _toUnit,
        'items': [
          for (final entry in _items.entries) {'product': entry.key, 'qty': entry.value.$2},
        ],
        'note': _note.text.trim(),
      });
      if (mounted) Navigator.pop(context, (created as Map<String, dynamic>)['id'] as int);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return AlertDialog(
      title: const Text('Ask another unit'),
      content: SizedBox(
        width: dialogWidth(context, 460),
        child: FutureBuilder<List<Map<String, dynamic>>>(
          future: _allUnits!,
          builder: (context, snapshot) {
            if (snapshot.connectionState != ConnectionState.done) {
              return const Spinner(height: 80);
            }
            if (snapshot.hasError) return Text('${snapshot.error}');
            final units = snapshot.data!;
            if (units.isEmpty) {
              return const Text(
                'This hospital keeps no units yet, so there is nothing to transfer between.',
              );
            }
            // Set on the first build for an account that already sits on a unit.
            if (_toUnit != null && _department == null) {
              _department = units.where((unit) => unit['id'] == _toUnit).firstOrNull?['department'];
            }
            final siblings = [
              for (final unit in units)
                if (unit['department'] == _department && unit['id'] != _toUnit) unit,
            ];
            return SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  PickerField<int?>(
                    label: 'Asking unit',
                    value: _toUnit,
                    entries: _unitEntries(units),
                    search: (query) async => _unitEntries(await _units(api, query: query)),
                    onSelected: (value) => _pickAsker(units, value),
                  ),
                  const SizedBox(height: 12),
                  PickerField<int?>(
                    label: 'Unit being asked',
                    enabled: _toUnit != null,
                    value: _fromUnit,
                    helperText: 'Only units of the same department',
                    entries: _unitEntries(siblings),
                    search: (query) async => _unitEntries([
                      for (final unit in await _units(api, department: _department, query: query))
                        if (unit['id'] != _toUnit) unit,
                    ]),
                    onSelected: (value) => setState(() {
                      _fromUnit = value;
                      _product = null;
                      _items.clear();
                    }),
                  ),
                  const SizedBox(height: 12),
                  if (_fromUnit != null)
                    FutureBuilder<List<Map<String, dynamic>>>(
                      // Keyed on the unit, so picking another one asks again
                      // instead of offering the last one's shelf.
                      key: ValueKey(_fromUnit),
                      future: _held(api, _fromUnit!),
                      builder: (context, stock) {
                        if (stock.connectionState != ConnectionState.done) {
                          return const Padding(
                            padding: EdgeInsets.all(12),
                            child: CircularProgressIndicator(),
                          );
                        }
                        final rows = stock.data ?? const <Map<String, dynamic>>[];
                        if (rows.isEmpty) {
                          return const Text('That unit holds nothing at the moment.');
                        }
                        return Column(
                          children: [
                            PickerField<Object?>(
                              label: 'Item',
                              value: _product,
                              entries: _stockEntries(rows),
                              search: (query) async =>
                                  _stockEntries(await _held(api, _fromUnit!, query: query)),
                              onSelected: (value) => setState(() {
                                _product = value;
                                _productLabel =
                                    '${rows.where((row) => row['product'] == value).firstOrNull?['product_name'] ?? value}';
                              }),
                            ),
                            const SizedBox(height: 12),
                            Row(
                              children: [
                                Expanded(
                                  child: TextField(
                                    controller: _qty,
                                    keyboardType: qtyKeyboard(),
                                    decoration: const InputDecoration(
                                      labelText: 'Quantity',
                                      isDense: true,
                                    ),
                                  ),
                                ),
                                const SizedBox(width: 8),
                                FilledButton.tonal(onPressed: _add, child: const Text('Add')),
                              ],
                            ),
                          ],
                        );
                      },
                    ),
                  if (_items.isNotEmpty) ...[
                    const SizedBox(height: 12),
                    Wrap(
                      spacing: 6,
                      runSpacing: 4,
                      children: [
                        for (final entry in _items.entries)
                          InputChip(
                            label: Text('${entry.value.$1} × ${qtyText(entry.value.$2)}'),
                            onDeleted: () => setState(() => _items.remove(entry.key)),
                          ),
                      ],
                    ),
                  ],
                  const SizedBox(height: 12),
                  TextField(
                    controller: _note,
                    decoration: const InputDecoration(
                      labelText: 'Note',
                      hintText: 'Why it is needed, and by when',
                      isDense: true,
                    ),
                  ),
                ],
              ),
            );
          },
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: _busy ? null : _send,
          child: Text(_busy ? 'Sending...' : 'Send request'),
        ),
      ],
    );
  }

  List<DropdownMenuEntry<Object?>> _stockEntries(List<Map<String, dynamic>> rows) => [
    for (final row in rows)
      DropdownMenuEntry<Object?>(
        value: row['product'],
        label: '${row['product_name']} · ${qtyText(row['balance'])} held',
      ),
  ];
}

/// The three steps that answer line by line: agreeing to a quantity, handing it
/// over, and saying what arrived. One dialog, because they differ only in what
/// each line is capped at and what the note beside it means.
class _LineQuantityDialog extends StatefulWidget {
  const _LineQuantityDialog._({
    required this.path,
    required this.lines,
    required this.action,
    required this.title,
    required this.field,
    required this.column,
    required this.cap,
    this.fromUnit,
    this.reasons = false,
  });

  /// Agree to what may be lent. Capped at what was asked for, and shown against
  /// what the holding unit actually has.
  factory _LineQuantityDialog.approve({
    required String path,
    required List<Map<String, dynamic>> lines,
    Object? fromUnit,
  }) => _LineQuantityDialog._(
    path: path,
    lines: lines,
    action: 'approve',
    title: 'Agree quantities',
    field: 'qty_approved',
    column: 'Agree',
    cap: 'qty_requested',
    fromUnit: fromUnit,
  );

  /// Hand it over. Capped at what was agreed, and this is the step that moves
  /// stock, so the shelf is shown again beside it.
  factory _LineQuantityDialog.issue({
    required String path,
    required List<Map<String, dynamic>> lines,
    Object? fromUnit,
  }) => _LineQuantityDialog._(
    path: path,
    lines: lines,
    action: 'issue',
    title: 'Hand over',
    field: 'qty',
    column: 'Give',
    cap: 'qty_approved',
    fromUnit: fromUnit,
  );

  /// Say what turned up. Anything short of what was issued is stock the
  /// hospital thought it had, so it asks what happened to it.
  factory _LineQuantityDialog.receive({
    required String path,
    required List<Map<String, dynamic>> lines,
  }) => _LineQuantityDialog._(
    path: path,
    lines: lines,
    action: 'receive',
    title: 'Confirm what arrived',
    field: 'qty_received',
    column: 'Got',
    cap: 'qty_issued',
    reasons: true,
  );

  final String path;
  final List<Map<String, dynamic>> lines;
  final String action;
  final String title;

  /// What the server calls the quantity on this step.
  final String field;
  final String column;

  /// The line field this step may not go above, which is also what it defaults to.
  final String cap;

  /// Whose shelf to show beside each line, where that is worth knowing.
  final Object? fromUnit;

  /// Whether a line short of its cap has to say why.
  final bool reasons;

  @override
  State<_LineQuantityDialog> createState() => _LineQuantityDialogState();
}

class _LineQuantityDialogState extends State<_LineQuantityDialog> {
  late final Map<int, TextEditingController> _quantities = {
    for (final line in widget.lines)
      line['id'] as int: TextEditingController(text: qtyText(line[widget.cap])),
  };
  late final Map<int, TextEditingController> _reasons = {
    if (widget.reasons)
      for (final line in widget.lines) line['id'] as int: TextEditingController(),
  };
  final _note = TextEditingController();
  Map<Object, double>? _shelf;
  bool _busy = false;
  bool _askedForShelf = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_askedForShelf || widget.fromUnit == null) return;
    _askedForShelf = true;
    _loadShelf(ApiScope.of(context));
  }

  Future<void> _loadShelf(Api api) async {
    try {
      final shelf = await _heldByProduct(api, widget.fromUnit!);
      if (mounted) setState(() => _shelf = shelf);
    } catch (_) {
      // Only the hint beside each box is missing. The quantity is still typed
      // by hand, and the server checks it against the shelf either way.
    }
  }

  @override
  void dispose() {
    for (final controller in [..._quantities.values, ..._reasons.values, _note]) {
      controller.dispose();
    }
    super.dispose();
  }

  String _subtitle(Map<String, dynamic> line) {
    final parts = ['asked ${qtyText(line['qty_requested'])}'];
    if (widget.cap == 'qty_approved') parts.add('agreed ${qtyText(line['qty_approved'])}');
    if (widget.cap == 'qty_issued') parts.add('given ${qtyText(line['qty_issued'])}');
    final held = _shelf?[line['product'] as Object];
    if (held != null) parts.add('on the shelf ${qtyText(held)}');
    return parts.join(' · ');
  }

  Future<void> _submit() async {
    final items = <Map<String, dynamic>>[];
    for (final line in widget.lines) {
      final id = line['id'] as int;
      final value = parseQty(_quantities[id]!.text);
      if (value == null || value < 0) {
        showError(context, badQtyMessage);
        return;
      }
      final reason = _reasons[id]?.text.trim() ?? '';
      if (widget.reasons && value < qty(line[widget.cap]) && reason.isEmpty) {
        showError(context, '${line['product_name']}: say what happened to the missing quantity.');
        return;
      }
      items.add({'line': id, widget.field: value, if (reason.isNotEmpty) 'reason': reason});
    }
    setState(() => _busy = true);
    try {
      await ApiScope.of(context).post('${widget.path}${widget.action}/', {
        'items': items,
        'note': _note.text.trim(),
      });
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return AlertDialog(
      title: Text(widget.title),
      content: SizedBox(
        width: dialogWidth(context, 460),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final line in widget.lines) ...[
                Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('${line['product_name']}'),
                          Text(
                            _subtitle(line),
                            style: theme.textTheme.labelSmall?.copyWith(
                              color: theme.colorScheme.onSurfaceVariant,
                            ),
                          ),
                        ],
                      ),
                    ),
                    SizedBox(
                      width: 80,
                      child: TextField(
                        controller: _quantities[line['id'] as int],
                        keyboardType: qtyKeyboard(),
                        decoration: InputDecoration(isDense: true, labelText: widget.column),
                      ),
                    ),
                  ],
                ),
                if (widget.reasons)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: TextField(
                      controller: _reasons[line['id'] as int],
                      decoration: const InputDecoration(
                        labelText: 'If short, what happened',
                        isDense: true,
                      ),
                    ),
                  ),
                const SizedBox(height: 10),
              ],
              TextField(
                controller: _note,
                decoration: const InputDecoration(labelText: 'Note', isDense: true),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: _busy ? null : _submit,
          child: Text(_busy ? 'Saving...' : widget.title),
        ),
      ],
    );
  }
}
