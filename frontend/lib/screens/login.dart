import 'package:flutter/material.dart';

import '../api.dart';
import '../main.dart';
import '../ui.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _phone = TextEditingController();
  final _password = TextEditingController();
  bool _busy = false;
  bool _hidePassword = true;

  @override
  void dispose() {
    _phone.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _busy = true);
    try {
      await ApiScope.of(context).signIn(_phone.text, _password.text);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Form(
              key: _formKey,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Icon(
                    Icons.local_hospital,
                    size: 56,
                    color: Theme.of(context).colorScheme.primary,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'PPP Supply',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.headlineMedium,
                  ),
                  Text(
                    'Hospital and supplier procurement',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                    ),
                  ),
                  const SizedBox(height: 28),
                  if (api.signedOutForIdle) ...[
                    Card(
                      child: ListTile(
                        leading: Icon(
                          Icons.timer_off_outlined,
                          color: Theme.of(context).colorScheme.error,
                        ),
                        title: const Text('Signed out'),
                        subtitle: Text(
                          'The session ended after ${api.idleTimeout.inMinutes} minutes without activity.',
                        ),
                      ),
                    ),
                    const SizedBox(height: 12),
                  ],
                  TextFormField(
                    controller: _phone,
                    keyboardType: TextInputType.phone,
                    decoration: const InputDecoration(
                      labelText: 'Phone number',
                      prefixIcon: Icon(Icons.phone),
                    ),
                    validator: (value) =>
                        (value ?? '').trim().length < 6 ? 'Enter your phone number' : null,
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _password,
                    obscureText: _hidePassword,
                    onFieldSubmitted: (_) => _submit(),
                    decoration: InputDecoration(
                      labelText: 'Password',
                      prefixIcon: const Icon(Icons.lock_outline),
                      suffixIcon: IconButton(
                        icon: Icon(_hidePassword ? Icons.visibility : Icons.visibility_off),
                        onPressed: () => setState(() => _hidePassword = !_hidePassword),
                      ),
                    ),
                    validator: (value) => (value ?? '').isEmpty ? 'Enter your password' : null,
                  ),
                  const SizedBox(height: 20),
                  FilledButton(
                    onPressed: _busy ? null : _submit,
                    child: _busy
                        ? const SizedBox(
                            height: 18,
                            width: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Text('Sign in'),
                  ),
                  const SizedBox(height: 8),
                  TextButton(
                    onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(builder: (_) => const RegisterHospitalScreen()),
                    ),
                    child: const Text('Register a hospital / organisation'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class RegisterHospitalScreen extends StatefulWidget {
  const RegisterHospitalScreen({super.key});

  @override
  State<RegisterHospitalScreen> createState() => _RegisterHospitalScreenState();
}

class _RegisterHospitalScreenState extends State<RegisterHospitalScreen> {
  final _formKey = GlobalKey<FormState>();
  final _fields = {
    'name': TextEditingController(),
    'phone': TextEditingController(),
    'address': TextEditingController(),
    'email': TextEditingController(),
    'registration_no': TextEditingController(),
    'admin_full_name': TextEditingController(),
    'admin_phone': TextEditingController(),
    'password': TextEditingController(),
  };
  bool _busy = false;

  @override
  void dispose() {
    for (final controller in _fields.values) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _busy = true);
    try {
      await ApiScope.of(
        context,
      ).registerHospital(_fields.map((key, value) => MapEntry(key, value.text.trim())));
      if (mounted) Navigator.pop(context);
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _field(String key, String label, {bool required = true, bool obscure = false}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextFormField(
        controller: _fields[key],
        obscureText: obscure,
        decoration: InputDecoration(labelText: label),
        validator: (value) =>
            required && (value ?? '').trim().isEmpty ? '$label is required' : null,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Register organisation')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Bounded(
          maxWidth: 480,
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text('Organisation', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                _field('name', 'Hospital / organisation name'),
                _field('phone', 'Organisation phone'),
                _field('address', 'Address', required: false),
                _field('email', 'Email', required: false),
                _field('registration_no', 'Registration number', required: false),
                const SizedBox(height: 8),
                Text('Administrator', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                _field('admin_full_name', 'Full name'),
                _field('admin_phone', 'Login phone number'),
                _field('password', 'Password', obscure: true),
                const SizedBox(height: 8),
                FilledButton(
                  onPressed: _busy ? null : _submit,
                  child: const Text('Create account'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class ChangePasswordDialog extends StatefulWidget {
  const ChangePasswordDialog({super.key, this.forced = false});

  final bool forced;

  @override
  State<ChangePasswordDialog> createState() => _ChangePasswordDialogState();
}

class _ChangePasswordDialogState extends State<ChangePasswordDialog> {
  final _current = TextEditingController();
  final _next = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    _current.dispose();
    _next.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() => _busy = true);
    try {
      await ApiScope.of(context).changePassword(_current.text, _next.text);
      if (mounted) {
        Navigator.pop(context);
        showDone(context, 'Password changed.');
      }
    } catch (error) {
      if (mounted) showError(context, error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: !widget.forced,
      child: AlertDialog(
        title: const Text('Change password'),
        content: SizedBox(
          width: dialogWidth(context, 380),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (widget.forced)
                  const Padding(
                    padding: EdgeInsets.only(bottom: 12),
                    child: Text(
                      'Your account uses a password set by someone else. Choose your own.',
                    ),
                  ),
                TextField(
                  controller: _current,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: 'Current password'),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _next,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: 'New password'),
                ),
              ],
            ),
          ),
        ),
        actions: [
          if (!widget.forced)
            TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: _busy ? null : _submit, child: const Text('Save')),
        ],
      ),
    );
  }
}

/// Shown from the More tab; kept here so all account screens live together.
class ProfileTile extends StatelessWidget {
  const ProfileTile({super.key});

  @override
  Widget build(BuildContext context) {
    final api = ApiScope.of(context);
    final user = api.user ?? const <String, dynamic>{};
    return ListTile(
      leading: const CircleAvatar(child: Icon(Icons.person)),
      title: Text('${user['full_name']}'),
      subtitle: Text('${user['phone']} · ${user['role']} · ${user['organization_name'] ?? ''}'),
    );
  }
}

Future<void> signOutFlow(BuildContext context, Api api) async {
  if (await confirm(context, 'Sign out', 'End this session?')) {
    await api.signOut();
  }
}
