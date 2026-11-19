import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from . import operations as ops
from . import reporting as rp
from .models import (Expense, ExpenseAllocation, ExpenseCategory, ExpensePayment, FinancialAuditLog, JournalEntry,
                     RecurringExpense, LedgerImmutable)
from .services import AccountingError, get_settings, trial_balance
from .testkit import D, TODAY, ago, balance, category, finance_shop, other_shop


class Base(TestCase):
    def setUp(self):
        self.shop = finance_shop()
        self.t, self.owner, self.main = self.shop["tenant"], self.shop["owner"], self.shop["branch"]
        self.rent = category(self.t, "Rent")

    def expense(self, amount="1000", day=None, user=None, **kw):
        return ops.create_expense(tenant=self.t, user=user or self.owner, title=kw.pop("title", "April rent"), category=kw.pop("category", self.rent),
                                  amount=amount, expense_date=day or ago(5), branch=kw.pop("branch", self.main), **kw)

    def pay(self, e, amount, day=None, method="CASH", user=None):
        return ops.record_expense_payment(e, user=user or self.owner, amount=amount, payment_date=day or ago(2), method=method)

    def filt(self, basis="ACCRUAL", branch_ids=None):
        return rp.Filter(tenant=self.t, start=ago(30), end=TODAY, basis=basis, branch_ids=branch_ids)


class CreationTests(Base):
    def test_default_categories_exist_and_map_to_accounts(self):
        names = set(ExpenseCategory.all_objects.filter(tenant=self.t).values_list("name", flat=True))
        for n in ("Rent", "Salaries and wages", "Delivery and transportation", "Utilities", "Marketing and advertising",
                  "Maintenance and repairs", "Communication", "Security", "Licences and permits", "Insurance",
                  "Banking and transaction charges", "Packaging", "Professional services", "Depreciation",
                  "Loan interest and finance costs", "Taxes", "Miscellaneous"):
            self.assertIn(n, names)
        self.assertEqual(category(self.t, "Rent").account.system_key, "EXP_RENT")
        self.assertEqual(category(self.t, "Loan interest and finance costs").account.system_key, "EXP_FINANCE")

    def test_new_expense_is_unpaid_and_not_cash(self):
        cash_before = balance(self.t, "CASH")
        e = self.expense("1000")
        self.assertEqual(e.status, "UNPAID")
        self.assertEqual((e.amount_paid, e.balance), (D("0.00"), D("1000.00")))
        self.assertEqual(balance(self.t, "CASH"), cash_before)                 # unpaid expenses never touch cash
        self.assertEqual(balance(self.t, "EXP_RENT"), D("1000.00"))            # but they are an expense (accrual)
        self.assertEqual(balance(self.t, "ACCRUED_EXPENSES"), D("1000.00"))    # and a liability

    def test_records_creator_tenant_and_timestamp(self):
        e = self.expense()
        self.assertEqual((e.created_by, e.tenant), (self.owner, self.t))
        self.assertIsNotNone(e.created_at)

    def test_validation(self):
        for bad in (dict(amount="0"), dict(amount="-5"), dict(title="  "), dict(day=ago(400)),
                    dict(due_date=ago(10), day=ago(5))):
            with self.assertRaises(AccountingError, msg=bad):
                self.expense(**bad)
        self.assertEqual(Expense.all_objects.count(), 0)

    def test_inactive_category_blocked(self):
        ops.set_category_active(self.rent, self.owner, False)
        with self.assertRaises(AccountingError):
            self.expense()

    def test_custom_category_maps_to_chosen_account(self):
        from .models import Account
        fuel = Account.all_objects.create(tenant=self.t, code="6500", name="Fuel", account_type="OPEX")
        cat = ops.create_category(tenant=self.t, user=self.owner, name="Fuel", account=fuel)
        self.expense("300", category=cat)
        self.assertEqual(balance(self.t, "EXP_RENT"), D("0"))
        self.assertEqual(sum(l.debit for l in fuel.lines.model.all_objects.filter(account=fuel)), D("300.00"))
        with self.assertRaises(AccountingError):
            ops.create_category(tenant=self.t, user=self.owner, name="fuel", account=fuel)        # duplicate name
        with self.assertRaises(AccountingError):
            ops.create_category(tenant=self.t, user=self.owner, name="Bad", account=Account.all_objects.get(tenant=self.t, system_key="CASH"))

    def test_default_categories_cannot_be_deleted(self):
        with self.assertRaises(LedgerImmutable):
            self.rent.delete()
        custom = ExpenseCategory.all_objects.create(tenant=self.t, name="Temp", account=self.rent.account)
        self.expense(category=custom)
        with self.assertRaises(LedgerImmutable):
            custom.delete()                                  # categories with expenses cannot be deleted either

    def test_audit_trail(self):
        e = self.expense()
        self.pay(e, "100")
        actions = set(FinancialAuditLog.all_objects.filter(tenant=self.t).values_list("action", flat=True))
        self.assertTrue({"EXPENSE_CREATED", "EXPENSE_PAYMENT", "JOURNAL_POSTED"} <= actions)


