import datetime

from django.test import TestCase
from django.utils.html import escape
from django.urls import reverse

from . import documents as docs, operations as ops
from .models import (Account, AccountingSettings, Expense, FinancialAuditLog, FixedAsset, JournalEntry, Loan, OtherIncome,
                     RecurringExpense, ExpenseCategory)
from .services import get_settings, post_journal
from .testkit import D, TODAY, ago, balance, category, finance_shop, other_shop

U = lambda name, *a: reverse(f"accounting:{name}", args=a)

# every page a user can open, with the role areas that may see it
PAGES = [
    ("overview", (), "overview"), ("expenses", (), "expenses"), ("expense-new", (), "expenses"), ("categories", (), "expenses"),
    ("recurring", (), "expenses"), ("income", (), "income"), ("income-new", (), "income"), ("profit-loss", (), "pnl"),
    ("cash-flow", (), "cashflow"), ("balance-sheet", (), "balance"), ("assets", (), "assets"), ("asset-new", (), "assets"),
    ("loan-new", (), "assets"), ("owner-new", (), "assets"), ("ledger", (), "ledger"), ("journal-new", (), "ledger"),
    ("account-new", (), "ledger"), ("settings", (), "ledger"), ("analysis", (), "analysis"), ("reports", (), "reports"),
]
MANAGER_OK = {"overview", "expenses", "pnl", "cashflow", "analysis", "reports"}


class Base(TestCase):
    def setUp(self):
        self.shop = finance_shop()
        self.t, self.owner, self.main, self.w = self.shop["tenant"], self.shop["owner"], self.shop["branch"], self.shop["second"]
        self.rent = category(self.t, "Rent")
        self.e = ops.create_expense(tenant=self.t, user=self.owner, title="Secret rent 987654", category=self.rent, amount="987654",
                                    expense_date=ago(3), branch=self.main)
        ops.record_other_income(tenant=self.t, user=self.owner, title="Kiosk", kind="RENTAL", amount="600", income_date=ago(5), branch=self.main)
        ops.record_loan(tenant=self.t, user=self.owner, lender="Bank", principal="1000", interest_rate_percent="10", start_date=ago(10),
                        installments=4, frequency="MONTHLY", received_via="BANK")
        ops.register_asset(tenant=self.t, user=self.owner, name="Fridge", category="EQUIPMENT", cost="2400", purchase_date=ago(20),
                           useful_life_months=24, paid_with="CASH")


