# POS -> Multi-tenant SaaS: backend migration guide

Architecture: **one database, one schema, a `tenant_id` on every business table**,
enforced by a fail-closed default manager. Tenant comes from the *logged-in user*
(never from a client-chosen header/subdomain). Billing is M-Pesa STK push with our
own invoice/subscription ledger.

Drop `tenants/`, `billing/`, `platform_admin/` next to your existing apps.

## 1. settings.py

```python
INSTALLED_APPS += ["tenants", "billing", "platform_admin"]

MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "tenants.middleware.TenantMiddleware",            # NEW (after auth)
    "billing.middleware.SubscriptionGateMiddleware",  # NEW (after tenant)
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Migration phase A only -- REMOVE after backfill (see step 3)
TENANCY_ALLOW_NULL_TENANT = env.bool("TENANCY_ALLOW_NULL_TENANT", default=False)

# Billing
BILLING_CALLBACK_BASE_URL = env("BILLING_CALLBACK_BASE_URL")        # https://yourapp.onrender.com
MPESA_BILLING_CALLBACK_SECRET = env("MPESA_BILLING_CALLBACK_SECRET")  # long random string
BILLING_EXEMPT_PATH_PREFIXES = ["/users/logout/"]                    # add your logout URL
# MPESA_TRANSACTION_TYPE = "CustomerBuyGoodsOnline"  # if you collect on a Till, not a Paybill
# BILLING = {"TERM_DISCOUNT_PERCENT": {1: 0, 3: 10, 6: 20, 12: 30}, "TRIAL_DAYS": 0}
```
Also delete `STATICFILES_STORAGE = ...` -- it was removed in Django 5.1+; your `STORAGES` dict already does the job.
`pip install requests` (used by `billing/mpesa.py`).

`config/urls.py`:
```python
path("signup/",   include("tenants.urls")),
path("billing/",  include("billing.urls")),
path("platform/", include("platform_admin.urls")),
```

## 2. Model changes (existing apps)

**users/models.py**
```python
from tenants.models import TenantOwnedModel

class User(AbstractUser):
    ...
    pin = models.CharField(max_length=128, blank=True, null=True, ...)   # was 28 -- see bug note
    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="users")
    is_owner = models.BooleanField(default=False)   # the billing owner; only they see /billing/
    # Phase B (after backfill) add to Meta.constraints:
    #   CheckConstraint(condition=Q(is_superuser=True) | Q(tenant__isnull=False), name="user_has_tenant")
    #   UniqueConstraint(fields=["tenant"], condition=Q(is_owner=True), name="one_owner_per_tenant")

class ActivityLog(TenantOwnedModel):
    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True, editable=False,
                               on_delete=models.SET_NULL, related_name="activity_logs")
    tenant_required = False        # failed logins have no tenant yet
    ...
```
> **Bug to fix now:** `pin` is `max_length=28` but a `pbkdf2_sha256$...` hash is ~90 chars.
> SQLite ignores the limit; PostgreSQL will raise `value too long` the first time a PIN is saved in production.

**Everything else:** change the base class and swap global-unique fields for per-tenant ones.

| Model | Change |
|---|---|
| `inventory.Branch` | `(TenantOwnedModel)`; `name` -> drop `unique=True`, add `UniqueConstraint(fields=["tenant","name"])` |
| `inventory.StockLevel, StockAdjustment, PurchaseOrder, PurchaseOrderItem, PurchasePayment, InventoryTransfer, InventoryTransferItem` | `(TenantOwnedModel)` |

