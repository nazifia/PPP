import 'package:flutter/material.dart';

import '../main.dart';
import '../ui.dart';
import 'more.dart';
import 'requests.dart';

/// One screen, two readings: a supplier edits its catalogue, a hospital shops it.
class CatalogueScreen extends StatefulWidget {
  const CatalogueScreen({super.key});

  @override
  State<CatalogueScreen> createState() => _CatalogueScreenState();
}

class _CatalogueScreenState extends State<CatalogueScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();
  String _supplierFilter = '';
  bool _availableOnly = false;

  /// Department or unit the wishlist is being built for, as 'unit:3'. Held for
  /// the whole browse, since one shopping trip stocks one place.
  String? _tag;
  Future<List<Map<String, dynamic>>>? _tags;

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final supplier = api.isSupplier;
    // The buying side, rather than "not the selling side": an account attached
    // to no organisation at all is neither, and asking for /companies/ as one
    // is a 403.
    final hospital = api.canActAsHospital;
    // Supplier staff read the catalogue; only an administrator edits it.
    final canEdit = supplier && api.isAdmin;
    return Scaffold(
      appBar: AppBar(
        title: Text(supplier ? 'My catalogue' : 'Catalogue / wishlist'),
        actions: [
          if (hospital)
            IconButton(
              tooltip: 'Open drafts',
              icon: const Icon(Icons.shopping_cart_outlined),
              onPressed: () async {
                await Navigator.push(
                  context,
                  MaterialPageRoute<void>(builder: (_) => const RequestsScreen(draftsOnly: true)),
                );
                _controller.reload();
              },
            ),
        ],
      ),
      floatingActionButton: canEdit
          ? FloatingActionButton.extended(
              onPressed: () => _editProduct(context, null),
              icon: const Icon(Icons.add),
              label: const Text('Add item'),
            )
          : null,
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search item, brand, company',
            trailing: [
              if (hospital)
                IconButton(
                  tooltip: 'Available only',
                  isSelected: _availableOnly,
                  icon: const Icon(Icons.inventory_2_outlined),
                  selectedIcon: const Icon(Icons.inventory_2),
                  onPressed: () {
                    setState(() => _availableOnly = !_availableOnly);
                    _controller.reload();
                  },
                ),
            ],
          ),
          if (hospital)
            _CompanyFilter(
              value: _supplierFilter,
              onChanged: (value) {
                setState(() => _supplierFilter = value);
                _controller.reload();
              },
            ),
          if (hospital)
            Bounded(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 4, 12, 4),
                child: FutureBuilder<List<Map<String, dynamic>>>(
                  future: _tags ??= tagOptions(api),
                  builder: (context, snapshot) {
                    final tags = snapshot.data ?? const <Map<String, dynamic>>[];
                    if (tags.isEmpty) return const SizedBox.shrink();
                    return DropdownButtonFormField<String>(
                      initialValue: _tag,
                      isExpanded: true,
                      decoration: const InputDecoration(
                        labelText: 'Requesting for (department / unit)',
                        isDense: true,
                      ),
                      items: [
                        const DropdownMenuItem(value: null, child: Text('Not specified')),
                        for (final tag in tags)
                          DropdownMenuItem(
                            value: '${tag['key']}',
                            child: Text('${tag['label']}', overflow: TextOverflow.ellipsis),
                          ),
                      ],
                      onChanged: (value) => setState(() => _tag = value),
                    );
                  },
                ),
              ),
            ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list('/products/', {
                'search': _search.text,
                'supplier': _supplierFilter,
                if (_availableOnly) 'available': 'true',
              }),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return const EmptyState('No items found.', icon: Icons.medication_outlined);
                }
                return ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    final item = rows[index];
                    final name = '${item['generic_name']} ${item['strength'] ?? ''}'.trim();
                    return ListTile(
                      onTap: () => Navigator.push(
                        context,
                        MaterialPageRoute<void>(
                          builder: (_) => StockLedgerScreen(product: item['id'], productName: name),
                        ),
                      ),
                      title: Text(name, style: Theme.of(context).textTheme.titleSmall),
                      subtitle: Text(
                        [
                          if ('${item['brand']}'.isNotEmpty) item['brand'],
                          if ('${item['formulation']}'.isNotEmpty) item['formulation'],
                          if (supplier)
                            qty(item['qty_reserved']) == 0
                                ? 'stock ${qtyText(item['stock_qty'])}'
                                : 'stock ${qtyText(item['stock_qty'])} '
                                      '(${qtyText(item['qty_reserved'])} held)'
                          else
                            item['supplier_name'],
                          '${amount(item['unit_price'])} / ${item['unit']}',
                        ].join(' · '),
                      ),
                      trailing: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          StatusChip('${item['availability']}'),
                          const SizedBox(width: 6),
                          if (canEdit)
                            PopupMenuButton<String>(
                              onSelected: (choice) async {
                                if (choice == 'edit') {
                                  await _editProduct(context, item);
                                } else if (choice == 'restock') {
                                  await _restock(context, item);
                                } else if (choice == 'remove') {
                                  if (await confirm(
                                    context,
                                    'Remove item',
                                    'Hide ${item['generic_name']} from hospitals?',
                                  )) {
                                    await api.delete('/products/${item['id']}/');
                                    reload();
                                  }
                                }
                              },
                              itemBuilder: (_) => const [
                                PopupMenuItem(value: 'edit', child: Text('Edit')),
                                PopupMenuItem(value: 'restock', child: Text('Restock')),
                                PopupMenuItem(value: 'remove', child: Text('Deactivate')),
                              ],
                            )
                          else if (hospital)
                            IconButton(
                              icon: const Icon(Icons.add_shopping_cart),
                              tooltip: 'Add to request',
                              onPressed: () => _addToWishlist(context, item),
                            ),
                        ],
                      ),
                    );
                  },
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _addToWishlist(BuildContext context, Map<String, dynamic> product) async {
    final api = ApiScope.of(context);
    final quantity = await promptText(
      context,
      title: 'Quantity of ${product['generic_name']}',
      hint: 'Units to request, half units allowed',
      initial: '1',
      keyboard: qtyKeyboard(),
    );
    if (quantity == null) return;
    final wanted = parseQty(quantity);
    if (!context.mounted) return;
    if (wanted == null || wanted < halfUnit) {
      showError(context, badQtyMessage);
      return;
    }
    final org = await orgField(context, api, kind: 'HOSPITAL');
    if (org == null || !context.mounted) return;
    try {
      final requisition =
          await api.post('/requisitions/wishlist/add/', {
                'product': product['id'],
                'qty': wanted,
                ...tagField(_tag),
                ...org,
              })
              as Map<String, dynamic>;
      if (context.mounted) {
        showDone(context, 'Added to draft ${requisition['reference']}.');
      }
    } catch (error) {
      if (context.mounted) showError(context, error);
    }
  }

  Future<void> _restock(BuildContext context, Map<String, dynamic> product) async {
    final api = ApiScope.of(context);
    final value = await promptText(
      context,
      title: 'Restock ${product['generic_name']}',
      hint: 'Quantity to add (negative to remove)',
      keyboard: qtyKeyboard(signed: true),
    );
    if (value == null) return;
    final change = parseQty(value);
    if (change == null) {
      if (context.mounted) showError(context, badQtyMessage);
      return;
    }
    try {
      await api.post('/products/${product['id']}/restock/', {'qty': change});
      _controller.reload();
    } catch (error) {
      if (context.mounted) showError(context, error);
    }
  }

  Future<void> _editProduct(BuildContext context, Map<String, dynamic>? product) async {
    final saved = await showDialog<bool>(
      context: context,
      builder: (_) => _ProductDialog(product: product),
    );
    if (saved == true) _controller.reload();
  }
}

