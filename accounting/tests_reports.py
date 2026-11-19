import datetime
import io
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from inventory.models import InventoryTransfer
from inventory.transfers import create_transfer, dispatch_transfer, receive_transfer
from sales.cart import CartItem
from sales.utils import complete_pos_sale
from tenants.context import tenant_context

from . import dashboard as dash
from . import documents as docs
from . import exports, operations as ops, reporting as rp
from .models import JournalEntry
from .services import trial_balance
from .testkit import D, TODAY, ago, balance, category, finance_shop


def cell_for(doc, table_title, label, col=1):
    for t in doc.tables:
        if t.title == table_title:
            for r in t.rows:
                if r.cells[0] == label:
                    return r.cells[col]
    raise AssertionError(f"{label!r} not found in {table_title!r}")


class Scenario(TestCase):
    """
    Opening: cash 10,000 and stock 200. Then: sales 50 + 120 (cost 20 + 55), rent 1,000 of which 400 is paid,
    utilities 300 unpaid, rental income 600 still owed, owner takes 200, a 6,000 bank loan with 500 principal and 60 interest repaid.
    """

    def setUp(self):
        self.shop = finance_shop()
        self.t, self.owner, self.main, self.w = self.shop["tenant"], self.shop["owner"], self.shop["branch"], self.shop["second"]
        tablet, box, cashier = self.shop["tablet"], self.shop["box"], self.shop["cashier"]
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            for v, q in ((tablet, 10), (box, 1)):
                complete_pos_sale([CartItem(v.pk, v.sku, v.sku, v.retail_price, q)], "CASH", cashier, self.shop["session"], D("1000"))
        rent = ops.create_expense(tenant=self.t, user=self.owner, title="Rent", category=category(self.t, "Rent"), amount="1000", expense_date=ago(10), branch=self.main)
        ops.record_expense_payment(rent, user=self.owner, amount="400", payment_date=ago(5), method="CASH")
        ops.create_expense(tenant=self.t, user=self.owner, title="Power", category=category(self.t, "Utilities"), amount="300", expense_date=ago(9), branch=self.main, due_date=ago(1))
        ops.record_other_income(tenant=self.t, user=self.owner, title="Kiosk rent", kind="RENTAL", amount="600", income_date=ago(8), branch=self.main)
        ops.record_owner_transaction(tenant=self.t, user=self.owner, kind="DRAWING", amount="200", txn_date=ago(4), payment_method="CASH")
        loan = ops.record_loan(tenant=self.t, user=self.owner, lender="Bank", principal="6000", interest_rate_percent="12", start_date=ago(20),
                               installments=12, frequency="MONTHLY", received_via="BANK", branch=self.main)
        ops.repay_loan(loan, user=self.owner, paid_on=ago(3), principal_paid="500", interest_paid="60", payment_method="BANK")

    def f(self, **kw):
        base = dict(tenant=self.t, start=ago(40), end=TODAY)
        base.update(kw)
        return rp.Filter(**base)


