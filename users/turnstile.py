"""
Cloudflare Turnstile (bot check) for the sign-in and sign-up pages.

Switched on by setting BOTH keys (from the Cloudflare dashboard -> Turnstile):

    TURNSTILE_SITE_KEY    public, rendered into the page
    TURNSTILE_SECRET_KEY  private, used only here to verify the token with Cloudflare

With either key missing the check is off: no widget is shown and verify() always passes, so local
development and the existing test-suite work without any Cloudflare account.

For local testing Cloudflare publishes dummy keys:
    site  1x00000000000000000000AA   /   secret  1x0000000000000000000000000000000AA   (always passes)
    site  2x00000000000000000000AB   /   secret  2x0000000000000000000000000000000AA   (always fails)
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
FIELD = "cf-turnstile-response"      # name of the hidden input the widget adds to the form
TIMEOUT_SECONDS = 5
ERROR_MESSAGE = "Please complete the security check and try again."


def site_key():
    return getattr(settings, "TURNSTILE_SITE_KEY", "") or ""


def enabled():
    return bool(site_key() and getattr(settings, "TURNSTILE_SECRET_KEY", ""))


def context_processor(request):
    """Gives templates `turnstile_site_key`, which is empty (so the widget is skipped) when disabled."""
    return {"turnstile_site_key": site_key() if enabled() else ""}


def verify(token, ip=None):
    """
    True if Cloudflare accepts `token` (or the check is switched off).

    Fails CLOSED: a missing token, a rejected token, or Cloudflare being unreachable all return False.
    A token is single-use, so a failed attempt needs a fresh widget (the pages reset/reload it).
    """
    if not enabled():
        return True
    token = (token or "").strip()
    if not token or len(token) > 2048:
        return False

    fields = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token}
    if ip:
        fields["remoteip"] = ip
    req = urllib.request.Request(VERIFY_URL, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            result = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        logger.exception("Turnstile verification request failed")
        return False

    if not result.get("success"):
        logger.info("Turnstile rejected a token: %s", result.get("error-codes"))
        return False
    return True
