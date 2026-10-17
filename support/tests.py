"""
Core-flow tests. The fixtures in setUpTestData assume simple Tenant/User models;
adjust those few lines to match yours (extra required fields, role names, etc.).
"""
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest import mock

from notifications.models import Notification
from tenants.models import Tenant

from . import conf, services
from .forms import validate_attachments
from .models import FeatureRequest, HelpArticle, Ticket

User = get_user_model()
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


@mock.patch.object(conf, "SUPPORT_EMAIL", "team@support.test")  # conf reads settings at import time
@override_settings(DEFAULT_FROM_EMAIL="noreply@pos.test")
class SupportFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme Pharmacy")
        cls.other_tenant = Tenant.objects.create(slug="other", name="Other Shop")
        cls.admin = User.objects.create_user("acme__admin", password="x", role="ADMIN", tenant=cls.tenant, email="admin@acme.test")
        cls.cashier = User.objects.create_user("acme__cash", password="x", role="CASHIER", tenant=cls.tenant)
        cls.other_admin = User.objects.create_user("other__admin", password="x", role="ADMIN", tenant=cls.other_tenant, email="o@other.test")
        cls.agent = User.objects.create_user("agent", password="x", is_staff=True, email="agent@support.test", first_name="Wanjiku")

    def make_ticket(self, user=None, **kw):
        return services.create_ticket(
            tenant=self.tenant, user=user or self.admin, kind=Ticket.Kind.ISSUE,
            subject=kw.pop("subject", "Receipt prints blank"), description="It prints nothing.",
            category="PRINTING", severity=kw.pop("severity", "HIGH"), **kw,
        )

    # ---- reporting
    def test_report_issue_captures_environment_and_numbers_ticket(self):
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(
                reverse("support:report-issue"),
                {
                    "category": "PRINTING", "severity": "CRITICAL", "subject": "Cannot print",
                    "description": "Nothing prints", "steps_to_reproduce": "1. Sell\n2. Print",
                    "client_env": '{"screen": "1920x1080", "evil": "x", "page": "/pos/"}',
                    "attachments": SimpleUploadedFile("shot.png", PNG, "image/png"),
                },
                HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            )
        t = Ticket.objects.get()
        self.assertRedirects(r, t.get_absolute_url())
        self.assertEqual(t.reference, f"TKT-{1000 + t.pk}")
        self.assertEqual(t.priority, "CRITICAL")
        self.assertEqual(t.environment["browser"], "Chrome 126")
        self.assertEqual(t.environment["os"], "Windows 10")
        self.assertEqual(t.environment["screen"], "1920x1080")
        self.assertNotIn("evil", t.environment)
        self.assertEqual(t.attachments.count(), 1)
        self.assertIn("[CRITICAL]", mail.outbox[0].subject)  # team alerted

    def test_attachment_validation(self):
        bad = SimpleUploadedFile("run.exe", b"MZ", "application/octet-stream")
        with self.assertRaises(Exception):
            validate_attachments([bad])

    # ---- isolation
    def test_other_business_cannot_open_ticket_or_attachment(self):
        t = self.make_ticket(files=[SimpleUploadedFile("a.png", PNG, "image/png")])
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(t.get_absolute_url()).status_code, 404)
        att = t.attachments.get()
        self.assertEqual(self.client.get(reverse("support:attachment", args=[att.pk])).status_code, 404)

    def test_cashier_only_sees_own_tickets(self):
        t = self.make_ticket(user=self.admin)
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(t.get_absolute_url()).status_code, 404)
        mine = self.make_ticket(user=self.cashier, subject="Mine")
        self.assertEqual(self.client.get(mine.get_absolute_url()).status_code, 200)

    # ---- conversation
    def test_staff_reply_internal_notes_and_status_flow(self):
        t = self.make_ticket()
        with self.captureOnCommitCallbacks(execute=True):
            services.add_message(t, author=self.agent, body="Secret: customer is on old version", internal=True, from_staff=True)
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.Status.OPEN)
        self.assertIsNone(t.first_response_at)
        self.assertFalse([m for m in mail.outbox if "admin@acme.test" in m.to])

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.add_message(t, author=self.agent, body="Please update your browser", from_staff=True)
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.Status.IN_PROGRESS)
        self.assertIsNotNone(t.first_response_at)
        to_customer = [m for m in mail.outbox if m.to == ["admin@acme.test"]]
        self.assertEqual(len(to_customer), 1)
        self.assertNotIn("Secret", to_customer[0].body)
        self.assertTrue(Notification.objects.filter(user=self.admin, tenant=self.tenant).exists())

        self.client.force_login(self.admin)
        page = self.client.get(t.get_absolute_url()).content.decode()
        self.assertIn("Please update your browser", page)
        self.assertNotIn("Secret", page)

    def test_customer_reply_reopens_resolved_ticket(self):
        t = self.make_ticket()
        services.update_ticket(t, actor=self.agent, status=Ticket.Status.RESOLVED, resolution_summary="Fixed")
        services.add_message(t, author=self.admin, body="Still broken")
        t.refresh_from_db()
        self.assertEqual(t.status, Ticket.Status.OPEN)
        self.assertIsNone(t.resolved_at)

    def test_closed_ticket_rejects_customer_reply(self):
        t = self.make_ticket()
        services.update_ticket(t, actor=self.agent, status=Ticket.Status.CLOSED, resolution_summary="Done")
        with self.assertRaises(ValueError):
            services.add_message(t, author=self.admin, body="hello?")

    def test_staff_queue_requires_staff(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("support:staff-queue")).status_code, 403)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("support:staff-queue")).status_code, 200)

    # ---- features
    def test_vote_toggle_and_release_notifies_voters(self):
        fr = services.create_feature_request(
            tenant=self.tenant, user=self.admin, title="Expiry alerts",
            description="Warn me before batches expire", module="INVENTORY",
        )
        self.assertEqual(fr.vote_count, 1)
        self.assertEqual(fr.ticket.kind, Ticket.Kind.FEATURE)

        voted, fr = services.toggle_vote(fr.pk, self.other_admin)
        self.assertTrue(voted)
        self.assertEqual(fr.vote_count, 2)
        voted, fr = services.toggle_vote(fr.pk, self.other_admin)
        self.assertFalse(voted)
        services.toggle_vote(fr.pk, self.other_admin)

        mail.outbox.clear()
        fr.status = FeatureRequest.Status.RELEASED
        fr.release_notes = "Expiry alerts are live under Inventory."
        fr.save()
        with self.captureOnCommitCallbacks(execute=True):
            services.feature_status_changed(fr)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["admin@acme.test", "o@other.test"])
        fr.refresh_from_db()
        fr.ticket.refresh_from_db()
        self.assertEqual(fr.ticket.status, Ticket.Status.RESOLVED)

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.feature_status_changed(fr)  # second call must not re-send
        self.assertEqual(len(mail.outbox), 0)
        with self.assertRaises(ValueError):
            services.toggle_vote(fr.pk, self.cashier)

    def test_cashier_cannot_use_feature_board(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("support:feature-list")).status_code, 403)

    # ---- help center
    def test_suggestions_only_return_published_articles(self):
        HelpArticle.objects.create(title="Receipt won't print", slug="print", topic="RECEIPTS", kind="TROUBLESHOOTING",
                                   summary="Fix a silent printer", body="<p>Check paper</p>", is_published=True)
        HelpArticle.objects.create(title="Receipt draft", slug="draft", topic="RECEIPTS", summary="x", is_published=False)
        self.client.force_login(self.admin)
        data = self.client.get(reverse("support:suggest"), {"q": "my receipt does not print"}).json()
        self.assertEqual([i["title"] for i in data["items"]], ["Receipt won't print"])
        r = self.client.get(reverse("support:help-center"), {"q": "receipt"})
        self.assertContains(r, "Receipt won&#x27;t print")
        self.assertNotContains(r, "Receipt draft")

    # ---- every page renders
    def test_all_pages_render(self):
        HelpArticle.objects.create(title="Open the register", slug="open-reg", topic="SALES", kind="VIDEO",
                                   summary="Start the day", body="<ol><li>Go</li></ol>", is_published=True,
                                   is_featured=True, video_url="https://youtu.be/dQw4w9WgXcQ")
        t = self.make_ticket(files=[SimpleUploadedFile("a.png", PNG, "image/png")])
        services.add_message(t, author=self.agent, body="On it", from_staff=True,
                             files=[SimpleUploadedFile("n.txt", b"hi", "text/plain")])
        services.add_message(t, author=self.agent, body="Note", internal=True, from_staff=True)
        fr = services.create_feature_request(tenant=self.tenant, user=self.admin, title="X", description="Y", module="SALES")

        self.client.force_login(self.admin)
        for name, args in [
            ("home", []), ("report-issue", []), ("contact", []), ("ticket-list", []),
            ("ticket-detail", [t.number]), ("feature-list", []), ("feature-new", []),
            ("help-center", []), ("help-article", ["open-reg"]),
        ]:
            r = self.client.get(reverse(f"support:{name}", args=args))
            self.assertEqual(r.status_code, 200, name)
        page = self.client.get(t.get_absolute_url()).content.decode()
        self.assertNotIn("Note", page.replace("Notification", ""))  # internal note hidden
        att = t.attachments.filter(message__isnull=True).get()
        self.assertEqual(self.client.get(reverse("support:attachment", args=[att.pk])).status_code, 200)

        self.client.force_login(self.agent)
        for name, args in [("staff-queue", []), ("staff-ticket", [t.number])]:
            self.assertEqual(self.client.get(reverse(f"support:{name}", args=args)).status_code, 200, name)
        self.assertContains(self.client.get(reverse("support:staff-queue"), {"q": f"TKT-{t.number}"}), t.reference)
        r = self.client.post(t.get_staff_url(), {"action": "manage", "status": "RESOLVED", "priority": "HIGH",
                                                  "assigned_to": self.agent.pk, "resolution_summary": ""})
        self.assertContains(r, "Add a short summary")  # validation re-render
        r = self.client.post(t.get_staff_url(), {"action": "manage", "status": "RESOLVED", "priority": "HIGH",
                                                  "assigned_to": self.agent.pk, "resolution_summary": "Replaced paper roll"})
        self.assertEqual(r.status_code, 302)
        t.refresh_from_db()
        self.assertEqual((t.status, t.assigned_to), (Ticket.Status.RESOLVED, self.agent))

        self.client.force_login(self.admin)
        r = self.client.post(reverse("support:feature-vote", args=[fr.pk]), {"next": "https://evil.test/"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(r["Location"].startswith("https://evil"))
        r = self.client.post(t.get_absolute_url(), {"body": "Thanks, works now"})
        self.assertEqual(r.status_code, 302)