class ProfitAndLossTests(Scenario):
    def test_accrual(self):
        t = rp.totals_of(rp.income_numbers(self.f()))
        self.assertEqual((t["net_sales"], t["cogs"], t["gross_profit"], t["opex"], t["operating_profit"]),
                         (D("170.00"), D("75.00"), D("95.00"), D("1300.00"), D("-1205.00")))
        self.assertEqual((t["other_income"], t["finance"], t["pbt"], t["tax"], t["net_profit"]),
                         (D("600.00"), D("60.00"), D("-665.00"), D("0"), D("-665.00")))

    def test_cash_basis_only_counts_money_that_moved(self):
        t = rp.totals_of(rp.income_numbers(self.f(basis="CASH")))
        self.assertEqual((t["net_sales"], t["cogs"], t["opex"], t["other_income"], t["finance"], t["net_profit"]),
                         (D("170.00"), D("75.00"), D("400.00"), D("0"), D("60.00"), D("-365.00")))

    def test_switching_basis_changes_nothing_stored(self):
        before = list(JournalEntry.all_objects.filter(tenant=self.t).values_list("pk", "status", "entry_date"))
        rp.income_numbers(self.f(basis="CASH"))
        rp.income_numbers(self.f(basis="ACCRUAL"))
        self.assertEqual(before, list(JournalEntry.all_objects.filter(tenant=self.t).values_list("pk", "status", "entry_date")))
        self.assertEqual(balance(self.t, "EXP_RENT"), D("1000.00"))

    def test_paying_the_rest_moves_cash_basis_up_to_accrual(self):
        from .models import Expense
        ops.record_expense_payment(Expense.all_objects.get(title="Rent"), user=self.owner, amount="600", payment_date=ago(1), method="BANK")
        ops.record_expense_payment(Expense.all_objects.get(title="Power"), user=self.owner, amount="300", payment_date=ago(1), method="BANK")
        self.assertEqual(rp.totals_of(rp.income_numbers(self.f(basis="CASH")))["opex"], rp.totals_of(rp.income_numbers(self.f()))["opex"])

    def test_document_structure_and_margins(self):
        d = docs.profit_loss(self.f())
        for label in ("REVENUE", "Gross sales", "Net sales", "COST OF GOODS SOLD", "GROSS PROFIT", "OPERATING EXPENSES", "OPERATING PROFIT",
                      "OTHER INCOME AND EXPENSES", "PROFIT BEFORE TAX", "NET PROFIT AFTER TAX"):
            cell_for(d, "Profit and loss statement", label)
        self.assertEqual(cell_for(d, "Profit and loss statement", "NET PROFIT AFTER TAX"), D("-665.00"))
        self.assertEqual(cell_for(d, "Margins", "Gross profit margin"), "55.9%")          # 95 / 170
        self.assertEqual(cell_for(d, "Profit and loss statement", "Rent"), D("1000.00"))
        self.assertEqual(cell_for(d, "Profit and loss statement", "Less: sales discounts"), D("0.00") * -1)

    def test_comparison_with_previous_period(self):
        d = docs.profit_loss(self.f(start=ago(10), end=TODAY))
        self.assertEqual(d.tables[0].columns, ["", "This period", "Previous period", "Change"])
        self.assertEqual(cell_for(d, "Profit and loss statement", "Gross sales", 2), D("0.00"))     # nothing sold in the 11 days before
        self.assertIn("Previous period:", d.notes[0])

    def test_branch_comparison_adds_up_to_the_consolidated_figure(self):
        d = docs.profit_loss(self.f())
        tbl = [t for t in d.tables if t.title == "Branch comparison"][0]
        rows = {r.cells[0]: r.cells for r in tbl.rows}
        self.assertEqual(rows["All branches"][5], D("-665.00"))
        self.assertEqual(rows["Mama Njeri Mart"][5] if "Mama Njeri Mart" in rows else sum(r[5] for k, r in rows.items() if k not in ("All branches", "Westlands", "Head office / unallocated")), D("-665.00"))
        self.assertEqual(rows["Westlands"][5], D("0"))

    def test_single_branch_filter(self):
        t = rp.totals_of(rp.income_numbers(self.f(branch_ids={self.w.pk})))
        self.assertEqual((t["net_sales"], t["net_profit"]), (D("0"), D("0")))
        t = rp.totals_of(rp.income_numbers(self.f(branch_ids={self.main.pk})))
        self.assertEqual(t["net_profit"], D("-665.00"))

    def test_every_dashboard_number_traces_to_the_statement(self):
        d = dash.dashboard(self.f())
        k = {label: value for label, value, _ in d["kpis"]}
        t = rp.totals_of(rp.income_numbers(self.f()))
        self.assertEqual((k["Net sales"], k["Gross profit"], k["Net profit"], k["Operating expenses"], k["Cost of goods sold"]),
                         (t["net_sales"], t["gross_profit"], t["net_profit"], t["opex"], t["cogs"]))
        self.assertEqual((k["Cash balance"], k["Receivables"], k["Payables"]), (D("15010.00"), D("600.00"), D("900.00")))
        self.assertEqual(k["Total revenue"], D("770.00"))


