import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from notifications.models import Notification
from tenants.services import compose_username, create_staff_user, register_business
from users.models import ActivityLog, PasswordResetRequest

User = get_user_model()
OWNER = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
             email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=3)
PW = "Pw-12345-xx"


def make_shop(**over):
    return register_business(**{**OWNER, **over})


@override_settings(TENANCY_ALLOW_NULL_TENANT=False)
class UnifiedLoginTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, _ = make_shop()
        self.cashier = create_staff_user(tenant=self.tenant, handle="njeri", role="CASHIER", password=PW,
                                         branch=self.owner.branch)
        self.manager = create_staff_user(tenant=self.tenant, handle="mike", role="MANAGER", password=PW)

    def login(self, username, password, shop_code="", **extra):
        return self.client.post("/users/login/", {"username": username, "password": password,
                                                  "shop_code": shop_code, **extra})

    def test_owner_signs_in_with_email_and_password(self):
        r = self.login("Njeri@Example.com", OWNER["password"])
        self.assertRedirects(r, "/users/admin/dashboard/", fetch_redirect_response=False)

    def test_staff_sign_in_with_shop_code_username_password(self):
        r = self.login("njeri", PW, self.tenant.slug)
        self.assertRedirects(r, "/pos/terminal/", fetch_redirect_response=False)    # cashier -> till
        self.client.logout()
        r = self.login("Mike", PW, self.tenant.slug.upper())
        self.assertRedirects(r, "/users/admin/dashboard/", fetch_redirect_response=False)

    def test_full_composed_username_works_without_shop_code(self):
        r = self.login(compose_username(self.tenant.slug, "njeri"), PW)
        self.assertEqual(r.status_code, 302)

    def test_wrong_password_or_missing_shop_code_fails(self):
        self.assertContains(self.login("njeri", "nope-nope-1", self.tenant.slug), "Wrong username or password")
        self.assertContains(self.login("njeri", PW), "Wrong username or password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_same_handle_in_two_shops_never_mixes(self):
        t2, _, _ = make_shop(business_name="Other", email="o@example.com")
        create_staff_user(tenant=t2, handle="njeri", role="CASHIER", password="Other-pw-99")
        self.assertContains(self.login("njeri", "Other-pw-99", self.tenant.slug), "Wrong username")
        self.login("njeri", "Other-pw-99", t2.slug)
        self.assertEqual(User.objects.get(pk=self.client.session["_auth_user_id"]).tenant, t2)

    def test_next_is_followed_only_when_local(self):
        r = self.login("njeri", PW, self.tenant.slug, next="/billing/")
        self.assertRedirects(r, "/billing/", fetch_redirect_response=False)
        self.client.logout()
        r = self.login("njeri", PW, self.tenant.slug, next="https://evil.example.org/")
        self.assertRedirects(r, "/pos/terminal/", fetch_redirect_response=False)

    def test_platform_admin_lands_on_platform(self):
        User.objects.create_superuser("root", "r@x.com", "Root-pw-12345")
        self.assertRedirects(self.login("root", "Root-pw-12345"), "/platform/", fetch_redirect_response=False)

    def test_cashier_pin_login_is_gone(self):
        self.assertFalse(hasattr(User, "pin"))
        r = self.client.get("/users/pos-login/")                 # old bookmark / installed PWA
        self.assertRedirects(r, "/users/login/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/"), "/users/login/", fetch_redirect_response=False)

    def test_repeated_failures_alert_the_shop_admins_and_are_logged(self):
        for _ in range(5):
            self.login("njeri", "wrong-pass-1", self.tenant.slug)
        self.assertTrue(Notification.objects.filter(tenant=self.tenant, title="Repeated failed sign-ins").exists())
        self.assertTrue(ActivityLog.all_objects.filter(tenant=self.tenant, action="LOGIN_FAILED").exists())

    def test_staff_need_a_valid_password(self):
        from django.core.exceptions import ValidationError
        for bad in ("1234", "12345678", ""):
            with self.assertRaises(ValidationError):
                create_staff_user(tenant=self.tenant, handle="x" + str(len(bad)), role="CASHIER", password=bad)

    def test_logout_returns_to_login(self):
        self.login("njeri", PW, self.tenant.slug)
        self.assertRedirects(self.client.get("/users/logout/"), "/users/login/", fetch_redirect_response=False)


