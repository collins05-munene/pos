import json
from unittest import mock

from django.test import TestCase, override_settings

from tenants.services import register_business
from users import turnstile

KEYS = dict(TURNSTILE_SITE_KEY="1x00000000000000000000AA", TURNSTILE_SECRET_KEY="1x0000000000000000000000000000000AA")
OWNER = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
             email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=3)
SIGNUP = dict(business_name="Kamau Pharmacy", business_type="PHARMACY", owner_name="Kamau K",
              email="kamau@example.com", phone="0712345678", password="S3cure-pass-99", term_months=1)


class VerifyTests(TestCase):
    def test_off_without_keys(self):
        self.assertFalse(turnstile.enabled())
        self.assertTrue(turnstile.verify(""))

    @override_settings(**KEYS)
    def test_missing_token_fails_without_calling_cloudflare(self):
        with mock.patch("users.turnstile.urllib.request.urlopen") as op:
            self.assertFalse(turnstile.verify(""))
        op.assert_not_called()

    @override_settings(**KEYS)
    def test_cloudflare_unreachable_fails_closed(self):
        with mock.patch("users.turnstile.urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(turnstile.verify("tok"))

    @override_settings(**KEYS)
    def test_accepts_and_rejects_by_cloudflare_answer(self):
        for success, expected in ((True, True), (False, False)):
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = json.dumps({"success": success}).encode()
            with mock.patch("users.turnstile.urllib.request.urlopen", return_value=resp):
                self.assertEqual(turnstile.verify("tok", "1.2.3.4"), expected)


@override_settings(**KEYS)
class PageTests(TestCase):
    def setUp(self):
        register_business(**OWNER)

    def test_widget_rendered_on_both_pages(self):
        for url in ("/users/login/", "/signup/"):
            self.assertContains(self.client.get(url), KEYS["TURNSTILE_SITE_KEY"])

    def test_login_without_token_is_refused(self):
        r = self.client.post("/users/login/", {"username": OWNER["email"], "password": OWNER["password"]})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, turnstile.ERROR_MESSAGE)

    def test_login_with_valid_token_works(self):
        with mock.patch("users.views.turnstile.verify", return_value=True):
            r = self.client.post("/users/login/", {"username": OWNER["email"], "password": OWNER["password"],
                                                   turnstile.FIELD: "tok"})
        self.assertEqual(r.status_code, 302)

    def test_signup_without_token_is_refused(self):
        r = self.client.post("/signup/", data=json.dumps(SIGNUP), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn(turnstile.ERROR_MESSAGE, r.json()["errors"]["__all__"])

    def test_signup_with_valid_token_goes_through(self):
        with mock.patch("tenants.views.turnstile.verify", return_value=True):
            r = self.client.post("/signup/", data=json.dumps({**SIGNUP, "turnstile_token": "tok"}),
                                 content_type="application/json")
        self.assertEqual(r.status_code, 202)