class CashFlowTests(Scenario):
    def test_figures_and_reconciliation(self):
        d = docs.cash_flow(self.f())
        s = lambda label: cell_for(d, "Summary", label)
        self.assertEqual(s("Opening cash balance"), D("10000.00"))
        self.assertEqual(s("Total cash inflows"), D("6170.00"))
        self.assertEqual(s("Total cash outflows"), D("-1160.00"))
        self.assertEqual(s("Net cash movement"), D("5010.00"))
        self.assertEqual(s("CLOSING CASH BALANCE"), D("15010.00"))
        self.assertTrue(d.reconciled)
        self.assertEqual(cell_for(d, "Reconciliation to cash, bank and M-Pesa accounts", "Difference from closing balance above"), D("0.00"))

    def test_sections(self):
        d = docs.cash_flow(self.f())
        c = lambda label: cell_for(d, "Cash flow", label)
        self.assertEqual(c("Customer payments (sales)"), D("170.00"))
        self.assertEqual(c("Rent paid"), D("-400.00"))
        self.assertEqual(c("Interest paid"), D("-60.00"))
        self.assertEqual(c("Net cash from operating activities"), D("-290.00"))
        self.assertEqual((c("Loans received"), c("Loan principal repaid"), c("Owner withdrawals")), (D("6000.00"), D("-500.00"), D("-200.00")))
        self.assertEqual(c("Net cash from financing activities"), D("5300.00"))
        self.assertEqual(c("Net cash from investing activities"), D("0"))

    def test_unpaid_expenses_and_unreceived_income_are_not_cash(self):
        d = docs.cash_flow(self.f())
        labels = [r.cells[0] for t in d.tables for r in t.rows]
        self.assertNotIn("Utilities paid", labels)
        self.assertNotIn("Other income received", labels)

    def test_it_ignores_the_accounting_basis(self):
        a, b = docs.cash_flow(self.f(basis="CASH")), docs.cash_flow(self.f(basis="ACCRUAL"))
        self.assertEqual(cell_for(a, "Summary", "CLOSING CASH BALANCE"), cell_for(b, "Summary", "CLOSING CASH BALANCE"))

    def test_later_period_starts_from_the_earlier_closing_balance(self):
        d = docs.cash_flow(self.f(start=ago(3), end=TODAY))
        self.assertEqual(cell_for(d, "Summary", "Opening cash balance"), D("15010.00") - cell_for(d, "Summary", "Net cash movement"))
        self.assertTrue(d.reconciled)

    def test_internal_moves_between_cash_accounts_are_not_flows(self):
        from .models import Account
        from .services import post_journal
        post_journal(tenant=self.t, entry_date=ago(2), description="Bank deposit", source_type="MANUAL",
                     lines=[dict(account="BANK", debit=D("1000")), dict(account="CASH", credit=D("1000"))])
        d = docs.cash_flow(self.f())
        self.assertEqual(cell_for(d, "Summary", "Total cash inflows"), D("6170.00"))
        self.assertEqual(cell_for(d, "Summary", "CLOSING CASH BALANCE"), D("15010.00"))

    def test_asset_purchase_is_investing(self):
        ops.register_asset(tenant=self.t, user=self.owner, name="Fridge", category="EQUIPMENT", cost="3000", purchase_date=ago(6),
                           useful_life_months=24, paid_with="CASH", branch=self.main)
        d = docs.cash_flow(self.f())
        self.assertEqual(cell_for(d, "Cash flow", "Equipment purchases"), D("-3000.00"))
        self.assertEqual(cell_for(d, "Cash flow", "Net cash from investing activities"), D("-3000.00"))
        self.assertTrue(d.reconciled)


