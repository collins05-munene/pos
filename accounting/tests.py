import datetime
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from billing.models import Subscription
from inventory.models import Branch
from tenants.context import tenant_context
from tenants.services import create_staff_user, register_business
from users.views import home_for

from .models import (Account, FinancialAuditLog, FiscalPeriod, JournalEntry, JournalLine, LedgerImmutable)
from .permissions import can_configure_accounting, can_view_finance
from .services import (AccountingError, get_settings, post_journal, reverse_entry, seed_chart_of_accounts,
                       set_opening_balances, trial_balance)

D = Decimal
PW = "S3cure-pass-99"
SIGNUP = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
              email="njeri@example.com", phone="254712345678", password=PW, term_months=3)
OTHER = dict(SIGNUP, business_name="Kamau Fresh Mart", email="kamau@example.com", phone="254722000111",
             owner_name="Kamau K")
TODAY = datetime.date.today()


def make_shop(signup=SIGNUP):
    tenant, owner, _ = register_business(**signup)
    Subscription.objects.filter(tenant=tenant).update(comp_until=timezone.now() + timedelta(days=30))
    with tenant_context(tenant):
        branch = Branch.objects.first()
    seed_chart_of_accounts(tenant)
    return tenant, owner, branch


def sale_lines(amount="100.00"):
    return [dict(account="CASH", debit=D(amount)), dict(account="SALES_REVENUE", credit=D(amount))]


class LedgerTestBase(TestCase):
    def setUp(self):
        self.tenant, self.owner, self.branch = make_shop()


class ChartOfAccountsTests(LedgerTestBase):
    def test_default_groups_and_accounts_exist(self):
        codes = set(Account.all_objects.filter(tenant=self.tenant).values_list("code", flat=True))
        for header in ("1000", "2000", "3000", "4000", "5000", "6000", "7000"):
            self.assertIn(header, codes)
        for key in ("CASH", "AR", "INVENTORY", "AP", "SALES_REVENUE", "COGS", "EXP_RENT", "EXP_DEPRECIATION"):
            self.assertTrue(Account.all_objects.filter(tenant=self.tenant, system_key=key).exists(), key)

    def test_seeding_twice_changes_nothing(self):
        before = Account.all_objects.filter(tenant=self.tenant).count()
        acct = Account.all_objects.get(tenant=self.tenant, system_key="CASH")
        acct.name = "Till Cash"
        acct.save()
        self.assertEqual(seed_chart_of_accounts(self.tenant), 0)
        self.assertEqual(Account.all_objects.filter(tenant=self.tenant).count(), before)
        acct.refresh_from_db()
        self.assertEqual(acct.name, "Till Cash")      # a rename survives re-seeding

    def test_every_default_expense_category_has_an_account(self):
        names = set(Account.all_objects.filter(tenant=self.tenant, account_type="OPEX").values_list("name", flat=True))
        for needed in ("Rent", "Salaries and Wages", "Delivery and Transportation", "Utilities",
                       "Marketing and Advertising", "Maintenance and Repairs", "Communication", "Security",
                       "Licences and Permits", "Insurance", "Banking and Transaction Charges", "Packaging",
                       "Professional Services", "Depreciation", "Miscellaneous"):
            self.assertIn(needed, names)

    def test_normal_sides(self):
        get = lambda k: Account.all_objects.get(tenant=self.tenant, system_key=k)
        self.assertEqual(get("CASH").normal_side, "DEBIT")
        self.assertEqual(get("AP").normal_side, "CREDIT")
        self.assertEqual(get("SALES_REVENUE").normal_side, "CREDIT")
        self.assertEqual(get("SALES_DISCOUNTS").normal_side, "DEBIT")       # contra revenue
        self.assertEqual(get("ACCUM_DEPRECIATION").normal_side, "CREDIT")   # contra asset
        self.assertEqual(get("OWNER_DRAWINGS").normal_side, "DEBIT")        # contra equity

    def test_custom_account_code_must_match_type(self):
        bad = Account(tenant=self.tenant, code="6500", name="Oops", account_type="REVENUE")
        with self.assertRaises(Exception):
            bad.clean()
        Account(tenant=self.tenant, code="6500", name="Fuel", account_type="OPEX").clean()

    def test_system_accounts_cannot_be_deleted_or_recoded(self):
        cash = Account.all_objects.get(tenant=self.tenant, system_key="CASH")
        with self.assertRaises(LedgerImmutable):
            cash.delete()
        cash.code = "1099"
        with self.assertRaises(LedgerImmutable):
            cash.save()

    def test_account_with_postings_cannot_be_deleted(self):
        custom = Account.all_objects.create(tenant=self.tenant, code="6500", name="Fuel", account_type="OPEX")
        post_journal(tenant=self.tenant, entry_date=TODAY, description="Fuel", source_type="MANUAL",
                     lines=[dict(account=custom, debit=D("50")), dict(account="CASH", credit=D("50"))])
        with self.assertRaises(LedgerImmutable):
            custom.delete()

    def test_cannot_post_to_header_or_inactive_account(self):
        header = Account.all_objects.get(tenant=self.tenant, code="4000")
        with self.assertRaises(AccountingError):
            post_journal(tenant=self.tenant, entry_date=TODAY, description="x", source_type="MANUAL",
                         lines=[dict(account="CASH", debit=D("5")), dict(account=header, credit=D("5"))])
        Account.all_objects.filter(tenant=self.tenant, system_key="CASH").update(is_active=False)
        with self.assertRaises(AccountingError):
            post_journal(tenant=self.tenant, entry_date=TODAY, description="x", source_type="MANUAL",
                         lines=sale_lines("5"))


