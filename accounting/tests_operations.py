from django.test import TestCase

from sales.models import CashTransaction

from . import integration as ig
from . import operations as ops
from . import reporting as rp
from .models import FixedAsset, JournalEntry, JournalLine, Loan, OtherIncome, AssetDepreciation
from .services import AccountingError, trial_balance
from .testkit import D, TODAY, ago, balance, finance_shop


class Base(TestCase):
    def setUp(self):
        self.shop = finance_shop()
        self.t, self.owner, self.main = self.shop["tenant"], self.shop["owner"], self.shop["branch"]

    def profit(self, basis="ACCRUAL"):
        f = rp.Filter(tenant=self.t, start=ago(40), end=TODAY, basis=basis)
        return rp.totals_of(rp.income_numbers(f))

    def month_ends(self, start, upto=TODAY):
        out, me = [], ops._month_end(start)
        while me <= upto:
            out.append(me)
            me = ops._next_month_end(me)
        return out


class OtherIncomeTests(Base):
    def income(self, **kw):
        kw = {**dict(tenant=self.t, user=self.owner, title="Shop-front rent", kind="RENTAL", amount="4000", income_date=ago(10), branch=self.main), **kw}
        return ops.record_other_income(**kw)

    def test_unpaid_income_is_a_receivable_and_counts_on_accrual_only(self):
        i = self.income()
        self.assertEqual((balance(self.t, "AR"), balance(self.t, "CASH"), balance(self.t, "INCOME_RENTAL")), (D("4000.00"), D("10000.00"), D("4000.00")))
        self.assertEqual(self.profit("ACCRUAL")["other_income"], D("4000.00"))
        self.assertEqual(self.profit("CASH")["other_income"], D("0"))

    def test_receiving_it_moves_the_receivable_into_cash_and_into_cash_basis(self):
        i = self.income()
        ops.mark_income_received(i, user=self.owner, received_on=ago(2), payment_method="MPESA")
        self.assertEqual((balance(self.t, "AR"), balance(self.t, "MPESA")), (D("0.00"), D("4000.00")))
        self.assertEqual(self.profit("CASH")["other_income"], D("4000.00"))
        self.assertEqual(self.profit("ACCRUAL")["other_income"], D("4000.00"))      # not counted twice
        with self.assertRaises(AccountingError):
            ops.mark_income_received(i, user=self.owner, received_on=ago(1), payment_method="CASH")

    def test_received_on_the_day_is_one_entry(self):
        n = JournalEntry.all_objects.count()
        self.income(received_on=ago(10), payment_method="BANK")
        self.assertEqual(JournalEntry.all_objects.count(), n + 1)
        self.assertEqual((balance(self.t, "AR"), balance(self.t, "BANK")), (D("0"), D("4000.00")))

    def test_received_later_than_earned_books_both_steps(self):
        self.income(received_on=ago(4), payment_method="CASH")
        self.assertEqual(balance(self.t, "AR"), D("0"))
        self.assertEqual(balance(self.t, "CASH"), D("14000.00"))

    def test_rules(self):
        with self.assertRaises(AccountingError):
            self.income(received_on=ago(20), payment_method="CASH")          # before it was earned
        with self.assertRaises(AccountingError):
            self.income(income_date=ago(90))                                    # before the books started
        with self.assertRaises(AccountingError):
            self.income(user=self.shop["manager"])
        with self.assertRaises(AccountingError):
            self.income(amount="0")

    def test_cancel_reverses_every_step(self):
        i = self.income(received_on=ago(4), payment_method="CASH")
        ops.cancel_other_income(i, user=self.owner, reason="Tenant never paid")
        self.assertEqual((balance(self.t, "CASH"), balance(self.t, "AR"), balance(self.t, "INCOME_RENTAL")), (D("10000.00"), D("0"), D("0")))
        self.assertEqual(self.profit()["other_income"], D("0"))
        with self.assertRaises(AccountingError):
            ops.cancel_other_income(i, user=self.owner, reason="again")


