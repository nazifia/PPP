import 'package:flutter/material.dart';

import '../main.dart';
import '../ui.dart';

class DeliveriesScreen extends StatefulWidget {
  const DeliveriesScreen({super.key});

  @override
  State<DeliveriesScreen> createState() => _DeliveriesScreenState();
}

class _DeliveriesScreenState extends State<DeliveriesScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();
  int? _selected;

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
      appBar: AppBar(title: const Text('Deliveries')),
      body: TwoPane(
        placeholder: 'Pick a consignment to see its lines.',
        // ponytail: keyed so a new selection rebuilds the detail from scratch.
        detail: _selected == null
            ? null
            : DeliveryDetailScreen(
                key: ValueKey(_selected),
                deliveryId: _selected!,
                onChanged: _controller,
              ),
        list: Column(
          children: [
            DebouncedSearch(
              controller: _search,
              onChanged: _controller.reload,
              hintText: 'Search reference, waybill, request, unit',
            ),
            Expanded(
              child: Loader<List<Map<String, dynamic>>>(
                controller: _controller,
                load: () => api.list('/deliveries/', {'search': _search.text}),
                builder: (context, rows, reload) {
                  if (rows.isEmpty) {
                    return const EmptyState(
                      'No consignments yet.',
                      icon: Icons.local_shipping_outlined,
                    );
                  }
                  return ListView.separated(
                    itemCount: rows.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final delivery = rows[index];
                      return ListTile(
                        leading: Icon(
                          delivery['status'] == 'IN_TRANSIT'
                              ? Icons.local_shipping_outlined
                              : Icons.inventory_2_outlined,
                        ),
                        title: Text(
                          '${delivery['reference']} · ${delivery['requisition_reference']}',
                        ),
                        subtitle: Text(
                          '${api.isSupplier ? delivery['hospital_name'] : delivery['supplier_name']}\n'
                          'Sent ${formatDate(delivery['dispatched_at'] as String?)}',
                        ),
                        isThreeLine: true,
                        selected: wide && _selected == delivery['id'],
                        trailing: StatusChip('${delivery['status']}'),
                        onTap: () async {
                          if (wide) {
                            setState(() => _selected = delivery['id'] as int);
                            return;
                          }
                          await Navigator.push(
                            context,
                            MaterialPageRoute<void>(
                              builder: (_) =>
                                  DeliveryDetailScreen(deliveryId: delivery['id'] as int),
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

class DeliveryDetailScreen extends StatefulWidget {
  const DeliveryDetailScreen({super.key, required this.deliveryId, this.onChanged});

  final int deliveryId;

  /// The list this detail is pinned beside, so it refetches when a verification
  /// here changes the row it is showing. Null when pushed as its own page: the
  /// list reloads on pop instead.
  final LoaderController? onChanged;

  @override
  State<DeliveryDetailScreen> createState() => _DeliveryDetailScreenState();
}

class _DeliveryDetailScreenState extends State<DeliveryDetailScreen> {
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
        title: const Text('Delivery'),
        actions: [
          PrintButton(
            api: api,
            path: '/deliveries/${widget.deliveryId}/',
            tooltip: 'Print delivery note',
            name: 'Delivery note',
          ),
        ],
      ),
      body: Loader<Map<String, dynamic>>(
        controller: _controller,
        onReload: widget.onChanged?.reload,
        load: () async =>
            await api.get('/deliveries/${widget.deliveryId}/') as Map<String, dynamic>,
        builder: (context, delivery, reload) {
          final lines = (delivery['lines'] as List).cast<Map<String, dynamic>>();
          final pending = delivery['status'] == 'IN_TRANSIT';
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
                            '${delivery['reference']}',
                            style: Theme.of(context).textTheme.titleLarge,
                          ),
                          StatusChip('${delivery['status']}'),
                        ],
                      ),
                      const SizedBox(height: 6),
                      Text('Request ${delivery['requisition_reference']}'),
                      Text('From ${delivery['supplier_name']} to ${delivery['hospital_name']}'),
                      Text('Waybill ${delivery['waybill_no']}'),
                      Text('Dispatched ${formatDate(delivery['dispatched_at'] as String?)}'),
                      if (delivery['verified_at'] != null)
                        Text('Verified ${formatDate(delivery['verified_at'] as String?)}'),
                      if ('${delivery['remark']}'.isNotEmpty) Text('Remark: ${delivery['remark']}'),
                      Text('Accepted value ${amount(delivery['accepted_value'])}'),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(8),
                  child: WideTable(
                    columns: const ['Item', 'Sent', 'Batch', 'Expiry', 'Accepted', 'Rejected'],
                    rows: [
                      for (final line in lines)
                        DataRow(
                          cells: [
                            DataCell(
                              SizedBox(
                                width: 150,
                                child: Text(
                                  '${line['product_name']}',
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ),
                            ),
                            DataCell(Text(qtyText(line['qty_supplied']))),
                            DataCell(Text('${line['batch_no']}')),
                            DataCell(Text('${line['expiry_date'] ?? '-'}')),
                            DataCell(Text(qtyText(line['qty_accepted']))),
                            DataCell(
                              Text(
                                '${qtyText(line['qty_rejected'])}'
                                '${'${line['reject_reason']}'.isNotEmpty ? ' (${line['reject_reason']})' : ''}',
                              ),
                            ),
                          ],
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 14),
              if (api.canActAsHospital && pending)
                FilledButton.icon(
                  icon: const Icon(Icons.checklist),
                  label: const Text('Verify and accept'),
                  onPressed: () async {
                    final done = await showDialog<bool>(
                      context: context,
                      builder: (_) => _VerifyDialog(deliveryId: widget.deliveryId, lines: lines),
                    );
                    if (done == true) reload();
                  },
                ),
              if (api.isSupplier && pending)
                Text(
                  'Waiting for the hospital to inspect this consignment.',
                  style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant),
                ),
            ],
          );
        },
      ),
    );
  }
}

class _VerifyDialog extends StatefulWidget {
  const _VerifyDialog({required this.deliveryId, required this.lines});

  final int deliveryId;
  final List<Map<String, dynamic>> lines;

  @override
  State<_VerifyDialog> createState() => _VerifyDialogState();
}

class _VerifyDialogState extends State<_VerifyDialog> {
  late final Map<int, TextEditingController> _accepted = {
    for (final line in widget.lines)
      line['id'] as int: TextEditingController(text: qtyText(line['qty_supplied'])),
  };
  late final Map<int, TextEditingController> _reasons = {
    for (final line in widget.lines) line['id'] as int: TextEditingController(),
  };
  final _remark = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    for (final controller in [..._accepted.values, ..._reasons.values, _remark]) {
      controller.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Verify consignment'),
      content: SizedBox(
        width: dialogWidth(context),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'Count what arrived. Anything not accepted goes back to the company '
                'and needs a reason.',
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 12),
              for (final line in widget.lines)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('${line['product_name']}  (sent ${qtyText(line['qty_supplied'])})'),
                      const SizedBox(height: 6),
                      Row(
                        children: [
                          SizedBox(
                            width: 90,
                            child: TextField(
                              controller: _accepted[line['id'] as int],
                              keyboardType: qtyKeyboard(),
                              onChanged: (_) => setState(() {}),
                              decoration: const InputDecoration(labelText: 'Accept', isDense: true),
                            ),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: TextField(
                              controller: _reasons[line['id'] as int],
                              decoration: InputDecoration(
                                labelText:
                                    'Reason for the rest'
                                    ' (${qtyText(_rejected(line))} rejected)',
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
                controller: _remark,
                decoration: const InputDecoration(labelText: 'Remark', isDense: true),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(onPressed: _busy ? null : _submit, child: const Text('Confirm')),
      ],
    );
  }

  double _rejected(Map<String, dynamic> line) {
    final supplied = qty(line['qty_supplied']);
    final accepted = parseQty(_accepted[line['id'] as int]!.text) ?? 0;
    final rest = supplied - accepted;
    return rest < 0 ? 0 : rest;
  }

  Future<void> _submit() async {
    // A count that will not parse is a typo. Sending 0 would send the whole
    // consignment back to the company on the strength of a slip of the finger.
    final accepted = <int, double>{};
    for (final line in widget.lines) {
      final id = line['id'] as int;
      final value = parseQty(_accepted[id]!.text);
      if (value == null || value < 0) {
        showError(context, badQtyMessage);
        return;
      }
      accepted[id] = value;
    }
    setState(() => _busy = true);
    try {
      await ApiScope.of(context).post('/deliveries/${widget.deliveryId}/verify/', {
        'lines': [
          for (final line in widget.lines)
            {
              'line': line['id'],
              'qty_accepted': accepted[line['id'] as int],
              'qty_rejected': _rejected(line),
              'reason': _reasons[line['id'] as int]!.text,
            },
        ],
        'remark': _remark.text,
      });
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}
