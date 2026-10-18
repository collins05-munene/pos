"""Help desk dashboard endpoints. Adjust the fixtures to your Tenant/User models."""
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from unittest import mock

from notifications.models import Notification
from support import conf, services
from support.models import FeatureRequest, HelpArticle, Ticket
from tenants.models import Tenant

User = get_user_model()


@mock.patch.object(conf, "SUPPORT_EMAIL", "")
class HelpdeskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(slug="acme", name="Acme Pharmacy", phone="0712345678", email="shop@acme.test")
        cls.owner = User.objects.create_user("acme__owner", password="x", role="ADMIN", tenant=cls.tenant, email="owner@acme.test")
        cls.root = User.objects.create_superuser("root", "root@x.test", "x")
        cls.agent = User.objects.create_user("agent", password="x", is_staff=True, first_name="Wanjiku")
        cls.ticket = services.create_ticket(
            tenant=cls.tenant, user=cls.owner, kind=Ticket.Kind.ISSUE, subject="Cannot print",
            description="Nothing prints", category="PRINTING", severity="CRITICAL",
            environment={"browser": "Chrome 126"}, files=[SimpleUploadedFile("a.png", b"\x89PNG0000", "image/png")],
        )

    def url(self, name, *args):
        return reverse(f"helpdesk:{name}", args=args)

    def test_access_control(self):
        for who in (None, self.owner):
            if who:
                self.client.force_login(who)
            for name in ("page", "api-summary", "api-tickets"):
                self.assertIn(self.client.get(self.url(name)).status_code, (302, 403), name)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(self.url("page")).status_code, 200)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(self.url("page")).status_code, 200)

    def test_summary_and_list(self):
        self.client.force_login(self.root)
        s = self.client.get(self.url("api-summary")).json()
        self.assertEqual((s["open"], s["unassigned"], s["urgent"]), (1, 1, 1))
        rows = self.client.get(self.url("api-tickets"), {"q": f"TKT-{self.ticket.number}"}).json()["results"]
        self.assertEqual(rows[0]["tenant"], "Acme Pharmacy")
        self.assertEqual(self.client.get(self.url("api-tickets"), {"view": "mine"}).json()["results"], [])

    def test_detail_reply_note_and_update(self):
        self.client.force_login(self.root)
        d = self.client.get(self.url("api-ticket", self.ticket.number)).json()
        self.assertEqual(d["customer"]["whatsapp"], "https://wa.me/254712345678")
        self.assertEqual(d["report"]["attachments"][0]["name"], "a.png")
        self.assertEqual(d["environment"]["browser"], "Chrome 126")

        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.url("api-ticket-reply", self.ticket.number),
                                 {"body": "Internal only", "is_internal": "on"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(mail.outbox), 0)
            r = self.client.post(self.url("api-ticket-reply", self.ticket.number),
                                 {"body": "Try a different cable", "attachments": SimpleUploadedFile("n.txt", b"hi")})
        msgs = r.json()["ticket"]["messages"]
        self.assertEqual([m["is_internal"] for m in msgs], [True, False])
        self.assertEqual(r.json()["ticket"]["status"], "IN_PROGRESS")
        self.assertEqual([m.to for m in mail.outbox], [["owner@acme.test"]])
        self.assertTrue(Notification.objects.filter(user=self.owner).exists())

        bad = self.client.post(self.url("api-ticket-update", self.ticket.number),
                               {"status": "RESOLVED", "priority": "HIGH", "assigned_to": "", "resolution_summary": ""})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("resolution_summary", bad.json()["errors"])
        ok = self.client.post(self.url("api-ticket-update", self.ticket.number),
                              {"status": "RESOLVED", "priority": "HIGH", "assigned_to": self.root.pk,
                               "resolution_summary": "Cable replaced"})
        t = ok.json()["ticket"]
        self.assertEqual((t["status"], t["priority"], t["assigned_to"]), ("RESOLVED", "HIGH", self.root.pk))
        again = self.client.post(self.url("api-ticket-assign", self.ticket.number))
        self.assertEqual(again.status_code, 200)

    def test_article_lifecycle(self):
        self.client.force_login(self.agent)
        r = self.client.post(self.url("api-article-save"), {"title": "Receipt won't print", "topic": "RECEIPTS", "kind": "TROUBLESHOOTING",
                             "summary": "Fix a silent printer", "body": "<ol><li>Check paper</li></ol>"})
        self.assertEqual(r.status_code, 200, r.content)
        a = r.json()["article"]
        self.assertEqual(a["slug"], "receipt-wont-print")
        self.assertFalse(a["is_published"])

        dup = self.client.post(self.url("api-article-save"), {"title": "Receipt won't print", "topic": "RECEIPTS", "kind": "FAQ",
                               "summary": "x", "body": "y"}).json()["article"]
        self.assertEqual(dup["slug"], "receipt-wont-print-2")

        v = self.client.post(self.url("api-article-save"), {"title": "Open the register", "topic": "SALES", "kind": "VIDEO", "summary": "x"})
        self.assertIn("video_url", v.json()["errors"])
        v = self.client.post(self.url("api-article-save"), {"title": "Open the register", "topic": "SALES", "kind": "VIDEO",
                             "summary": "x", "video_url": "https://example.com/v.mp4"})
        self.assertEqual(v.status_code, 400)
        v = self.client.post(self.url("api-article-save"), {"title": "Open the register", "topic": "SALES", "kind": "VIDEO",
                             "summary": "x", "video_url": "https://youtu.be/dQw4w9WgXcQ", "is_published": "on"})
        self.assertEqual(v.status_code, 200, v.content)
        self.assertTrue(v.json()["article"]["embed_url"].startswith("https://www.youtube-nocookie.com/embed/"))

        empty = self.client.post(self.url("api-article-publish", a["id"]), {"published": "1"})
        self.assertEqual(empty.status_code, 200)  # has body
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("support:help-article", args=[a["slug"]])).status_code, 200)
        self.client.force_login(self.root)
        self.client.post(self.url("api-article-publish", a["id"]), {"published": "0"})
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("support:help-article", args=[a["slug"]])).status_code, 404)
        self.client.force_login(self.root)
        self.assertEqual(self.client.post(self.url("api-article-delete", a["id"])).status_code, 200)
        self.assertFalse(HelpArticle.objects.filter(pk=a["id"]).exists())
        self.assertEqual(len(self.client.get(self.url("api-articles"), {"status": "draft"}).json()["results"]), 1 + 0 * 1)

    def test_feature_release_requires_notes_and_emails_voters(self):
        fr = services.create_feature_request(tenant=self.tenant, user=self.owner, title="Expiry alerts",
                                             description="Warn me", module="INVENTORY")
        self.client.force_login(self.root)
        row = self.client.get(self.url("api-features")).json()["results"][0]
        self.assertEqual((row["votes"], row["shop"]), (1, "Acme Pharmacy"))
        r = self.client.post(self.url("api-feature-update", fr.pk), {"status": "RELEASED", "is_visible": "on"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("release_notes", r.json()["errors"])
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.url("api-feature-update", fr.pk),
                                 {"status": "RELEASED", "release_notes": "Live under Inventory.", "is_visible": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([m.to for m in mail.outbox], [["owner@acme.test"]])
        fr.refresh_from_db()
        self.assertEqual(fr.ticket.status, Ticket.Status.RESOLVED)
        self.assertEqual(self.client.post(self.url("api-feature-update", fr.pk), {"status": "NOPE"}).status_code, 400)
        hidden = self.client.post(self.url("api-feature-update", fr.pk), {"status": "RELEASED", "release_notes": "x"})
        self.assertFalse(hidden.json()["feature"]["is_visible"])  # unticked box hides it
