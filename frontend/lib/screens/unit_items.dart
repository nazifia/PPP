import 'package:flutter/material.dart';

import '../api.dart';
import '../main.dart';
import '../ui.dart';
import 'more.dart';

/// What a unit keeps on its own shelf, whoever it came from: the count the
/// server holds as `/unit-items/`. Everyone in the hospital reads every shelf;
/// only the unit's own staff or an administrator change one.
class UnitItemsScreen extends StatefulWidget {
  const UnitItemsScreen({super.key});

  @override
  State<UnitItemsScreen> createState() => _UnitItemsScreenState();
}

class _UnitItemsScreenState extends State<UnitItemsScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();

  /// Which shelf is listed, as a unit id. Empty is every shelf.
  String _unit = '';

  /// Which department's shelves, when no one unit is picked. Null is all.
  Object? _department;
  Future<List<Map<String, dynamic>>>? _departments;
  bool _started = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_started) return;
    _started = true;
    // Somebody placed on a unit opens on their own shelf.
    _unit = '${ApiScope.of(context).unitId ?? ''}';
    _departments = ApiScope.of(context).list('/departments/');
  }

  List<DropdownMenuEntry<Object?>> _departmentEntries(List<Map<String, dynamic>> rows) => [
    const DropdownMenuEntry(value: null, label: 'Every department'),
    for (final row in rows) DropdownMenuEntry(value: row['id'], label: '${row['name']}'),
  ];

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  /// The server decides for real; this only keeps buttons off a screen that
  /// would be refused for pressing them.
  bool _actsFor(Api api, Object? unit) =>
      api.isAdmin || api.isSuperuser || (unit != null && api.unitId == unit);

  Future<void> _edit(Map<String, dynamic>? item) async {
    final saved = await showDialog<bool>(
      context: context,
      builder: (_) => _ItemDialog(item: item, unit: _unit),
    );
    if (saved == true) _controller.reload();
  }

  Future<void> _delete(Api api, Map<String, dynamic> item) async {
    final ok = await confirm(
      context, 'Remove item', 'Take ${item['name']} off ${item['unit_name']}?',
    );
    if (!ok) return;
    try {
      await api.delete('/unit-items/${item['id']}/');
      _controller.reload();
    } catch (error) {
      if (mounted) showError(context, error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Unit items')),
      floatingActionButton: api.isHospital && (api.isAdmin || api.unitId != null)
          ? FloatingActionButton(onPressed: () => _edit(null), child: const Icon(Icons.add))
          : null,
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search items',
          ),
          Bounded(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Row(
                children: [
                  Expanded(
                    child: FutureBuilder<List<Map<String, dynamic>>>(
                      future: _departments,
                      builder: (context, snapshot) {
                        final rows = snapshot.data ?? const <Map<String, dynamic>>[];
                        // One department is no choice at all.
                        if (rows.length < 2) return const SizedBox.shrink();
                        return PickerField<Object?>(
                          label: 'Department',
                          value: _department,
                          entries: _departmentEntries(rows),
                          search: (query) async => _departmentEntries(
                            await api.list('/departments/', {'search': query}),
                          ),
                          onSelected: (value) {
                            // A unit of the old department is no longer on offer.
                            setState(() {
                              _department = value;
                              _unit = '';
                            });
                            _controller.reload();
                          },
                        );
                      },
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: UnitField(
                      value: _unit,
                      label: 'Unit',
                      placeholder: 'Every unit',
                      department: _department,
                      padding: EdgeInsets.zero,
                      onSelected: (value) {
                        setState(() => _unit = value);
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
              load: () => api.list('/unit-items/', {
                'search': _search.text,
                if (_unit.isNotEmpty) 'unit': _unit
                else if (_department != null) 'department': '$_department',
              }),
              builder: (context, rows, reload) {
                if (rows.isEmpty) return const EmptyState('Nothing on this shelf yet.');
                return ListView(
                  children: [
                    for (final row in rows)
                      ListTile(
                        leading: Icon(
                          row['is_active'] == true
                              ? Icons.medication_outlined
                              : Icons.block_outlined,
                        ),
                        title: Text(
                          [
                            row['name'],
                            if ('${row['strength']}'.isNotEmpty) row['strength'],
                            if ('${row['brand']}'.isNotEmpty) '(${row['brand']})',
                          ].join(' '),
                        ),
                        subtitle: Text(
                          [
                            if (_unit.isEmpty) row['unit_name'],
                            if ('${row['formulation_name']}'.isNotEmpty) row['formulation_name'],
                            if ('${row['expiry_date'] ?? ''}'.isNotEmpty)
                              'Expires ${formatDate('${row['expiry_date']}')}',
                          ].join(' · '),
                        ),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Text(
                              '${qtyText(row['qty'])} ${row['dispensing_unit_name'] ?? ''}'.trim(),
                              style: Theme.of(context).textTheme.titleMedium,
                            ),
                            if (row['is_low_stock'] == true) ...[
                              const SizedBox(width: 8),
                              const StatusChip('LOW'),
                            ],
                            if (_actsFor(api, row['unit']))
                              IconButton(
                                icon: const Icon(Icons.delete_outline, size: 20),
                                onPressed: () => _delete(api, row),
                              ),
                          ],
                        ),
                        onTap: _actsFor(api, row['unit']) ? () => _edit(row) : null,
                      ),
                  ],
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

/// Adds an item to a shelf, or edits one already on it.
class _ItemDialog extends StatefulWidget {
  const _ItemDialog({this.item, required this.unit});

  final Map<String, dynamic>? item;

  /// The shelf the list was showing, which a new item starts on.
  final String unit;

  @override
  State<_ItemDialog> createState() => _ItemDialogState();
}

class _ItemDialogState extends State<_ItemDialog> {
  late final Map<String, TextEditingController> _fields = {
    'name': TextEditingController(text: '${widget.item?['name'] ?? ''}'),
    'brand': TextEditingController(text: '${widget.item?['brand'] ?? ''}'),
    'strength': TextEditingController(text: '${widget.item?['strength'] ?? ''}'),
    'qty': TextEditingController(text: qtyText(widget.item?['qty'] ?? 0)),
    'reorder_level': TextEditingController(
      text: widget.item?['reorder_level'] == null ? '' : qtyText(widget.item!['reorder_level']),
    ),
    'expiry_date': TextEditingController(text: '${widget.item?['expiry_date'] ?? ''}'),
    'note': TextEditingController(text: '${widget.item?['note'] ?? ''}'),
  };
  late String _unit = '${widget.item?['unit'] ?? widget.unit}';
  late int? _formulation = widget.item?['formulation'] as int?;
  late int? _dispensingUnit = widget.item?['dispensing_unit'] as int?;
  late bool _active = widget.item?['is_active'] != false;
  Future<List<List<Map<String, dynamic>>>>? _terms;
  bool _busy = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final api = ApiScope.of(context);
    _terms ??= Future.wait([api.list('/formulations/'), api.list('/dispensing-units/')]);
  }

  @override
  void dispose() {
    for (final controller in _fields.values) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _pickDate() async {
    final picked = await showDatePicker(
      context: context,
      initialDate: DateTime.tryParse(_fields['expiry_date']!.text) ?? DateTime.now(),
      firstDate: DateTime(2000),
      lastDate: DateTime(2100),
    );
    if (picked != null) {
      _fields['expiry_date']!.text = picked.toIso8601String().substring(0, 10);
    }
  }

  Future<void> _save() async {
    final quantity = parseQty(_fields['qty']!.text);
    final reorder = _fields['reorder_level']!.text.trim();
    if (_unit.isEmpty || _fields['name']!.text.trim().isEmpty) {
      showError(context, 'Pick the unit and name the item.');
      return;
    }
    if (quantity == null || quantity < 0 || (reorder.isNotEmpty && parseQty(reorder) == null)) {
      showError(context, badQtyMessage);
      return;
    }
    final api = ApiScope.of(context);
    setState(() => _busy = true);
    final expiry = _fields['expiry_date']!.text.trim();
    final body = <String, dynamic>{
      'unit': int.parse(_unit),
      for (final key in ['name', 'brand', 'strength', 'note']) key: _fields[key]!.text.trim(),
      'qty': quantity,
      'reorder_level': reorder.isEmpty ? null : parseQty(reorder),
      'expiry_date': expiry.isEmpty ? null : expiry,
      'formulation': _formulation,
      'dispensing_unit': _dispensingUnit,
      'is_active': _active,
    };
    try {
      if (widget.item == null) {
        await api.post('/unit-items/', body);
      } else {
        await api.patch('/unit-items/${widget.item!['id']}/', body);
      }
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  List<DropdownMenuEntry<int?>> _entries(List<Map<String, dynamic>> rows) => [
    const DropdownMenuEntry(value: null, label: '—'),
    for (final row in rows)
      if (row['is_active'] != false)
        DropdownMenuEntry(value: row['id'] as int, label: '${row['name']}'),
  ];

  Widget _text(String key, String label, {String? hint, TextInputType? keyboard}) => Padding(
    padding: const EdgeInsets.only(top: 12),
    child: TextField(
      controller: _fields[key],
      keyboardType: keyboard,
      decoration: InputDecoration(labelText: label, hintText: hint),
    ),
  );

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(widget.item == null ? 'Add item' : 'Edit item'),
      content: SizedBox(
        width: dialogWidth(context, 400),
        child: FutureBuilder<List<List<Map<String, dynamic>>>>(
          future: _terms,
          builder: (context, snapshot) {
            if (snapshot.connectionState != ConnectionState.done) {
              return const Spinner(height: 80);
            }
            if (snapshot.hasError) return Text('${snapshot.error}');
            final [formulations, units] = snapshot.data!;
            return SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  UnitField(
                    value: _unit,
                    label: 'Unit',
                    placeholder: 'Pick a unit',
                    padding: EdgeInsets.zero,
                    onSelected: (value) => setState(() => _unit = value),
                  ),
                  _text('name', 'Name', hint: 'e.g. Paracetamol'),
                  _text('brand', 'Brand'),
                  _text('strength', 'Strength', hint: 'e.g. 500mg'),
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: PickerField<int?>(
                      label: 'Formulation',
                      value: _formulation,
                      entries: _entries(formulations),
                      onSelected: (value) => setState(() => _formulation = value),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: PickerField<int?>(
                      label: 'Dispensing unit',
                      value: _dispensingUnit,
                      entries: _entries(units),
                      onSelected: (value) => setState(() => _dispensingUnit = value),
                    ),
                  ),
                  _text('qty', 'Quantity', keyboard: qtyKeyboard()),
                  _text(
                    'reorder_level',
                    'Reorder level',
                    hint: 'Warn below this',
                    keyboard: qtyKeyboard(),
                  ),
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: TextField(
                      controller: _fields['expiry_date'],
                      decoration: InputDecoration(
                        labelText: 'Expiry date',
                        hintText: 'YYYY-MM-DD',
                        suffixIcon: IconButton(
                          icon: const Icon(Icons.event_outlined),
                          onPressed: _pickDate,
                        ),
                      ),
                    ),
                  ),
                  _text('note', 'Note'),
                  if (widget.item != null)
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: const Text('In use'),
                      subtitle: const Text('Off retires it without losing the count'),
                      value: _active,
                      onChanged: (value) => setState(() => _active = value),
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
          onPressed: _busy ? null : _save,
          child: Text(_busy ? 'Saving...' : 'Save'),
        ),
      ],
    );
  }
}
