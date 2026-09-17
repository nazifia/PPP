import 'package:flutter/material.dart';

import '../api.dart';
import '../main.dart';
import '../ui.dart';
import 'deliveries.dart';
import 'more.dart';

/// Departments and units in one picker. Each option carries the field name the
/// API wants with it, because the two are separate resources: 'department:3'.
/// [query] is passed to the server, which searches unit names by department
/// name too — reach the 26th department, which the first page never carries.
Future<List<Map<String, dynamic>>> tagOptions(Api api, [String query = '']) async {
  final search = {if (query.isNotEmpty) 'search': query};
  final lists = await Future.wait([
    api.list('/departments/', search),
    api.list('/units/', search),
  ]);
  return [
    for (final row in lists[0]) {'key': 'department:${row['id']}', 'label': '${row['name']}'},
    for (final row in lists[1]) {'key': 'unit:${row['id']}', 'label': '${row['full_name']}'},
  ]..sort((a, b) => '${a['label']}'.compareTo('${b['label']}'));
}

/// The picker options for [tagOptions] rows. [placeholder], when given, is the
/// 'no particular department' entry, and survives every query.
List<DropdownMenuEntry<String?>> tagEntries(
  List<Map<String, dynamic>> rows, {
  String? placeholder,
}) => [
  if (placeholder != null) DropdownMenuEntry(value: null, label: placeholder),
  for (final row in rows) DropdownMenuEntry(value: '${row['key']}', label: '${row['label']}'),
];

List<DropdownMenuEntry<int?>> companyEntries(List<Map<String, dynamic>> rows) => [
  for (final row in rows) DropdownMenuEntry(value: row['id'] as int, label: '${row['name']}'),
];

/// Turns 'department:3' back into the pair a query or request body needs.
Map<String, dynamic> tagField(String? tag) {
  if (tag == null) return const {};
  final parts = tag.split(':');
  return {parts[0]: int.parse(parts[1])};
}

class RequestsScreen extends StatefulWidget {
  const RequestsScreen({super.key, this.draftsOnly = false, this.department, this.month});

  final bool draftsOnly;

  /// Opens filtered to this department and its units. Null shows everything.
  final int? department;

  /// Opens filtered to one calendar month, as the ISO date of its first day.
  /// Null shows every month. The dashboard's trend drills in this way.
  final String? month;

  @override
  State<RequestsScreen> createState() => _RequestsScreenState();
}