@override_settings(TENANCY_ALLOW_NULL_TENANT=False, SITE_URL="https://shop.example.com")
class PasswordResetApprovalTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, _ = make_shop()
        self.cashier = create_staff_user(tenant=self.tenant, handle="njeri", role="CASHIER", password=PW,
                                         branch=self.owner.branch)           # no email of her own
        self.su = User.objects.create_superuser("root", "root@example.com", "Root-pw-12345")
        self.platform = self.client_class()
        self.platform.force_login(self.su)
        mail.outbox.clear()

    def ask(self, username, shop_code=""):
        return self.client.post("/users/password-reset/", {"username": username, "shop_code": shop_code})

    def pending(self):
        return PasswordResetRequest.objects.get(status="PENDING")

    def decide(self, req, action, **data):
        return self.platform.post(f"/platform/resets/api/{req.pk}/{action}/", data)

    def link(self, n=-1):
        return re.search(r"https://shop\.example\.com(/users/password-reset/\S+/)", mail.outbox[n].body).group(1)

    def test_request_waits_for_approval_and_notifies_owner_and_platform(self):
        self.assertRedirects(self.ask(self.owner.username), "/users/password-reset/sent/", fetch_redirect_response=False)
        req = self.pending()
        self.assertEqual(req.user, self.owner)
        self.assertIsNone(req.token_hash)                                    # no link exists yet
        self.assertTrue(Notification.objects.filter(tenant=self.tenant, user=self.owner,
                                                    title="Password reset requested").exists())
        recipients = [m.to[0] for m in mail.outbox]
        self.assertIn("root@example.com", recipients)                        # platform alerted
        self.assertIn("njeri@example.com", recipients)                       # owner emailed
        self.assertFalse(any("password-reset/" in m.body and "approved" in m.subject.lower() for m in mail.outbox))

    def test_unknown_account_gets_the_same_answer_and_nothing_is_created(self):
        r = self.ask("nobody", "no-such-shop")
        self.assertRedirects(r, "/users/password-reset/sent/", fetch_redirect_response=False)
        self.assertEqual(PasswordResetRequest.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_superusers_and_duplicates_are_ignored(self):
        self.ask("root")
        self.assertEqual(PasswordResetRequest.objects.count(), 0)
        self.ask(self.owner.username); self.ask(self.owner.username)
        self.assertEqual(PasswordResetRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(title="Password reset requested", user=self.owner).count(), 1)

    def test_only_platform_admins_can_decide(self):
        self.ask(self.owner.username)
        self.client.force_login(self.owner)
        r = self.client.post(f"/platform/resets/api/{self.pending().pk}/approve/")
        self.assertIn(r.status_code, (302, 403))          # billing gate or permission check, never 200
        self.assertIn(self.client.get("/platform/resets/api/").status_code, (302, 403))
        self.assertEqual(self.pending().status, "PENDING")
        self.assertEqual(len(mail.outbox), 0)

    def test_full_flow_approve_link_new_password_works_once(self):
        self.ask(self.owner.username)
        self.client.force_login(self.owner)                                  # an old session of the owner
        mail.outbox.clear()
        listing = self.platform.get("/platform/resets/api/").json()
        self.assertEqual(listing["summary"]["pending_count"], 1)
        self.assertEqual(listing["pending"][0]["email_to"], "n****@example.com")

        r = self.decide(self.pending(), "approve")
        self.assertEqual(r.json()["status"], "APPROVED")
        self.assertEqual(mail.outbox[0].to, ["njeri@example.com"])
        self.assertTrue(Notification.objects.filter(tenant=self.tenant, title="Password reset approved").exists())

        url = self.link()
        self.assertContains(self.client_class().get(url), "Choose a new password")
        fresh = self.client_class()
        weak = fresh.post(url, {"password1": "12345678", "password2": "12345678"})
        self.assertEqual(weak.status_code, 400)
        mismatch = fresh.post(url, {"password1": "Brand-new-77", "password2": "Brand-new-78"})
        self.assertEqual(mismatch.status_code, 400)
        ok = fresh.post(url, {"password1": "Brand-new-77", "password2": "Brand-new-77"})
        self.assertRedirects(ok, "/users/login/?reset=1", fetch_redirect_response=False)

        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("Brand-new-77"))
        self.assertFalse(self.owner.check_password(OWNER["password"]))
        self.assertEqual(self.client.get("/billing/").status_code, 302)      # old session was signed out
        self.assertEqual(fresh.get(url).status_code, 404)                    # link is spent
        self.assertTrue(Notification.objects.filter(tenant=self.tenant, title="Password was changed").exists())
        self.assertEqual(PasswordResetRequest.objects.get().status, "COMPLETED")
        self.assertTrue(ActivityLog.all_objects.filter(tenant=self.tenant, action="PASSWORD_RESET_COMPLETED").exists())

    def test_staff_without_email_sends_link_to_owner(self):
        self.ask("njeri", self.tenant.slug)
        self.decide(self.pending(), "approve")
        self.assertEqual(mail.outbox[-1].to, ["njeri@example.com"])
        self.assertIn("pass it on", mail.outbox[-1].body)

    def test_no_email_anywhere_cannot_be_approved(self):
        self.ask("njeri", self.tenant.slug)
        User.objects.filter(pk=self.owner.pk).update(email="")
        self.tenant.email = ""; self.tenant.save()
        r = self.decide(self.pending(), "approve")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PasswordResetRequest.objects.get().status, "PENDING")

    def test_email_failure_leaves_request_pending(self):
        self.ask(self.owner.username)
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            r = self.decide(self.pending(), "approve")
        self.assertEqual(r.status_code, 502)
        req = PasswordResetRequest.objects.get()
        self.assertEqual((req.status, req.token_hash), ("PENDING", None))

    def test_reject_notifies_and_blocks_a_second_decision(self):
        self.ask(self.owner.username)
        req = self.pending()
        self.assertEqual(self.decide(req, "reject", note="not verified").json()["status"], "REJECTED")
        self.assertTrue(Notification.objects.filter(tenant=self.tenant, title="Password reset declined").exists())
        self.assertEqual(self.decide(req, "approve").status_code, 409)
        self.ask(self.owner.username)                                        # may ask again afterwards
        self.assertEqual(PasswordResetRequest.objects.filter(status="PENDING").count(), 1)

    def test_expired_and_bogus_links(self):
        self.ask(self.owner.username)
        self.decide(self.pending(), "approve")
        url = self.link()
        PasswordResetRequest.objects.update(link_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(url).status_code, 410)
        self.assertEqual(self.client.get("/users/password-reset/bogus-token/").status_code, 404)

    def test_other_shops_are_not_told(self):
        t2, o2, _ = make_shop(business_name="Other", email="o@example.com")
        self.ask(self.owner.username)
        self.assertFalse(Notification.objects.filter(tenant=t2).exists())

    def test_approval_works_while_platform_admin_is_in_another_shops_support_session(self):
        t2, _, _ = make_shop(business_name="Other", email="o@example.com")
        self.ask(self.owner.username)
        self.platform.post(f"/platform/tenants/{t2.pk}/support-start/")
        self.assertEqual(self.decide(self.pending(), "approve").status_code, 200)
        self.assertTrue(ActivityLog.all_objects.filter(tenant=self.tenant, action="PASSWORD_RESET_APPROVED").exists())

    def test_dashboard_page_renders(self):
        self.ask(self.owner.username)
        page = self.platform.get("/platform/resets/")
        self.assertContains(page, "Password resets")
        self.assertContains(self.platform.get("/platform/", HTTP_ACCEPT="text/html"), "Password resets (1)")