class _CompanyFilter extends StatelessWidget {
  const _CompanyFilter({required this.value, required this.onChanged});

  final String value;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return FutureBuilder<List<Map<String, dynamic>>>(
      future: api.list('/companies/'),
      builder: (context, snapshot) {
        final companies = snapshot.data ?? const <Map<String, dynamic>>[];
        if (companies.isEmpty) return const SizedBox.shrink();
        return Bounded(
          child: SizedBox(
            height: 44,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 12),
              children: [
                Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: ChoiceChip(
                    label: const Text('All companies'),
                    selected: value.isEmpty,
                    onSelected: (_) => onChanged(''),
                  ),
                ),
                for (final company in companies)
                  Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: ChoiceChip(
                      label: Text('${company['name']}'),
                      selected: value == '${company['id']}',
                      onSelected: (_) => onChanged('${company['id']}'),
                    ),
                  ),
              ],
            ),
          ),
        );
      },
    );
  }
}

class _ProductDialog extends StatefulWidget {
  const _ProductDialog({this.product});

  final Map<String, dynamic>? product;

  @override
  State<_ProductDialog> createState() => _ProductDialogState();
}

class _ProductDialogState extends State<_ProductDialog> {
  late final Map<String, TextEditingController> _fields = {
    'generic_name': TextEditingController(text: '${widget.product?['generic_name'] ?? ''}'),
    'brand': TextEditingController(text: '${widget.product?['brand'] ?? ''}'),
    'strength': TextEditingController(text: '${widget.product?['strength'] ?? ''}'),
    'formulation': TextEditingController(text: '${widget.product?['formulation'] ?? ''}'),
    'unit': TextEditingController(text: '${widget.product?['unit'] ?? 'UNIT'}'),
    'unit_price': TextEditingController(text: '${widget.product?['unit_price'] ?? '0.00'}'),
    'stock_qty': TextEditingController(text: qtyText(widget.product?['stock_qty'] ?? 0)),
    'max_order_qty': TextEditingController(
      text: widget.product?['max_order_qty'] == null
          ? ''
          : qtyText(widget.product!['max_order_qty']),
    ),
  };
  bool _busy = false;

  @override
  void dispose() {
    for (final controller in _fields.values) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    final api = ApiScope.of(context);
    // Only a new item needs the tenant named; an edit takes it from the row.
    var org = const <String, dynamic>{};
    if (widget.product == null) {
      final picked = await orgField(context, api, kind: 'SUPPLIER');
      if (picked == null || !mounted) return;
      org = picked;
    }
    setState(() => _busy = true);
    final body = <String, dynamic>{
      for (final entry in _fields.entries)
        if (entry.key != 'max_order_qty' || entry.value.text.trim().isNotEmpty)
          entry.key: entry.value.text.trim(),
    };
    if (body['max_order_qty'] == null) body['max_order_qty'] = null;
    try {
      if (widget.product == null) {
        await api.post('/products/', {...body, ...org});
      } else {
        await api.patch('/products/${widget.product!['id']}/', body);
      }
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(widget.product == null ? 'New catalogue item' : 'Edit item'),
      content: SizedBox(
        width: dialogWidth(context, 400),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final entry in _fields.entries)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: TextField(
                    controller: entry.value,
                    keyboardType:
                        const ['unit_price', 'stock_qty', 'max_order_qty'].contains(entry.key)
                        ? qtyKeyboard()
                        : TextInputType.text,
                    decoration: InputDecoration(
                      labelText: entry.key.replaceAll('_', ' '),
                      isDense: true,
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(onPressed: _busy ? null : _save, child: const Text('Save')),
      ],
    );
  }
}