class PaymentTests(Base):
    def test_partial_then_full_payment(self):
        e = self.expense("1000")
        self.pay(e, "400")
        e.refresh_from_db()
        self.assertEqual((e.status, e.amount_paid, e.balance), ("PARTIAL", D("400.00"), D("600.00")))
        self.pay(e, "600")
        e.refresh_from_db()
        self.assertEqual((e.status, e.balance), ("PAID", D("0.00")))
        self.assertEqual(balance(self.t, "ACCRUED_EXPENSES"), D("0"))
        self.assertEqual(balance(self.t, "CASH"), D("9000.00"))

    def test_overpayment_blocked(self):
        e = self.expense("1000")
        self.pay(e, "900")
        with self.assertRaisesMessage(AccountingError, "more than the balance"):
            self.pay(e, "200")
        self.assertEqual(ExpensePayment.all_objects.filter(expense=e).count(), 1)

    def test_payment_rules(self):
        e = self.expense("1000", day=ago(5))
        with self.assertRaises(AccountingError):
            self.pay(e, "10", day=ago(6))                       # before the expense
        with self.assertRaises(AccountingError):
            self.pay(e, "10", day=TODAY + timedelta(days=1))    # future
        with self.assertRaises(AccountingError):
            self.pay(e, "0")
        with self.assertRaises(AccountingError):
            self.pay(e, "10", method="BITCOIN")

    def test_payment_method_decides_the_account(self):
        e = self.expense("1000")
        self.pay(e, "300", method="MPESA")
        self.pay(e, "200", method="BANK")
        self.assertEqual((balance(self.t, "MPESA"), balance(self.t, "BANK"), balance(self.t, "CASH")), (D("-300.00"), D("-200.00"), D("10000.00")))

    def test_void_payment_restores_balance_and_keeps_history(self):
        e = self.expense("1000")
        p = self.pay(e, "1000")
        ops.void_expense_payment(p, user=self.owner, reason="Paid wrong landlord")
        e.refresh_from_db()
        self.assertEqual((e.status, e.balance), ("UNPAID", D("1000.00")))
        self.assertEqual(balance(self.t, "CASH"), D("10000.00"))
        self.assertTrue(ExpensePayment.all_objects.get(pk=p.pk).is_voided)
        with self.assertRaises(AccountingError):
            ops.void_expense_payment(p, user=self.owner, reason="again")
        with self.assertRaises(AccountingError):
            ops.void_expense_payment(self.pay(e, "100"), user=self.owner, reason=" ")

    def test_many_small_payments_never_drift(self):
        e = self.expense("100.00")
        for _ in range(3):
            self.pay(e, "33.33")
        self.pay(e, "0.01")
        e.refresh_from_db()
        self.assertEqual((e.status, e.balance), ("PAID", D("0.00")))

    def test_overdue_is_derived_not_stored(self):
        e = self.expense("500", day=ago(20), due_date=ago(3))
        self.assertTrue(e.is_overdue)
        self.assertEqual(e.display_status, "Overdue")
        self.pay(e, "500")
        e.refresh_from_db()
        self.assertFalse(e.is_overdue)