class PostingTests(LedgerTestBase):
    def post(self, **over):
        kw = dict(tenant=self.tenant, entry_date=TODAY, description="Cash sale", source_type="SALE",
                  lines=sale_lines(), user=self.owner)
        kw.update(over)
        return post_journal(**kw)

    def test_balanced_entry_posts(self):
        entry, created = self.post()
        self.assertTrue(created)
        self.assertEqual(entry.status, "POSTED")
        self.assertEqual(entry.total_debit, entry.total_credit)
        self.assertEqual(entry.get_lines().count(), 2)
        self.assertTrue(entry.reference.startswith("JE-"))

    def test_unbalanced_entry_is_rejected_and_nothing_is_written(self):
        before = JournalEntry.all_objects.count()
        with self.assertRaisesMessage(AccountingError, "does not balance"):
            self.post(lines=[dict(account="CASH", debit=D("100")), dict(account="SALES_REVENUE", credit=D("99.99"))])
        self.assertEqual(JournalEntry.all_objects.count(), before)
        self.assertEqual(JournalLine.all_objects.count(), 0)

    def test_needs_two_lines_and_nonzero_amount(self):
        with self.assertRaises(AccountingError):
            self.post(lines=[dict(account="CASH", debit=D("5"))])
        with self.assertRaises(AccountingError):
            self.post(lines=[dict(account="CASH", debit=D("0")), dict(account="SALES_REVENUE", credit=D("0"))])

    def test_line_cannot_carry_both_sides(self):
        with self.assertRaises(AccountingError):
            self.post(lines=[dict(account="CASH", debit=D("5"), credit=D("5")),
                             dict(account="SALES_REVENUE", credit=D("5"))])

    def test_floats_are_refused(self):
        with self.assertRaisesMessage(AccountingError, "float"):
            self.post(lines=[dict(account="CASH", debit=100.10), dict(account="SALES_REVENUE", credit=100.10)])

    def test_amounts_are_rounded_to_cents_with_decimals(self):
        entry, _ = self.post(lines=[dict(account="CASH", debit="10.005"),
                                    dict(account="SALES_REVENUE", credit="10.01")])
        self.assertEqual(entry.total_debit, D("10.01"))

    def test_same_source_key_posts_exactly_once(self):
        e1, c1 = self.post(source_key="sale:42")
        e2, c2 = self.post(source_key="sale:42")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(e1.pk, e2.pk)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.tenant, source_key="sale:42").count(), 1)
        self.assertEqual(JournalLine.all_objects.filter(entry=e1).count(), 2)

    def test_database_rejects_a_duplicate_source_key(self):
        self.post(source_key="sale:7")
        first = JournalEntry.all_objects.get(source_key="sale:7")
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalEntry.all_objects.create(tenant=self.tenant, reference="JE-X", entry_date=TODAY,
                                            description="dup", period=first.period, source_type="SALE",
                                            source_key="sale:7")

    def test_references_are_unique_and_sequential(self):
        a, _ = self.post()
        b, _ = self.post()
        self.assertNotEqual(a.reference, b.reference)
        self.assertEqual(int(b.reference[3:]) - int(a.reference[3:]), 1)

    def test_period_is_created_and_a_closed_period_blocks_posting(self):
        entry, _ = self.post()
        FiscalPeriod.all_objects.filter(pk=entry.period_id).update(status="CLOSED")
        with self.assertRaisesMessage(AccountingError, "closed"):
            self.post()

    def test_posting_before_the_opening_date_is_refused(self):
        cfg = get_settings(self.tenant)
        cfg.opening_balance_date = TODAY - timedelta(days=5)
        cfg.save()
        with self.assertRaisesMessage(AccountingError, "ledger starts"):
            self.post(entry_date=TODAY - timedelta(days=6))
        self.post(entry_date=TODAY - timedelta(days=5))

    def test_audit_trail_records_the_posting(self):
        entry, _ = self.post()
        log = FinancialAuditLog.all_objects.get(tenant=self.tenant, action="JOURNAL_POSTED", object_id=str(entry.pk))
        self.assertEqual(log.user, self.owner)
        self.assertEqual(log.details["amount"], "100.00")

    def test_line_branch_defaults_to_entry_branch(self):
        entry, _ = self.post(branch=self.branch)
        self.assertEqual(set(entry.get_lines().values_list("branch_id", flat=True)), {self.branch.pk})

    def test_check_constraint_blocks_a_two_sided_line_even_around_the_service(self):
        entry, _ = self.post()
        cash = Account.all_objects.get(tenant=self.tenant, system_key="CASH")
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalLine.all_objects.bulk_create([JournalLine(tenant=self.tenant, entry=entry, account=cash,
                                                             debit=D("1"), credit=D("1"))])


class ImmutabilityTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.entry, _ = post_journal(tenant=self.tenant, entry_date=TODAY, description="Sale",
                                     source_type="SALE", lines=sale_lines(), user=self.owner)

    def test_posted_entry_cannot_be_edited_or_deleted(self):
        self.entry.description = "tampered"
        with self.assertRaises(LedgerImmutable):
            self.entry.save()
        with self.assertRaises(LedgerImmutable):
            self.entry.delete()

    def test_posted_entry_fields_cannot_be_changed_even_with_status_posted(self):
        self.entry.entry_date = TODAY - timedelta(days=30)
        with self.assertRaises(LedgerImmutable):
            self.entry.save()

    def test_posted_lines_cannot_be_edited_or_deleted(self):
        line = self.entry.get_lines().first()
        line.debit = D("1.00")
        with self.assertRaises(LedgerImmutable):
            line.save()
        with self.assertRaises(LedgerImmutable):
            line.delete()

    def test_bulk_update_and_delete_are_blocked_on_both_managers(self):
        for manager in (JournalLine.objects, JournalLine.all_objects):
            with tenant_context(self.tenant):
                with self.assertRaises(LedgerImmutable):
                    manager.filter(entry=self.entry).update(debit=D("999"))
                with self.assertRaises(LedgerImmutable):
                    manager.filter(entry=self.entry).delete()
        for manager in (JournalEntry.objects, JournalEntry.all_objects):
            with tenant_context(self.tenant):
                with self.assertRaises(LedgerImmutable):
                    manager.filter(pk=self.entry.pk).update(description="x")
                with self.assertRaises(LedgerImmutable):
                    manager.filter(pk=self.entry.pk).delete()
        self.assertEqual(self.entry.get_lines().count(), 2)

    def test_audit_log_is_append_only(self):
        log = FinancialAuditLog.all_objects.filter(tenant=self.tenant).first()
        log.action = "X"
        with self.assertRaises(LedgerImmutable):
            log.save()
        with self.assertRaises(LedgerImmutable):
            log.delete()