| `PurchaseOrder.po_number`, `InventoryTransfer.transfer_number` | drop `unique=True`; `UniqueConstraint(fields=["tenant","po_number"])` etc. |
| `supplier.Supplier` | `(TenantOwnedModel)` |
| `sales.CashRegisterSession, CashTransaction, Order, OrderItem` | `(TenantOwnedModel)` |
| `Order.invoice_number` | drop `unique=True`; `UniqueConstraint(fields=["tenant","invoice_number"])`; generate with `next_number(tenant, "invoice", "INV-")` so every shop starts at 000001 |
| `products.*` (I haven't seen this app) | same treatment; `sku`/`barcode`/category name become unique **per tenant** |

Keep your existing `Meta.constraints` / `unique_together` as they are (they hang off `branch`, which is already tenant-owned).
`PurchasePayment.cash_transaction` (`OneToOneField('sales.CashTransaction')`) needs no change.

## 3. Zero-downtime data migration (2 phases)

```bash
# Phase A -- columns nullable
export TENANCY_ALLOW_NULL_TENANT=True
python manage.py makemigrations tenants billing users inventory supplier products sales
python manage.py migrate

# Backfill: today's data becomes ONE tenant with permanent free access
python manage.py backfill_legacy_tenant --name "<your existing customer's business name>"

# Phase B -- make tenant NOT NULL and swap the unique constraints
unset TENANCY_ALLOW_NULL_TENANT      # (or set False)
python manage.py makemigrations
python manage.py migrate
```
Take a `pg_dump` first and rehearse on a copy. Never deploy with the flag left on.

## 4. Authentication changes

* **Owner** signs up at `/signup/`; `username = email`, `role=ADMIN`, `is_owner=True`, `is_staff=False`.
* **Staff** are created with `tenants.services.create_staff_user(...)`; stored username is `<business_code>__<handle>` (Django's username is globally unique). Change `CashierPinLoginForm` to have **Business code + Username + PIN** and call `authenticate(username=compose_username(code, handle), pin=pin)`.
* Send me `users/backends.py`, `users/forms.py`, `users/mixins.py`, `users/utils.py` and I'll patch them (the PIN backend must also reject users whose tenant `has_access` is false, and `log_action` should record `tenant`).
* Always query users via `User.objects.filter(tenant=request.tenant)` in staff-management screens.

## 5. What each piece does

| Requirement | Implementation |
|---|---|
| Isolation | `TenantOwnedModel.objects` filters by the ContextVar tenant; no tenant => empty queryset. `save()` refuses cross-tenant writes. `all_objects` is the explicit escape hatch (admin, cron). |
| Resolve tenant | `TenantMiddleware` (user -> tenant). Optional `X-Tenant` header is only a consistency check. Superusers get a tenant only in an audited support session. |
| Provisioning | `register_business()` is one atomic transaction: Tenant, owner, "Main Branch", Subscription (PENDING), first Invoice. Extend `provision_workspace()`. |
| Pricing | `billing/pricing.py`, driven by `settings.BILLING`. Installation fee is added to the first invoice only. |
| Billing events | `BillingEvent` rows + `billing_event` signal (hook SMS/email there). |
| Lock-out | `SubscriptionGateMiddleware` + `Subscription.grants_access()`, computed from dates (so a late cron never gives free access). |
| Renewals | `python manage.py run_billing_cycle` daily (Render Cron Job). |
| Super admin | `/platform/` JSON API + Django admin; actions: comp days, custom price, suspend, disable, support session. |

Event names: `subscription.created`, `invoice.created`, `invoice.payment_succeeded`, `invoice.payment_failed`, `invoice.payment_underpaid`, `subscription.activated`, `subscription.past_due`, `subscription.suspended`, `subscription.deleted` (cancelled), `subscription.reactivated`, `override.*`, `support.*`.

## 6. Things to know

* **M-Pesa cannot auto-debit.** "Recurring" = we open a renewal invoice 7 days early, the owner taps *Pay* and approves an STK prompt. Daraja's standing-order (Ratiba) product can be evaluated later.
* Daraja callbacks are unsigned: protected by secret URL segment + matching a `CheckoutRequestID` we created + amount check + idempotency. Add a reconcile job (Daraja "STK query") for lost callbacks.
* The current `MPESA_*` keys are treated as **the platform's** collection account. Letting each tenant take customer payments into their own till needs per-tenant encrypted credentials (later phase).
* Prices are assumed VAT-inclusive; check VAT/eTIMS obligations for your own invoices.
* `bulk_create` on tenant models auto-fills the tenant, but raw SQL and `.extra()` bypass scoping -- grep for them.
* Later hardening: PostgreSQL Row-Level Security as a second net; tenant data export/purge command for offboarding.
* Tests: `python manage.py test tenants` (isolation, fail-closed, pricing, payment idempotency, lock-out, gate, platform API).