class CancelAndEditTests(Base):
    def test_cancel_unpaid_reverses_everything(self):
        e = self.expense("700")
        ops.cancel_expense(e, user=self.owner, reason="Duplicate")
        e.refresh_from_db()
        self.assertEqual((e.status, e.balance), ("CANCELLED", D("0.00")))
        self.assertEqual((balance(self.t, "EXP_RENT"), balance(self.t, "ACCRUED_EXPENSES")), (D("0"), D("0")))
        self.assertEqual(self.net_profit(), D("0"))
        with self.assertRaises(AccountingError):
            ops.cancel_expense(e, user=self.owner, reason="again")
        with self.assertRaises(AccountingError):
            self.pay(e, "10")

    def net_profit(self):
        return rp.totals_of(rp.income_numbers(self.filt()))["net_profit"]

    def filt(self):
        return rp.Filter(tenant=self.t, start=ago(30), end=TODAY)

    def test_cannot_cancel_with_payments(self):
        e = self.expense("700")
        p = self.pay(e, "100")
        with self.assertRaisesMessage(AccountingError, "Void them first"):
            ops.cancel_expense(e, user=self.owner, reason="x")
        ops.void_expense_payment(p, user=self.owner, reason="oops")
        ops.cancel_expense(e, user=self.owner, reason="x")

    def test_cosmetic_edit_does_not_restate_the_ledger(self):
        e = self.expense("700")
        n = JournalEntry.all_objects.count()
        ops.update_expense(e, user=self.owner, title="Rent for April", vendor="Landlord Ltd")
        self.assertEqual(JournalEntry.all_objects.count(), n)
        e.refresh_from_db()
        self.assertEqual((e.title, e.revision), ("Rent for April", 1))

    def test_amount_edit_restates_through_reversal(self):
        e = self.expense("700")
        ops.update_expense(e, user=self.owner, amount="900")
        e.refresh_from_db()
        self.assertEqual((e.amount, e.revision), (D("900.00"), 2))
        self.assertEqual((balance(self.t, "EXP_RENT"), balance(self.t, "ACCRUED_EXPENSES")), (D("900.00"), D("900.00")))
        sources = list(JournalEntry.all_objects.filter(tenant=self.t, source_type__in=["EXPENSE", "REVERSAL"]).values_list("status", flat=True))
        self.assertEqual(sorted(sources), ["POSTED", "POSTED", "REVERSED"])      # nothing was deleted
        _, td, tc = trial_balance(self.t)
        self.assertEqual(td, tc)

    def test_cannot_restate_once_paid(self):
        e = self.expense("700")
        self.pay(e, "100")
        with self.assertRaisesMessage(AccountingError, "already has payments"):
            ops.update_expense(e, user=self.owner, amount="800")
        ops.update_expense(e, user=self.owner, vendor="Fine to change")

    def test_category_change_moves_cost_between_accounts(self):
        e = self.expense("700")
        ops.update_expense(e, user=self.owner, category=category(self.t, "Utilities"))
        self.assertEqual((balance(self.t, "EXP_RENT"), balance(self.t, "EXP_UTILITIES")), (D("0"), D("700.00")))


