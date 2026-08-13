# PPP Supply

Multi-tenant procurement between hospitals and the supplier companies they register:
medicines, laboratory reagents and consumables.

- **backend/** — Django 5.2 + Django REST Framework, SQLite, token auth on phone + password.
- **frontend/** — Flutter app (Android, iOS, web, desktop) for both hospital and supplier users.

## Run it

```bash
# backend
cd backend
.venv/Scripts/activate           # Windows; use source .venv/bin/activate elsewhere
python manage.py migrate
python manage.py seed_demo       # optional demo tenants
python manage.py runserver 0.0.0.0:8000

# frontend
cd frontend
flutter run                      # or: flutter run -d chrome --dart-define=API_BASE=http://localhost:8000/api
```

The app defaults to `http://10.0.2.2:8000/api` on Android (the emulator's route to the host)
and `http://localhost:8000/api` elsewhere. Change it at any time from the login screen or
the More tab — useful when the server sits on another machine on the ward network.

Demo accounts (`seed_demo`), all with password `Passw0rd!2026`:

| Phone | Who |
|---|---|
| 08010000002 | Hospital administrator |
| 08010000003 | Hospital staff |
| 08030000001 | Valour Pharmaceuticals (supplier) |
| 08030000002 | DCL Lab Products (supplier) |

## How the tenancy works

One `Organization` table holds both kinds of tenant, told apart by `kind`:
`HOSPITAL` or `SUPPLIER`. A user belongs to exactly one organisation and is either
`ADMIN` or `STAFF` within it.

Suspending an organisation (`is_active`) shuts it out at once, not at the next login: the
sessions already open are cut on their next request, however the suspension was made — the
API, the admin site or a shell.

A hospital splits itself into `Department`s, and a department into `Unit`s. A request
carries one or the other — `department` or `unit` — and a department stands for its own
units when anything is counted or filtered, so tagging the unit is enough: unit staff
raise their requests by picking their unit, and the department still sees them. Nesting stops
there: a unit holds no units of its own. Department names are unique within an
organisation and unit names within their department, so two departments may each keep
their own `HAEMATOLOGY`.

Clearing `is_active` on a department or a unit retires it: no new request may be tagged with
it, and retiring a department closes the units under it, since a department stands for its
units here as it does everywhere else. Nothing already tagged with it moves, an open draft
that carries it stays editable, and it keeps its place in the list so it can be brought
back. The dispensing units and formulations work the same way — a retired term describes no
new item, and the items already described by it keep it.

An organisation administrator opens the accounts inside it, edits any detail of one —
name, phone, email, job title and role, between `ADMIN` and `STAFF` — resets a forgotten
password (held to the same validators as sign-up, and the owner must change it at next login)
and disables an account — `DELETE /users/{id}/`, which also cuts the login token.
`PATCH /users/{id}/` with `is_active: true` turns it back on. Two things are refused, so a
tenant can never be locked out of its own account management: nobody disables their own
account, and the last active administrator can neither be demoted nor disabled — promote
someone else first.

A hospital can only trade with a company it has a link to — that link is a
`Partnership`, and every query is filtered through it. A supplier sees the requests
addressed to it and nothing else; it never sees another hospital's drafts, and a
hospital never sees another hospital's requests.

There are two ways to open the link. `POST /companies/` registers a company that is not on
the platform yet, creating its account along with the link. `POST /companies/link/` with a
`phone` opens the link to a company already there, because another hospital registered it
first — a supplier serves as many hospitals as it likes, and nothing is created but the
link. The phone number is what names it, which is what the company hands out.

The company is not asked, on either road: knowing its phone number is the whole of the
check, exactly as it is when a hospital registers one outright. That is a deliberate
choice, not an oversight — if a supplier should be able to refuse a hospital, the link
needs a pending state and an inbox to decide it from, and neither is here.

## The request lifecycle

```
DRAFT ──submit──▶ SUBMITTED ──decide──┬─▶ APPROVED ──────┐
(wishlist)                            ├─▶ PARTIALLY_APPROVED ─┼─dispatch─▶ DISPATCHED
                                      └─▶ REJECTED           │
                                                             ▼
                                          CLOSED ◀──verify── DELIVERED
```

1. **Wishlist.** Hospital staff browse the catalogues of their partner companies and add
   items. The catalogue carries a *Requesting for* picker holding one department or unit
   for the whole browse, and each item lands in the open draft for the company that sells
   it *under that tag*, so one browse can build several requests at once and haematology's
   basket never swallows the laboratory's. One open draft per person, company and
   department: a second would be a basket the catalogue never reaches. Submitting frees
   the slot. A draft can be moved to another department or unit from its detail screen
   (`PATCH /requisitions/{id}/` with `department` and `unit`, the unused one set to null),
   as long as no other open draft already holds that tag.
2. **Submit.** Prices are frozen onto the request at this moment, so a later catalogue
   edit cannot change what was agreed.
3. **Decide.** A company administrator approves a quantity per line, capped by what is *available*:
   stock on hand less what earlier approvals already hold. Approving reserves that
   quantity, so the same carton cannot be promised to two hospitals. Lines left out of the
   decision count as rejected — silence is never a promise.
   All zero → `REJECTED`; some short → `PARTIALLY_APPROVED`; everything in full → `APPROVED`.
4. **Dispatch.** The company ships all or part of what it approved, with batch numbers and
   expiry dates. Stock leaves the shelf here, not at approval, the reservation is released by
   the same amount, and a `StockMovement` records it. A request can carry several consignments.
5. **Verify.** The hospital counts what arrived and accepts or rejects each line; a rejected
   quantity needs a written reason. Accepted goods enter the hospital's stock ledger and an
   invoice is raised for exactly what was accepted. Rejected goods go back to the company's
   stock and stay reserved, since they are still owed on the request.
6. **Close.** Once every approved quantity has been accepted, the request closes.
7. **Dispense.** Hospital staff record what a ward hands out, which is what draws the goods
   back out of the hospital's ledger. No approval: the item has already been used. A
   hospital owns no catalogue row, so its ledger is the only record of what it holds, and
   nothing may be dispensed beyond what that ledger adds up to.
8. **Write off.** Dispensing means a ward used the goods. Everything else that takes
   something off the shelf — a drug past its date, a broken vial, a count that never
   matched the book — is `POST /stock-movements/adjust/`: a signed quantity and a reason,
   from a hospital administrator. Without it the ledger drifts away from the shelf and
   never comes back. The supplier's half of this is `POST /products/{id}/restock/`.

On the company's side the same rule holds from the item's first day: a catalogue item added
with `stock_qty` on it opens its ledger with that figure, and after that `restock/` is the
only thing that moves it. Both sides therefore have a ledger that adds up to what is on the
shelf, and a supplier's `stock_qty` is a running total of its own movements rather than a
second, quietly editable figure beside them.

If a company approves something and then cannot ship it, a company administrator posts
`POST /requisitions/{id}/release/`
with a reason, which cuts each line back to what actually went out and frees the reservation, so
held stock cannot be stranded on a request nobody will fulfil. Anything already dispatched
is untouched and the hospital still verifies it; if nothing had shipped, the request is
cancelled.

Nobody remembers to press that button, so run the sweep daily from cron or Task Scheduler:

```bash
python manage.py release_stale_approvals --days 14        # --dry-run to see it first
```

It puts every approval older than `--days` through the same release, acting as whoever
decided the request, so the audit trail reads the same as a manual one.

Its counterpart is quieter, and deliberately so:

```bash
python manage.py stale_deliveries --days 7
```

An approval nobody ships holds stock the company could have sold, which is why the sweep
above frees it. A consignment nobody verifies holds nothing — the goods have already left —
but no invoice is raised until somebody says what arrived, so the company is owed money it
cannot ask for and the hospital is holding stock its own ledger has never heard of. This
only names them: accepting goods on a hospital's behalf would raise a real debt on nobody's
word, so what it prints is a list to walk down the corridor about.

Every state change is written to an append-only `AuditLog`.

## Being told about it

Nothing is sent anywhere. The platform has no mail server and no SMS account, and a
notification table would be a second copy of figures the dashboard already counts — kept up
to date by hand, and wrong the first time somebody forgot to write to it. So the app badges
its own tabs from `GET /dashboard/`: requests waiting on a supplier's decision, consignments
waiting at a hospital's door, and payments and overdue invoices behind **More**. The counts
are refreshed when the shell is touched — on sign-in and on every tab change — so they are
never stale by more than one tap.

That means nobody is told anything while the app is shut. The two cron sweeps above are what
covers the long silences; a real channel (email needs an address, which is optional and
often blank, so in practice it means SMS and a provider account) is the next step if that
turns out not to be enough.

## Batches, expiry and reorder levels

A dispatch is marked with a batch number and an expiry date, and both now travel with
the goods onto whichever ledger they land on — the hospital's when a line is accepted,
the supplier's when it is rejected and goes back. A hospital owns no catalogue row, so
the ledger is the only place it could hold them.

```
GET /stock-movements/expiring/?days=90    # soonest first, already-expired included
```

Receipts rather than balances: a dispense names no batch, so the ledger cannot say how
much of one particular batch is left. This is the shelf-check list — what came in and
when it goes out of date — and the pharmacist counts the rest.

Each catalogue item carries its own `reorder_level`, and the dashboard's low-stock tile
answers to that rather than to one figure for everything: ten cartons of gauze and ten
vials of adrenaline are not the same alarm. An item that names none falls back to 10. The
comparison is against the *available* quantity, not what is on the shelf, since stock
already promised to an approved request cannot be sold to anybody else.

## Payments

The platform holds no money and talks to no bank. It records the two halves of a payment
that happened elsewhere, so an invoice never reads as settled on one side's word alone:

```
                  ┌──confirm───▶ CONFIRMED   invoice.amount_paid grows
record ──▶ PENDING┼──reject────▶ REJECTED    supplier: never arrived
                  └──withdraw──▶ WITHDRAWN   hospital: taken back
```

Neither ending moves money, and both free the reference to be recorded again.

1. **Record.** A hospital administrator posts `POST /invoices/{id}/pay/` with the `amount`,
   the `method` (`TRANSFER`, `CASH`, `CHEQUE`, `POS`), a `payer_reference`, an optional
   `note` and an optional `receipt`. Everything but cash needs the reference — it is what
   the company looks up in its bank, and without it there is nothing to confirm. The same
   reference cannot be entered twice on one invoice.
2. **Wait.** The payment sits `PENDING`. It counts against `amount_unclaimed`, so the same
   money cannot be declared twice, but not against `amount_paid`: the invoice stays `UNPAID`.
3. **Confirm.** A supplier administrator posts `POST /payments/{id}/confirm/`. Only now does
   `amount_paid` grow and the invoice move `UNPAID` → `PART_PAID` → `PAID`.
4. **Reject.** `POST /payments/{id}/reject/` with a `reason` if the money never arrived. The
   invoice does not move and the reference is freed, so a corrected entry can be recorded.
5. **Withdraw.** `POST /payments/{id}/withdraw/` is the hospital's half of the same door,
   for an entry against the wrong invoice or for the wrong amount. It works only while the
   payment is `PENDING` — once the supplier has decided, the decision stands — and without
   it a keying mistake would sit there holding down `amount_unclaimed` until the supplier
   troubled itself to reject it. The withdrawn entry stays on the ledger, so the correction
   reads as a correction.

An invoice therefore carries four figures: `amount`, `amount_paid` (confirmed), `balance`
(`amount - amount_paid`) and `amount_pending` (declared, undecided). A part payment is
ordinary — pay an invoice in as many instalments as the two sides agree.

Nothing here moves money or talks to a bank. Every payment is entered by hand and confirmed
by hand.

### Credit notes

Verification catches what is visible while the van is still at the door: a broken seal, a
short count, the wrong item. It cannot catch a batch that turns out to be counterfeit, or
one that was already three weeks from its date when it arrived, because nobody knows that
yet. A credit note is how those are put right after the invoice was raised:

```
                       ┌──confirm──▶ CONFIRMED   invoice.amount falls, stock written down
raise ──▶ PENDING──────┤
                       └──reject───▶ REJECTED    nothing moves
```

A hospital administrator posts `POST /invoices/{id}/credit/` naming delivery lines, a
quantity on each and a `reason`; `GET /invoices/{id}/creditable/` says what is still open
after earlier notes, pending ones included, so the same carton is never credited twice. The
supplier accepts it with `POST /credits/{id}/confirm/` or refuses it with `reject/` and a
reason — the same two-sided rule as a payment, because it moves the same money.

An accepted note takes its amount off `invoice.amount` and writes the goods out of the
hospital's ledger as a `RETURN`. They do not go back on the supplier's shelf: bad goods are
not stock, and where they physically end up is between the two of them.

A credit is capped at what is still owed. The platform moves no money, so it cannot hand any
back — a credit larger than the balance is a refund for the two sides to settle themselves,
and the refusal says so.

### Terms and due dates

Each supplier keeps a `payment_terms_days` (default 30) on its organisation profile. An
invoice copies it as `terms_days` and dates itself `due_date = today + terms_days` when it
is raised, so changing the terms later never moves a date already agreed. `days_overdue` and
`is_overdue` are worked out from that date and are always zero once the invoice is `PAID`.

Chase the late ones with `GET /invoices/?overdue=true` — unpaid, past due, oldest first —
or from the dashboard's overdue tile.

### Receipts

A payment may carry a photo or scan of the teller slip, transfer advice or cheque: `jpg`,
`jpeg`, `png`, `webp`, `heic` or `pdf`, up to 5 MB, posted as multipart on the same
`pay/` endpoint. Files land in `MEDIA_ROOT` (`backend/media/`) under
`receipts/<year>/<month>/<payment reference>`.

Nothing serves `MEDIA_ROOT` by path. The slip comes back through the API instead, and
the `receipt` field on a payment is that address rather than a file path:

```
GET /api/payments/{id}/receipt/   →  the image or PDF
```

It goes through the payment viewset, so the same rule that decides who may read a
payment decides who may see its slip — the hospital that recorded it and the supplier
being asked to confirm it, and nobody else.

## Printing

Six documents print: the purchase request, the delivery note, the invoice, the
payment receipt, and the two ledgers — stock and audit. One A4 sheet each, from
one template, in either of two forms:

- **HTML** — opens the browser's own print dialog on load.
- **PDF** — the same sheet rendered by xhtml2pdf, with the document name and
  `page n of m` on every page and the table header repeated on each. Add
  `&format=pdf` to the link.

The app prints straight to the printer. The printer icon fetches the PDF over
the ordinary API, token and all, and hands the bytes to the platform's print
dialog — no browser tab, no link to open, and the same dialog is where a device
offers to save it as a file instead:

```
GET /api/requisitions/{id}/print/   →  application/pdf
GET /api/deliveries/{id}/print/
GET /api/invoices/{id}/print/
GET /api/payments/{id}/print/
GET /api/stock-movements/print/?kind=RECEIPT&from=…&to=…
GET /api/audit-logs/print/?entity=Requisition
```

A browser cannot send that header, so for a browser each of those also has a
`print-link/` twin handing back a signed URL to the HTML sheet — add
`&format=pdf` for the PDF of it:

```
GET /api/invoices/{id}/print-link/   →  {"url": "…/print/invoice/{id}/?s=…", "expires_in": 600}
```

Both routes go through the viewset that already reads the row, so a hospital
can only print paper it may already see, and the audit trail stays where it was
— with administrators, checked again when the sheet is drawn. A signature is
good for ten minutes and for that one document. A ledger's filters ride
*inside* the signature rather than on the address, so nobody can widen a sheet
to rows the screen never offered; a printed ledger stops at 500 rows and says
so.

Only a *confirmed* payment prints as a `PAYMENT RECEIPT`. Anything else prints
as a `PAYMENT ADVICE` banner-marked `NOT CONFIRMED`, so the hospital's own word
that it paid can never be filed as the supplier's acknowledgement.

The naira sign is not in reportlab's built-in fonts, so the PDF looks for a
TrueType face that has it (DejaVu Sans on Linux, Arial on Windows or macOS). If
the machine has none, amounts read `NGN` instead of `₦`.

## API

Everything lives under `/api/`. Authenticate with `Authorization: Token <key>`.

| Endpoint | Purpose |
|---|---|
| `GET /health/` | Is the process up and can it still reach its database. No token — whatever watches it holds none |
| `POST /auth/register/` | Public hospital sign-up (organisation + first admin) |
| `POST /auth/login/` `POST /auth/logout/` | Phone + password session (10 attempts a minute per address; sign-up, 5 an hour) |
| `GET/PATCH /auth/me/`, `POST /auth/change-password/` | Own account |
| `GET/PATCH /organization/` | Own organisation profile |
| `GET /dashboard/` | Counts, outstanding money, recent activity, and for a hospital what each department was invoiced |
| `/companies/` | Hospital registers and manages supplier companies (administrators only; `POST /companies/link/` with a `phone` trades with one already on the platform, `DELETE` suspends trading, `POST /companies/{id}/reactivate/` resumes it) |
| `/users/` | Staff accounts (everyone reads; administrators write), `POST /users/{id}/reset_password/` |
| `/departments/` | Hospital departments (everyone reads; administrators write) |
| `/units/` | Units of those departments (`?department=<id>`; administrators write) |
| `/products/` | Supplier catalogue — one row per generic name, brand and strength (administrators write; `stock_qty` is opening stock only, after which `POST /products/{id}/restock/` is the one way it moves); hospitals get a read-only view of partners |
| `/requisitions/` | Requests (`?status=`, `?department=<id>` — covers its units — or `?unit=<id>`), plus `submit/`, `decide/` and `release/` (supplier administrators), `dispatch/`, `cancel/`, `wishlist/`, `wishlist/add/` |
| `/requisition-lines/` | Line editing while the request is a draft. The quantity moves; the item does not — remove the line and add the other one |
| `/deliveries/` | Consignments, plus `verify/` |
| `/invoices/` | Invoices (`?status=`, `?overdue=true`), plus `pay/` (hospital records a payment) and `payments/` |
| `/payments/` | Payment ledger (`?status=`, `?invoice=<id>`), plus `confirm/` and `reject/` (supplier), `withdraw/` (hospital, while still pending) and `receipt/` for the attached slip |
| `/credits/` | Credit notes (`?status=`, `?invoice=<id>`), plus `confirm/` and `reject/` (supplier). Raised at `POST /invoices/{id}/credit/`; `GET /invoices/{id}/creditable/` says what is still open |
| `/stock-movements/` | Stock ledger (`?product=<id>`, `?kind=`, `?from=`/`?to=` as inclusive dates), plus `balances/` for the per-item totals, `dispense/` (hospital records what it handed out), `adjust/` (hospital writes stock off) and `expiring/?days=90` (the shelf check) |
| `/audit-logs/` | Audit trail (administrators) |
| `…/print/`, `…/print-link/` | The sheet as a PDF, and a signed ten-minute link to it for a browser — on a requisition, delivery, invoice or payment, and on the `/stock-movements/` and `/audit-logs/` lists (see Printing) |

Phone numbers are normalised on the way in, so `0803 123-4567` and `08031234567` are the
same account.

## Tests

```bash
cd backend && python manage.py test core     # lifecycle, stock, tenant isolation
cd frontend && flutter test                  # app shell smoke tests
```

## Before deploying

Set `DJANGO_ENV=prod` and the settings file flips over by itself: `DEBUG` off, secure
cookies, HSTS, HTTPS redirect, and CORS narrowed to the origins you name. It refuses to
start until the two things it cannot guess are in the environment:

```bash
DJANGO_ENV=prod
DJANGO_SECRET_KEY=<long random value>
DJANGO_ALLOWED_HOSTS=api.example.com,example.com
DJANGO_CORS_ORIGINS=https://example.com      # browser clients, also CSRF-trusted
DJANGO_DB_PATH=/var/lib/ppp/db.sqlite3       # optional, defaults next to manage.py
DJANGO_SSL_REDIRECT=0                        # optional, if the proxy already redirects
```

A superuser with the admin site but no shell can flip the same switch from **Runtime mode**
in the admin header (`/admin/env/`), which writes `dev` or `prod` to `backend/.django_env`.
It refuses to save `prod` while the secret key or the allowed hosts are missing, since that
would leave the next start raising `ImproperlyConfigured` with no admin left to fix it from.
Either way the mode is read once, at startup, so a change needs a restart; and `DJANGO_ENV`
in the environment wins over the file wherever it is set.

`python manage.py check --deploy` with those set should come back clean. The rest is
outside Django:

- Serve over HTTPS and remove `android:usesCleartextTraffic` from the Android manifest.
- Give `MEDIA_ROOT` (payment receipts) a volume that survives a redeploy. Do *not* put it
  behind a `/media/` route: the API serves those files itself and checks who is asking.
- Install a TrueType font carrying the naira sign (`fonts-dejavu-core` on Debian and
  Ubuntu), or every PDF will read `NGN` where it should read `₦`.
- The login and sign-up throttles count attempts in Django's cache, which defaults to
  local memory — that is per worker process. Point `CACHES` at something shared (Redis,
  Memcached) if you run more than one, or the rate is multiplied by however many there are.
- Move off SQLite to MySQL if more than one hospital will be writing at once (`pip install
  mysqlclient`, then swap `DATABASES['default']` for the `django.db.backends.mysql` engine).
  SQLite is opened `IMMEDIATE` with WAL on, which is what makes the `select_for_update()`
  calls in `core/services.py` mean anything — SQLite has no row locks, and Django drops
  the clause silently rather than refusing it, so without that the stock reservations
  would be read-then-write races.
"# PPP" 
