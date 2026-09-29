# Frontend -- Duka OS

Plain HTML/CSS/JS (no build step, no framework) for the three SaaS-facing screens:
sign-up, owner billing, and the platform super-admin dashboard, plus the locked page.

## Files

```
frontend/
  static/duka/css/duka.css       design tokens + every component's styles
  static/duka/js/duka-api.js     shared fetch() helper (CSRF, money/date formatting, el())
  static/duka/js/signup.js
  static/duka/js/billing.js
  static/duka/js/platform.js
  templates/tenants/signup.html
  templates/billing/overview.html
  templates/billing/locked.html
  templates/platform_admin/overview.html
```

Copy `frontend/static/*` into your project's `static/` and `frontend/templates/*` into
`templates/` -- your `settings.py` already points `STATICFILES_DIRS` and `TEMPLATES.DIRS`
at those two folders, so nothing else to configure.

## How a page and its API share one URL

Each page's own JavaScript is the only thing that needs the JSON your views already
return, so I didn't add new endpoints. Instead: a **browser** requesting `/billing/`
gets the HTML page; that page's `billing.js` then calls `fetch('/billing/')` **again**,
and gets JSON back, because `duka-api.js` always sends `Accept: application/json` and
the view branches on that header (`tenants/rendering.py: html_or_json`). This changed
three views (`SignupView.get`, `BillingOverviewView.get`, `platform_admin.OverviewView.get`)
and `billing.locked_view`, all in this delivery already. `TenantListView`, `TenantDetailView`
and `TenantActionView` are untouched -- they're only ever called by `platform.js`, never
navigated to directly, so they stay JSON-only.

## Pages

- **`/signup/`** -- business name/type, owner details, M-Pesa number, password, a plan
  picker (1/3/6/12 months, pulling live pricing from `quote_table()`). Submits to the
  same `SignupView.post`, logs the owner in, redirects to `/billing/`.
- **`/billing/`** (owner only) -- current status in plain language ("runs out in 4 days"),
  the open invoice with a phone field and an STK-push button that polls
  `/billing/payments/<id>/` every 3s until it succeeds, fails, or times out after ~2 min.
  When there's no open invoice it shows the term picker instead, so a cancelled or
  about-to-renew owner can pick a plan and get a fresh invoice via `/billing/checkout/`.
  Full invoice history at the bottom.
- **`/billing/locked/`** -- shown by `SubscriptionGateMiddleware`. Different copy for the
  owner (a link back to billing) vs. staff (told to ask the owner), driven by
  `{{ is_owner }}` from the view.
- **`/platform/`** (superuser only) -- MRR/ARR/active-shops strip, a searchable and
  status-filterable tenant table, and a slide-in drawer per shop with the override actions
  (`comp`, `set-price`, `suspend`, `disable`/`enable`, `support-start`) and its recent
  invoices. Every action re-fetches the drawer and the table so the numbers never go stale.

## Notes

- No CSS framework, no bundler -- `duka-api.js`'s `el()` helper builds DOM nodes directly,
  same idea as a template literal but without `innerHTML` (avoids XSS from tenant-supplied
  names showing up in the platform table).
- CSRF: `duka-api.js` reads Django's `csrftoken` cookie and sends it as `X-CSRFToken` on
  every non-GET call -- works as-is with `CsrfViewMiddleware`, no template tag needed since
  these pages don't use Django forms.
- Accessibility: visible focus rings, `aria-pressed` on the plan picker, `aria-hidden` on
  the drawer, Escape closes it, `prefers-reduced-motion` respected (only real motion is the
  STK spinner and the drawer slide).
- Found while wiring this up: `templates/tenants/signup.html` originally used
  `{% url 'login' %}`, which doesn't exist in your URL config (`pin_login` does) and would
  have 500'd the page. Fixed to a plain `/users/pos-login/` link instead of guessing your
  URL names.
- Tests: `tenants/tests.py` now also checks that `/billing/` and `/platform/` serve HTML to
  a plain GET and JSON when `Accept: application/json` is sent, and that `/billing/locked/`
  renders. All 12 tests pass together with the earlier backend and patched-app tests.