class ReversalTests(LedgerTestBase):
    def setUp(self):
        super().setUp()
        self.entry, _ = post_journal(tenant=self.tenant, entry_date=TODAY, description="Sale",
                                     source_type="SALE", lines=sale_lines("250.00"), user=self.owner)

    def test_reversal_cancels_the_original_and_keeps_both_on_the_books(self):
        rev = reverse_entry(self.entry, user=self.owner, reason="Posted in error")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, "REVERSED")
        self.assertEqual(rev.status, "POSTED")
        self.assertEqual(rev.reverses_id, self.entry.pk)
        self.assertEqual(rev.reversal_reason, "Posted in error")
        rows, td, tc = trial_balance(self.tenant)
        self.assertEqual(td, tc)
        self.assertTrue(all(r["balance"] == 0 for r in rows))
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.tenant).count(), 2)

    def test_reversal_lines_are_mirrored(self):
        rev = reverse_entry(self.entry, user=self.owner, reason="Wrong customer")
        cash = rev.get_lines().get(account__system_key="CASH")
        self.assertEqual((cash.debit, cash.credit), (D("0.00"), D("250.00")))

    def test_cannot_reverse_twice_or_reverse_a_reversal(self):
        rev = reverse_entry(self.entry, user=self.owner, reason="oops")
        with self.assertRaisesMessage(AccountingError, "already been reversed"):
            reverse_entry(self.entry, user=self.owner, reason="again")
        with self.assertRaises(AccountingError):
            reverse_entry(rev, user=self.owner, reason="undo the undo")

    def test_a_reason_is_required(self):
        with self.assertRaises(AccountingError):
            reverse_entry(self.entry, user=self.owner, reason="  ")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, "POSTED")

    def test_reversal_is_audited(self):
        reverse_entry(self.entry, user=self.owner, reason="oops")
        self.assertTrue(FinancialAuditLog.all_objects.filter(tenant=self.tenant, action="JOURNAL_REVERSED").exists())


class OpeningBalanceTests(LedgerTestBase):
    def test_opening_balances_balance_and_use_the_plug_account(self):
        as_of = TODAY - timedelta(days=10)
        entry = set_opening_balances(tenant=self.tenant, as_of=as_of, user=self.owner, balances={
            "CASH": "50000", "INVENTORY": "120000", "AP": "30000"})
        self.assertEqual(entry.total_debit, entry.total_credit)
        rows, td, tc = trial_balance(self.tenant)
        by_key = {r["account"].system_key: r for r in rows}
        self.assertEqual(by_key["CASH"]["balance"], D("50000.00"))
        self.assertEqual(by_key["AP"]["balance"], D("30000.00"))
        self.assertEqual(by_key["OPENING_EQUITY"]["balance"], D("140000.00"))   # assets 170k - liabilities 30k
        self.assertEqual(td, tc)
        self.assertEqual(get_settings(self.tenant).opening_balance_date, as_of)

    def test_cannot_be_set_twice(self):
        set_opening_balances(tenant=self.tenant, as_of=TODAY, balances={"CASH": "10"})
        with self.assertRaises(AccountingError):
            set_opening_balances(tenant=self.tenant, as_of=TODAY, balances={"CASH": "20"})

    def test_empty_opening_balances_still_start_the_books(self):
        self.assertIsNone(set_opening_balances(tenant=self.tenant, as_of=TODAY, balances={}))
        self.assertEqual(get_settings(self.tenant).opening_balance_date, TODAY)

    def test_liabilities_land_on_the_credit_side(self):
        set_opening_balances(tenant=self.tenant, as_of=TODAY, balances={"LOANS": "1000"})
        line = JournalLine.all_objects.get(tenant=self.tenant, account__system_key="LOANS")
        self.assertEqual((line.debit, line.credit), (D("0.00"), D("1000.00")))