class OwnerMoneyTests(Base):
    def txn(self, kind, amount, **kw):
        return ops.record_owner_transaction(tenant=self.t, user=kw.pop("user", self.owner), kind=kind, amount=amount, txn_date=ago(3),
                                            payment_method=kw.pop("method", "CASH"), **kw)

    def test_capital_and_drawings_never_touch_profit(self):
        self.txn("CAPITAL", "5000")
        self.txn("DRAWING", "1200")
        self.assertEqual(balance(self.t, "CASH"), D("13800.00"))
        self.assertEqual((balance(self.t, "OWNER_CAPITAL"), balance(self.t, "OWNER_DRAWINGS")), (D("5000.00"), D("1200.00")))
        self.assertEqual(self.profit()["net_profit"], D("0"))
        self.assertEqual(self.profit()["opex"], D("0"))

    def test_only_owner_or_admin(self):
        with self.assertRaises(AccountingError):
            self.txn("DRAWING", "10", user=self.shop["accountant"])


class AssetTests(Base):
    def asset(self, **kw):
        base = dict(tenant=self.t, user=self.owner, name="Chiller", category="EQUIPMENT", cost="120000", purchase_date=ago(35),
                    useful_life_months=12, residual_value="0", branch=self.main, paid_with="BANK")
        return ops.register_asset(**{**base, **kw})

    def test_purchase_is_capitalised_not_expensed(self):
        self.asset()
        self.assertEqual((balance(self.t, "FIXED_ASSETS"), balance(self.t, "BANK")), (D("120000.00"), D("-120000.00")))
        self.assertEqual(self.profit()["opex"], D("0"))

    def test_credit_purchase_creates_a_payable(self):
        self.asset(paid_with="CREDIT")
        self.assertEqual(balance(self.t, "AP"), D("120000.00"))

    def test_straight_line_depreciation_is_idempotent_and_periodic(self):
        a = self.asset()
        expected = len(self.month_ends(a.purchase_date))
        self.assertEqual(ops.generate_depreciation(self.t, up_to=TODAY), expected)
        self.assertEqual(ops.generate_depreciation(self.t, up_to=TODAY), 0)
        rows = list(AssetDepreciation.all_objects.filter(asset=a).order_by("period_end"))
        self.assertEqual([r.period_end for r in rows], self.month_ends(a.purchase_date))
        self.assertTrue(all(r.amount == D("10000.00") for r in rows))
        a.refresh_from_db()
        self.assertEqual(a.accumulated_depreciation, D("10000.00") * expected)
        self.assertEqual(a.book_value, D("120000.00") - D("10000.00") * expected)
        self.assertEqual(balance(self.t, "ACCUM_DEPRECIATION"), a.accumulated_depreciation)
        self.assertEqual(balance(self.t, "EXP_DEPRECIATION"), a.accumulated_depreciation)
        self.assertEqual(self.profit()["opex"], a.accumulated_depreciation)       # depreciation is the only expense recognised

    def test_depreciation_lands_in_the_right_period(self):
        a = self.asset()
        ops.generate_depreciation(self.t, up_to=TODAY)
        for e in JournalEntry.all_objects.filter(tenant=self.t, source_type="DEPRECIATION"):
            self.assertEqual(e.entry_date, ops._month_end(e.entry_date))          # month-end dated
            self.assertEqual(e.period.start_date, e.entry_date.replace(day=1))

    def test_total_never_exceeds_cost_minus_residual_and_rounding_is_absorbed(self):
        a = self.asset(cost="100", useful_life_months=3, residual_value="0", purchase_date=ago(39))
        ops.generate_depreciation(self.t, up_to=TODAY + __import__("datetime").timedelta(days=200))
        a.refresh_from_db()
        self.assertEqual(a.accumulated_depreciation, D("100.00"))
        self.assertEqual(sorted(AssetDepreciation.all_objects.filter(asset=a).values_list("amount", flat=True)), [D("33.33"), D("33.33"), D("33.34")])
        self.assertEqual(a.book_value, D("0.00"))
        b = self.asset(name="Van", cost="1000", residual_value="400", useful_life_months=2, purchase_date=ago(39))
        ops.generate_depreciation(self.t, up_to=TODAY + __import__("datetime").timedelta(days=200))
        b.refresh_from_db()
        self.assertEqual(b.book_value, D("400.00"))

    def test_validation(self):
        for bad in (dict(cost="0"), dict(residual_value="120000"), dict(useful_life_months=0), dict(purchase_date=ago(90)),
                    dict(paid_with=""), dict(user=self.shop["cashier"])):
            with self.assertRaises(AccountingError, msg=str(bad)):
                self.asset(**bad)

    def test_existing_asset_is_not_bought_again(self):
        n = JournalEntry.all_objects.count()
        a = self.asset(purchase_date=ago(400), existing_asset=True, opening_accumulated_depreciation="50000", paid_with="")
        self.assertEqual(JournalEntry.all_objects.count(), n)
        self.assertEqual(a.book_value, D("70000.00"))
        with self.assertRaises(AccountingError):
            self.asset(purchase_date=ago(5), existing_asset=True)
        with self.assertRaises(AccountingError):
            self.asset(purchase_date=ago(400), existing_asset=True, opening_accumulated_depreciation="999999")

    def test_disposal_with_gain(self):
        a = self.asset(cost="1200", useful_life_months=12, purchase_date=ago(39))
        ops.dispose_asset(a, user=self.owner, disposal_date=ago(1), proceeds="2000", method="CASH")
        a.refresh_from_db()
        self.assertEqual(a.status, "DISPOSED")
        self.assertEqual((balance(self.t, "FIXED_ASSETS"), balance(self.t, "ACCUM_DEPRECIATION")), (D("0.00"), D("0.00")))
        self.assertGreater(balance(self.t, "INCOME_OTHER"), 0)
        _, td, tc = trial_balance(self.t)
        self.assertEqual(td, tc)
        with self.assertRaises(AccountingError):
            ops.dispose_asset(a, user=self.owner, disposal_date=ago(1), proceeds="1", method="CASH")

    def test_disposal_with_loss_and_no_proceeds(self):
        a = self.asset(cost="1200", useful_life_months=120, purchase_date=ago(39))
        ops.dispose_asset(a, user=self.owner, disposal_date=ago(1))
        self.assertGreater(balance(self.t, "EXP_MISC"), 0)
        self.assertEqual(balance(self.t, "FIXED_ASSETS"), D("0"))

    def test_depreciation_is_not_a_cash_basis_expense(self):
        self.asset()
        ops.generate_depreciation(self.t, up_to=TODAY)
        self.assertGreater(self.profit("ACCRUAL")["opex"], 0)
        self.assertEqual(self.profit("CASH")["opex"], D("0"))