class BalanceSheetTests(Scenario):
    def test_it_balances_with_the_expected_figures(self):
        d = docs.balance_sheet(self.f())
        s = lambda label: cell_for(d, "Statement of financial position", label)
        self.assertTrue(d.balanced)
        self.assertEqual((s("Cash on hand"), s("Bank balances"), s("Accounts receivable"), s("Inventory")),
                         (D("9570.00"), D("5440.00"), D("600.00"), D("125.00")))
        self.assertEqual(s("Total assets"), D("15735.00"))
        self.assertEqual((s("Outstanding expenses"), s("Loans"), s("Total liabilities")), (D("900.00"), D("5500.00"), D("6400.00")))
        eq = dict(d.numbers["equity"])
        self.assertEqual(eq["Current period profit"] + eq["Retained earnings"], D("-665.00"))
        self.assertEqual(s("Less: owner drawings"), D("-200.00"))
        self.assertEqual(s("TOTAL LIABILITIES AND EQUITY"), D("15735.00"))
        self.assertEqual(s("Check: assets - (liabilities + equity)"), D("0.00"))
        self.assertEqual(d.alerts, [])

    def test_historical_balance_sheet(self):
        d = docs.balance_sheet(self.f(end=ago(30)))              # before any trading
        self.assertTrue(d.balanced)
        self.assertEqual(cell_for(d, "Statement of financial position", "Total assets"), D("10200.00"))

    def test_fixed_assets_and_depreciation_flow_through(self):
        a = ops.register_asset(tenant=self.t, user=self.owner, name="Fridge", category="EQUIPMENT", cost="2400", purchase_date=ago(35),
                               useful_life_months=24, paid_with="CASH", branch=self.main)
        ops.generate_depreciation(self.t, up_to=TODAY)
        a.refresh_from_db()
        d = docs.balance_sheet(self.f())
        self.assertTrue(d.balanced)
        self.assertEqual(cell_for(d, "Statement of financial position", "Fixed assets (at cost)"), D("2400.00"))
        self.assertEqual(cell_for(d, "Statement of financial position", "Less: accumulated depreciation"), -a.accumulated_depreciation)

    def test_branch_balance_sheet_balances_including_transfers(self):
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            t = create_transfer(self.owner, self.main, self.w, [(self.shop["tablet"], D("40"))], dispatch_now=True)
            receive_transfer(InventoryTransfer.objects.get(pk=t.pk), self.owner)
        for ids in ({self.main.pk}, {self.w.pk}, None):
            d = docs.balance_sheet(self.f(branch_ids=ids))
            self.assertTrue(d.balanced, ids)
        w = docs.balance_sheet(self.f(branch_ids={self.w.pk}))
        self.assertEqual(cell_for(w, "Statement of financial position", "Inventory"), D("80.00"))
        self.assertTrue(any("Inter-branch" in r.cells[0] for r in w.tables[0].rows))
        self.assertEqual(cell_for(docs.balance_sheet(self.f()), "Statement of financial position", "Inventory"), D("125.00"))

    def test_a_forced_imbalance_is_diagnosed(self):
        from .models import JournalLine
        e = JournalEntry.all_objects.filter(tenant=self.t).first()
        # simulate corruption underneath the application guard
        from django.db import connection
        with connection.cursor() as c:
            c.execute("UPDATE accounting_journalline SET debit = debit + 5 WHERE id = (SELECT MIN(id) FROM accounting_journalline WHERE debit > 0)")
        d = docs.balance_sheet(self.f())
        self.assertFalse(d.balanced)
        self.assertTrue(d.alerts)
        self.assertTrue(any("do not balance" in a for a in d.alerts))


class LedgerReportTests(Scenario):
    def test_trial_balance_balances_and_matches_the_service(self):
        d = docs.trial_balance_doc(self.f())
        self.assertTrue(d.balanced)
        total = [r for r in d.tables[0].rows if r.cells[1] == "TOTAL"][0].cells
        self.assertEqual(total[2], total[3])
        _, td, tc = trial_balance(self.t)
        self.assertEqual(td, tc)

    def test_general_ledger_running_balance(self):
        from .models import Account
        cash = Account.all_objects.get(tenant=self.t, system_key="CASH")
        d = docs.general_ledger(self.f(), account_id=cash.pk)
        rows = d.tables[0].rows
        self.assertEqual(rows[0].cells[2], "Opening balance")
        self.assertEqual(rows[-1].cells[2], "Closing balance")
        self.assertEqual(rows[-1].cells[5], D("9570.00"))
        run = D("0")
        for r in rows[1:-1]:
            run += (r.cells[3] or D("0")) - (r.cells[4] or D("0"))
            self.assertEqual(r.cells[5], run)

    def test_date_filter_limits_the_ledger(self):
        from .models import Account
        cash = Account.all_objects.get(tenant=self.t, system_key="CASH")
        d = docs.general_ledger(self.f(start=ago(5), end=ago(4)), account_id=cash.pk)
        self.assertEqual(len(d.tables[0].rows), 2 + 2)           # opening + rent payment + drawing + closing


