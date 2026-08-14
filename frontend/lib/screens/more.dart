import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

import '../api.dart';
import '../main.dart';
import '../ui.dart';
import 'deliveries.dart';
import 'login.dart';
import 'transfers.dart';

/// What the server accepts as a payment receipt. Kept in step with
/// `RECEIPT_EXTENSIONS` and `RECEIPT_MAX_BYTES` in core/models.py.
const receiptExtensions = ['jpg', 'jpeg', 'png', 'webp', 'heic', 'pdf'];
const receiptMaxBytes = 5 * 1024 * 1024;

class MoreScreen extends StatelessWidget {
  const MoreScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    void open(Widget screen) =>
        Navigator.push(context, MaterialPageRoute<void>(builder: (_) => screen));

    return Scaffold(
      appBar: AppBar(title: const Text('More')),
      body: Bounded(
        child: ListView(
          children: [
            const ProfileTile(),
            const Divider(),
            if (api.isSuperuser)
              ListTile(
                leading: const Icon(Icons.admin_panel_settings_outlined),
                title: const Text('Act as organisation'),
                subtitle: Text(
                  api.actAsOrg == null
                      ? 'Reading every organisation'
                      : 'Working inside ${api.organization?['name'] ?? 'one organisation'}',
                ),
                onTap: () => open(const ActAsScreen()),
              ),
            if (api.isHospital)
              ListTile(
                leading: const Icon(Icons.business),
                title: const Text('Supplier companies'),
                subtitle: const Text('Register and manage the companies you buy from'),
                onTap: () => open(const CompaniesScreen()),
              ),
            if (api.isHospital)
              ListTile(
                leading: const Icon(Icons.account_tree_outlined),
                title: const Text('Departments'),
                onTap: () => open(const DepartmentsScreen()),
              ),
            if (api.canActAsSupplier) ...[
              ListTile(
                leading: const Icon(Icons.straighten),
                title: const Text('Dispensing units'),
                subtitle: const Text('What the catalogue sells in'),
                onTap: () => open(
                  const CatalogueTermsScreen(
                    path: '/dispensing-units/',
                    title: 'Dispensing units',
                    label: 'dispensing unit',
                  ),
                ),
              ),
              ListTile(
                leading: const Icon(Icons.science_outlined),
                title: const Text('Formulations'),
                subtitle: const Text('The forms items come in'),
                onTap: () => open(
                  const CatalogueTermsScreen(
                    path: '/formulations/',
                    title: 'Formulations',
                    label: 'formulation',
                  ),
                ),
              ),
            ],
            ListTile(
              leading: const Icon(Icons.receipt_long),
              title: Text(api.isSupplier ? 'Invoices receivable' : 'Invoices payable'),
              onTap: () => open(const InvoicesScreen()),
            ),
            ListTile(
              leading: const Icon(Icons.payments_outlined),
              title: const Text('Payments'),
              subtitle: Text(
                api.isSupplier
                    ? 'Confirm what hospitals say they have paid'
                    : 'What you have recorded, and whether it was confirmed',
              ),
              onTap: () => open(const PaymentsScreen()),
            ),
            ListTile(
              leading: const Icon(Icons.assignment_return_outlined),
              title: const Text('Credit notes'),
              subtitle: Text(
                api.isSupplier
                    ? 'Accept or refuse what hospitals say arrived bad'
                    : 'What you have raised against goods that arrived bad',
              ),
              onTap: () => open(const CreditsScreen()),
            ),
            ListTile(
              leading: const Icon(Icons.swap_vert),
              title: const Text('Stock ledger'),
              subtitle: const Text('Every item in and out'),
              onTap: () => open(const StockLedgerScreen()),
            ),
            // Between two units of one department, so it only exists where an
            // organisation is divided that far — which is a hospital.
            if (api.canActAsHospital)
              ListTile(
                leading: const Icon(Icons.swap_horiz),
                title: const Text('Unit transfers'),
                subtitle: const Text('Borrow stock from another unit of your department'),
                onTap: () => open(const TransfersScreen()),
              ),
            // A supplier's stock carries no expiry date until it is delivered,
            // so the shelf check belongs to whoever is holding the shelf.
            if (api.canActAsHospital)
              ListTile(
                leading: const Icon(Icons.event_busy_outlined),
                title: const Text('Expiring stock'),
                subtitle: const Text('What is going out of date, soonest first'),
                onTap: () => open(const ExpiringScreen()),
              ),
            ListTile(
              leading: const Icon(Icons.people_outline),
              title: const Text('Staff accounts'),
              onTap: () => open(const UsersScreen()),
            ),
            if (api.isAdmin)
              ListTile(
                leading: const Icon(Icons.fact_check_outlined),
                title: const Text('Audit trail'),
                onTap: () => open(const AuditScreen()),
              ),
            ListTile(
              leading: const Icon(Icons.apartment),
              title: const Text('Organisation profile'),
              onTap: () => open(const OrganizationScreen()),
            ),
            const Divider(),
            const _AppearanceTile(),
            const _AutoSignOutTile(),
            ListTile(
              leading: const Icon(Icons.password),
              title: const Text('Change password'),
              onTap: () =>
                  showDialog<void>(context: context, builder: (_) => const ChangePasswordDialog()),
            ),
            ListTile(
              leading: Icon(Icons.logout, color: Theme.of(context).colorScheme.error),
              title: Text('Sign out', style: TextStyle(color: Theme.of(context).colorScheme.error)),
              onTap: () => signOutFlow(context, api),
            ),
          ],
        ),
      ),
    );
  }
}

/// Light / dark / follow-the-system, remembered between launches.
class _AppearanceTile extends StatelessWidget {
  const _AppearanceTile();

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<ThemeMode>(
      valueListenable: themeMode,
      builder: (context, mode, _) => ListTile(
        leading: const Icon(Icons.brightness_6_outlined),
        title: const Text('Appearance'),
        subtitle: Text(switch (mode) {
          ThemeMode.light => 'Light',
          ThemeMode.dark => 'Dark',
          ThemeMode.system => 'Follow device',
        }),
        trailing: SegmentedButton<ThemeMode>(
          showSelectedIcon: false,
          segments: const [
            ButtonSegment(value: ThemeMode.light, icon: Icon(Icons.light_mode_outlined)),
            ButtonSegment(value: ThemeMode.system, icon: Icon(Icons.brightness_auto_outlined)),
            ButtonSegment(value: ThemeMode.dark, icon: Icon(Icons.dark_mode_outlined)),
          ],
          selected: {mode},
          onSelectionChanged: (choice) => setThemeMode(choice.first),
        ),
      ),
    );
  }
}

/// How long this device waits before ending an untouched session.
class _AutoSignOutTile extends StatelessWidget {
  const _AutoSignOutTile();

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final policy = api.idlePolicy;
    // A device may sit under the organisation's ceiling but not over it, so say
    // which one the session is actually running on.
    final capped = policy != null && policy < api.deviceIdleTimeout;
    return ListTile(
      leading: const Icon(Icons.timer_outlined),
      title: const Text('Auto sign-out'),
      subtitle: Text(
        capped
            ? 'Your organisation caps this at ${policy.inMinutes} min, so that is what applies'
            : 'Ends the session after this long without activity',
      ),
      trailing: PickerField<Duration?>(
        label: 'After',
        value: api.deviceIdleTimeout,
        width: 132,
        // A value from an older build or a --dart-define may not be on the
        // list; showing it keeps the dropdown from asserting on it.
        entries: [
          for (final choice in {...idleTimeoutChoices, api.deviceIdleTimeout})
            DropdownMenuEntry(value: choice, label: '${choice.inMinutes} min'),
        ],
        onSelected: (choice) => choice == null ? null : api.setIdleTimeout(choice),
      ),
    );
  }
}

class CompaniesScreen extends StatefulWidget {
  const CompaniesScreen({super.key});

  @override
  State<CompaniesScreen> createState() => _CompaniesScreenState();
}

class _CompaniesScreenState extends State<CompaniesScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  Future<void> _link(BuildContext context, Api api) async {
    final phone = await promptText(
      context,
      title: 'Link a company',
      hint: 'The company\'s phone number',
    );
    if (phone == null || phone.trim().isEmpty || !context.mounted) return;
    // A link joins two organisations, so the hospital side has to be named.
    final org = await orgField(context, api, kind: 'HOSPITAL');
    if (org == null || !context.mounted) return;
    try {
      await api.post('/companies/link/', {'phone': phone.trim(), ...org});
      _controller.reload();
    } catch (error) {
      if (context.mounted) showError(context, error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Supplier companies'),
        actions: [
          // A company another hospital put on the platform already has its own
          // account and catalogue, so there is nothing to register — only a
          // trading link to open, and its phone number is what names it.
          if (api.isAdmin)
            IconButton(
              icon: const Icon(Icons.add_link),
              tooltip: 'Trade with a company already on the platform',
              onPressed: () => _link(context, api),
            ),
        ],
      ),
      floatingActionButton: api.isAdmin
          ? FloatingActionButton.extended(
              onPressed: () async {
                final added = await showDialog<bool>(
                  context: context,
                  builder: (_) => const _CompanyDialog(),
                );
                if (added == true) _controller.reload();
              },
              icon: const Icon(Icons.add),
              label: const Text('Add company'),
            )
          : null,
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search name, phone, category',
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list('/companies/', {'search': _search.text}),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return const EmptyState('No companies registered yet.', icon: Icons.business);
                }
                return ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    final company = rows[index];
                    final trading = company['partnership_active'] != false;
                    return ListTile(
                      leading: CircleAvatar(child: Text('${company['name']}'[0])),
                      title: Text('${company['name']}'),
                      subtitle: Text(
                        '${company['phone']} · ${company['category']} · '
                        '${company['product_count'] ?? 0} item(s)'
                        '${trading ? '' : ' · suspended'}',
                      ),
                      trailing: api.isAdmin
                          ? IconButton(
                              icon: Icon(trading ? Icons.block : Icons.play_arrow, size: 20),
                              tooltip: trading ? 'Suspend trading' : 'Resume trading',
                              onPressed: () async {
                                if (!await confirm(
                                  context,
                                  trading ? 'Suspend company' : 'Resume company',
                                  trading
                                      ? 'Stop sending new requests to ${company['name']}?'
                                      : 'Start trading with ${company['name']} again?',
                                )) {
                                  return;
                                }
                                if (!context.mounted) return;
                                // A link joins two organisations, so the
                                // hospital side has to be named either way.
                                final org = await orgField(context, api, kind: 'HOSPITAL');
                                if (org == null || !context.mounted) return;
                                try {
                                  if (trading) {
                                    await api.delete('/companies/${company['id']}/', org);
                                  } else {
                                    await api.post('/companies/${company['id']}/reactivate/', org);
                                  }
                                  reload();
                                } catch (error) {
                                  if (context.mounted) showError(context, error);
                                }
                              },
                            )
                          : null,
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
}

class _CompanyDialog extends StatefulWidget {
  const _CompanyDialog();

