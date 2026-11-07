"""
Django email backend that delivers through the Resend HTTP API instead of SMTP.

Why a backend (and not a helper function)?  Every place in UniSale that sends mail already goes
through Django's mail API -- EmailMultiAlternatives(...).send(), send_mail(), send_mass_mail():

    tenants/services.py        sign-up verification link
    users/password_reset.py    reset link + approval notices
    support/services.py        ticket / feature-release notices

Plugging Resend in at the backend level switches ALL of them over at once, keeps their templates and
wording untouched, and keeps Django's test runner working (it swaps in the in-memory backend, so
`mail.outbox` assertions keep passing and no test ever calls Resend).

Settings used (see config/settings.py):
    RESEND_API_KEY       secret key from the Resend dashboard (never commit it)
    DEFAULT_FROM_EMAIL   e.g. "UniSale Systems <no-reply@mail.yourdomain.co.ke>" -- the domain
                         MUST be verified in Resend or every send is rejected
    RESEND_TIMEOUT       seconds to wait for Resend per request (default 10)
"""
import logging
import time
import uuid

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger(__name__)

# Resend allows ~2 requests/second per team by default; one short retry smooths over bursts
# (e.g. a feature-release announcement) without holding a web request open for long.
_MAX_ATTEMPTS = 2
_RETRY_DELAY = 0.6


class EmailDeliveryError(Exception):
    """Resend refused the message, could not be reached, or is not configured."""


def _addresses(values):
    return [a for a in (values or []) if a]


class ResendEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = getattr(settings, "RESEND_API_KEY", "") or ""
        self.timeout = int(getattr(settings, "RESEND_TIMEOUT", 10))

    # ------------------------------------------------------------------ public API
    def send_messages(self, email_messages):
        """Send each message; returns how many were accepted by Resend."""
        if not email_messages:
            return 0
        if not self.api_key:
            log.error("RESEND_API_KEY is not set; %d email(s) were NOT sent.", len(email_messages))
            if self.fail_silently:
                return 0
            raise EmailDeliveryError("RESEND_API_KEY is not configured.")

        import resend  # imported lazily so the app still boots if the SDK is missing in dev

        resend.api_key = self.api_key
        resend.default_http_client = resend.RequestsClient(timeout=self.timeout)

        sent = 0
        for message in email_messages:
            try:
                self._send_one(resend, message)
                sent += 1
            except EmailDeliveryError:
                if not self.fail_silently:
                    raise
        return sent

    # ------------------------------------------------------------------ internals
    def _send_one(self, resend, message):
        recipients = _addresses(message.to) + _addresses(message.cc) + _addresses(message.bcc)
        if not recipients:
            return
        params = self._build_params(message)
        # One key per message: if Resend processed a request but the response was lost and we retry,
        # the recipient still gets exactly one email.
        options = {"idempotency_key": str(uuid.uuid4())}

        last_exc = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                result = resend.Emails.send(params, options)
                log.info("Resend accepted email id=%s subject=%r to=%d recipient(s)",
                         (result or {}).get("id"), message.subject, len(params["to"]))
                return
            except Exception as exc:  # SDK raises ResendError subclasses; network errors surface as ResendError too
                last_exc = exc
                if attempt < _MAX_ATTEMPTS and self._is_retryable(exc):
                    log.warning("Resend send attempt %d failed (%s); retrying.", attempt, self._describe(exc))
                    time.sleep(_RETRY_DELAY)
                    continue
                break

        log.error("Resend rejected email subject=%r: %s", message.subject, self._describe(last_exc))
        raise EmailDeliveryError(self._describe(last_exc)) from last_exc

    @staticmethod
    def _build_params(message):
        subject = " ".join((message.subject or "").split())  # never allow newlines in a header
        params = {
            "from": message.from_email or settings.DEFAULT_FROM_EMAIL,
            "to": _addresses(message.to),
            "subject": subject,
        }
        if message.cc:
            params["cc"] = _addresses(message.cc)
        if message.bcc:
            params["bcc"] = _addresses(message.bcc)
        if message.reply_to:
            params["reply_to"] = _addresses(message.reply_to)

        # Plain text + HTML, exactly as Django composed them.
        html = None
        for content, mimetype in getattr(message, "alternatives", []) or []:
            if mimetype == "text/html":
                html = content
                break
        if getattr(message, "content_subtype", "plain") == "html":
            html, text = message.body, None
        else:
            text = message.body
        if html:
            params["html"] = html
        if text:
            params["text"] = text

        attachments = []
        for att in message.attachments or []:
            if isinstance(att, tuple) and len(att) == 3:
                filename, content, mimetype = att
                if isinstance(content, str):
                    content = content.encode("utf-8")
                attachments.append({"filename": filename or "attachment",
                                    "content": list(content), "content_type": mimetype})
            else:
                log.warning("Skipping unsupported attachment type %s on email %r", type(att).__name__, subject)
        if attachments:
            params["attachments"] = attachments
        return params

    @staticmethod
    def _is_retryable(exc):
        """Rate limits, 5xx and transport failures are worth one more try; bad requests/keys are not."""
        code = str(getattr(exc, "code", "") or "")
        if code == "429" or code.startswith("5"):
            return True
        # SDK reports transport problems (timeouts, DNS) as error_type "HttpClientError"
        return getattr(exc, "error_type", "") in ("HttpClientError", "rate_limit_exceeded")

    @staticmethod
    def _describe(exc):
        if exc is None:
            return "unknown error"
        code = getattr(exc, "code", None)
        etype = getattr(exc, "error_type", None)
        msg = getattr(exc, "message", None) or str(exc)
        bits = [str(b) for b in (code, etype) if b]
        return f"[{' '.join(bits)}] {msg}" if bits else msg