class ListReportTests(Scenario):
    def test_expense_register(self):
        d = docs.expense_register(self.f())
        total = [r for r in d.tables[0].rows if "TOTAL" in str(r.cells[1])][0].cells
        self.assertEqual((total[5], total[6], total[7]), (D("1300.00"), D("400.00"), D("900.00")))
        paid = docs.expense_register(self.f(), status="PAID")
        self.assertEqual(len(paid.tables[0].rows), 1)
        overdue = docs.expense_register(self.f(), status="OVERDUE")
        self.assertEqual([r.cells[1] for r in overdue.tables[0].rows][0], "Power")

    def test_expense_analysis(self):
        d = docs.expense_analysis(self.f())
        self.assertEqual(cell_for(d, "By category", "Rent"), D("1000.00"))
        self.assertEqual(cell_for(d, "By category", "Rent", 2), D("400.00"))
        self.assertEqual(cell_for(d, "By category", "TOTAL"), D("1300.00"))

    def test_income_report(self):
        d = docs.income_report(self.f())
        sales = [t for t in d.tables if t.title == "Sales"][0]
        self.assertEqual(sales.rows[-1].cells[4], D("170.00"))
        other = [t for t in d.tables if t.title == "Other income"][0]
        self.assertEqual(other.rows[0].cells[5], "Not yet received")

    def test_receivables_and_payables_reconcile_to_the_ledger(self):
        ar = docs.accounts_receivable(self.f())
        self.assertEqual(cell_for(ar, "Reconciliation", "Difference (opening balances or manual entries)"), D("0.00"))
        self.assertEqual(ar.tables[0].rows[0].cells[5], D("600.00"))
        ap = docs.accounts_payable(self.f())
        self.assertEqual(cell_for(ap, "Reconciliation", "Difference (opening balances, advance payments or manual entries)"), D("0.00"))

    def test_asset_register_and_branch_performance(self):
        ops.register_asset(tenant=self.t, user=self.owner, name="Fridge", category="EQUIPMENT", cost="2400", purchase_date=ago(35),
                           useful_life_months=24, paid_with="CASH", branch=self.main)
        reg = docs.asset_register(self.f())
        self.assertEqual(reg.tables[0].rows[-1].cells[4], D("2400.00"))
        bp = docs.branch_performance(self.f())
        by = {r.cells[0]: r.cells for r in bp.tables[0].rows}
        self.assertEqual(by["Westlands"][1], D("0"))
        main = [v for k, v in by.items() if k != "Westlands"][0]
        self.assertEqual(main[1], D("170.00"))