  @override
  State<_CompanyDialog> createState() => _CompanyDialogState();
}

class _CompanyDialogState extends State<_CompanyDialog> {
  final _fields = {
    'name': TextEditingController(),
    'phone': TextEditingController(),
    'email': TextEditingController(),
    'address': TextEditingController(),
    'registration_no': TextEditingController(),
    'contact_full_name': TextEditingController(),
    'contact_phone': TextEditingController(),
    'password': TextEditingController(),
  };
  String _category = 'PHARMACY';
  bool _busy = false;

  @override
  void dispose() {
    for (final controller in _fields.values) {
      controller.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Register a company'),
      content: SizedBox(
        width: dialogWidth(context, 440),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final entry in _fields.entries)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: TextField(
                    controller: entry.value,
                    obscureText: entry.key == 'password',
                    decoration: InputDecoration(
                      labelText: entry.key.replaceAll('_', ' '),
                      isDense: true,
                      helperText: entry.key == 'contact_phone'
                          ? 'The company signs in with this number'
                          : null,
                    ),
                  ),
                ),
              PickerField<String?>(
                label: 'What they supply',
                value: _category,
                entries: const [
                  DropdownMenuEntry(value: 'PHARMACY', label: 'Medicines / pharmacy'),
                  DropdownMenuEntry(value: 'LABORATORY', label: 'Laboratory reagents'),
                  DropdownMenuEntry(value: 'CONSUMABLES', label: 'Medical consumables'),
                  DropdownMenuEntry(value: 'MIXED', label: 'Mixed'),
                ],
                onSelected: (value) => setState(() => _category = value ?? 'PHARMACY'),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: _busy
              ? null
              : () async {
                  final api = ApiScope.of(context);
                  final org = await orgField(context, api, kind: 'HOSPITAL');
                  if (org == null || !context.mounted) return;
                  setState(() => _busy = true);
                  try {
                    await api.post('/companies/', {
                      for (final entry in _fields.entries) entry.key: entry.value.text.trim(),
                      'category': _category,
                      ...org,
                    });
                    if (context.mounted) Navigator.pop(context, true);
                  } catch (error) {
                    if (context.mounted) showError(context, error);
                  } finally {
                    if (mounted) setState(() => _busy = false);
                  }
                },
          child: const Text('Register'),
        ),
      ],
    );
  }
}

/// Departments, or the units of one when [department] is given. One screen
/// either way; only the endpoint underneath changes.
class DepartmentsScreen extends StatefulWidget {
  const DepartmentsScreen({super.key, this.department});

  final Map<String, dynamic>? department;

  @override
  State<DepartmentsScreen> createState() => _DepartmentsScreenState();
}

