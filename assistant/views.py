import json
import logging
import re

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django_ratelimit.decorators import ratelimit

from . import conf, gemini
from .prompts import build_system_prompt

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 30_000
MAX_MESSAGE_CHARS = 1000
MAX_HISTORY_ITEMS = 12
MAX_HISTORY_CHARS = 1500
_PAGE_RE = re.compile(r"^[A-Za-z0-9/_\-.]{0,120}$")


def _err(message, status):
    return JsonResponse({"error": message}, status=status)


def _quota_key(tenant):
    return f"assistant:q:{tenant.pk}:{timezone.localdate():%Y%m%d}"


def _consume_quota(tenant):
    """Per-shop daily cap so one business can't use up the shared free quota."""
    limit = conf.tenant_daily_limit()
    if limit <= 0:
        return True
    key = _quota_key(tenant)
    cache.add(key, 0, 60 * 60 * 26)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 60 * 60 * 26)
        used = 1
    return used <= limit


def _refund_quota(tenant):
    if conf.tenant_daily_limit() <= 0:
        return
    try:
        cache.decr(_quota_key(tenant))
    except ValueError:
        pass


def _parse(request):
    """-> (message, history, page) or raises ValueError(user-facing text)."""
    if len(request.body) > MAX_BODY_BYTES:
        raise ValueError("That message is too large.")
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        raise ValueError("Invalid request.")
    if not isinstance(data, dict):
        raise ValueError("Invalid request.")

    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Please type a question.")
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Please keep your question under {MAX_MESSAGE_CHARS} characters.")

    history = []
    raw = data.get("history")
    if isinstance(raw, list):
        for item in raw[-MAX_HISTORY_ITEMS:]:
            if (isinstance(item, dict) and item.get("role") in ("user", "assistant")
                    and isinstance(item.get("text"), str) and item["text"].strip()):
                history.append((item["role"], item["text"].strip()[:MAX_HISTORY_CHARS]))

    page = data.get("page")
    page = page.split("?")[0].split("#")[0] if isinstance(page, str) else ""
    if not _PAGE_RE.match(page):
        page = ""
    return message, history, page


def build_contents(history, message):
    """Gemini wants alternating turns that start with the user."""
    turns = []

    def add(role, text):
        if turns and turns[-1]["role"] == role:
            turns[-1]["parts"][0]["text"] += "\n" + text
        elif turns or role == "user":          # drop leading assistant turns
            turns.append({"role": role, "parts": [{"text": text}]})

    for role, text in history:
        add("user" if role == "user" else "model", text)
    add("user", message)
    return turns


@method_decorator(never_cache, name="dispatch")
@method_decorator(ratelimit(key="user", rate="12/m", method="POST", block=False), name="post")
@method_decorator(ratelimit(key="user", rate="150/d", method="POST", block=False), name="post")
class ChatView(View):
    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        # JSON errors instead of login redirects, so the widget can show a clear message.
        if not request.user.is_authenticated:
            return _err("Your session has expired. Please sign in again.", 401)
        if not conf.enabled():
            return _err("The assistant is switched off.", 503)
        if getattr(request, "tenant", None) is None:
            return _err("No active business selected.", 403)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        if getattr(request, "limited", False):
            return _err("You're sending messages too quickly. Please wait a moment.", 429)
        try:
            message, history, page = _parse(request)
        except ValueError as exc:
            return _err(str(exc), 400)

        tenant = request.tenant
        if not _consume_quota(tenant):
            return _err("Your business has used today's assistant allowance. It resets tomorrow.", 429)

        try:
            reply = gemini.generate(
                build_system_prompt(request.user, tenant, page),
                build_contents(history, message),
            )
        except gemini.GeminiError as exc:
            _refund_quota(tenant)
            return _err(exc.user_message, exc.http_status if exc.http_status != 200 else 422)
        except Exception:
            log.exception("Assistant failed unexpectedly")
            _refund_quota(tenant)
            return _err("Something went wrong. Please try again.", 500)

        return JsonResponse({"reply": reply})