class LoanTests(Base):
    def loan(self, **kw):
        base = dict(tenant=self.t, user=self.owner, lender="Equity Bank", principal="60000", interest_rate_percent="12", start_date=ago(30),
                    installments=12, frequency="MONTHLY", received_via="BANK", branch=self.main)
        return ops.record_loan(**{**base, **kw})

    def repay(self, loan, principal, interest, day=None, method="BANK"):
        return ops.repay_loan(loan, user=self.owner, paid_on=day or ago(2), principal_paid=principal, interest_paid=interest, payment_method=method)

    def test_receipt_creates_cash_and_a_liability_not_income(self):
        self.loan()
        self.assertEqual((balance(self.t, "BANK"), balance(self.t, "LOANS")), (D("60000.00"), D("60000.00")))
        self.assertEqual(self.profit()["net_profit"], D("0"))

    def test_principal_is_not_an_expense_but_interest_is(self):
        l = self.loan()
        self.repay(l, "5000", "600")
        self.assertEqual((balance(self.t, "LOANS"), balance(self.t, "EXP_FINANCE"), balance(self.t, "BANK")), (D("55000.00"), D("600.00"), D("54400.00")))
        p = self.profit()
        self.assertEqual((p["finance"], p["opex"], p["net_profit"]), (D("600.00"), D("0"), D("-600.00")))
        l.refresh_from_db()
        self.assertEqual((l.outstanding_principal, l.principal_repaid, l.interest_paid), (D("55000.00"), D("5000.00"), D("600.00")))

    def test_cash_basis_counts_interest_when_paid(self):
        l = self.loan()
        self.repay(l, "5000", "600")
        self.assertEqual(self.profit("CASH")["finance"], D("600.00"))

    def test_cannot_overpay_and_loan_closes_when_cleared(self):
        l = self.loan(principal="1000", installments=2)
        with self.assertRaises(AccountingError):
            self.repay(l, "1000.01", "0")
        self.repay(l, "1000", "10")
        l.refresh_from_db()
        self.assertTrue(l.is_closed)
        with self.assertRaises(AccountingError):
            self.repay(l, "1", "0")

    def test_repayment_validation(self):
        l = self.loan()
        for p, i in (("0", "0"), ("-1", "5")):
            with self.assertRaises(AccountingError):
                self.repay(l, p, i)
        with self.assertRaises(AccountingError):
            self.repay(l, "10", "1", day=ago(35))                      # before the loan started
        with self.assertRaises(AccountingError):
            ops.repay_loan(l, user=self.shop["cashier"], paid_on=ago(1), principal_paid="1", interest_paid="0", payment_method="CASH")

    def test_existing_loan_uses_the_opening_balance(self):
        n = JournalEntry.all_objects.count()
        l = self.loan(start_date=ago(400), existing_loan=True, opening_outstanding="40000", received_via="")
        self.assertEqual(JournalEntry.all_objects.count(), n)
        self.assertEqual(l.outstanding_principal, D("40000.00"))
        self.repay(l, "10000", "400", method="CASH")
        l.refresh_from_db()
        self.assertEqual(l.outstanding_principal, D("30000.00"))
        with self.assertRaises(AccountingError):
            self.loan(start_date=ago(5), existing_loan=True, opening_outstanding="1")
        with self.assertRaises(AccountingError):
            self.loan(start_date=ago(400), existing_loan=True, opening_outstanding="999999")

    def test_schedule_adds_up(self):
        l = self.loan()
        s = ops.loan_schedule(l)
        self.assertEqual(len(s), 12)
        self.assertEqual(sum(r["principal"] for r in s), D("60000.00"))
        self.assertEqual(s[0]["interest"], D("600.00"))
        self.assertEqual(s[-1]["closing"], D("0.00"))
        self.assertTrue(all(a["due"] < b["due"] for a, b in zip(s, s[1:])))