class AllocationTests(Base):
    def test_head_office_expense_split_across_branches(self):
        w = self.shop["second"]
        e = self.expense("1000", branch=None, allocations=[(self.main, "60"), (w, "40")])
        self.assertEqual(ExpenseAllocation.all_objects.filter(expense=e).count(), 2)
        self.assertEqual(balance(self.t, "EXP_RENT", branch=self.main), D("600.00"))
        self.assertEqual(balance(self.t, "EXP_RENT", branch=w), D("400.00"))
        self.assertEqual(balance(self.t, "EXP_RENT"), D("1000.00"))
        f = rp.Filter(tenant=self.t, start=ago(30), end=TODAY, branch_ids={w.pk})
        self.assertEqual(dict(rp.income_numbers(f)["opex"])["Rent"], D("400.00"))

    def test_uneven_split_adds_up_exactly(self):
        w = self.shop["second"]
        self.expense("100.00", branch=None, allocations=[(self.main, "33.33"), (w, "66.67")])
        self.assertEqual(balance(self.t, "EXP_RENT"), D("100.00"))

    def test_bad_allocations_rejected(self):
        w = self.shop["second"]
        for bad in ([(self.main, "50"), (w, "40")], [(self.main, "100"), (self.main, "0.01")], [(self.main, "-5"), (w, "105")]):
            with self.assertRaises(AccountingError):
                self.expense("100", branch=None, allocations=bad)
        with self.assertRaises(AccountingError):
            self.expense("100", branch=self.main, allocations=[(self.main, "100")])      # only head-office expenses

    def test_cash_basis_branch_view_gets_its_share_of_payments(self):
        w = self.shop["second"]
        e = self.expense("1000", branch=None, allocations=[(self.main, "60"), (w, "40")])
        self.pay(e, "500")
        f = rp.Filter(tenant=self.t, start=ago(30), end=TODAY, basis="CASH", branch_ids={w.pk})
        self.assertEqual(dict(rp.income_numbers(f)["opex"])["Rent"], D("200.00"))


class RecurringTests(Base):
    def rec(self, freq="MONTHLY", start=None, **kw):
        return ops.create_recurring_expense(tenant=self.t, user=self.owner, title="Shop rent", category=self.rent, amount="5000",
                                            frequency=freq, start_date=start or ago(35), branch=self.main, **kw)

    def test_monthly_generation_is_unpaid(self):
        self.rec()
        n = ops.generate_recurring_expenses(self.t)
        self.assertGreaterEqual(n, 1)
        for e in Expense.all_objects.filter(tenant=self.t):
            self.assertEqual((e.status, e.amount_paid), ("UNPAID", D("0.00")))
        self.assertEqual(balance(self.t, "CASH"), D("10000.00"))                # nothing paid automatically

    def test_running_twice_creates_no_duplicates(self):
        self.rec("WEEKLY", start=ago(28))
        first = ops.generate_recurring_expenses(self.t)
        self.assertEqual(first, 5)
        self.assertEqual(ops.generate_recurring_expenses(self.t), 0)
        self.assertEqual(Expense.all_objects.filter(tenant=self.t).count(), 5)
        before = JournalEntry.all_objects.filter(tenant=self.t, source_type="EXPENSE").count()
        ops.generate_recurring_expenses(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="EXPENSE").count(), before)

    def test_database_blocks_a_second_expense_for_the_same_period(self):
        r = self.rec()
        ops.generate_recurring_expenses(self.t)
        e = Expense.all_objects.filter(recurring=r).first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Expense.all_objects.create(tenant=self.t, title="dup", category=self.rent, amount=D("1"), expense_date=e.expense_date,
                                       created_by=self.owner, recurring=r, recurrence_date=e.recurrence_date)

    def test_end_date_pause_and_cutover_are_respected(self):
        r = self.rec("DAILY", start=ago(60), end_date=ago(28))
        ops.generate_recurring_expenses(self.t)
        days = sorted(Expense.all_objects.filter(recurring=r).values_list("recurrence_date", flat=True))
        self.assertEqual((days[0], days[-1]), (ago(40), ago(28)))          # nothing before the books started, nothing after the end
        r2 = self.rec("DAILY", start=ago(3))
        ops.set_recurring_active(r2, self.owner, False)
        ops.generate_recurring_expenses(self.t)
        self.assertFalse(Expense.all_objects.filter(recurring=r2).exists())

    def test_schedules(self):
        import datetime
        mk = lambda f, s: RecurringExpense(frequency=f, start_date=s, end_date=None)
        jan31 = datetime.date(2026, 1, 31)
        m = ops.occurrence_dates(mk("MONTHLY", jan31), datetime.date(2026, 4, 30))
        self.assertEqual(m, [jan31, datetime.date(2026, 2, 28), datetime.date(2026, 3, 31), datetime.date(2026, 4, 30)])
        q = ops.occurrence_dates(mk("QUARTERLY", datetime.date(2026, 1, 15)), datetime.date(2026, 12, 31))
        self.assertEqual([d.month for d in q], [1, 4, 7, 10])
        y = ops.occurrence_dates(mk("ANNUAL", datetime.date(2024, 2, 29)), datetime.date(2027, 3, 1))
        self.assertEqual([d.isoformat() for d in y], ["2024-02-29", "2025-02-28", "2026-02-28", "2027-02-28"])
        self.assertEqual(len(ops.occurrence_dates(mk("DAILY", datetime.date(2026, 1, 1)), datetime.date(2026, 1, 10))), 10)
        self.assertEqual(len(ops.occurrence_dates(mk("WEEKLY", datetime.date(2026, 1, 1)), datetime.date(2026, 1, 29))), 5)

    def test_only_finance_staff_can_create(self):
        with self.assertRaises(AccountingError):
            ops.create_recurring_expense(tenant=self.t, user=self.shop["manager"], title="x", category=self.rent, amount="1",
                                         frequency="DAILY", start_date=ago(1))