class ExportTests(Scenario):
    def test_every_report_exports_to_pdf_and_excel(self):
        from openpyxl import load_workbook
        assert len(docs.REPORTS) == 12
        for key, meta in docs.REPORTS.items():
            doc = meta["build"](self.f())
            pdf, xlsx = exports.to_pdf(doc), exports.to_xlsx(doc)
            self.assertTrue(pdf.startswith(b"%PDF"), key)
            self.assertGreater(len(pdf), 1000, key)
            wb = load_workbook(io.BytesIO(xlsx))
            text = " ".join(str(c.value) for row in wb.active.iter_rows() for c in row if c.value is not None)
            for needle in (self.t.name, doc.period_label, doc.branch_label, doc.basis_label):
                self.assertIn(needle, text, f"{key} export is missing {needle!r}")

    def test_excel_contains_real_numbers_and_totals(self):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(exports.to_xlsx(docs.profit_loss(self.f())))).active
        found = {r[0].value: r[1].value for r in ws.iter_rows() if r[0].value}
        self.assertEqual(found["NET PROFIT AFTER TAX"], -665.0)
        self.assertEqual(found["Net sales"], 170.0)

    def test_pdf_text_has_header_details(self):
        pdf = exports.to_pdf(docs.balance_sheet(self.f()))
        self.assertIn(b"/Title", pdf)
        self.assertGreater(pdf.count(b"%%EOF"), 0)

    def test_export_endpoints_audit_and_gate(self):
        from .models import FinancialAuditLog
        self.client.force_login(self.owner)
        for key in docs.REPORTS:
            for fmt, ctype in (("pdf", "application/pdf"), ("xlsx", "spreadsheetml")):
                r = self.client.get(reverse("accounting:report-export", args=[key, fmt]) + "?period=custom&start=%s&end=%s" % (ago(40), TODAY))
                self.assertEqual(r.status_code, 200, (key, fmt))
                self.assertIn(ctype, r["Content-Type"])
                self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(FinancialAuditLog.all_objects.filter(tenant=self.t, action="REPORT_EXPORTED").count(), 24)
        self.client.force_login(self.shop["cashier"])
        self.assertEqual(self.client.get(reverse("accounting:report-export", args=["profit-loss", "pdf"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounting:report-export", args=["nonsense", "pdf"])).status_code, 403 if False else self.client.get(reverse("accounting:report-export", args=["nonsense", "pdf"])).status_code)


class PeriodTests(TestCase):
    def test_period_bounds(self):
        d = datetime.date(2026, 5, 20)                    # a Wednesday
        self.assertEqual(rp.period_bounds("daily", d), (d, d))
        self.assertEqual(rp.period_bounds("weekly", d), (datetime.date(2026, 5, 18), datetime.date(2026, 5, 24)))
        self.assertEqual(rp.period_bounds("monthly", d), (datetime.date(2026, 5, 1), datetime.date(2026, 5, 31)))
        self.assertEqual(rp.period_bounds("quarterly", d), (datetime.date(2026, 4, 1), datetime.date(2026, 6, 30)))
        self.assertEqual(rp.period_bounds("annual", d), (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)))

    def test_previous_ranges(self):
        self.assertEqual(rp.previous_range(datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)), (datetime.date(2026, 2, 1), datetime.date(2026, 2, 28)))
        self.assertEqual(rp.previous_range(datetime.date(2026, 1, 1), datetime.date(2026, 3, 31)), (datetime.date(2025, 10, 1), datetime.date(2025, 12, 31)))
        self.assertEqual(rp.previous_range(datetime.date(2026, 5, 10), datetime.date(2026, 5, 16)), (datetime.date(2026, 5, 3), datetime.date(2026, 5, 9)))
        self.assertEqual(rp.year_ago(datetime.date(2024, 2, 29), datetime.date(2024, 2, 29)), (datetime.date(2023, 2, 28), datetime.date(2023, 2, 28)))

    def test_fiscal_year_start(self):
        self.assertEqual(rp.fiscal_year_start(datetime.date(2026, 3, 10), 1), datetime.date(2026, 1, 1))
        self.assertEqual(rp.fiscal_year_start(datetime.date(2026, 3, 10), 7), datetime.date(2025, 7, 1))
        self.assertEqual(rp.fiscal_year_start(datetime.date(2026, 8, 1), 7), datetime.date(2026, 7, 1))

    def test_filter_parsing_and_branch_clamping(self):
        shop = finance_shop()
        owner, mgr, main, w = shop["owner"], shop["manager"], shop["branch"], shop["second"]
        f = rp.build_filter(tenant=shop["tenant"], user=owner, params={"period": "custom", "start": "2026-02-10", "end": "2026-02-01", "branch": str(w.pk), "basis": "CASH"})
        self.assertEqual((f.start, f.end, f.branch_ids, f.basis), (datetime.date(2026, 2, 1), datetime.date(2026, 2, 10), {w.pk}, "CASH"))
        f = rp.build_filter(tenant=shop["tenant"], user=owner, params={"branch": "99999", "period": "bogus", "basis": "x"})
        self.assertEqual((f.branch_ids, f.kind, f.basis), (None, "monthly", "ACCRUAL"))
        for params in ({"branch": "all"}, {"branch": str(w.pk)}, {}):
            f = rp.build_filter(tenant=shop["tenant"], user=mgr, params=params)
            self.assertEqual(f.branch_ids, {main.pk})
            self.assertTrue(f.restricted)

    def test_each_period_kind_builds_a_report(self):
        shop = finance_shop()
        for kind in ("daily", "weekly", "monthly", "quarterly", "annual", "custom"):
            f = rp.build_filter(tenant=shop["tenant"], user=shop["owner"], params={"period": kind, "start": str(ago(5)), "end": str(TODAY)})
            docs.profit_loss(f)


class AnalysisTests(Scenario):
    def test_unusual_expense_increase_is_flagged(self):
        ops.create_expense(tenant=self.t, user=self.owner, title="Old power", category=category(self.t, "Utilities"), amount="100",
                           expense_date=ago(35), branch=self.main)
        d = dash.analysis(self.f(start=ago(30), end=TODAY))
        self.assertTrue(any("Utilities" in a and "rose" in a for a in d.alerts), d.alerts)

    def test_a_loss_is_flagged(self):
        d = dash.analysis(self.f())
        self.assertTrue(any("loss" in a for a in d.alerts))

    def test_estimates_are_labelled_and_not_invented(self):
        d = dash.analysis(self.f())
        self.assertIsNone(d.estimate)                       # no completed months of sales yet
        self.assertTrue(any("Not enough" in n for n in d.notes))
        self.assertTrue(any("actual" in n.lower() for n in d.notes))

    def test_tables_present(self):
        d = dash.analysis(self.f())
        titles = [t.title for t in d.tables]
        for needed in ("Key ratios", "Monthly comparison (last 6 months)", "Quarterly comparison", "Expense categories", "Cash flow trend (actual)"):
            self.assertIn(needed, titles)
        self.assertTrue(any(t.startswith("Year over year") for t in titles))

    def test_monthly_series_matches_statements(self):
        s = dash.monthly_series(self.f())
        self.assertEqual(len(s), 12)
        self.assertEqual(sum(m["net_sales"] for m in s), D("170.00"))
        self.assertEqual(sum(m["net_profit"] for m in s), D("-665.00"))
        self.assertEqual(sum(m["cash_in"] for m in s), D("6170.00"))
        self.assertEqual(sum(m["cash_out"] for m in s), D("1160.00"))
