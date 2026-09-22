import 'dart:convert';

import 'package:file_picker/file_picker.dart';
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

  Future<void> _import() async {
    final done = await showDialog<bool>(
      context: context,
      builder: (_) => _ImportDialog(unit: _unit),
    );
    if (done == true) _controller.reload();
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
      appBar: AppBar(
        title: const Text('Unit items'),
        actions: [
          if (api.isHospital && (api.isAdmin || api.unitId != null))
            IconButton(
              icon: const Icon(Icons.upload_file_outlined),
              tooltip: 'Import from CSV',
              onPressed: _import,
            ),
        ],
      ),
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
                final low = rows.where((row) => row['is_low_stock'] == true).length;
                final scheme = Theme.of(context).colorScheme;
                return ListView(
                  children: [
                    // The alarm sits above the list, not only on the rows it
                    // is about, so it is seen before anyone scrolls.
                    if (low > 0)
                      Material(
                        color: scheme.errorContainer,
                        child: ListTile(
                          leading: Icon(Icons.warning_amber_rounded, color: scheme.onErrorContainer),
                          title: Text(
                            '$low ${low == 1 ? 'item is' : 'items are'} below reorder level',
                            style: TextStyle(
                              color: scheme.onErrorContainer,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                    for (final row in rows)
                      ListTile(
                        leading: Icon(
                          row['is_active'] != true
                              ? Icons.block_outlined
                              : row['is_low_stock'] == true
                                  ? Icons.warning_amber_rounded
                                  : Icons.medication_outlined,
                          color: row['is_low_stock'] == true ? scheme.error : null,
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

/// The columns an import file may carry, in the order the template shows them.
/// Only `name` is required; the rest are sent as given and the server decides.
const importColumns = ['name', 'strength', 'brand', 'qty', 'reorder_level', 'expiry_date', 'note'];

/// Turns pasted or picked CSV text into rows ready for `/unit-items/`.
///
/// The first line is a header naming the columns, in any order, so a sheet
/// exported with extra columns still loads. Blank lines are skipped.
///
/// ponytail: split on commas and tabs, no quoted fields — a name with a comma
/// in it needs a proper CSV parser, add one when a sheet turns up with one.
List<Map<String, dynamic>> parseImport(String text) {
  final lines = const LineSplitter().convert(text).where((line) => line.trim().isNotEmpty).toList();
  if (lines.isEmpty) return [];
  List<String> cells(String line) =>
      line.split(RegExp(r'[,\t]')).map((cell) => cell.trim()).toList();
  final header = cells(lines.first).map((cell) => cell.toLowerCase().replaceAll(' ', '_')).toList();
  if (!header.contains('name')) {
    throw const FormatException('The first line must name the columns and include "name".');
  }
  return [
    for (final line in lines.skip(1))
      {
        for (final (index, column) in header.indexed)
          if (importColumns.contains(column) &&
              index < cells(line).length &&
              cells(line)[index].isNotEmpty)
            column: column == 'qty' || column == 'reorder_level'
                ? parseQty(cells(line)[index])
                : cells(line)[index],
      },
  ];
}

/// Loads many items onto one shelf at once, from a CSV pasted or picked.
/// Each row is its own request, so one bad line fails alone and says why.
class _ImportDialog extends StatefulWidget {
  const _ImportDialog({required this.unit});

  /// The shelf the list was showing, which the import starts on.
  final String unit;

  @override
  State<_ImportDialog> createState() => _ImportDialogState();
}

class _ImportDialogState extends State<_ImportDialog> {
  final _text = TextEditingController();
  late String _unit = widget.unit;
  bool _busy = false;
  int _done = 0;
  int _total = 0;
  final _failures = <String>[];

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _pickFile() async {
    final picked = await FilePicker.pickFiles(
      type: FileType.custom,
      allowedExtensions: const ['csv', 'txt', 'tsv'],
      withData: true,
    );
    final file = (picked?.files.isEmpty ?? true) ? null : picked!.files.first;
    if (file?.bytes == null) return;
    setState(() => _text.text = utf8.decode(file!.bytes!, allowMalformed: true));
  }

  Future<void> _run() async {
    if (_unit.isEmpty) return showError(context, 'Pick the unit first.');
    final List<Map<String, dynamic>> rows;
    try {
      rows = parseImport(_text.text);
    } on FormatException catch (error) {
      return showError(context, error.message);
    }
    if (rows.isEmpty) return showError(context, 'Nothing to import.');
    final api = ApiScope.of(context);
    setState(() {
      _busy = true;
      _done = 0;
      _total = rows.length;
      _failures.clear();
    });
    for (final (index, row) in rows.indexed) {
      try {
        await api.post('/unit-items/', {...row, 'unit': int.parse(_unit)});
      } catch (error) {
        _failures.add('Line ${index + 2} (${row['name'] ?? '?'}): $error');
      }
      if (mounted) setState(() => _done = index + 1);
    }
    if (!mounted) return;
    setState(() => _busy = false);
    if (_failures.isEmpty) {
      showDone(context, 'Imported $_total items.');
      Navigator.pop(context, true);
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Import items'),
      content: SizedBox(
        width: dialogWidth(context, 480),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              UnitField(
                value: _unit,
                label: 'Unit',
                placeholder: 'Pick a unit',
                padding: EdgeInsets.zero,
                onSelected: (value) => setState(() => _unit = value),
              ),
              const SizedBox(height: 12),
              Text(
                'First line names the columns: ${importColumns.join(', ')}. '
                'Only name is required. Dates as YYYY-MM-DD.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _text,
                maxLines: 8,
                enabled: !_busy,
                style: const TextStyle(fontFamily: 'monospace', fontSize: 13),
                decoration: const InputDecoration(
                  labelText: 'CSV',
                  hintText: 'name,strength,qty\nParacetamol,500mg,100',
                  alignLabelWithHint: true,
                ),
              ),
              TextButton.icon(
                onPressed: _busy ? null : _pickFile,
                icon: const Icon(Icons.folder_open_outlined),
                label: const Text('Pick a file'),
              ),
              if (_total > 0) ...[
                LinearProgressIndicator(value: _done / _total),
                const SizedBox(height: 4),
                Text('$_done of $_total · ${_failures.length} failed'),
              ],
              for (final failure in _failures)
                Text(failure, style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          // Some rows may have landed before one failed; the list reloads then.
          onPressed: _busy ? null : () => Navigator.pop(context, _done > _failures.length),
          child: Text(_failures.isEmpty ? 'Cancel' : 'Close'),
        ),
        FilledButton(
          onPressed: _busy ? null : _run,
          child: Text(_busy ? 'Importing...' : 'Import'),
        ),
      ],
    );
  }
}