class PermissionTests(Base):
    def test_cashier_cannot_record_expenses(self):
        with self.assertRaises(AccountingError):
            self.expense(user=self.shop["cashier"])

    def test_accountant_can(self):
        self.expense(user=self.shop["accountant"])

    def test_manager_limited_to_own_branch(self):
        m = self.shop["manager"]
        self.expense(user=m, branch=self.main)
        with self.assertRaises(AccountingError):
            self.expense(user=m, branch=self.shop["second"])
        with self.assertRaises(AccountingError):
            self.expense(user=m, branch=None)                     # head office is not theirs
        e_other = self.expense(branch=self.shop["second"])
        with self.assertRaises(AccountingError):
            self.pay(e_other, "10", user=m)
        with self.assertRaises(AccountingError):
            ops.cancel_expense(e_other, user=m, reason="x")

    def test_owner_can_switch_off_manager_expenses(self):
        cfg = get_settings(self.t)
        cfg.managers_can_manage_expenses = False
        cfg.save()
        with self.assertRaises(AccountingError):
            self.expense(user=self.shop["manager"], branch=self.main)

    def test_books_must_be_open(self):
        fresh = finance_shop.__globals__["make_shop"](business_name="Fresh", email="fresh@example.com", phone="254700999000")
        from .services import seed_chart_of_accounts
        seed_chart_of_accounts(fresh["tenant"])
        with self.assertRaisesMessage(AccountingError, "Set up accounting first"):
            ops.create_expense(tenant=fresh["tenant"], user=fresh["owner"], title="x", category=category(fresh["tenant"], "Rent"),
                               amount="5", expense_date=TODAY)


