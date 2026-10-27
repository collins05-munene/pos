# Branch feature redo

Copy these files over your project (paths match), then:

    python manage.py migrate        # inventory 0011, users 0005, billing 0004
    python manage.py test tenants.tests_branches

## What changed
- **Branch model**: code, phone, is_main, awaiting_payment, created_at. One main branch per shop.
  States: active / awaiting payment / archived (history is never deleted).
- **Users**: `User.branch`. Cashiers are pinned to it. Owner/admin/manager start there and can switch
  from the top bar (session-based). `resolve_user_branch()` now honours this (it used to fall back to
  `Branch.objects.first()` for everyone).
- **Signup**: "How many branches?" + a name per branch. All are created, first is main, owner is placed there.
- **Pricing**: 1 branch included; each extra = `BRANCH_MONTHLY_PRICE`/month (term discount applies).
  Set the real figure in `settings.BILLING` -- the default (500) is a PLACEHOLDER.
- **Adding later** (/branches/, owner/admin): within paid slots -> active at once. Beyond them -> created
  "awaiting payment" and a pro-rated add-on invoice is issued (never extends the term). Paying it (M-Pesa
  code or credit) switches the branch on. Archiving drops it from the next renewal.
- **Branch page**: /branches/ (list, add, edit, archive/restore). Managers can edit and switch, not add.
- **Staff**: pick a branch when adding staff; move staff between branches from the Manage drawer.
- Inventory dropdowns only offer active branches and default to the branch you're working at.

## Existing data (migrations)
- Each shop's oldest branch becomes its main branch; every existing user is placed at it.
- `Subscription.branch_slots` = branches the shop has now, so nobody is blocked or charged retroactively.
  Shops with several branches will see the extra-branch fee on their NEXT renewal invoice.

## Not done / worth a look
- Dashboard and reports are still shop-wide (not filtered to the active branch).
- The HTML pages and JS were syntax-checked but not clicked through in a browser.