class _DepartmentsScreenState extends State<DepartmentsScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();

  bool get _isUnitList => widget.department != null;
  String get _label => _isUnitList ? 'unit' : 'department';
  String get _path => _isUnitList ? '/units/' : '/departments/';

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final departmentId = widget.department?['id'];
    return Scaffold(
      appBar: AppBar(
        title: Text(_isUnitList ? '${widget.department!['name']} units' : 'Departments'),
      ),
      floatingActionButton: api.isAdmin
          ? FloatingActionButton(
              onPressed: () async {
                final name = await promptText(context, title: 'New $_label');
                if (name == null || name.trim().isEmpty) return;
                if (!context.mounted) return;
                final org = await orgField(context, api, kind: 'HOSPITAL');
                if (org == null || !context.mounted) return;
                try {
                  await api.post(_path, {
                    'name': name.trim(),
                    if (_isUnitList) 'department': departmentId,
                    ...org,
                  });
                  _controller.reload();
                } catch (error) {
                  if (context.mounted) showError(context, error);
                }
              },
              child: const Icon(Icons.add),
            )
          : null,
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search ${_label}s',
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list(_path, {
                'search': _search.text,
                if (_isUnitList) 'department': departmentId,
              }),
              builder: (context, rows, reload) {
                if (rows.isEmpty) return EmptyState('No ${_label}s yet.');
                return ListView(
                  children: [
                    for (final row in rows)
                      ListTile(
                        leading: Icon(
                          _isUnitList
                              ? Icons.subdirectory_arrow_right
                              : Icons.account_tree_outlined,
                        ),
                        title: Text('${row['name']}'),
                        subtitle: _isUnitList
                            ? null
                            : Text(switch (row['unit_count'] as int? ?? 0) {
                                0 => 'No units',
                                1 => '1 unit',
                                final count => '$count units',
                              }),
                        onTap: _isUnitList
                            ? null
                            : () async {
                                await Navigator.push(
                                  context,
                                  MaterialPageRoute<void>(
                                    builder: (_) => DepartmentsScreen(department: row),
                                  ),
                                );
                                reload();
                              },
                        trailing: api.isAdmin
                            ? IconButton(
                                icon: const Icon(Icons.delete_outline, size: 20),
                                onPressed: () async {
                                  final units = row['unit_count'] as int? ?? 0;
                                  final warning = units > 0 ? ' and its $units unit(s)' : '';
                                  if (await confirm(
                                    context,
                                    'Delete',
                                    'Remove ${row['name']}$warning?',
                                  )) {
                                    await api.delete('$_path${row['id']}/');
                                    reload();
                                  }
                                },
                              )
                            : null,
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

/// One of the catalogue's picked-not-typed lists: dispensing units, or the
/// forms items come in. One screen for both, since the rules are the same —
/// the standard entries are read-only and a company's own are its to edit.
class CatalogueTermsScreen extends StatefulWidget {
  const CatalogueTermsScreen({
    super.key,
    required this.path,
    required this.title,
    required this.label,
  });

  /// The list endpoint, e.g. `/dispensing-units/`.
  final String path;

  final String title;

  /// What one row is called, for the prompts: 'dispensing unit'.
  final String label;

  @override
  State<CatalogueTermsScreen> createState() => _CatalogueTermsScreenState();
}

class _CatalogueTermsScreenState extends State<CatalogueTermsScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  Future<void> _add(Api api) async {
    final name = await promptText(context, title: 'New ${widget.label}');
    if (name == null || name.trim().isEmpty || !mounted) return;
    try {
      await api.post(widget.path, {'name': name.trim()});
      _controller.reload();
    } catch (error) {
      if (mounted) showError(context, error);
    }
  }

  Future<void> _rename(Api api, Map<String, dynamic> row) async {
    final name = await promptText(
      context, title: 'Rename ${widget.label}', initial: '${row['name']}',
    );
    if (name == null || name.trim().isEmpty || !mounted) return;
    try {
      await api.patch('${widget.path}${row['id']}/', {'name': name.trim()});
      _controller.reload();
    } catch (error) {
      if (mounted) showError(context, error);
    }
  }

  Future<void> _delete(Api api, Map<String, dynamic> row) async {
    if (!await confirm(context, 'Delete', 'Remove ${row['name']}?')) return;
    try {
      await api.delete('${widget.path}${row['id']}/');
      _controller.reload();
    } catch (error) {
      // The commonest refusal is that catalogue items still use it, which the
      // server says in words worth showing.
      if (mounted) showError(context, error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    // Only a supplier defines these, and only its administrator.
    final canEdit = api.isSupplier && api.isAdmin;
    return Scaffold(
      appBar: AppBar(title: Text(widget.title)),
      floatingActionButton: canEdit
          ? FloatingActionButton(
              onPressed: () => _add(api),
              child: const Icon(Icons.add),
            )
          : null,
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search ${widget.title.toLowerCase()}',
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list(widget.path, {'search': _search.text}),
              builder: (context, rows, reload) {
                if (rows.isEmpty) return EmptyState('No ${widget.label}s yet.');
                return ListView(
                  children: [
                    for (final row in rows)
                      if (row['is_shared'] == true)
                        ListTile(
                          leading: const Icon(Icons.lock_outline),
                          title: Text('${row['name']}'),
                          subtitle: const Text('Standard — the same for every company'),
                        )
                      else
                        ListTile(
                          leading: const Icon(Icons.edit_outlined),
                          title: Text('${row['name']}'),
                          subtitle: const Text('Yours'),
                          onTap: canEdit ? () => _rename(api, row) : null,
                          trailing: canEdit
                              ? IconButton(
                                  icon: const Icon(Icons.delete_outline, size: 20),
                                  onPressed: () => _delete(api, row),
                                )
                              : null,
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

class UsersScreen extends StatefulWidget {
  const UsersScreen({super.key});

  @override
  State<UsersScreen> createState() => _UsersScreenState();
}

class _UsersScreenState extends State<UsersScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Staff accounts')),
      floatingActionButton: api.isAdmin
          ? FloatingActionButton.extended(
              onPressed: () async {
                final added = await showDialog<bool>(
                  context: context,
                  builder: (_) => const _UserDialog(),
                );
                if (added == true) _controller.reload();
              },
              icon: const Icon(Icons.person_add_alt),
              label: const Text('Add staff'),
            )
          : null,
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search name, phone, job title',
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list('/users/', {'search': _search.text}),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return const EmptyState('No staff accounts found.', icon: Icons.people_outline);
                }
                return ListView(
                  children: [
                    for (final user in rows)
                      ListTile(
                        leading: CircleAvatar(
                          backgroundColor: user['is_active'] == true
                              ? null
                              : Theme.of(context).colorScheme.surfaceContainerHighest,
                          child: const Icon(Icons.person),
                        ),
                        title: Text('${user['full_name']}'),
                        subtitle: Text(
                          '${user['phone']} · ${user['role']}'
                          '${'${user['unit_name'] ?? ''}'.isEmpty ? '' : ' · ${user['unit_name']}'}'
                          '${user['is_active'] == true ? '' : ' · disabled'}',
                        ),
                        trailing: api.isAdmin
                            ? PopupMenuButton<String>(
                                onSelected: (choice) async {
                                  try {
                                    if (choice == 'reset') {
                                      final password = await promptText(
                                        context,
                                        title: 'New password for ${user['full_name']}',
                                        obscure: true,
                                      );
                                      if (password == null || !context.mounted) return;
                                      await api.post('/users/${user['id']}/reset_password/', {
                                        'new_password': password,
                                      });
                                      if (context.mounted) showDone(context, 'Password reset.');
                                    } else if (choice == 'disable') {
                                      await api.delete('/users/${user['id']}/');
                                    } else if (choice == 'enable') {
                                      await api.patch('/users/${user['id']}/', {
                                        'is_active': true,
                                      });
                                    } else if (choice == 'edit') {
                                      final saved = await showDialog<bool>(
                                        context: context,
                                        builder: (_) => _UserDialog(user: user),
                                      );
                                      if (saved != true) return;
                                    }
                                    reload();
                                  } catch (error) {
                                    if (context.mounted) showError(context, error);
                                  }
                                },
                                itemBuilder: (_) => [
                                  const PopupMenuItem(
                                    value: 'edit',
                                    child: Text('Edit details'),
                                  ),
                                  const PopupMenuItem(
                                    value: 'reset',
                                    child: Text('Reset password'),
                                  ),
                                  if (user['is_active'] == true)
                                    const PopupMenuItem(
                                      value: 'disable',
                                      child: Text('Disable account'),
                                    )
                                  else
                                    const PopupMenuItem(
                                      value: 'enable',
                                      child: Text('Enable account'),
                                    ),
                                ],
                              )
                            : null,
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

class _UserDialog extends StatefulWidget {
  /// The account to edit, or null to open a new one.
  const _UserDialog({this.user});

  final Map<String, dynamic>? user;

  @override
  State<_UserDialog> createState() => _UserDialogState();
}

class _UserDialogState extends State<_UserDialog> {
  late final Map<String, TextEditingController> _fields = {
    for (final name in ['full_name', 'phone', 'email', 'job_title'])
      name: TextEditingController(text: '${widget.user?[name] ?? ''}'),
    // A password is set once here and changed afterwards by Reset password,
    // which is the path that forces the owner to pick their own.
    if (widget.user == null) 'password': TextEditingController(),
  };
  late String _role = '${widget.user?['role'] ?? 'STAFF'}';

  /// Which unit this person works on. It is what lets them agree to a transfer
  /// out of that unit's stock, or sign for what arrives — see `require_unit_member`
  /// in core/services.py — so it is granted here rather than chosen by the
  /// account itself.
  late String _unit = '${widget.user?['unit'] ?? ''}';
  bool _busy = false;

  bool get _editing => widget.user != null;

  @override
  void dispose() {
    for (final controller in _fields.values) {
      controller.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(_editing ? 'Edit ${widget.user!['full_name']}' : 'New staff account'),
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
                    obscureText: entry.key == 'password',
                    decoration: InputDecoration(
                      labelText: entry.key.replaceAll('_', ' '),
                      isDense: true,
                    ),
                  ),
                ),
              PickerField<String?>(
                label: 'Role',
                value: _role,
                entries: const [
                  DropdownMenuEntry(value: 'STAFF', label: 'Staff'),
                  DropdownMenuEntry(value: 'ADMIN', label: 'Administrator'),
                ],
                onSelected: (value) => setState(() => _role = value ?? 'STAFF'),
              ),
              // Draws nothing where the organisation keeps no units, which is
              // every supplier and any hospital that has not divided itself up.
              _UnitField(
                label: 'Unit',
                placeholder: 'No particular unit',
                value: _unit,
                onSelected: (value) => setState(() => _unit = value),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: _busy
              ? null
              : () async {
                  final api = ApiScope.of(context);
                  // No kind: a supplier opens staff accounts as a hospital does.
                  // An existing account already sits in an organisation, and
                  // moving it to another is not something this screen does.
                  final org = _editing ? const <String, dynamic>{} : await orgField(context, api);
                  if (org == null || !context.mounted) return;
                  setState(() => _busy = true);
                  final body = <String, dynamic>{
                    for (final entry in _fields.entries) entry.key: entry.value.text.trim(),
                    'role': _role,
                    // Null clears it: an account taken off a unit acts for none.
                    'unit': _unit.isEmpty ? null : int.parse(_unit),
                    ...org,
                  };
                  try {
                    if (_editing) {
                      await api.patch('/users/${widget.user!['id']}/', body);
                    } else {
                      await api.post('/users/', body);
                    }
                    if (context.mounted) Navigator.pop(context, true);
                  } catch (error) {
                    if (context.mounted) showError(context, error);
                  } finally {
                    if (mounted) setState(() => _busy = false);
                  }
                },
          child: Text(_editing ? 'Save' : 'Create'),
        ),
      ],
    );
  }
}

class InvoicesScreen extends StatefulWidget {
  const InvoicesScreen({super.key});

  @override
  State<InvoicesScreen> createState() => _InvoicesScreenState();
}

class _InvoicesScreenState extends State<InvoicesScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();
  bool _overdueOnly = false;

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Invoices')),
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search invoice, delivery, unit',
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: FilterChip(
                label: const Text('Overdue only'),
                selected: _overdueOnly,
                onSelected: (value) {
                  setState(() => _overdueOnly = value);
                  _controller.reload();
                },
              ),
            ),
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list('/invoices/', {
                'search': _search.text,
                if (_overdueOnly) 'overdue': 'true',
              }),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return EmptyState(
                    _overdueOnly ? 'Nothing is overdue.' : 'No invoices yet.',
                    icon: Icons.receipt_long,
                  );
                }
                return ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    final invoice = rows[index];
                    final pending = double.tryParse('${invoice['amount_pending']}') ?? 0;
                    final daysOverdue = invoice['days_overdue'] as int? ?? 0;
                    return ListTile(
                      title: Text('${invoice['reference']} · ${amount(invoice['amount'])}'),
                      subtitle: Text(
                        '${api.isSupplier ? invoice['hospital_name'] : invoice['supplier_name']}\n'
                        'outstanding ${amount(invoice['balance'])}'
                        '${pending > 0 ? ' · ${amount(pending)} awaiting confirmation' : ''}\n'
                        '${daysOverdue > 0 ? 'Overdue by $daysOverdue day${daysOverdue == 1 ? '' : 's'}' : 'Due ${invoice['due_date']}'}',
                        style: daysOverdue > 0
                            ? TextStyle(color: Theme.of(context).colorScheme.error)
                            : null,
                      ),
                      isThreeLine: true,
                      trailing: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          StatusChip(daysOverdue > 0 ? 'OVERDUE' : '${invoice['status']}'),
                          IconButton(
                            icon: const Icon(Icons.assignment_return_outlined),
                            tooltip: 'Credit notes',
                            onPressed: () async {
                              await Navigator.push(
                                context,
                                MaterialPageRoute<void>(
                                  builder: (_) => CreditsScreen(invoice: invoice),
                                ),
                              );
                              // A confirmed credit changes what this row owes.
                              reload();
                            },
                          ),
                          IconButton(
                            icon: const Icon(Icons.history),
                            tooltip: 'Payments',
                            onPressed: () => Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                builder: (_) => PaymentsScreen(invoiceId: invoice['id'] as int),
                              ),
                            ),
                          ),
                        ],
                      ),
                      // A hospital records what it paid; the supplier confirms it
                      // on the payments screen before the invoice moves.
                      onTap: canRecordPayment(api, invoice)
                          ? () async {
                              if (await recordPayment(context, invoice)) reload();
                            }
                          : () => Navigator.push(
                              context,
                              MaterialPageRoute<void>(
                                builder: (_) => PaymentsScreen(invoiceId: invoice['id'] as int),
                              ),
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
}

/// What the hospital declares about a payment it has already made elsewhere.
/// The reference is what the company checks against its bank, so everything
/// but cash asks for one.
class RecordPaymentDialog extends StatefulWidget {
  const RecordPaymentDialog({super.key, required this.reference, required this.unclaimed});

  final String reference;
  final double unclaimed;

  @override
  State<RecordPaymentDialog> createState() => _RecordPaymentDialogState();
}

class _RecordPaymentDialogState extends State<RecordPaymentDialog> {
  static const methods = {
    'TRANSFER': 'Bank transfer',
    'CASH': 'Cash',
    'CHEQUE': 'Cheque',
    'POS': 'Card / POS',
  };

  late final _amount = TextEditingController(text: widget.unclaimed.toStringAsFixed(2));
  final _payerReference = TextEditingController();
  final _note = TextEditingController();
  String _method = 'TRANSFER';
  String? _error;
  PlatformFile? _receipt;

  Future<void> _pickReceipt() async {
    final picked = await FilePicker.pickFiles(
      type: FileType.custom,
      allowedExtensions: receiptExtensions,
      withData: true, // the bytes go straight up; nothing is copied to disk.
    );
    final file = (picked?.files.isEmpty ?? true) ? null : picked!.files.first;
    if (file == null || file.bytes == null) return;
    if (file.size > receiptMaxBytes) {
      return setState(() => _error = 'Receipt must be under 5 MB.');
    }
    setState(() {
      _receipt = file;
      _error = null;
    });
  }

  @override
  void dispose() {
    _amount.dispose();
    _payerReference.dispose();
    _note.dispose();
    super.dispose();
  }

  void _submit() {
    final value = double.tryParse(_amount.text.trim());
    if (value == null || value <= 0) {
      return setState(() => _error = 'Enter an amount greater than zero.');
    }
    if (value > widget.unclaimed) {
      return setState(
        () => _error = 'Only ${amount(widget.unclaimed)} is left unclaimed on this invoice.',
      );
    }
    if (_method != 'CASH' && _payerReference.text.trim().isEmpty) {
      return setState(() => _error = 'Give the transaction reference for a ${methods[_method]}.');
    }
    Navigator.pop(context, {
      'amount': value.toStringAsFixed(2),
      'method': _method,
      'payer_reference': _payerReference.text.trim(),
      'note': _note.text.trim(),
      if (_receipt != null) 'receipt': _receipt,
    });
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text('Record payment · ${widget.reference}'),
    content: SingleChildScrollView(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _amount,
            autofocus: true,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: InputDecoration(
              labelText: 'Amount',
              helperText: '${amount(widget.unclaimed)} unclaimed',
            ),
          ),
          const SizedBox(height: 12),
          PickerField<String?>(
            label: 'How it was paid',
            value: _method,
            entries: [
              for (final entry in methods.entries)
                DropdownMenuEntry(value: entry.key, label: entry.value),
            ],
            onSelected: (value) => setState(() => _method = value ?? _method),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _payerReference,
            decoration: InputDecoration(
              labelText: 'Transaction reference',
              helperText: _method == 'CASH'
                  ? 'Optional for cash'
                  : 'Teller, transfer or cheque no.',
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _note,
            decoration: const InputDecoration(labelText: 'Note (optional)'),
          ),
          const SizedBox(height: 12),
          // The slip is what the company checks the payment against, so it is
          // worth attaching even though nothing forces it.
          Align(
            alignment: Alignment.centerLeft,
            child: _receipt == null
                ? OutlinedButton.icon(
                    onPressed: _pickReceipt,
                    icon: const Icon(Icons.attach_file),
                    label: const Text('Attach receipt'),
                  )
                : Chip(
                    avatar: const Icon(Icons.receipt, size: 18),
                    label: Text(_receipt!.name, overflow: TextOverflow.ellipsis),
                    onDeleted: () => setState(() => _receipt = null),
                  ),
          ),
          if (_error != null) ...[
            const SizedBox(height: 12),
            Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
          ],
        ],
      ),
    ),
    actions: [
      TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
      FilledButton(onPressed: _submit, child: const Text('Record')),
    ],
  );
}

/// Whether this user may record a payment against [invoice] — a hospital admin,
/// and only for what is not already claimed by an earlier payment.
bool canRecordPayment(Api api, Map<String, dynamic> invoice) =>
    api.canActAsHospital &&
    api.isAdmin &&
    (double.tryParse('${invoice['amount_unclaimed']}') ?? 0) > 0;

/// Declares a payment against [invoice]. Returns true when one was recorded, so
/// the caller can reload. The money moved at a bank; this only tells the
/// company to look for it.
Future<bool> recordPayment(BuildContext context, Map<String, dynamic> invoice) async {
  final api = ApiScope.of(context);
  final entry = await showDialog<Map<String, dynamic>>(
    context: context,
    builder: (_) => RecordPaymentDialog(
      reference: '${invoice['reference']}',
      unclaimed: double.tryParse('${invoice['amount_unclaimed']}') ?? 0,
    ),
  );
  if (entry == null || !context.mounted) return false;
  try {
    final receipt = entry.remove('receipt') as PlatformFile?;
    final path = '/invoices/${invoice['id']}/pay/';
    await (receipt == null
        ? api.post(path, entry)
        : api.postWithFile(
            path,
            entry,
            field: 'receipt',
            filename: receipt.name,
            bytes: receipt.bytes!,
          ));
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Recorded. The company confirms it before the invoice is marked paid.'),
        ),
      );
    }
    return true;
  } catch (error) {
    if (context.mounted) showError(context, error);
    return false;
  }
}

/// The company says whether the money arrived. Returns true when it decided.
Future<bool> decidePayment(BuildContext context, Map<String, dynamic> payment, bool accept) async {
  final api = ApiScope.of(context);
  String? reason;
  if (!accept) {
    reason = await promptText(
      context,
      title: 'Why was it not received?',
      hint: 'Cheque bounced, nothing in the account…',
    );
    if (reason == null || reason.trim().isEmpty) return false;
  } else if (!await confirm(
    context,
    'Confirm payment',
    '${amount(payment['amount'])} received against invoice '
        '${payment['invoice_reference']}? This settles that much of the invoice.',
  )) {
    return false;
  }
  if (!context.mounted) return false;
  try {
    final path = '/payments/${payment['id']}/${accept ? 'confirm' : 'reject'}/';
    await api.post(path, accept ? null : {'reason': reason});
    return true;
  } catch (error) {
    if (context.mounted) showError(context, error);
    return false;
  }
}

/// Goods accepted at the door and found bad afterwards — a counterfeit batch, a
/// batch already nearly out of date. Verification cannot catch those, so the
/// invoice would otherwise be final from the moment it was raised.
///
/// Shaped like a payment because it is the same kind of thing: it moves what one
/// side owes the other, so it takes both of them.
class _RaiseCreditDialog extends StatefulWidget {
  const _RaiseCreditDialog({required this.invoice});

  final Map<String, dynamic> invoice;

  @override
  State<_RaiseCreditDialog> createState() => _RaiseCreditDialogState();
}

class _RaiseCreditDialogState extends State<_RaiseCreditDialog> {
  late final Future<List<Map<String, dynamic>>> _lines;
  final _qty = <Object, TextEditingController>{};
  final _reason = TextEditingController();
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    // What is left after earlier notes, from the server: working it out here is
    // how the two come to disagree.
    _lines = ApiScope.of(context)
        .get('/invoices/${widget.invoice['id']}/creditable/')
        .then((rows) => (rows as List).cast<Map<String, dynamic>>());
  }

  @override
  void dispose() {
    for (final controller in _qty.values) {
      controller.dispose();
    }
    _reason.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final lines = <Map<String, dynamic>>[];
    for (final entry in _qty.entries) {
      if (entry.value.text.trim().isEmpty) continue;
      final asked = parseQty(entry.value.text);
      if (asked == null || asked <= 0) {
        showError(context, badQtyMessage);
        return;
      }
      lines.add({'line': entry.key, 'qty': asked});
    }
    if (lines.isEmpty) {
      showError(context, 'Name at least one item to credit.');
      return;
    }
    if (_reason.text.trim().isEmpty) {
      showError(context, 'Say what is wrong with the goods.');
      return;
    }
    final api = ApiScope.of(context);
    final org = await orgField(context, api, kind: 'HOSPITAL');
    if (org == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await api.post('/invoices/${widget.invoice['id']}/credit/', {
        'lines': lines,
        'reason': _reason.text.trim(),
        ...org,
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
    return AlertDialog(
      title: const Text('Raise a credit note'),
      content: SizedBox(
        width: dialogWidth(context, 420),
        child: FutureBuilder<List<Map<String, dynamic>>>(
          future: _lines,
          builder: (context, snapshot) {
            if (snapshot.connectionState != ConnectionState.done) {
              return const SizedBox(height: 80, child: Center(child: CircularProgressIndicator()));
            }
            if (snapshot.hasError) return Text('${snapshot.error}');
            final open = [
              for (final row in snapshot.data!)
                if (qty(row['qty_creditable']) > 0) row,
            ];
            if (open.isEmpty) {
              return const Text('Everything on this invoice has already been credited.');
            }
            return SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Align(
                    alignment: Alignment.centerLeft,
                    child: Padding(
                      padding: EdgeInsets.only(bottom: 8),
                      child: Text('Leave a line blank to keep it.'),
                    ),
                  ),
                  for (final row in open)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 10),
                      child: TextField(
                        controller: _qty.putIfAbsent(
                          row['line'] as Object,
                          TextEditingController.new,
                        ),
                        keyboardType: qtyKeyboard(),
                        decoration: InputDecoration(
                          labelText: '${row['product_name']}',
                          helperText:
                              '${qtyText(row['qty_creditable'])} still open · '
                              '${amount(row['unit_price'])} each',
                          isDense: true,
                        ),
                      ),
                    ),
                  TextField(
                    controller: _reason,
                    decoration: const InputDecoration(
                      labelText: 'What is wrong with them',
                      hintText: 'Counterfeit batch, arrived short dated',
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
          onPressed: _busy ? null : _submit,
          child: Text(_busy ? 'Saving...' : 'Raise'),
        ),
      ],
    );
  }
}

/// The company says whether it accepts the credit. Returns true when it decided.
Future<bool> decideCredit(BuildContext context, Map<String, dynamic> credit, bool accept) async {
  final api = ApiScope.of(context);
  String? reason;
  if (!accept) {
    reason = await promptText(
      context,
      title: 'Why is the credit refused?',
      hint: 'The dates were on the delivery note…',
    );
    if (reason == null || reason.trim().isEmpty) return false;
  } else if (!await confirm(
    context,
    'Accept credit note',
    'Take ${amount(credit['amount'])} off invoice ${credit['invoice_reference']}? '
        'The goods come off the hospital\'s books with it.',
  )) {
    return false;
  }
  if (!context.mounted) return false;
  try {
    await api.post(
      '/credits/${credit['id']}/${accept ? 'confirm' : 'reject'}/',
      accept ? null : {'reason': reason},
    );
    return true;
  } catch (error) {
    if (context.mounted) showError(context, error);
    return false;
  }
}

/// Credit notes, for one invoice or across the board. A company administrator
/// decides a pending note here; the hospital raises them and watches.
class CreditsScreen extends StatefulWidget {
  const CreditsScreen({super.key, this.invoice});

  /// Set to show only one invoice's notes, and to offer raising another.
  final Map<String, dynamic>? invoice;

  @override
  State<CreditsScreen> createState() => _CreditsScreenState();
}

class _CreditsScreenState extends State<CreditsScreen> {
  final _controller = LoaderController();
  String _status = '';

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _raise() async {
    final raised = await showDialog<bool>(
      context: context,
      builder: (_) => _RaiseCreditDialog(invoice: widget.invoice!),
    );
    if (raised == true) _controller.reload();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final invoice = widget.invoice;
    return Scaffold(
      appBar: AppBar(
        title: Text(
          invoice == null ? 'Credit notes' : 'Credits · ${invoice['reference']}',
        ),
      ),
      floatingActionButton: invoice != null && api.canActAsHospital && api.isAdmin
          ? FloatingActionButton.extended(
              onPressed: _raise,
              icon: const Icon(Icons.receipt_long_outlined),
              label: const Text('Raise credit'),
            )
          : null,
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Wrap(
              spacing: 8,
              children: [
                for (final status in ['PENDING', 'CONFIRMED', 'REJECTED', ''])
                  ChoiceChip(
                    label: Text(status.isEmpty ? 'All' : status),
                    selected: _status == status,
                    onSelected: (_) {
                      setState(() => _status = status);
                      _controller.reload();
                    },
                  ),
              ],
            ),
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list('/credits/', {
                'status': _status,
                if (invoice != null) 'invoice': '${invoice['id']}',
              }),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return const EmptyState(
                    'No credit notes here.',
                    icon: Icons.receipt_long_outlined,
                  );
                }
                return ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    final credit = rows[index];
                    final pending = credit['status'] == 'PENDING';
                    final canDecide = api.canActAsSupplier && api.isAdmin && pending;
                    final items = (credit['lines'] as List)
                        .map((line) => '${line['product_name']} x${qtyText(line['qty'])}')
                        .join(', ');
                    return ListTile(
                      isThreeLine: true,
                      leading: const Icon(Icons.assignment_return_outlined),
                      title: Text(
                        '${amount(credit['amount'])} · invoice ${credit['invoice_reference']}',
                      ),
                      subtitle: Text(
                        '${credit['reason']}\n$items'
                        '${'${credit['reject_reason'] ?? ''}'.isEmpty ? '' : '\nRefused: ${credit['reject_reason']}'}',
                      ),
                      trailing: canDecide
                          ? Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                IconButton(
                                  icon: const Icon(Icons.close),
                                  tooltip: 'Refuse',
                                  onPressed: () async {
                                    if (await decideCredit(context, credit, false)) reload();
                                  },
                                ),
                                IconButton(
                                  icon: const Icon(Icons.check),
                                  tooltip: 'Accept',
                                  onPressed: () async {
                                    if (await decideCredit(context, credit, true)) reload();
                                  },
                                ),
                              ],
                            )
                          : StatusChip('${credit['status']}'),
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
}

/// The hospital takes back an entry the company has not decided yet: the wrong
/// invoice, the wrong amount, a slip keyed twice. Returns true when it did.
Future<bool> withdrawPayment(BuildContext context, Map<String, dynamic> payment) async {
  if (!await confirm(
    context,
    'Withdraw payment',
    'Take back the ${amount(payment['amount'])} recorded against invoice '
        '${payment['invoice_reference']}? It stays on the ledger as withdrawn, and '
        'the invoice is free to be paid again.',
  )) {
    return false;
  }
  if (!context.mounted) return false;
  final reason = await promptText(
    context,
    title: 'Why is it being withdrawn?',
    hint: 'Wrong invoice, keyed twice…',
  );
  if (reason == null || !context.mounted) return false;
  try {
    await ApiScope.of(context).post('/payments/${payment['id']}/withdraw/', {'reason': reason});
    return true;
  } catch (error) {
    if (context.mounted) showError(context, error);
    return false;
  }
}

/// One line of the ledger, wherever the ledger is shown. A company admin
/// decides a pending payment from here, and the hospital that recorded it may
/// take it back until they do; everyone else reads its status.
class PaymentTile extends StatelessWidget {
  const PaymentTile({super.key, required this.payment, required this.onChanged});

  final Map<String, dynamic> payment;
  final VoidCallback onChanged;

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final pending = payment['status'] == 'PENDING';
    final canDecide = api.canActAsSupplier && api.isAdmin && pending;
    final canWithdraw = api.canActAsHospital && api.isAdmin && pending;
    final receipt = '${payment['receipt'] ?? ''}';
    final party = api.isSupplier ? payment['hospital_name'] : payment['supplier_name'];
    return ListTile(
      isThreeLine: true,
      leading: receipt.isEmpty
          ? const Icon(Icons.receipt_long_outlined)
          : IconButton(
              icon: const Icon(Icons.image_outlined),
              tooltip: 'Receipt',
              onPressed: () => showDialog<void>(
                context: context,
                builder: (_) => ReceiptDialog(url: receipt),
              ),
            ),
      title: Text('${amount(payment['amount'])} · ${payment['method_display']}'),
      subtitle: Text(
        '$party · invoice ${payment['invoice_reference']}\n'
        '${payment['payer_reference'].toString().isEmpty ? 'no reference' : payment['payer_reference']}'
        '${payment['reject_reason'].toString().isEmpty ? '' : ' · ${payment['reject_reason']}'}',
      ),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          // A confirmed payment prints as a receipt, anything else as an advice
          // marked unconfirmed — see core/printing.py.
          PrintButton(
            api: api,
            path: '/payments/${payment['id']}/',
            tooltip: payment['status'] == 'CONFIRMED' ? 'Print receipt' : 'Print advice',
            name: payment['status'] == 'CONFIRMED' ? 'Payment receipt' : 'Payment advice',
          ),
          if (canWithdraw)
            IconButton(
              icon: const Icon(Icons.undo),
              tooltip: 'Withdraw',
              onPressed: () async {
                if (await withdrawPayment(context, payment)) onChanged();
              },
            ),
          if (canDecide) ...[
            IconButton(
              icon: const Icon(Icons.close),
              tooltip: 'Not received',
              onPressed: () async {
                if (await decidePayment(context, payment, false)) onChanged();
              },
            ),
            IconButton(
              icon: const Icon(Icons.check),
              tooltip: 'Confirm received',
              onPressed: () async {
                if (await decidePayment(context, payment, true)) onChanged();
              },
            ),
          ],
          if (!canDecide && !canWithdraw) StatusChip('${payment['status']}'),
        ],
      ),
    );
  }
}

/// Shows an attached receipt. A photographed slip is drawn; a PDF has no
/// viewer here, so its address is shown to be opened in a browser.
class ReceiptDialog extends StatelessWidget {
  const ReceiptDialog({super.key, required this.url});

  final String url;

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: const Text('Receipt'),
    content: url.toLowerCase().endsWith('.pdf')
        ? SelectableText(url)
        : InteractiveViewer(
            child: Image.network(url, errorBuilder: (context, _, _) => SelectableText(url)),
          ),
    actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Close'))],
  );
}

/// The payment ledger. A supplier decides what the hospital has declared here;
/// a hospital watches for the confirmation.
class PaymentsScreen extends StatefulWidget {
  const PaymentsScreen({super.key, this.invoiceId});

  /// Set to show only one invoice's payments.
  final int? invoiceId;

  @override
  State<PaymentsScreen> createState() => _PaymentsScreenState();
}

class _PaymentsScreenState extends State<PaymentsScreen> {
  final _controller = LoaderController();
  String _status = 'PENDING';

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
        title: const Text('Payments'),
        actions: [
          // Only when this screen stands for one invoice; the whole ledger has
          // no single document behind it.
          if (widget.invoiceId != null)
            PrintButton(
              api: api,
              path: '/invoices/${widget.invoiceId}/',
              tooltip: 'Print invoice',
              name: 'Invoice',
            ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Wrap(
              spacing: 8,
              children: [
                for (final status in ['PENDING', 'CONFIRMED', 'REJECTED', 'WITHDRAWN', ''])
                  ChoiceChip(
                    label: Text(status.isEmpty ? 'All' : status),
                    selected: _status == status,
                    onSelected: (_) {
                      setState(() => _status = status);
                      _controller.reload();
                    },
                  ),
              ],
            ),
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: () => api.list('/payments/', {
                'status': _status,
                if (widget.invoiceId != null) 'invoice': '${widget.invoiceId}',
              }),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return EmptyState(
                    _status == 'PENDING' ? 'Nothing waiting to be confirmed.' : 'No payments here.',
                    icon: Icons.payments_outlined,
                  );
                }
                return ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) =>
                      PaymentTile(payment: rows[index], onChanged: reload),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

/// The movement kinds, in the order the filter row offers them. The keys are
/// `StockMovement.KIND_CHOICES` in core/models.py.
const stockMovementKinds = {
  'RECEIPT': 'Received',
  'DISPENSE': 'Dispensed',
  'ISSUE': 'Issued',
  'RETURN': 'Returned',
  'ADJUST': 'Adjusted',
  'TRANSFER': 'Moved between units',
};

class StockLedgerScreen extends StatefulWidget {
  const StockLedgerScreen({super.key, this.product, this.productName});

  /// Show one item's movements instead of the whole ledger.
  final Object? product;
  final String? productName;

  @override
  State<StockLedgerScreen> createState() => _StockLedgerScreenState();
}

class _StockLedgerScreenState extends State<StockLedgerScreen> {
  final _controller = LoaderController();
  final _search = TextEditingController();
  String _kind = '';
  DateTimeRange? _range;

  /// Whose shelf to read: one unit's, the organisation's own store ('store'),
  /// or the whole organisation when it is empty.
  String _unit = '';

  /// Totals per item instead of the movements themselves.
  bool _showBalances = false;

  /// The ledger only grows, so it outruns one page. The first page comes from
  /// the [Loader]; every page after it is appended here.
  final _extraRows = <Map<String, dynamic>>[];
  int _page = 1;
  bool _hasNext = false;
  bool _loadingMore = false;

  @override
  void dispose() {
    _search.dispose();
    _controller.dispose();
    super.dispose();
  }

  /// The server reads a plain date, and takes both ends inclusively.
  static String _day(DateTime date) =>
      '${date.year}-${'${date.month}'.padLeft(2, '0')}-${'${date.day}'.padLeft(2, '0')}';

  Map<String, dynamic> _query(int page) => {
    'search': _search.text,
    'kind': _kind,
    'unit': _unit,
    if (widget.product != null) 'product': '${widget.product}',
    if (_range != null) 'from': _day(_range!.start),
    if (_range != null) 'to': _day(_range!.end),
    if (page > 1) 'page': '$page',
  };

  Future<List<Map<String, dynamic>>> _loadFirstPage() async {
    final api = ApiScope.of(context);
    _extraRows.clear();
    _page = 1;
    if (_showBalances) {
      // One unpaginated total per item, so there is no next page to chase.
      _hasNext = false;
      return api.list('/stock-movements/balances/', _query(1));
    }
    final (rows, hasNext) = await api.listPage('/stock-movements/', _query(1));
    _hasNext = hasNext;
    return rows;
  }

  Future<void> _loadMore() async {
    setState(() => _loadingMore = true);
    try {
      final (rows, hasNext) = await ApiScope.of(
        context,
      ).listPage('/stock-movements/', _query(_page + 1));
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

  void _filterBy(String kind) {
    setState(() => _kind = _kind == kind ? '' : kind);
    _controller.reload();
  }

  /// A signed quantity, coloured by which way it went.
  Widget _signedQty(double quantity) {
    final scheme = Theme.of(context).colorScheme;
    return Text(
      quantity >= 0 ? '+${qtyText(quantity)}' : qtyText(quantity),
      style: Theme.of(context).textTheme.labelLarge?.copyWith(
        color: quantity >= 0 ? scheme.primary : scheme.error,
        fontWeight: FontWeight.bold,
      ),
    );
  }

  Widget _balanceTile(Map<String, dynamic> row) => ListTile(
    dense: true,
    leading: const Icon(Icons.inventory_2_outlined),
    title: Text('${row['product_name']}'),
    trailing: _signedQty(qty(row['balance'])),
  );

  Widget _movementTile(Map<String, dynamic> row) {
    final api = ApiScope.of(context);
    final moved = qty(row['qty']);
    final scheme = Theme.of(context).colorScheme;
    final note = '${row['note'] ?? ''}';
    final delivery = row['delivery'] as int?;
    final unitName = '${row['unit_name'] ?? ''}';
    final lines = [
      [
        stockMovementKinds[row['kind']] ?? '${row['kind']}',
        formatDate(row['created_at'] as String?),
        // Whose shelf it came off or landed on. Blank is the organisation's own
        // store, which is where everything sat before units held stock.
        if (unitName.isNotEmpty) unitName,
        // Rows from every tenant arrive mixed together for a superuser reading
        // platform-wide, so say whose movement this is.
        if (api.isSuperuser && api.actAsOrg == null) '${row['organization_name']}',
        if (row['delivery_reference'] != null) '${row['delivery_reference']}',
      ].join(' · '),
      // A return carries the hospital's rejection reason, not a free remark.
      if (note.isNotEmpty) row['kind'] == 'RETURN' ? 'Rejected: $note' : note,
    ];
    return ListTile(
      dense: true,
      leading: Icon(
        moved >= 0 ? Icons.arrow_downward : Icons.arrow_upward,
        color: moved >= 0 ? scheme.primary : scheme.error,
      ),
      title: Text('${row['product_name']}'),
      subtitle: Text(lines.join('\n')),
      trailing: _signedQty(moved),
      onTap: delivery == null
          ? null
          : () => Navigator.push(
              context,
              MaterialPageRoute<void>(builder: (_) => DeliveryDetailScreen(deliveryId: delivery)),
            ),
    );
  }

  Future<void> _dispense() async {
    final recorded = await showDialog<bool>(
      context: context,
      builder: (_) => _DispenseDialog(product: widget.product),
    );
    if (recorded == true) _controller.reload();
  }

  Future<void> _adjust() async {
    final recorded = await showDialog<bool>(
      context: context,
      builder: (_) => _AdjustDialog(product: widget.product),
    );
    if (recorded == true) _controller.reload();
  }

  Future<void> _pickRange() async {
    if (_range != null) {
      setState(() => _range = null);
      _controller.reload();
      return;
    }
    final now = DateTime.now();
    final picked = await showDateRangePicker(
      context: context,
      firstDate: DateTime(now.year - 5),
      lastDate: now,
    );
    if (picked == null) return;
    setState(() => _range = picked);
    _controller.reload();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      floatingActionButton: ApiScope.of(context).isHospital
          ? FloatingActionButton.extended(
              onPressed: _dispense,
              icon: const Icon(Icons.medication_liquid_outlined),
              label: const Text('Dispense'),
            )
          : null,
      appBar: AppBar(
        title: Text(widget.productName ?? 'Stock ledger'),
        actions: [
          // Dispensing means a ward used the goods; everything else that takes
          // something off the shelf comes through here, and it answers to an
          // administrator because it is the one way stock moves without a
          // delivery or a ward behind it.
          if (ApiScope.of(context).canActAsHospital && ApiScope.of(context).isAdmin)
            IconButton(
              icon: const Icon(Icons.edit_note),
              tooltip: 'Adjust stock',
              onPressed: _adjust,
            ),
          // The same filters the list is showing, so the sheet covers what is
          // on screen. Balances are a different query and print nothing yet.
          if (!_showBalances)
            PrintButton(
              api: ApiScope.of(context),
              path: '/stock-movements/',
              query: _query(1),
              tooltip: 'Print this ledger',
              name: 'Stock ledger',
            ),
          IconButton(
            icon: Icon(_showBalances ? Icons.swap_vert : Icons.functions),
            tooltip: _showBalances ? 'Show movements' : 'Show balances',
            onPressed: () {
              setState(() => _showBalances = !_showBalances);
              _controller.reload();
            },
          ),
        ],
      ),
      body: Column(
        children: [
          DebouncedSearch(
            controller: _search,
            onChanged: _controller.reload,
            hintText: 'Search item, movement, note',
          ),
          // Whose shelf, where the organisation is divided into units. Draws
          // nothing where it is not, which is every supplier.
          Bounded(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: _UnitField(
                placeholder: 'Anywhere',
                extras: const {'store': 'Organisation store'},
                padding: EdgeInsets.zero,
                value: _unit,
                onSelected: (value) {
                  setState(() => _unit = value);
                  _controller.reload();
                },
              ),
            ),
          ),
          Bounded(
            child: SizedBox(
              height: 48,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: [
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: FilterChip(
                      avatar: const Icon(Icons.date_range, size: 18),
                      label: Text(
                        _range == null
                            ? 'Any date'
                            : '${_day(_range!.start)} to ${_day(_range!.end)}',
                      ),
                      selected: _range != null,
                      onSelected: (_) => _pickRange(),
                    ),
                  ),
                  for (final entry in stockMovementKinds.entries)
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: FilterChip(
                        label: Text(entry.value),
                        selected: _kind == entry.key,
                        onSelected: (_) => _filterBy(entry.key),
                      ),
                    ),
                ],
              ),
            ),
          ),
          Expanded(
            child: Loader<List<Map<String, dynamic>>>(
              controller: _controller,
              load: _loadFirstPage,
              builder: (context, firstPage, reload) {
                final rows = [...firstPage, ..._extraRows];
                if (rows.isEmpty) {
                  return EmptyState(
                    _kind.isEmpty && _search.text.isEmpty && _range == null
                        ? 'Nothing has moved yet.'
                        : 'No movement matches that.',
                  );
                }
                final list = ListView.separated(
                  // The extra row is the footer that loads the next page.
                  itemCount: rows.length + (_hasNext ? 1 : 0),
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) => index == rows.length
                      ? const Padding(
                          padding: EdgeInsets.all(16),
                          child: Center(child: CircularProgressIndicator()),
                        )
                      : _showBalances
                      ? _balanceTile(rows[index])
                      : _movementTile(rows[index]),
                );
                if (!_hasNext) return list;
                // Fetch the next page as the footer comes into view, so the
                // list keeps going without the user asking for it.
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
    );
  }
}

/// What the ledger says this hospital holds, item by item. [query] is searched
/// by the server across the product name and brand; [inStock] drops the items
/// run down to nothing, which cannot be dispensed but can still be corrected.
/// [unit] narrows it to one unit's shelf, leaving the rest of the hospital out.
Future<List<Map<String, dynamic>>> _balances(
  Api api, {
  String query = '',
  bool inStock = false,
  Object? unit,
}) async {
  final rows = await api.list('/stock-movements/balances/', {
    if (query.isNotEmpty) 'search': query,
    if (unit != null) 'unit': '$unit',
  });
  if (!inStock) return rows;
  return [
    for (final row in rows)
      if (qty(row['balance']) > 0) row,
  ];
}

/// The picker options for [_balances] rows. [suffix] says what the number is.
List<DropdownMenuEntry<Object?>> _balanceEntries(
  List<Map<String, dynamic>> rows,
  String suffix,
) => [
  for (final row in rows)
    DropdownMenuEntry<Object?>(
      value: row['product'],
      label: '${row['product_name']} · ${qtyText(row['balance'])} $suffix',
    ),
];

/// Whose shelf a movement is recorded against: one unit's, or the
/// organisation's own store. Draws nothing where the organisation keeps no
/// units, since then there is only ever the store.
class _UnitField extends StatefulWidget {
  const _UnitField({
    required this.value,
    required this.onSelected,
    this.label = 'Shelf',
    this.placeholder = 'Organisation store',
    this.extras = const {},
    this.padding = const EdgeInsets.only(top: 12),
  });

  /// A unit id as text. Empty is [placeholder], which the server reads as no
  /// unit at all — the organisation's own store on a write, everywhere at once
  /// on a read.
  final String value;
  final ValueChanged<String> onSelected;
  final String label;
  final String placeholder;

  /// Fixed options offered after the placeholder, value to label. The ledger
  /// uses it for `store`, which is the store on its own rather than everywhere.
  final Map<String, String> extras;

  final EdgeInsetsGeometry padding;

  @override
  State<_UnitField> createState() => _UnitFieldState();
}

class _UnitFieldState extends State<_UnitField> {
  Future<List<Map<String, dynamic>>>? _units;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _units ??= ApiScope.of(context).list('/units/');
  }

  List<DropdownMenuEntry<String?>> _entries(List<Map<String, dynamic>> rows) => [
    DropdownMenuEntry(value: '', label: widget.placeholder),
    for (final entry in widget.extras.entries)
      DropdownMenuEntry(value: entry.key, label: entry.value),
    for (final row in rows)
      DropdownMenuEntry(value: '${row['id']}', label: '${row['full_name']}'),
  ];

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return FutureBuilder<List<Map<String, dynamic>>>(
      future: _units,
      builder: (context, snapshot) {
        final rows = snapshot.data;
        // An organisation that keeps no units has only ever had one shelf, so
        // there is nothing here to choose between.
        if (rows == null || rows.isEmpty) return const SizedBox.shrink();
        return Padding(
          padding: widget.padding,
          child: PickerField<String?>(
            label: widget.label,
            value: widget.value,
            entries: _entries(rows),
            search: (query) async =>
                _entries(await api.list('/units/', {if (query.isNotEmpty) 'search': query})),
            onSelected: (value) => widget.onSelected(value ?? ''),
          ),
        );
      },
    );
  }
}

/// Records what a ward handed out. The picker offers only what the ledger says
/// the hospital still holds, so the commonest mistake cannot be typed at all.
class _DispenseDialog extends StatefulWidget {
  const _DispenseDialog({this.product});

  final Object? product;

  @override
  State<_DispenseDialog> createState() => _DispenseDialogState();
}

class _DispenseDialogState extends State<_DispenseDialog> {
  final _qty = TextEditingController();
  final _note = TextEditingController();
  late Future<List<Map<String, dynamic>>> _held;
  Object? _product;

  /// Whose shelf it comes off, as a unit id or empty for the organisation's own
  /// store. An account that sits on a unit dispenses from that unit's shelf
  /// unless it says otherwise.
  String _unit = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _product = widget.product;
    _unit = '${ApiScope.of(context).unitId ?? ''}';
    _held = _balances(ApiScope.of(context), inStock: true, unit: _shelf);
  }

  /// What to ask the balances for: one unit, or the store on its own.
  String get _shelf => _unit.isEmpty ? 'store' : _unit;

  /// A different shelf holds different things, so the item list follows it.
  void _pickUnit(String unit) => setState(() {
    _unit = unit;
    _product = null;
    _held = _balances(ApiScope.of(context), inStock: true, unit: _shelf);
  });

  @override
  void dispose() {
    _qty.dispose();
    _note.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final dispensed = parseQty(_qty.text);
    if (_product == null || dispensed == null || dispensed < halfUnit) {
      showError(context, 'Pick an item and a quantity. $badQtyMessage');
      return;
    }
    final api = ApiScope.of(context);
    final org = await orgField(context, api, kind: 'HOSPITAL');
    if (org == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await api.post('/stock-movements/dispense/', {
        'product': _product,
        'qty': dispensed,
        'note': _note.text,
        if (_unit.isNotEmpty) 'unit': int.parse(_unit),
        ...org,
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
    return AlertDialog(
      title: const Text('Dispense'),
      content: SizedBox(
        width: dialogWidth(context, 380),
        child: FutureBuilder<List<Map<String, dynamic>>>(
          future: _held,
          builder: (context, snapshot) {
            if (snapshot.connectionState != ConnectionState.done) {
              return const SizedBox(height: 80, child: Center(child: CircularProgressIndicator()));
            }
            if (snapshot.hasError) return Text('${snapshot.error}');
            final stocked = snapshot.data!;
            return SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _UnitField(value: _unit, onSelected: _pickUnit),
                  const SizedBox(height: 12),
                  if (stocked.isEmpty)
                    const Text('Nothing on that shelf to dispense.')
                  else
                    PickerField<Object?>(
                      label: 'Item',
                      value: stocked.any((row) => row['product'] == _product) ? _product : null,
                      entries: _balanceEntries(stocked, 'left'),
                      search: (query) async => _balanceEntries(
                        await _balances(
                          ApiScope.of(context),
                          query: query,
                          inStock: true,
                          unit: _shelf,
                        ),
                        'left',
                      ),
                      onSelected: (value) => setState(() => _product = value),
                    ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _qty,
                    keyboardType: qtyKeyboard(),
                    decoration: const InputDecoration(labelText: 'Quantity dispensed'),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _note,
                    decoration: const InputDecoration(
                      labelText: 'Note',
                      hintText: 'Ward, patient, or why',
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
          onPressed: _busy ? null : _submit,
          child: Text(_busy ? 'Saving...' : 'Record'),
        ),
      ],
    );
  }
}

/// Puts the ledger right when something left the shelf without being dispensed:
/// a drug past its date, a broken vial, a count that never matched the book.
/// Signed, so a negative writes stock off and a positive corrects a miscount,
/// and always explained — an unexplained correction is indistinguishable from a
/// mistake later on.
class _AdjustDialog extends StatefulWidget {
  const _AdjustDialog({this.product});

  final Object? product;

  @override
  State<_AdjustDialog> createState() => _AdjustDialogState();
}

class _AdjustDialogState extends State<_AdjustDialog> {
  final _qty = TextEditingController();
  final _reason = TextEditingController();
  late Future<List<Map<String, dynamic>>> _held;
  Object? _product;

  /// Which shelf is being corrected, as a unit id. Empty is the store.
  String _unit = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _product = widget.product;
    // Every item the shelf has ever heard of, including the ones it has run
    // down to nothing: a miscount is corrected upwards as often as down. An
    // item with no history at all is one this hospital never received, and the
    // server refuses it.
    _held = _balances(ApiScope.of(context), unit: 'store');
  }

  void _pickUnit(String unit) => setState(() {
    _unit = unit;
    _product = null;
    // One unit's shelf, or the store on its own — which is what the correction
    // will be written against.
    _held = _balances(ApiScope.of(context), unit: unit.isEmpty ? 'store' : unit);
  });

  @override
  void dispose() {
    _qty.dispose();
    _reason.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final change = parseQty(_qty.text);
    if (_product == null || change == null || change == 0) {
      showError(context, 'Pick an item and a signed quantity. $badQtyMessage');
      return;
    }
    if (_reason.text.trim().isEmpty) {
      showError(context, 'Say why the ledger is being adjusted.');
      return;
    }
    final api = ApiScope.of(context);
    final org = await orgField(context, api, kind: 'HOSPITAL');
    if (org == null || !mounted) return;
    setState(() => _busy = true);
    try {
      await api.post('/stock-movements/adjust/', {
        'product': _product,
        'qty': change,
        'reason': _reason.text.trim(),
        if (_unit.isNotEmpty) 'unit': int.parse(_unit),
        ...org,
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
    return AlertDialog(
      title: const Text('Adjust stock'),
      content: SizedBox(
        width: dialogWidth(context, 380),
        child: FutureBuilder<List<Map<String, dynamic>>>(
          future: _held,
          builder: (context, snapshot) {
            if (snapshot.connectionState != ConnectionState.done) {
              return const SizedBox(height: 80, child: Center(child: CircularProgressIndicator()));
            }
            if (snapshot.hasError) return Text('${snapshot.error}');
            final rows = snapshot.data!;
            return SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _UnitField(value: _unit, onSelected: _pickUnit),
                  const SizedBox(height: 12),
                  if (rows.isEmpty)
                    const Text('Nothing has been received on that shelf yet.')
                  else
                    PickerField<Object?>(
                      label: 'Item',
                      value: rows.any((row) => row['product'] == _product) ? _product : null,
                      entries: _balanceEntries(rows, 'on the books'),
                      search: (query) async => _balanceEntries(
                        await _balances(
                          ApiScope.of(context),
                          query: query,
                          unit: _unit.isEmpty ? 'store' : _unit,
                        ),
                        'on the books',
                      ),
                      onSelected: (value) => setState(() => _product = value),
                    ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _qty,
                    keyboardType: qtyKeyboard(signed: true),
                    decoration: const InputDecoration(
                      labelText: 'Change',
                      helperText: '-2 writes two off; 2 puts two back',
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _reason,
                    decoration: const InputDecoration(
                      labelText: 'Reason',
                      hintText: 'Expired, broken, miscounted',
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
          onPressed: _busy ? null : _submit,
          child: Text(_busy ? 'Saving...' : 'Adjust'),
        ),
      ],
    );
  }
}

/// The shelf check: what was received with an expiry date inside the window,
/// soonest first, already-expired at the top.
///
/// Receipts rather than balances, because a dispense names no batch — so this
/// says what came in and when it goes out of date, and the pharmacist counts
/// what is left of it. See `expiring` in core/views.py.
class ExpiringScreen extends StatefulWidget {
  const ExpiringScreen({super.key});

  @override
  State<ExpiringScreen> createState() => _ExpiringScreenState();
}

class _ExpiringScreenState extends State<ExpiringScreen> {
  final _controller = LoaderController();

  /// The windows worth offering. The server caps anything past two years.
  static const _windows = {30: '30 days', 90: '3 months', 180: '6 months', 365: 'a year'};
  int _days = 90;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final today = DateTime.now();
    return Scaffold(
      appBar: AppBar(title: const Text('Expiring stock')),
      body: Column(
        children: [
          Bounded(
            child: SizedBox(
              height: 48,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: [
                  for (final entry in _windows.entries)
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        label: Text(entry.value),
                        selected: _days == entry.key,
                        onSelected: (_) {
                          setState(() => _days = entry.key);
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
              load: () => api.list('/stock-movements/expiring/', {'days': '$_days'}),
              builder: (context, rows, reload) {
                if (rows.isEmpty) {
                  return EmptyState(
                    'Nothing dated inside ${_windows[_days]}.',
                    icon: Icons.event_available_outlined,
                  );
                }
                return ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    final row = rows[index];
                    final expiry = DateTime.tryParse('${row['expiry_date']}');
                    final gone = expiry != null && expiry.isBefore(today);
                    return ListTile(
                      leading: Icon(
                        gone ? Icons.dangerous_outlined : Icons.schedule,
                        color: gone ? Theme.of(context).colorScheme.error : null,
                      ),
                      title: Text('${row['product_name']}'),
                      subtitle: Text(
                        '${gone ? 'Expired' : 'Expires'} ${formatDate('${row['expiry_date']}')}'
                        '${'${row['batch_no']}'.isEmpty ? '' : ' · batch ${row['batch_no']}'}\n'
                        'received ${qtyText(row['qty'])} on '
                        '${formatDate(row['created_at'] as String?)}',
                      ),
                      isThreeLine: true,
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
}

class AuditScreen extends StatelessWidget {
  const AuditScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Audit trail'),
        actions: [
          PrintButton(
            api: api,
            path: '/audit-logs/',
            tooltip: 'Print the trail',
            name: 'Audit trail',
          ),
        ],
      ),
      body: Loader<List<Map<String, dynamic>>>(
        load: () => api.list('/audit-logs/'),
        builder: (context, rows, reload) {
          if (rows.isEmpty) return const EmptyState('Nothing recorded yet.');
          return ListView.separated(
            itemCount: rows.length,
            separatorBuilder: (_, _) => const Divider(height: 1),
            itemBuilder: (context, index) {
              final row = rows[index];
              return ListTile(
                dense: true,
                title: Text('${row['action']} · ${row['entity']} #${row['entity_id']}'),
                subtitle: Text(
                  '${row['actor_name'] ?? 'system'} · ${formatDate(row['created_at'] as String?)}'
                  '${(row['detail'] as Map).isEmpty ? '' : '\n${row['detail']}'}',
                ),
              );
            },
          );
        },
      ),
    );
  }
}

class OrganizationScreen extends StatelessWidget {
  const OrganizationScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Organisation')),
      body: Loader<Map<String, dynamic>>(
        load: () async => await api.get('/organization/') as Map<String, dynamic>,
        builder: (context, org, reload) {
          Future<void> edit(String field, String label) async {
            final value = await promptText(context, title: label, initial: '${org[field] ?? ''}');
            if (value == null || !context.mounted) return;
            try {
              await api.patch('/organization/', {field: value});
              // The profile carries the organisation's settings the rest of the
              // app reads, such as the idle policy.
              await api.refreshProfile();
              reload();
            } catch (error) {
              if (context.mounted) showError(context, error);
            }
          }

          return ListView(
            children: [
              ListTile(
                title: const Text('Name'),
                subtitle: Text('${org['name']}'),
                trailing: api.isAdmin ? const Icon(Icons.edit, size: 18) : null,
                onTap: api.isAdmin ? () => edit('name', 'Organisation name') : null,
              ),
              ListTile(title: const Text('Type'), subtitle: Text('${org['kind']}')),
              ListTile(
                title: const Text('Phone'),
                subtitle: Text('${org['phone']}'),
                onTap: api.isAdmin ? () => edit('phone', 'Phone') : null,
              ),
              ListTile(
                title: const Text('Address'),
                subtitle: Text('${org['address']}'),
                onTap: api.isAdmin ? () => edit('address', 'Address') : null,
              ),
              ListTile(
                title: const Text('Registration number'),
                subtitle: Text('${org['registration_no']}'),
                onTap: api.isAdmin ? () => edit('registration_no', 'Registration number') : null,
              ),
              if (api.isSupplier)
                ListTile(
                  title: const Text('Payment terms'),
                  subtitle: Text(
                    '${org['payment_terms_days']} days from the invoice. '
                    'Applies to invoices raised from now on.',
                  ),
                  trailing: api.isAdmin ? const Icon(Icons.edit, size: 18) : null,
                  onTap: api.isAdmin
                      ? () => edit('payment_terms_days', 'Days to pay an invoice')
                      : null,
                ),
              ListTile(
                title: const Text('Auto sign-out policy'),
                subtitle: Text(
                  '${org['idle_timeout_minutes']} minutes idle. The longest any '
                  'device here may stay signed in untouched; a device may pick less.',
                ),
                trailing: api.isAdmin ? const Icon(Icons.edit, size: 18) : null,
                onTap: api.isAdmin
                    ? () => edit('idle_timeout_minutes', 'Minutes idle before sign-out')
                    : null,
              ),
              ListTile(
                title: const Text('Registered'),
                subtitle: Text(formatDate(org['created_at'] as String?)),
              ),
            ],
          );
        },
      ),
    );
  }
}

/// Lets a superuser step into one organisation and work as its administrator,
/// or step back out to the read-only view across all of them.
class ActAsScreen extends StatelessWidget {
  const ActAsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);

    Future<void> choose(int? id) async {
      try {
        await api.setActAsOrg(id);
        if (context.mounted) Navigator.pop(context);
      } catch (error) {
        if (context.mounted) showError(context, error);
      }
    }

    Widget tile({required int? id, required String title, required String subtitle}) => ListTile(
      leading: Icon(id == null ? Icons.public : Icons.apartment),
      title: Text(title),
      subtitle: Text(subtitle),
      trailing: api.actAsOrg == id ? const Icon(Icons.check) : null,
      onTap: () => choose(id),
    );

    return Scaffold(
      appBar: AppBar(title: const Text('Act as organisation')),
      body: Loader<List<Map<String, dynamic>>>(
        // Not /companies/: that is a hospital's supplier list, so it is gone
        // the moment the superuser steps into a supplier and cannot step out.
        load: () => api.list('/organizations/'),
        builder: (context, rows, reload) => ListView(
          children: [
            tile(
              id: null,
              title: 'Every organisation',
              subtitle: 'Read every tenant, write for none',
            ),
            const Divider(),
            for (final row in rows)
              tile(
                id: row['id'] as int,
                title: '${row['name']}',
                subtitle: [
                  '${row['kind']}',
                  if ('${row['category'] ?? ''}'.isNotEmpty) '${row['category']}',
                  '${row['phone']}',
                ].join(' · '),
              ),
          ],
        ),
      ),
    );
  }
}
