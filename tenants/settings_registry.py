"""
The Settings page is built from this list. To add a rule:
  1. add a field to tenants.models.TenantSettings (+ migration),
  2. add its name to a section below,
  3. read it with get_settings(tenant) where the rule applies.
Validation, the audit trail and the page layout need no further changes.
"""
SECTIONS = [
    {
        "key": "discounts",
        "title": "Discounts",
        "desc": "Rules for the discounts managers and admins set up. The discounts themselves live under Manage discounts.",
        "link": ("discount-list", "Manage discounts"),
        "fields": ["discounts_enabled", "managers_can_manage_discounts", "max_discount_percent",
                   "manager_max_discount_percent", "below_cost_policy"],
    },
    {
        "key": "refunds",
        "title": "Refunds",
        "desc": "How long after a sale a refund can be made.",
        "link": ("refund-list", "View refunds"),
        "fields": ["refund_window_days"],
    },
    {
        "key": "receipts",
        "title": "Receipts",
        "desc": "What customers see on the printed receipt.",
        "link": ("receipt-list", "View receipts"),
        "fields": ["receipt_footer"],
    },
    {
        "key": "offline",
        "title": "Offline selling",
        "desc": "How long a till may keep selling without internet.",
        "link": None,
        "fields": ["offline_max_hours"],
    },
]

# Settings that live on their own pages; shown as shortcuts so the hub is the one place to look.
SHORTCUTS = [
    ("Backups", "Manual and automatic data backups.", "backup-list"),
    ("Staff", "Add cashiers and managers, reset PINs.", "staff:staff-list"),
    ("Branches", "Add and manage your outlets.", "branches:branch-list"),
    ("Audit logs", "Who changed what, and when.", "activity-logs"),
]