class TillMovementTests(Base):
    def txn(self, kind, amount="500", reason="Float"):
        return CashTransaction.all_objects.create(tenant=self.t, session=self.shop["session"], user=self.owner, transaction_type=kind,
                                                  amount=D(amount), reason=reason)

    def test_unclassified_list_and_classification(self):
        t = self.txn("CASH_IN")
        self.assertEqual([x.pk for x in ig.unclassified_cash_movements(self.t)], [t.pk])
        from .models import Account
        cap = Account.all_objects.get(tenant=self.t, system_key="OWNER_CAPITAL")
        ops.classify_cash_movement(t, user=self.owner, account=cap)
        self.assertEqual(balance(self.t, "CASH"), D("10500.00"))
        self.assertEqual(balance(self.t, "OWNER_CAPITAL"), D("500.00"))
        self.assertEqual(ig.unclassified_cash_movements(self.t), [])
        ops.classify_cash_movement(t, user=self.owner, account=cap)               # idempotent
        self.assertEqual(balance(self.t, "CASH"), D("10500.00"))

    def test_cash_out_to_expense_account(self):
        from .models import Account
        t = self.txn("CASH_OUT", "200", "Tea")
        ops.classify_cash_movement(t, user=self.owner, account=Account.all_objects.get(tenant=self.t, system_key="EXP_MISC"))
        self.assertEqual((balance(self.t, "CASH"), balance(self.t, "EXP_MISC")), (D("9800.00"), D("200.00")))

    def test_cannot_classify_against_cash_or_without_rights(self):
        from .models import Account
        t = self.txn("CASH_IN")
        with self.assertRaises(AccountingError):
            ops.classify_cash_movement(t, user=self.owner, account=Account.all_objects.get(tenant=self.t, system_key="CASH"))
        with self.assertRaises(AccountingError):
            ops.classify_cash_movement(t, user=self.shop["cashier"], account=Account.all_objects.get(tenant=self.t, system_key="BANK"))


class HousekeepingCommandTests(Base):
    def test_command_generates_everything_once(self):
        from io import StringIO

        from django.core.management import call_command

        ops.create_recurring_expense(tenant=self.t, user=self.owner, title="Rent", category=__import__("accounting.testkit", fromlist=["x"]).category(self.t, "Rent"),
                                     amount="100", frequency="WEEKLY", start_date=ago(14))
        ops.register_asset(tenant=self.t, user=self.owner, name="Chiller", category="EQUIPMENT", cost="1200", purchase_date=ago(38),
                           useful_life_months=12, paid_with="CASH")
        out = StringIO()
        call_command("accounting_housekeeping", stdout=out)
        self.assertIn("complete", out.getvalue())
        from .models import Expense
        n = Expense.all_objects.filter(tenant=self.t).count()
        self.assertEqual(n, 3)
        call_command("accounting_housekeeping", "--tenant", str(self.t.pk), stdout=StringIO())
        self.assertEqual(Expense.all_objects.filter(tenant=self.t).count(), n)