class _RequestsScreenState extends State<RequestsScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();
  String _status = '';
  int? _selected;
  String? _tag;
  Future<List<Map<String, dynamic>>>? _tags;

  static const _filters = {
    '': 'All',
    'DRAFT': 'Drafts',
    'SUBMITTED': 'Submitted',
    'APPROVED,PARTIALLY_APPROVED': 'Approved',
    'DISPATCHED,DELIVERED': 'In delivery',
    'CLOSED': 'Closed',
    'REJECTED,CANCELLED': 'Rejected',
  };

  @override
  void initState() {
    super.initState();
    if (widget.draftsOnly) _status = 'DRAFT';
    if (widget.department != null) _tag = 'department:${widget.department}';
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final api = ApiScope.of(context);
    if (api.isHospital && !widget.draftsOnly) _tags ??= tagOptions(api);
  }

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final wide = TwoPane.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(
          widget.draftsOnly
              ? 'Draft requests'
              : widget.month == null
              ? 'Requests'
              : monthLabel(widget.month!),
        ),
        automaticallyImplyLeading:
            widget.draftsOnly || widget.department != null || widget.month != null,
      ),
      floatingActionButton: api.canActAsHospital && !widget.draftsOnly
          ? FloatingActionButton.extended(
              onPressed: () async {
                final created = await showDialog<int>(
                  context: context,
                  builder: (_) => const _NewRequestDialog(),
                );
                if (created == null || !context.mounted) return;
                if (wide) {
                  setState(() => _selected = created);
                  _controller.reload();
                  return;
                }
                await Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                    builder: (_) => RequestDetailScreen(requisitionId: created),
                  ),
                );
                _controller.reload();
              },
              icon: const Icon(Icons.add),
              label: const Text('New request'),
            )
          : null,
      body: TwoPane(
        placeholder: 'Pick a request to see its lines.',
        // ponytail: keyed so a new selection rebuilds the detail from scratch.
        detail: _selected == null
            ? null
            : RequestDetailScreen(
                key: ValueKey(_selected),
                requisitionId: _selected!,
                onChanged: _controller,
              ),
        list: Column(
          children: [
            DebouncedSearch(
              controller: _search,
              onChanged: _controller.reload,
              hintText: 'Search reference, company, unit, note',
            ),
            if (!widget.draftsOnly)
              Bounded(
                child: SizedBox(
                  height: 48 * textScale(context),
                  child: ListView(
                    scrollDirection: Axis.horizontal,
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                    children: [
                      for (final entry in _filters.entries)
                        if (!(api.isSupplier && entry.key == 'DRAFT'))
                          Padding(
                            padding: const EdgeInsets.only(right: 6),
                            child: ChoiceChip(
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
            if (_tags != null)
              Bounded(
                child: FutureBuilder<List<Map<String, dynamic>>>(
                  future: _tags,
                  builder: (context, snapshot) {
                    final rows = snapshot.data ?? const <Map<String, dynamic>>[];
                    if (rows.isEmpty) return const SizedBox.shrink();
                    return Padding(
                      padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
                      child: PickerField<String?>(
                        label: 'Department / unit',
                        value: _tag,
                        entries: tagEntries(rows, placeholder: 'All departments'),
                        search: (query) async =>
                            tagEntries(await tagOptions(api, query), placeholder: 'All departments'),
                        onSelected: (value) {
                          setState(() => _tag = value);
                          _controller.reload();
                        },
                      ),
                    );
                  },
                ),
              ),
            Expanded(
              child: Loader<List<Map<String, dynamic>>>(
                controller: _controller,
                // A department covers its own units, so picking one is enough.
                load: () => api.listAll('/requisitions/', {
                  'status': _status,
                  'search': _search.text,
                  'month': widget.month ?? '',
                  ...tagField(_tag),
                }),
                builder: (context, rows, reload) {
                  if (rows.isEmpty) return const EmptyState('No requests here yet.');
                  return ListView.separated(
                    itemCount: rows.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final row = rows[index];
                      return ListTile(
                        title: Text('${row['reference']}  ·  ${row['item_count']} item(s)'),
                        subtitle: Text(
                          '${api.isSupplier ? row['hospital_name'] : row['supplier_name']}\n'
                          '${row['department_name'] ?? 'No department'} · '
                          '${amount(row['requested_value'])} requested',
                        ),
                        isThreeLine: true,
                        selected: wide && _selected == row['id'],
                        trailing: StatusChip('${row['status']}'),
                        onTap: () async {
                          if (wide) {
                            setState(() => _selected = row['id'] as int);
                            return;
                          }
                          await Navigator.push(
                            context,
                            MaterialPageRoute<void>(
                              builder: (_) => RequestDetailScreen(requisitionId: row['id'] as int),
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

class _NewRequestDialog extends StatefulWidget {
  const _NewRequestDialog();

  @override
  State<_NewRequestDialog> createState() => _NewRequestDialogState();
}

class _NewRequestDialogState extends State<_NewRequestDialog> {
  int? _supplier;
  String? _tag;
  final _note = TextEditingController();

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return AlertDialog(
      title: const Text('New request'),
      content: SizedBox(
        width: dialogWidth(context, 400),
        child: FutureBuilder<List<List<Map<String, dynamic>>>>(
          future: Future.wait([api.list('/companies/'), tagOptions(api)]),
          builder: (context, snapshot) {
            if (!snapshot.hasData) {
              return const Spinner(height: 80);
            }
            final companies = snapshot.data![0];
            final tags = snapshot.data![1];
            return SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  PickerField<int?>(
                    label: 'Company',
                    value: _supplier,
                    entries: companyEntries(companies),
                    search: (query) async =>
                        companyEntries(await api.list('/companies/', {'search': query})),
                    onSelected: (value) => setState(() => _supplier = value),
                  ),
                  const SizedBox(height: 12),
                  PickerField<String?>(
                    label: 'Department / unit',
                    value: _tag,
                    entries: tagEntries(tags),
                    search: (query) async => tagEntries(await tagOptions(api, query)),
                    onSelected: (value) => setState(() => _tag = value),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _note,
                    decoration: const InputDecoration(labelText: 'Note', isDense: true),
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
          onPressed: () async {
            if (_supplier == null) return;
            final api = ApiScope.of(context);
            final org = await orgField(context, api, kind: 'HOSPITAL');
            if (org == null || !context.mounted) return;
            try {
              final created =
                  await api.post('/requisitions/', {
                        'supplier': _supplier,
                        ...tagField(_tag),
                        'note': _note.text,
                        ...org,
                      })
                      as Map<String, dynamic>;
              if (context.mounted) Navigator.pop(context, created['id'] as int);
            } catch (error) {
              if (context.mounted) showError(context, error);
            }
          },
          child: const Text('Create'),
        ),
      ],
    );
  }
}

/// A request's money, rolled up from the invoices its deliveries were billed
/// on. Only what the company has confirmed counts as paid; what the hospital
/// has declared and is still waiting on sits in [pending].
class RequisitionMoney {
  RequisitionMoney(List<Map<String, dynamic>> deliveries)
    : invoices = [
        for (final delivery in deliveries)
          if (delivery['invoice'] != null) delivery['invoice'] as Map<String, dynamic>,
      ];

  final List<Map<String, dynamic>> invoices;

  /// Below half a kobo is paid, not a rounding artefact of summing doubles.
  static const _epsilon = 0.005;

  double _sum(String field) =>
      invoices.fold(0, (total, invoice) => total + (double.tryParse('${invoice[field]}') ?? 0));

  double get invoiced => _sum('amount');

  double get paid => _sum('amount_paid');

  double get pending => _sum('amount_pending');

  double get balance => invoiced - paid;

  bool get isOverdue => invoices.any((invoice) => (invoice['days_overdue'] as int? ?? 0) > 0);

  String get status {
    if (invoiced > 0 && balance <= _epsilon) return 'PAID';
    return paid > _epsilon ? 'PART_PAID' : 'UNPAID';
  }
}

class RequestDetailScreen extends StatefulWidget {
  const RequestDetailScreen({super.key, required this.requisitionId, this.onChanged});

  final int requisitionId;

  /// The list this detail is pinned beside, so it refetches when an edit here
  /// changes the row it is showing. Null when pushed as its own page: the list
  /// reloads on pop instead.
  final LoaderController? onChanged;

  @override
  State<RequestDetailScreen> createState() => _RequestDetailScreenState();
}

class _RequestDetailScreenState extends State<RequestDetailScreen> {
  final _controller = LoaderController();

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
        title: const Text('Request'),
        actions: [
          PrintButton(
            api: api,
            path: '/requisitions/${widget.requisitionId}/',
            tooltip: 'Print this request',
            name: 'Purchase request',
          ),
        ],
      ),
      body: Loader<Map<String, dynamic>>(
        controller: _controller,
        onReload: widget.onChanged?.reload,
        load: () async =>
            await api.get('/requisitions/${widget.requisitionId}/') as Map<String, dynamic>,
        builder: (context, data, reload) {
          final lines = (data['lines'] as List).cast<Map<String, dynamic>>();
          final deliveries = (data['deliveries'] as List).cast<Map<String, dynamic>>();
          final status = '${data['status']}';
          final isDraft = status == 'DRAFT';
          final money = RequisitionMoney(deliveries);

          return ListView(
            padding: const EdgeInsets.all(12),
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
                      _kv('Hospital', '${data['hospital_name']}'),
                      _kv('Company', '${data['supplier_name']}'),
                      Row(
                        children: [
                          Expanded(child: _kv('Department', '${data['department_name'] ?? '-'}')),
                          if (isDraft && api.canActAsHospital)
                            IconButton(
                              icon: const Icon(Icons.edit_outlined, size: 18),
                              tooltip: 'Change department / unit',
                              onPressed: () => _retag(context, data, reload),
                            ),
                        ],
                      ),
                      _kv('Raised by', '${data['created_by_name'] ?? '-'}'),
                      _kv('Created', formatDate(data['created_at'] as String?)),
                      if (data['submitted_at'] != null)
                        _kv('Submitted', formatDate(data['submitted_at'] as String?)),
                      if (data['decided_at'] != null)
                        _kv('Decided', formatDate(data['decided_at'] as String?)),
                      _kv('Requested value', amount(data['requested_value'])),
                      _kv('Approved value', amount(data['approved_value'])),
                      if ('${data['note']}'.isNotEmpty) _kv('Note', '${data['note']}'),
                      if ('${data['supplier_note']}'.isNotEmpty)
                        _kv('Company note', '${data['supplier_note']}'),
                      if (money.invoices.isNotEmpty) ...[
                        const Divider(height: 20),
                        _kvChild(
                          'Payment',
                          Wrap(
                            spacing: 6,
                            runSpacing: 4,
                            children: [
                              StatusChip(money.status),
                              if (money.isOverdue) const StatusChip('OVERDUE'),
                            ],
                          ),
                        ),
                        _kv('Invoiced', amount(money.invoiced)),
                        if (money.paid > 0) _kv('Paid', amount(money.paid)),
                        if (money.balance > 0) _kv('Outstanding', amount(money.balance)),
                        if (money.pending > 0) _kv('Awaiting confirmation', amount(money.pending)),
                      ],
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      WideTable(
                        columns: const ['Item', 'Req', 'Appr', 'Sup', 'Acc', 'Price', 'Status', ''],
                        rows: [
                          for (final line in lines)
                            DataRow(
                              cells: [
                                DataCell(
                                  SizedBox(
                                    width: 160,
                                    child: Text(
                                      '${line['product_name']}',
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                  ),
                                ),
                                DataCell(Text(qtyText(line['qty_requested']))),
                                DataCell(Text(qtyText(line['qty_approved']))),
                                DataCell(Text(qtyText(line['qty_supplied']))),
                                DataCell(Text(qtyText(line['qty_accepted']))),
                                DataCell(Text(amount(line['unit_price']))),
                                DataCell(StatusChip('${line['status']}')),
                                DataCell(
                                  isDraft && api.canActAsHospital
                                      ? Row(
                                          mainAxisSize: MainAxisSize.min,
                                          children: [
                                            IconButton(
                                              tooltip: 'Change quantity',
                                              icon: const Icon(Icons.edit, size: 18),
                                              onPressed: () => _editLine(context, line, reload),
                                            ),
                                            IconButton(
                                              tooltip: 'Remove line',
                                              icon: const Icon(Icons.close, size: 18),
                                              onPressed: () =>
                                                  _removeLine(context, line, reload),
                                            ),
                                          ],
                                        )
                                      : const SizedBox.shrink(),
                                ),
                              ],
                            ),
                        ],
                      ),
                      if (isDraft && api.canActAsHospital)
                        TextButton.icon(
                          icon: const Icon(Icons.add),
                          label: const Text('Add item'),
                          onPressed: () async {
                            final added = await showDialog<bool>(
                              context: context,
                              builder: (_) => _AddLineDialog(
                                requisitionId: data['id'] as int,
                                supplierId: data['supplier'] as int,
                                onRequest: {
                                  for (final line in lines)
                                    line['product'] as int: qty(line['qty_requested']),
                                },
                              ),
                            );
                            if (added == true) reload();
                          },
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              _actions(context, data, lines, reload),
              if (deliveries.isNotEmpty) ...[
                Padding(
                  padding: const EdgeInsets.only(top: 16, bottom: 6),
                  child: Text('Deliveries', style: Theme.of(context).textTheme.titleMedium),
                ),
                for (final delivery in deliveries)
                  Card(
                    child: Column(
                      children: [
                        ListTile(
                          title: Text(
                            '${delivery['reference']} · ${delivery['lines'].length} line(s)',
                          ),
                          subtitle: Text(
                            'Sent ${formatDate(delivery['dispatched_at'] as String?)}'
                            '${delivery['waybill_no'] != '' ? ' · waybill ${delivery['waybill_no']}' : ''}',
                          ),
                          trailing: StatusChip('${delivery['status']}'),
                          onTap: () async {
                            await Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                builder: (_) =>
                                    DeliveryDetailScreen(deliveryId: delivery['id'] as int),
                              ),
                            );
                            reload();
                          },
                        ),
                        if (delivery['invoice'] != null)
                          _InvoiceSection(
                            invoice: delivery['invoice'] as Map<String, dynamic>,
                            onChanged: reload,
                          ),
                      ],
                    ),
                  ),
              ],
            ],
          );
        },
      ),
    );
  }

  Widget _kv(String key, String value) => _kvChild(
    key,
    Builder(builder: (context) => Text(value, style: Theme.of(context).textTheme.bodyMedium)),
  );

  Widget _kvChild(String key, Widget value) => Builder(
    builder: (context) {
      final theme = Theme.of(context);
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 120,
              child: Text(
                key,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
            ),
            Expanded(child: value),
          ],
        ),
      );
    },
  );

  /// Move a draft to another department or unit. Both fields are sent so the
  /// old tag is cleared, since a request carries one or the other, never both.
  Future<void> _retag(BuildContext context, Map<String, dynamic> data, VoidCallback reload) async {
    final api = ApiScope.of(context);
    final tags = await tagOptions(api);
    if (!context.mounted) return;
    var tag = data['unit'] != null
        ? 'unit:${data['unit']}'
        : data['department'] != null
        ? 'department:${data['department']}'
        : null;
    final picked = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Department / unit'),
        content: SizedBox(
          width: dialogWidth(context, 360),
          child: StatefulBuilder(
            builder: (context, setState) => PickerField<String?>(
              label: 'Department / unit',
              value: tag,
              entries: tagEntries(tags, placeholder: 'Not specified'),
              search: (query) async =>
                  tagEntries(await tagOptions(api, query), placeholder: 'Not specified'),
              onSelected: (value) => setState(() => tag = value),
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () =>
                Navigator.pop(context, {'department': null, 'unit': null, ...tagField(tag)}),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (picked == null || !context.mounted) return;
    await _run(
      context,
      () => api.patch('/requisitions/${data['id']}/', picked),
      reload,
      'Department updated.',
    );
  }

  Widget _actions(
    BuildContext context,
    Map<String, dynamic> data,
    List<Map<String, dynamic>> lines,
    VoidCallback reload,
  ) {
    final api = ApiScope.of(context);
    final status = '${data['status']}';
    final id = data['id'];
    final buttons = <Widget>[];

    if (api.canActAsHospital && status == 'DRAFT') {
      buttons.add(
        FilledButton.icon(
          icon: const Icon(Icons.send),
          label: const Text('Submit to company'),
          onPressed: () => _run(
            context,
            () => api.post('/requisitions/$id/submit/'),
            reload,
            'Request submitted.',
          ),
        ),
      );
      buttons.add(
        OutlinedButton.icon(
          icon: const Icon(Icons.delete_outline),
          label: const Text('Delete draft'),
          onPressed: () async {
            if (await confirm(context, 'Delete draft', 'Remove this draft request?')) {
              await api.delete('/requisitions/$id/');
              if (context.mounted) Navigator.pop(context);
            }
          },
        ),
      );
    }
    if (api.canActAsHospital && status == 'SUBMITTED') {
      buttons.add(
        OutlinedButton.icon(
          icon: const Icon(Icons.cancel_outlined),
          label: const Text('Cancel request'),
          onPressed: () async {
            final reason = await promptText(context, title: 'Reason for cancelling');
            if (reason == null) return;
            if (context.mounted) {
              await _run(
                context,
                () => api.post('/requisitions/$id/cancel/', {'reason': reason}),
                reload,
                'Request cancelled.',
              );
            }
          },
        ),
      );
    }
    if (api.canActAsSupplier && api.isAdmin && status == 'SUBMITTED') {
      buttons.add(
        FilledButton.icon(
          icon: const Icon(Icons.fact_check_outlined),
          label: const Text('Review and approve'),
          onPressed: () async {
            final done = await showDialog<bool>(
              context: context,
              builder: (_) => _DecideDialog(requisitionId: id as int, lines: lines),
            );
            if (done == true) reload();
          },
        ),
      );
    }
    if (api.canActAsSupplier &&
        ['APPROVED', 'PARTIALLY_APPROVED', 'DISPATCHED', 'DELIVERED'].contains(status) &&
        lines.any((line) => qty(line['qty_approved']) > qty(line['qty_supplied']))) {
      buttons.add(
        FilledButton.icon(
          icon: const Icon(Icons.local_shipping_outlined),
          label: const Text('Dispatch supplies'),
          onPressed: () async {
            final done = await showDialog<bool>(
              context: context,
              builder: (_) => _DispatchDialog(requisitionId: id as int, lines: lines),
            );
            if (done == true) reload();
          },
        ),
      );
      // Releasing cuts back an approval, so it is the administrator's, as
      // approving was. Dispatching what is already approved is not.
      if (api.isAdmin) {
        buttons.add(
          OutlinedButton.icon(
            icon: const Icon(Icons.lock_open_outlined),
            label: const Text('Release the rest'),
            onPressed: () async {
              final reason = await promptText(
                context,
                title: 'Why release the undispatched quantity?',
              );
              if (reason == null || reason.trim().isEmpty) return;
              if (context.mounted) {
                await _run(
                  context,
                  () => api.post('/requisitions/$id/release/', {'reason': reason}),
                  reload,
                  'Held stock released.',
                );
              }
            },
          ),
        );
      }
    }
    if (buttons.isEmpty) return const SizedBox.shrink();
    return Wrap(spacing: 10, runSpacing: 10, children: buttons);
  }

  /// Takes a line off a draft. No confirmation: one line off a wishlist is a
  /// small thing to undo and a large thing to be asked about every time, so
  /// the snack bar carries the way back instead.
  Future<void> _removeLine(
    BuildContext context,
    Map<String, dynamic> line,
    VoidCallback reload,
  ) async {
    final api = ApiScope.of(context);
    try {
      await api.delete('/requisition-lines/${line['id']}/');
    } catch (error) {
      if (context.mounted) showError(context, error);
      return;
    }
    reload();
    if (!context.mounted) return;
    showUndo(context, 'Removed ${line['product_name']}.', () async {
      // A fresh row rather than the old one: the server hands out the id, and
      // what the line said is all that has to come back.
      await api.post('/requisition-lines/', {
        'requisition': widget.requisitionId,
        'product': line['product'],
        'qty_requested': qty(line['qty_requested']),
      });
      reload();
    });
  }

  Future<void> _editLine(
    BuildContext context,
    Map<String, dynamic> line,
    VoidCallback reload,
  ) async {
    final value = await promptText(
      context,
      title: 'Quantity for ${line['product_name']}',
      initial: qtyText(line['qty_requested']),
      keyboard: qtyKeyboard(),
    );
    if (value == null) return;
    final wanted = parseQty(value);
    if (!context.mounted) return;
    if (wanted == null || wanted < halfUnit) {
      showError(context, badQtyMessage);
      return;
    }
    await _run(
      context,
      () => ApiScope.of(
        context,
      ).patch('/requisition-lines/${line['id']}/', {'qty_requested': wanted}),
      reload,
      'Updated.',
    );
  }

  Future<void> _run(
    BuildContext context,
    Future<dynamic> Function() action,
    VoidCallback reload,
    String message,
  ) async {
    try {
      await action();
      reload();
      if (context.mounted) showDone(context, message);
    } catch (error) {
      if (context.mounted) showError(context, error);
    }
  }
}

/// Adds a line to a draft straight from the company's catalogue, so a request
/// can be finished where it is read instead of going back round the catalogue
/// screen. An item already on the request is still listed, showing what is on
/// it, since adding it again tops that line up.
class _AddLineDialog extends StatefulWidget {
  const _AddLineDialog({
    required this.requisitionId,
    required this.supplierId,
    required this.onRequest,
  });

  final int requisitionId;
  final int supplierId;

  /// Quantity already asked for, by product id.
  final Map<int, double> onRequest;

  @override
  State<_AddLineDialog> createState() => _AddLineDialogState();
}

class _AddLineDialogState extends State<_AddLineDialog> {
  final _controller = LoaderController();
  final _search = TextEditingController();

  /// Quantity boxes, one per item the user has looked at, kept by product id so
  /// a typed figure survives the row scrolling out of view.
  final _quantities = <int, TextEditingController>{};

  /// Pages after the first, appended as the list is scrolled.
  final _extraRows = <Map<String, dynamic>>[];
  int _page = 1;
  bool _hasNext = false;
  bool _loadingMore = false;

  /// What each line holds, topped up as items are added here, so the picker
  /// reads right without refetching the request.
  late final Map<int, double> _onRequest = {...widget.onRequest};

  /// Whether anything was added during this visit, so the caller knows whether
  /// the request changed.
  bool _added = false;
  int? _busy;

  @override
  void dispose() {
    for (final controller in _quantities.values) {
      controller.dispose();
    }
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  Map<String, dynamic> _query(int page) => {
    'supplier': '${widget.supplierId}',
    'search': _search.text,
    if (page > 1) 'page': '$page',
  };

  Future<List<Map<String, dynamic>>> _loadFirstPage() async {
    _extraRows.clear();
    _page = 1;
    final (rows, hasNext) = await ApiScope.of(context).listPage('/products/', _query(1));
    _hasNext = hasNext;
    return rows;
  }

  Future<void> _loadMore() async {
    setState(() => _loadingMore = true);
    try {
      final (rows, hasNext) = await ApiScope.of(context).listPage('/products/', _query(_page + 1));
      if (!mounted) return;
      setState(() {
        _page += 1;
        _extraRows.addAll(rows);
        _hasNext = hasNext;
      });
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _loadingMore = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Add item'),
      content: SizedBox(
        width: dialogWidth(context),
        height: 380,
        child: Column(
          children: [
            DebouncedSearch(
              controller: _search,
              onChanged: _controller.reload,
              hintText: 'Search item, brand',
            ),
            Expanded(
              child: Loader<List<Map<String, dynamic>>>(
                controller: _controller,
                load: _loadFirstPage,
                builder: (context, firstPage, reload) {
                  // An item already on the request stays in the picker: adding
                  // it again tops the line up rather than duplicating it.
                  final items = [...firstPage, ..._extraRows];
                  if (items.isEmpty && !_hasNext) {
                    return const EmptyState(
                      'Nothing to add from this company.',
                      icon: Icons.medication_outlined,
                    );
                  }
                  final list = ListView.separated(
                    // The extra row is the footer that loads the next page.
                    itemCount: items.length + (_hasNext ? 1 : 0),
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) => index == items.length
                        ? const Padding(
                            padding: EdgeInsets.all(16),
                            child: Spinner(),
                          )
                        : _itemRow(items[index]),
                  );
                  if (!_hasNext) return list;
                  return NotificationListener<ScrollNotification>(
                    onNotification: (notification) {
                      if (!_loadingMore && notification.metrics.extentAfter < 400) _loadMore();
                      return false;
                    },
                    child: list,
                  );
                },
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, _added),
          child: const Text('Close'),
        ),
      ],
    );
  }

  Widget _itemRow(Map<String, dynamic> item) {
    final id = item['id'] as int;
    final quantity = _quantities.putIfAbsent(id, () => TextEditingController(text: '1'));
    final onRequest = _onRequest[id];
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('${item['generic_name']} ${item['strength'] ?? ''}'.trim()),
                Text(
                  [
                    if ('${item['brand']}'.isNotEmpty) item['brand'],
                    '${amount(item['unit_price'])} / ${item['unit']}',
                    'available ${qtyText(item['available_qty'] ?? item['stock_qty'])}',
                    if (onRequest != null) 'on request ${qtyText(onRequest)}',
                  ].join(' · '),
                  style: Theme.of(context).textTheme.labelSmall?.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          SizedBox(
            width: 72,
            child: TextField(
              controller: quantity,
              keyboardType: qtyKeyboard(),
              textAlign: TextAlign.center,
              decoration: const InputDecoration(labelText: 'Qty', isDense: true),
              onSubmitted: (_) => _add(item),
            ),
          ),
          IconButton(
            tooltip: onRequest == null ? 'Add to request' : 'Add more to request',
            icon: const Icon(Icons.add_shopping_cart),
            onPressed: _busy == null ? () => _add(item) : null,
          ),
        ],
      ),
    );
  }

  Future<void> _add(Map<String, dynamic> product) async {
    final id = product['id'] as int;
    final wanted = parseQty(_quantities[id]?.text);
    if (wanted == null || wanted < halfUnit) {
      showError(context, badQtyMessage);
      return;
    }
    setState(() => _busy = id);
    try {
      await ApiScope.of(context).post('/requisition-lines/', {
        'requisition': widget.requisitionId,
        'product': id,
        'qty_requested': wanted,
      });
      if (!mounted) return;
      // Mirrors the server, which adds to the line rather than replacing it.
      setState(() {
        _onRequest[id] = (_onRequest[id] ?? 0) + wanted;
        _added = true;
      });
      showDone(context, 'Added ${product['generic_name']}.');
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }
}

/// A delivery's invoice with its ledger folded underneath, so what was billed,
/// what was paid and what is still waiting on the company all read in one
/// place. A hospital admin records a payment from here rather than going round
/// by the invoices screen.
class _InvoiceSection extends StatelessWidget {
  const _InvoiceSection({required this.invoice, required this.onChanged});

  final Map<String, dynamic> invoice;
  final VoidCallback onChanged;

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final payments = (invoice['payments'] as List).cast<Map<String, dynamic>>();
    final pending = double.tryParse('${invoice['amount_pending']}') ?? 0;
    final daysOverdue = invoice['days_overdue'] as int? ?? 0;
    return ExpansionTile(
      leading: const Icon(Icons.receipt_long_outlined),
      title: Row(
        children: [
          Expanded(
            child: Text(
              '${invoice['reference']} · ${amount(invoice['amount'])}',
              overflow: TextOverflow.ellipsis,
            ),
          ),
          StatusChip(daysOverdue > 0 ? 'OVERDUE' : '${invoice['status']}'),
        ],
      ),
      subtitle: Text(
        'outstanding ${amount(invoice['balance'])}'
        '${pending > 0 ? ' · ${amount(pending)} awaiting confirmation' : ''}\n'
        '${daysOverdue > 0 ? 'Overdue by $daysOverdue day${daysOverdue == 1 ? '' : 's'}' : 'Due ${invoice['due_date']}'}',
        style: daysOverdue > 0 ? TextStyle(color: Theme.of(context).colorScheme.error) : null,
      ),
      children: [
        if (payments.isEmpty)
          const ListTile(dense: true, title: Text('Nothing paid against this invoice yet.')),
        for (final payment in payments) PaymentTile(payment: payment, onChanged: onChanged),
        Align(
          alignment: Alignment.centerLeft,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                PrintButton(
                  api: api,
                  path: '/invoices/${invoice['id']}/',
                  label: 'Print invoice',
                  name: 'Invoice ${invoice['reference']}',
                ),
                if (canRecordPayment(api, invoice))
                  FilledButton.tonalIcon(
                    onPressed: () async {
                      if (await recordPayment(context, invoice)) onChanged();
                    },
                    icon: const Icon(Icons.add),
                    label: const Text('Record payment'),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _DecideDialog extends StatefulWidget {
  const _DecideDialog({required this.requisitionId, required this.lines});

  final int requisitionId;
  final List<Map<String, dynamic>> lines;

  @override
  State<_DecideDialog> createState() => _DecideDialogState();
}

class _DecideDialogState extends State<_DecideDialog> {
  late final Map<int, TextEditingController> _quantities = {
    for (final line in widget.lines)
      line['id'] as int: TextEditingController(
        // Default to the smaller of what was asked and what is on the shelf.
        text: qtyText(_cap(line)),
      ),
  };
  final _note = TextEditingController();
  bool _busy = false;

  double _cap(Map<String, dynamic> line) {
    final requested = qty(line['qty_requested']);
    // Free stock, i.e. what is on the shelf less what is already promised elsewhere.
    final free = qty(line['available_qty'] ?? line['stock_qty'] ?? requested);
    return requested < free ? requested : free;
  }

  @override
  void dispose() {
    for (final controller in _quantities.values) {
      controller.dispose();
    }
    _note.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Approve quantities'),
      content: SizedBox(
        width: dialogWidth(context, 420),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final line in widget.lines)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: Row(
                    children: [
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text('${line['product_name']}'),
                            Text(
                              'asked ${qtyText(line['qty_requested'])} · '
                              'available ${qtyText(line['available_qty'] ?? line['stock_qty'])}',
                              style: Theme.of(context).textTheme.labelSmall?.copyWith(
                                color: Theme.of(context).colorScheme.onSurfaceVariant,
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
                          decoration: const InputDecoration(isDense: true, labelText: 'Approve'),
                        ),
                      ),
                    ],
                  ),
                ),
              TextField(
                controller: _note,
                decoration: const InputDecoration(labelText: 'Note to hospital', isDense: true),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        TextButton(
          onPressed: _busy
              ? null
              : () {
                  for (final controller in _quantities.values) {
                    controller.text = '0';
                  }
                  setState(() {});
                },
          child: const Text('Reject all'),
        ),
        FilledButton(onPressed: _busy ? null : _submit, child: const Text('Save decision')),
      ],
    );
  }

  Future<void> _submit() async {
    // A quantity that will not parse is a typo, not a rejection: sending 0 for
    // it would refuse the line on the hospital's behalf.
    final approved = <int, double>{};
    for (final entry in _quantities.entries) {
      final value = parseQty(entry.value.text);
      if (value == null || value < 0) {
        showError(context, badQtyMessage);
        return;
      }
      approved[entry.key] = value;
    }
    setState(() => _busy = true);
    try {
      await ApiScope.of(context).post('/requisitions/${widget.requisitionId}/decide/', {
        'lines': [
          for (final entry in approved.entries) {'line': entry.key, 'qty_approved': entry.value},
        ],
        'note': _note.text,
      });
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}

class _DispatchDialog extends StatefulWidget {
  const _DispatchDialog({required this.requisitionId, required this.lines});

  final int requisitionId;
  final List<Map<String, dynamic>> lines;

  @override
  State<_DispatchDialog> createState() => _DispatchDialogState();
}

class _DispatchDialogState extends State<_DispatchDialog> {
  late final List<Map<String, dynamic>> _pending = widget.lines
      .where((line) => qty(line['qty_approved']) > qty(line['qty_supplied']))
      .toList();
  late final Map<int, TextEditingController> _quantities = {
    for (final line in _pending)
      line['id'] as int: TextEditingController(
        text: qtyText(qty(line['qty_approved']) - qty(line['qty_supplied'])),
      ),
  };
  late final Map<int, TextEditingController> _batches = {
    for (final line in _pending) line['id'] as int: TextEditingController(),
  };
  final _waybill = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    for (final controller in [..._quantities.values, ..._batches.values, _waybill]) {
      controller.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Dispatch supplies'),
      content: SizedBox(
        width: dialogWidth(context),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final line in _pending)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('${line['product_name']}'),
                      Text(
                        'approved ${qtyText(line['qty_approved'])} · '
                        'already sent ${qtyText(line['qty_supplied'])}',
                        style: Theme.of(context).textTheme.labelSmall?.copyWith(
                          color: Theme.of(context).colorScheme.onSurfaceVariant,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Row(
                        children: [
                          SizedBox(
                            width: 90,
                            child: TextField(
                              controller: _quantities[line['id'] as int],
                              keyboardType: qtyKeyboard(),
                              decoration: const InputDecoration(labelText: 'Qty', isDense: true),
                            ),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: TextField(
                              controller: _batches[line['id'] as int],
                              decoration: const InputDecoration(
                                labelText: 'Batch no',
                                isDense: true,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              TextField(
                controller: _waybill,
                decoration: const InputDecoration(labelText: 'Waybill number', isDense: true),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(onPressed: _busy ? null : _submit, child: const Text('Dispatch')),
      ],
    );
  }

  Future<void> _submit() async {
    final sending = <int, double>{};
    for (final entry in _quantities.entries) {
      final value = parseQty(entry.value.text);
      if (value == null || value < 0) {
        showError(context, badQtyMessage);
        return;
      }
      sending[entry.key] = value;
    }
    setState(() => _busy = true);
    try {
      await ApiScope.of(context).post('/requisitions/${widget.requisitionId}/dispatch/', {
        'items': [
          for (final entry in sending.entries)
            {'line': entry.key, 'qty': entry.value, 'batch_no': _batches[entry.key]!.text},
        ],
        'waybill_no': _waybill.text,
      });
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}
