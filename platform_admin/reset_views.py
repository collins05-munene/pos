"""
Platform admin -> Password resets.

Anyone who forgets their password files a request from the login page. It lands here, and nothing
is sent until a platform admin approves it (the shop's owner/admins are notified at every step).
Same pattern as the Payments page: one HTML shell plus JSON endpoints.
"""
import json
from datetime import timedelta

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from notifications.utils import who
from users import password_reset
from users.models import PasswordResetRequest
from .views import SuperuserRequiredMixin

Status = PasswordResetRequest.Status
RECENT_DAYS = 30


def pending_reset_count():
    return PasswordResetRequest.objects.filter(status=Status.PENDING).count()


def _row(r):
    to = password_reset._email_for(r.user)            # where an approval would send the link
    return {
        "id": r.pk, "status": r.status,
        "person": who(r.user), "username": password_reset.staff_label(r.user),
        "role": "Owner" if r.user.is_owner else r.user.get_role_display(),
        "phone": r.user.phone,
        "shop": r.tenant.name, "slug": r.tenant.slug, "shop_phone": r.tenant.phone,
        "requested_at": r.requested_at, "requested_ip": r.requested_ip,
        "email_to": password_reset.mask_email(to),
        "email_is_owners": bool(to) and to != r.user.email,
        "has_email": bool(to),
        "decided_by": r.decided_by.username if r.decided_by else None,
        "decided_at": r.decided_at, "decision_note": r.decision_note,
        "link_sent_to": password_reset.mask_email(r.link_sent_to),
        "link_expires_at": r.link_expires_at, "completed_at": r.completed_at,
    }


def _payload(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or b"{}")
        except ValueError:
            return {}
    return request.POST


class ResetsPageView(SuperuserRequiredMixin, View):
    def get(self, request):
        return render(request, "platform_admin/resets.html")


@method_decorator(never_cache, name="dispatch")
class ResetsListView(SuperuserRequiredMixin, View):
    def get(self, request):
        base = PasswordResetRequest.objects.select_related("user", "user__tenant", "tenant", "decided_by")
        pending = list(base.filter(status=Status.PENDING).order_by("requested_at")[:200])
        since = timezone.now() - timedelta(days=RECENT_DAYS)
        recent = list(base.exclude(status=Status.PENDING).filter(requested_at__gte=since)
                      .order_by("-requested_at")[:100])
        return JsonResponse({
            "summary": {"pending_count": len(pending)},
            "pending": [_row(r) for r in pending],
            "recent": [_row(r) for r in recent],
        })


class ResetActionView(SuperuserRequiredMixin, View):
    """POST /platform/resets/api/<id>/<approve|reject>/"""

    def post(self, request, pk, action):
        get_object_or_404(PasswordResetRequest, pk=pk)
        note = (_payload(request).get("note") or "").strip()
        try:
            if action == "approve":
                req = password_reset.approve(pk, by=request.user, note=note)
            elif action == "reject":
                req = password_reset.reject(pk, by=request.user, note=note)
            else:
                raise Http404
        except password_reset.RequestNotPending:
            return JsonResponse({"error": "Someone already handled this request. Refresh the list."}, status=409)
        except password_reset.NoEmailOnFile:
            return JsonResponse({"error": "There is no email address on this account or on the shop owner's, "
                                          "so no link can be sent. Reject it and ask the shop owner to set "
                                          "the password from their Staff page."}, status=400)
        except password_reset.ResetEmailFailed:
            return JsonResponse({"error": "The email couldn't be sent just now, so nothing was approved. "
                                          "Try again in a minute."}, status=502)
        return JsonResponse({"ok": True, "status": req.status})
