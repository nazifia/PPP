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

A hospital splits itself into `Department`s, and a department into `Unit`s. A request
carries one or the other — `department` or `unit` — and a department stands for its own
units when anything is counted or filtered, so tagging the unit is enough: unit staff
raise their requests by picking their unit, and the department still sees them. Nesting stops
there: a unit holds no units of its own. Department names are unique within an
organisation and unit names within their department, so two departments may each keep
their own `HAEMATOLOGY`.

An organisation administrator opens the accounts inside it, edits any detail of one —
name, phone, email, job title and role, between `ADMIN` and `STAFF` — resets a forgotten password (the owner must change it at next login)
and disables an account — `DELETE /users/{id}/`, which also cuts the login token.
`PATCH /users/{id}/` with `is_active: true` turns it back on. Two things are refused, so a
tenant can never be locked out of its own account management: nobody disables their own
account, and the last active administrator can neither be demoted nor disabled — promote
someone else first.

A hospital can only trade with a company it has registered — that link is a
`Partnership`, and every query is filtered through it. A supplier sees the requests
addressed to it and nothing else; it never sees another hospital's drafts, and a
hospital never sees another hospital's requests.

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

Every state change is written to an append-only `AuditLog`.

## Payments

The platform holds no money and talks to no bank. It records the two halves of a payment
that happened elsewhere, so an invoice never reads as settled on one side's word alone:

```
                  ┌──confirm──▶ CONFIRMED   invoice.amount_paid grows
record ──▶ PENDING┤
                  └──reject───▶ REJECTED    nothing moves; may be recorded again
```

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

An invoice therefore carries four figures: `amount`, `amount_paid` (confirmed), `balance`
(`amount - amount_paid`) and `amount_pending` (declared, undecided). A part payment is
ordinary — pay an invoice in as many instalments as the two sides agree.

Nothing here moves money or talks to a bank. Every payment is entered by hand and confirmed
by hand.

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
`receipts/<year>/<month>/<payment reference>`. Django serves them itself only while
`DEBUG` is on.

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
| `POST /auth/register/` | Public hospital sign-up (organisation + first admin) |
| `POST /auth/login/` `POST /auth/logout/` | Phone + password session |
| `GET/PATCH /auth/me/`, `POST /auth/change-password/` | Own account |
| `GET/PATCH /organization/` | Own organisation profile |
| `GET /dashboard/` | Counts, outstanding money, recent activity, and for a hospital what each department was invoiced |
| `/companies/` | Hospital registers and manages supplier companies (administrators only; `DELETE` suspends trading, `POST /companies/{id}/reactivate/` resumes it) |
| `/users/` | Staff accounts (everyone reads; administrators write), `POST /users/{id}/reset_password/` |
| `/departments/` | Hospital departments (everyone reads; administrators write) |
| `/units/` | Units of those departments (`?department=<id>`; administrators write) |
| `/products/` | Supplier catalogue (administrators write, `POST /products/{id}/restock/`); hospitals get a read-only view of partners |
| `/requisitions/` | Requests (`?status=`, `?department=<id>` — covers its units — or `?unit=<id>`), plus `submit/`, `decide/` and `release/` (supplier administrators), `dispatch/`, `cancel/`, `wishlist/`, `wishlist/add/` |
| `/requisition-lines/` | Line editing while the request is a draft |
| `/deliveries/` | Consignments, plus `verify/` |
| `/invoices/` | Invoices (`?status=`, `?overdue=true`), plus `pay/` (hospital records a payment) and `payments/` |
| `/payments/` | Payment ledger (`?status=`, `?invoice=<id>`), plus `confirm/` and `reject/` (supplier) |
| `/stock-movements/` | Stock ledger (`?product=<id>`, `?kind=`, `?from=`/`?to=` as inclusive dates), plus `balances/` for the per-item totals and `dispense/` (hospital records what it handed out) |
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

- `DEBUG = False`, a real `SECRET_KEY` from the environment, and `ALLOWED_HOSTS` filled in.
- Replace `CORS_ALLOW_ALL_ORIGINS` with an explicit `CORS_ALLOWED_ORIGINS` list.
- Serve over HTTPS and remove `android:usesCleartextTraffic` from the Android manifest.
- Hand `/media/` (payment receipts) to the web server or object storage; Django only serves
  it while `DEBUG` is on, and the files are not access-checked, so keep the paths unguessable.
- Install a TrueType font carrying the naira sign (`fonts-dejavu-core` on Debian and
  Ubuntu), or every PDF will read `NGN` where it should read `₦`.
- Move off SQLite if more than one hospital will be writing at once.
"# PPP" 