class TrialBalanceTests(LedgerTestBase):
    def test_always_balances_and_filters_by_date_and_branch(self):
        post_journal(tenant=self.tenant, entry_date=TODAY, description="a", source_type="SALE",
                     lines=sale_lines("100"), branch=self.branch)
        post_journal(tenant=self.tenant, entry_date=TODAY, description="b", source_type="SALE",
                     lines=sale_lines("40"))                                  # no branch (head office)
        _, td, tc = trial_balance(self.tenant)
        self.assertEqual((td, tc), (D("140.00"), D("140.00")))
        _, bd, bc = trial_balance(self.tenant, branch=self.branch)
        self.assertEqual((bd, bc), (D("100.00"), D("100.00")))
        _, fd, fc = trial_balance(self.tenant, start=TODAY + timedelta(days=1))
        self.assertEqual((fd, fc), (D("0"), D("0")))


class TenantIsolationTests(TestCase):
    def setUp(self):
        self.a, self.owner_a, self.branch_a = make_shop(SIGNUP)
        self.b, self.owner_b, self.branch_b = make_shop(OTHER)

    def test_ledgers_are_separate(self):
        post_journal(tenant=self.a, entry_date=TODAY, description="A sale", source_type="SALE",
                     lines=sale_lines("500"))
        _, td_b, _ = trial_balance(self.b)
        self.assertEqual(td_b, 0)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.b).count(), 0)

    def test_same_source_key_in_two_tenants_does_not_collide(self):
        for t in (self.a, self.b):
            _, created = post_journal(tenant=t, entry_date=TODAY, description="s", source_type="SALE",
                                      source_key="sale:1", lines=sale_lines())
            self.assertTrue(created)

    def test_cannot_post_to_another_tenants_account(self):
        foreign = Account.all_objects.get(tenant=self.b, system_key="SALES_REVENUE")
        with self.assertRaisesMessage(AccountingError, "another business"):
            post_journal(tenant=self.a, entry_date=TODAY, description="x", source_type="SALE",
                         lines=[dict(account="CASH", debit=D("5")), dict(account=foreign, credit=D("5"))])

    def test_default_manager_only_sees_the_active_tenant(self):
        post_journal(tenant=self.a, entry_date=TODAY, description="A", source_type="SALE", lines=sale_lines())
        post_journal(tenant=self.b, entry_date=TODAY, description="B", source_type="SALE", lines=sale_lines())
        with tenant_context(self.a):
            self.assertEqual(list(JournalEntry.objects.values_list("description", flat=True)), ["A"])
            self.assertTrue(all(a.tenant_id == self.a.pk for a in Account.objects.all()))
        with tenant_context(self.b):
            self.assertEqual(list(JournalEntry.objects.values_list("description", flat=True)), ["B"])

    def test_seeding_one_tenant_does_not_touch_the_other(self):
        self.assertEqual(Account.all_objects.filter(tenant=self.a).count(),
                         Account.all_objects.filter(tenant=self.b).count())
        self.assertFalse(Account.all_objects.filter(tenant=self.a, pk__in=Account.all_objects.filter(
            tenant=self.b).values("pk")).exists())


class AccountantRoleAndAccessTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, self.branch = make_shop()
        mk = lambda handle, role: create_staff_user(tenant=self.tenant, handle=handle, role=role, password=PW,
                                                    branch=self.branch)
        self.accountant = mk("wanjiru", "ACCOUNTANT")
        self.cashier = mk("amina", "CASHIER")
        self.manager = mk("peter", "MANAGER")
        self.other_tenant, self.other_owner, _ = make_shop(OTHER)

    def test_accountant_role_exists_and_can_be_created(self):
        self.assertEqual(self.accountant.role, "ACCOUNTANT")
        self.assertEqual(self.accountant.get_role_display(), "Accountant")

    def test_permission_matrix(self):
        self.assertTrue(can_view_finance(self.owner))
        self.assertTrue(can_view_finance(self.accountant))
        self.assertFalse(can_view_finance(self.cashier))
        self.assertFalse(can_view_finance(self.manager))      # managers get branch-only access, not firm-wide
        self.assertTrue(can_configure_accounting(self.owner))
        self.assertFalse(can_configure_accounting(self.accountant))

    def test_accountant_lands_on_financial_overview(self):
        self.assertEqual(home_for(self.accountant), "/finance/")
        self.assertEqual(home_for(self.cashier), "/pos/terminal/")

    def test_overview_access_by_role(self):
        expected = {self.owner: 200, self.accountant: 200, self.cashier: 403}
        for user, status in expected.items():
            self.client.force_login(user)
            self.assertEqual(self.client.get("/finance/").status_code, status, user.role)

    def test_anonymous_is_sent_to_login(self):
        r = self.client.get("/finance/")
        self.assertIn(r.status_code, (302, 403))

    def test_accountant_cannot_open_pos_admin_pages(self):
        self.client.force_login(self.accountant)
        from django.urls import reverse
        self.assertEqual(self.client.get(reverse("product-list")).status_code, 403)

    def test_setup_flow_seeds_books_and_records_opening_balances(self):
        self.client.force_login(self.owner)
        r = self.client.post("/finance/setup/", {"opening_date": TODAY.isoformat(), "CASH": "1,500.50",
                                                 "INVENTORY": "4000"})
        self.assertEqual(r.status_code, 302)
        cfg = get_settings(self.tenant)
        self.assertEqual(cfg.opening_balance_date, TODAY)
        _, td, tc = trial_balance(self.tenant)
        self.assertEqual((td, tc), (D("5500.50"), D("5500.50")))
        page = self.client.get("/finance/")
        self.assertContains(page, "1,500.50")                 # the opening cash now shows as the cash balance

    def test_setup_rejects_bad_input_and_writes_nothing(self):
        self.client.force_login(self.owner)
        for bad in ({"opening_date": "nonsense"},
                    {"opening_date": (TODAY + timedelta(days=3)).isoformat()},
                    {"opening_date": TODAY.isoformat(), "CASH": "abc"},
                    {"opening_date": TODAY.isoformat(), "CASH": "-5"}):
            self.client.post("/finance/setup/", bad)
            self.assertIsNone(get_settings(self.tenant).opening_balance_date, bad)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.tenant).count(), 0)

    def test_accountant_cannot_run_setup(self):
        self.client.force_login(self.accountant)
        self.client.post("/finance/setup/", {"opening_date": TODAY.isoformat(), "CASH": "10"})
        self.assertIsNone(get_settings(self.tenant).opening_balance_date)

    def test_cashier_cannot_post_to_setup(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.post("/finance/setup/", {"opening_date": TODAY.isoformat()}).status_code, 403)

    def test_overview_never_shows_another_tenants_books(self):
        post_journal(tenant=self.other_tenant, entry_date=TODAY, description="SECRET-OTHER-SHOP",
                     source_type="SALE", lines=sale_lines("987654"))
        set_opening_balances(tenant=self.tenant, as_of=TODAY, balances={"CASH": "10"})
        self.client.force_login(self.owner)
        r = self.client.get("/finance/")
        self.assertNotContains(r, "987654")
        self.assertNotContains(r, "SECRET-OTHER-SHOP")