class IsolationAndReceiptTests(Base):
    def test_other_tenant_cannot_see_or_open_an_expense(self):
        e = self.expense()
        other = other_shop()
        self.client.force_login(other["owner"])
        self.assertEqual(self.client.get(reverse("accounting:expense-detail", args=[e.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("accounting:expense-pay", args=[e.pk]), {"amount": "5"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("accounting:receipt", args=[e.pk])).status_code, 404)

    def test_other_tenants_categories_are_not_usable(self):
        other = other_shop()
        with self.assertRaises(AccountingError):
            self.expense(category=category(other["tenant"], "Rent"))

    def test_manager_gets_404_for_another_branchs_expense(self):
        e_other = self.expense(branch=self.shop["second"])
        self.client.force_login(self.shop["manager"])
        self.assertEqual(self.client.get(reverse("accounting:expense-detail", args=[e_other.pk])).status_code, 404)
        mine = self.expense(branch=self.main)
        self.assertEqual(self.client.get(reverse("accounting:expense-detail", args=[mine.pk])).status_code, 200)

    def test_receipt_upload_and_protected_download(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(SUPPORT_UPLOAD_ROOT=tmp):
            self.client.force_login(self.owner)
            pdf = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
            r = self.client.post(reverse("accounting:expense-new"), dict(
                title="Power bill", category=category(self.t, "Utilities").pk, amount="2500", expense_date=ago(2).isoformat(),
                branch=self.main.pk, receipt=pdf))
            self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
            e = Expense.all_objects.get(title="Power bill")
            self.assertTrue(e.receipt)
            got = self.client.get(reverse("accounting:receipt", args=[e.pk]))
            self.assertEqual(got.status_code, 200)
            self.assertEqual(b"".join(got.streaming_content), b"%PDF-1.4 test")
            self.client.force_login(self.shop["cashier"])
            self.assertEqual(self.client.get(reverse("accounting:receipt", args=[e.pk])).status_code, 403)

    def test_bad_receipt_types_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(SUPPORT_UPLOAD_ROOT=tmp):
            self.client.force_login(self.owner)
            exe = SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream")
            r = self.client.post(reverse("accounting:expense-new"), dict(title="x", category=self.rent.pk, amount="5",
                                                                         expense_date=ago(1).isoformat(), branch=self.main.pk, receipt=exe))
            self.assertEqual(r.status_code, 200)
            self.assertFalse(Expense.all_objects.filter(title="x").exists())


class ExpenseScreenFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_create_pay_void_cancel_through_the_screens(self):
        r = self.client.post(reverse("accounting:expense-new"), dict(title="Security guard", category=category(self.t, "Security").pk,
                                                                      amount="3000", expense_date=ago(3).isoformat(), branch=self.main.pk))
        self.assertEqual(r.status_code, 302)
        e = Expense.all_objects.get(title="Security guard")
        self.assertEqual(self.client.get(reverse("accounting:expense-detail", args=[e.pk])).status_code, 200)
        self.client.post(reverse("accounting:expense-pay", args=[e.pk]), dict(amount="1000", payment_date=ago(1).isoformat(), payment_method="MPESA", reference="QWE123"))
        e.refresh_from_db()
        self.assertEqual((e.status, e.balance), ("PARTIAL", D("2000.00")))
        pay = ExpensePayment.all_objects.get(expense=e)
        self.client.post(reverse("accounting:payment-void", args=[pay.pk]), {"reason": "typo"})
        e.refresh_from_db()
        self.assertEqual(e.status, "UNPAID")
        self.client.post(reverse("accounting:expense-cancel", args=[e.pk]), {"reason": "not needed"})
        e.refresh_from_db()
        self.assertEqual(e.status, "CANCELLED")

    def test_list_filters_and_search(self):
        a = self.expense("100", title="Alpha bill", day=ago(3))
        b = self.expense("200", title="Beta bill", day=ago(3), category=category(self.t, "Utilities"))
        self.pay(a, "100")
        base = reverse("accounting:expenses") + "?period=custom&start=%s&end=%s" % (ago(10), TODAY)
        titles = lambda qs: [e.title for e in self.client.get(base + qs).context["page"]]
        self.assertEqual(sorted(titles("")), ["Alpha bill", "Beta bill"])
        self.assertEqual(titles("&status=PAID"), ["Alpha bill"])
        self.assertEqual(titles("&category=%s" % category(self.t, "Utilities").pk), ["Beta bill"])
        self.assertEqual(titles("&q=alp"), ["Alpha bill"])

    def test_repeat_choice_creates_a_schedule(self):
        self.client.post(reverse("accounting:expense-new"), dict(title="Cleaner", category=category(self.t, "Miscellaneous").pk, amount="800",
                                                                  expense_date=ago(20).isoformat(), branch=self.main.pk, repeat="WEEKLY"))
        self.assertEqual(RecurringExpense.all_objects.filter(tenant=self.t).count(), 1)
        self.assertGreaterEqual(Expense.all_objects.filter(tenant=self.t, title="Cleaner").count(), 3)