class AccessMatrixTests(Base):
    def get(self, user, name, *a):
        self.client.force_login(user)
        return self.client.get(U(name, *a)).status_code

    def test_owner_admin_accountant_open_every_page(self):
        for user in (self.owner, self.shop["accountant"]):
            for name, args, area in PAGES:
                self.assertEqual(self.get(user, name, *args), 200, (user.role, name))

    def test_cashier_can_open_nothing_and_anonymous_is_sent_away(self):
        for name, args, area in PAGES:
            self.assertEqual(self.get(self.shop["cashier"], name, *args), 403, name)
        self.client.logout()
        for name, args, area in PAGES:
            self.assertIn(self.client.get(U(name, *args)).status_code, (302, 403), name)

    def test_branch_manager_sees_only_their_areas(self):
        for name, args, area in PAGES:
            want = 200 if area in MANAGER_OK else 403
            self.assertEqual(self.get(self.shop["manager"], name, *args), want, name)

    def test_manager_report_access(self):
        self.client.force_login(self.shop["manager"])
        for key, meta in docs.REPORTS.items():
            want = 200 if meta["manager"] else 403
            self.assertEqual(self.client.get(U("report", key)).status_code, want, key)
            self.assertEqual(self.client.get(U("report-export", key, "pdf")).status_code, want, key)
        hub = self.client.get(U("reports"))
        self.assertEqual(len(hub.context["items"]), sum(1 for m in docs.REPORTS.values() if m["manager"]))

    def test_managers_see_only_their_branch_numbers_whatever_the_url_says(self):
        ops.create_expense(tenant=self.t, user=self.owner, title="Westlands rent 555111", category=self.rent, amount="555111", expense_date=ago(2), branch=self.w)
        self.client.force_login(self.shop["manager"])
        for q in ("", "?branch=all", f"?branch={self.w.pk}"):
            page = self.client.get(U("expenses") + q + ("&" if q else "?") + "period=custom&start=%s&end=%s" % (ago(10), TODAY))
            self.assertContains(page, "Secret rent")
            self.assertNotContains(page, "Westlands rent")
        r = self.client.get(U("report-export", "expense-register", "xlsx") + f"?branch={self.w.pk}&period=custom&start={ago(10)}&end={TODAY}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"555111", r.content)

    def test_manager_cannot_use_branch_pages_after_owner_switches_it_off(self):
        cfg = get_settings(self.t)
        cfg.managers_can_manage_expenses = False
        cfg.save()
        self.assertEqual(self.get(self.shop["manager"], "expenses"), 403)
        self.assertEqual(self.get(self.shop["manager"], "profit-loss"), 200)

    def test_sidebar_links_follow_the_role(self):
        self.client.force_login(self.owner)
        page = self.client.get(U("overview")).content.decode()
        for label in ("Expenses", "Other Income", "Profit &amp; Loss", "Cash Flow", "Balance Sheet", "Assets &amp; Liabilities", "Accounting Ledger", "Financial Analysis", "Financial Reports"):
            self.assertIn(label, page)
        self.client.force_login(self.shop["manager"])
        page = self.client.get(U("overview")).content.decode()
        self.assertNotIn("Balance Sheet", page)
        self.assertIn("Branch Finances", page)


class TenantIsolationViewTests(Base):
    def test_another_shop_sees_none_of_this_shops_numbers(self):
        other = other_shop()
        self.client.force_login(other["owner"])
        for name, args, area in PAGES:
            r = self.client.get(U(name, *args) + "?period=custom&start=%s&end=%s" % (ago(60), TODAY))
            self.assertEqual(r.status_code, 200, name)
            body = r.content.decode()
            for secret in ("987654", "Secret rent", "Kiosk", "Fridge"):
                self.assertNotIn(secret, body, (name, secret))
        for key in docs.REPORTS:
            for fmt in ("xlsx",):
                r = self.client.get(U("report-export", key, fmt) + "?period=custom&start=%s&end=%s" % (ago(60), TODAY))
                self.assertNotIn(b"987654", r.content, key)

    def test_other_shops_objects_are_404_not_403(self):
        other = other_shop()
        self.client.force_login(other["owner"])
        loan = Loan.all_objects.get(tenant=self.t)
        asset = FixedAsset.all_objects.get(tenant=self.t)
        inc = OtherIncome.all_objects.get(tenant=self.t)
        je = JournalEntry.all_objects.filter(tenant=self.t).first()
        for url in (U("loan", loan.pk), U("asset-dispose", asset.pk), U("journal", je.pk)):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        for url in (U("income-receive", inc.pk), U("income-cancel", inc.pk), U("journal-reverse", je.pk), U("category-toggle", self.rent.pk),
                    U("account-toggle", Account.all_objects.filter(tenant=self.t).first().pk), U("classify", 1)):
            self.assertEqual(self.client.post(url, {"reason": "x", "account": 1}).status_code, 404, url)
        self.assertTrue(Expense.all_objects.filter(pk=self.e.pk).exclude(status="CANCELLED").exists())
        self.assertEqual(get_settings(self.t).opening_balance_date, ago(40))


class ScreenFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_income_flow(self):
        r = self.client.post(U("income-new"), dict(title="Interest", kind="INTEREST", amount="250", income_date=ago(4).isoformat()))
        self.assertEqual(r.status_code, 302)
        inc = OtherIncome.all_objects.get(title="Interest")
        self.client.post(U("income-receive", inc.pk), dict(received_on=ago(1).isoformat(), payment_method="MPESA"))
        inc.refresh_from_db()
        self.assertEqual(inc.received_on, ago(1))
        self.client.post(U("income-cancel", inc.pk), {"reason": "oops"})
        inc.refresh_from_db()
        self.assertTrue(inc.is_cancelled)
        page = self.client.get(U("income") + "?period=custom&start=%s&end=%s" % (ago(10), TODAY))
        self.assertContains(page, "Kiosk")

    def test_asset_loan_and_owner_screens(self):
        self.assertEqual(self.client.post(U("asset-new"), dict(name="Van", category="VEHICLE", cost="50000", purchase_date=ago(5).isoformat(),
                                                               useful_life_months="60", residual_value="5000", paid_with="BANK")).status_code, 302)
        van = FixedAsset.all_objects.get(name="Van")
        self.assertEqual(self.client.post(U("asset-dispose", van.pk), dict(disposal_date=ago(1).isoformat(), proceeds="45000", method="BANK")).status_code, 302)
        self.assertEqual(self.client.post(U("loan-new"), dict(lender="SACCO", principal="9000", interest_rate_percent="12", start_date=ago(5).isoformat(),
                                                              installments="9", frequency="MONTHLY", received_via="CASH")).status_code, 302)
        loan = Loan.all_objects.get(lender="SACCO")
        page = self.client.post(U("loan", loan.pk), dict(paid_on=ago(1).isoformat(), principal_paid="1000", interest_paid="90", payment_method="CASH"))
        self.assertEqual(page.status_code, 302)
        bad = self.client.post(U("loan", loan.pk), dict(paid_on=ago(1).isoformat(), principal_paid="999999", interest_paid="0", payment_method="CASH"))
        self.assertContains(bad, "exceeds")
        self.assertEqual(self.client.post(U("owner-new"), dict(kind="CAPITAL", amount="700", txn_date=ago(2).isoformat(), payment_method="CASH")).status_code, 302)
        for tab in ("assets", "loans", "owner"):
            self.assertEqual(self.client.get(U("assets") + f"?tab={tab}").status_code, 200)
        self.assertEqual(self.client.get(U("loan", loan.pk)).status_code, 200)
        self.assertEqual(self.client.post(U("depreciation-run")).status_code, 302)

    def test_form_errors_are_shown_not_swallowed(self):
        r = self.client.post(U("asset-new"), dict(name="Bad", category="VEHICLE", cost="100", purchase_date=ago(5).isoformat(),
                                                  useful_life_months="12", residual_value="500"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(FixedAsset.all_objects.filter(name="Bad").exists())

    def test_manual_journal_and_reversal(self):
        cash = Account.all_objects.get(tenant=self.t, system_key="CASH").pk
        misc = Account.all_objects.get(tenant=self.t, system_key="EXP_MISC").pk
        form = dict(entry_date=ago(1).isoformat(), description="Petty cash tea", token="tok1", branch=self.main.pk,
                    account=[misc, cash, "", ""], debit=["50", "", "", ""], credit=["", "50", "", ""], memo=["", "", "", ""])
        r = self.client.post(U("journal-new"), form)
        self.assertEqual(r.status_code, 302)
        je = JournalEntry.all_objects.get(source_type="MANUAL")
        self.client.post(U("journal-new"), form)                                            # double click
        self.assertEqual(JournalEntry.all_objects.filter(source_type="MANUAL").count(), 1)
        bad = dict(form, token="tok2", credit=["", "49", "", ""])
        self.assertContains(self.client.post(U("journal-new"), bad), "does not balance")
        self.assertEqual(JournalEntry.all_objects.filter(source_type="MANUAL").count(), 1)
        self.assertEqual(self.client.get(U("journal", je.pk)).status_code, 200)
        self.client.post(U("journal-reverse", je.pk), {"reason": "typo"})
        je.refresh_from_db()
        self.assertEqual(je.status, "REVERSED")
        self.client.post(U("journal-reverse", je.pk), {"reason": "again"})
        self.assertEqual(JournalEntry.all_objects.filter(source_type="REVERSAL").count(), 1)

    def test_accountant_can_post_but_manager_cannot(self):
        cash = Account.all_objects.get(tenant=self.t, system_key="CASH").pk
        misc = Account.all_objects.get(tenant=self.t, system_key="EXP_MISC").pk
        form = dict(entry_date=ago(1).isoformat(), description="Adj", token="t9", account=[misc, cash], debit=["5", ""], credit=["", "5"], memo=["", ""])
        self.client.force_login(self.shop["manager"])
        self.assertEqual(self.client.post(U("journal-new"), form).status_code, 403)
        self.client.force_login(self.shop["accountant"])
        self.assertEqual(self.client.post(U("journal-new"), form).status_code, 302)

    def test_ledger_tabs_and_filters(self):
        for tab in ("journal", "accounts", "till", "tools"):
            self.assertEqual(self.client.get(U("ledger") + f"?tab={tab}").status_code, 200)
        page = self.client.get(U("ledger") + "?period=custom&start=%s&end=%s&source=EXPENSE" % (ago(10), TODAY))
        self.assertContains(page, "Secret rent")
        page = self.client.get(U("ledger") + "?period=custom&start=%s&end=%s&q=zzzz" % (ago(10), TODAY))
        self.assertContains(page, "No journal entries")

    def test_catch_up_classify_and_revalue_actions(self):
        self.assertEqual(self.client.post(U("catch-up")).status_code, 302)
        self.assertEqual(self.client.post(U("revalue")).status_code, 302)
        self.assertTrue(FinancialAuditLog.all_objects.filter(tenant=self.t, action="LEDGER_CATCH_UP").exists())

    def test_chart_of_accounts_management(self):
        r = self.client.post(U("account-new"), dict(code="6500", name="Fuel", account_type="OPEX"))
        self.assertEqual(r.status_code, 302)
        a = Account.all_objects.get(tenant=self.t, code="6500")
        self.assertEqual(a.parent.code, "6000")
        self.assertContains(self.client.post(U("account-new"), dict(code="6500", name="Dup", account_type="OPEX")), "already used")
        self.assertContains(self.client.post(U("account-new"), dict(code="4500", name="Wrong", account_type="OPEX")), "are Revenue")
        self.client.post(U("account-toggle", a.pk))
        a.refresh_from_db()
        self.assertFalse(a.is_active)
        header = Account.all_objects.get(tenant=self.t, code="6000")
        self.client.post(U("account-toggle", header.pk))
        header.refresh_from_db()
        self.assertTrue(header.is_active)
        self.client.force_login(self.shop["accountant"])
        self.client.post(U("account-toggle", a.pk))
        a.refresh_from_db()
        self.assertFalse(a.is_active)                      # unchanged: accountants do not change the chart

    def test_settings_change_is_audited_and_never_rewrites_history(self):
        before = list(JournalEntry.all_objects.filter(tenant=self.t).values_list("pk", "entry_date", "status"))
        r = self.client.post(U("settings"), dict(reporting_basis="CASH", currency_code="usd", fiscal_year_start_month="7",
                                                 default_tax_rate_percent="", managers_can_manage_expenses="on"))
        self.assertEqual(r.status_code, 302)
        cfg = get_settings(self.t)
        self.assertEqual((cfg.reporting_basis, cfg.currency_code, cfg.fiscal_year_start_month), ("CASH", "USD", 7))
        self.assertEqual(before, list(JournalEntry.all_objects.filter(tenant=self.t).values_list("pk", "entry_date", "status")))
        self.assertTrue(FinancialAuditLog.all_objects.filter(tenant=self.t, action="SETTINGS_CHANGED").exists())
        page = self.client.get(U("profit-loss") + "?period=custom&start=%s&end=%s" % (ago(40), TODAY))      # now defaults to cash basis
        self.assertContains(page, "Cash basis")
        self.assertContains(self.client.post(U("settings"), dict(reporting_basis="CASH", currency_code="XX", fiscal_year_start_month="13")), "month")
        self.client.force_login(self.shop["accountant"])
        self.client.post(U("settings"), dict(reporting_basis="ACCRUAL", currency_code="KES", fiscal_year_start_month="1"))
        self.assertEqual(get_settings(self.t).reporting_basis, "CASH")

    def test_category_and_recurring_screens(self):
        self.client.post(U("categories"), dict(name="Fuel", account=Account.all_objects.get(tenant=self.t, system_key="EXP_DELIVERY").pk))
        cat = ExpenseCategory.all_objects.get(name="Fuel")
        self.client.post(U("category-toggle", cat.pk))
        cat.refresh_from_db()
        self.assertFalse(cat.is_active)
        self.assertContains(self.client.post(U("categories"), dict(name="fuel", account=Account.all_objects.get(tenant=self.t, system_key="EXP_DELIVERY").pk)), "already exists")
        rec = ops.create_recurring_expense(tenant=self.t, user=self.owner, title="Cleaner", category=self.rent, amount="100", frequency="WEEKLY", start_date=ago(10))
        self.client.post(U("recurring-toggle", rec.pk))
        rec.refresh_from_db()
        self.assertFalse(rec.is_active)
        self.assertEqual(self.client.get(U("recurring")).status_code, 200)

    def test_overview_opening_cash_prompts_when_not_set_up(self):
        fresh = finance_shop.__globals__["make_shop"](business_name="Fresh Mart", email="fresh2@example.com", phone="254711000222")
        self.client.force_login(fresh["owner"])
        r = self.client.get(U("overview"))
        self.assertContains(r, "Start my books")
        self.assertContains(r, "Stock on hand is worth")

    def test_dashboard_renders_charts_and_kpis_for_a_busy_period(self):
        r = self.client.get(U("overview") + "?period=custom&start=%s&end=%s" % (ago(40), TODAY))
        self.assertContains(r, "ch-rev-exp")
        self.assertContains(r, "fin-charts")
        kpis = {k: v for k, v, _ in r.context["d"]["kpis"]}
        for needed in ("Total revenue", "Net sales", "Cost of goods sold", "Gross profit", "Operating expenses", "Operating profit",
                       "Profit before tax", "Net profit", "Profit margin", "Cash balance", "Receivables", "Payables"):
            self.assertIn(needed, kpis)
        self.assertEqual(r.context["d"]["charts"]["labels"].__len__(), 12)
        self.assertEqual(kpis["Receivables"], D("600.00"))

    def test_empty_period_shows_an_empty_state_not_zeros(self):
        r = self.client.get(U("overview") + "?period=custom&start=2020-01-01&end=2020-01-31")
        self.assertContains(r, "No financial activity")

    def test_pagination_works_on_long_lists(self):
        for i in range(30):
            ops.create_expense(tenant=self.t, user=self.owner, title=f"Bulk {i:02d}", category=self.rent, amount="1", expense_date=ago(2), branch=self.main)
        base = U("expenses") + "?period=custom&start=%s&end=%s" % (ago(10), TODAY)
        p1 = self.client.get(base)
        self.assertEqual(len(p1.context["page"]), 25)
        p2 = self.client.get(base + "&page=2")
        self.assertEqual(p2.context["page"].number, 2)
        self.assertEqual(len(p2.context["page"]), 6)          # 30 bulk + the setUp expense = 31

    def test_reports_hub_and_all_report_screens_render(self):
        self.client.get(U("reports"))
        for key in docs.REPORTS:
            r = self.client.get(U("report", key) + "?period=custom&start=%s&end=%s" % (ago(40), TODAY))
            self.assertEqual(r.status_code, 200, key)
            self.assertContains(r, escape(docs.REPORTS[key]["title"]))
        self.assertEqual(self.client.get(U("report", "does-not-exist")).status_code, 404)
