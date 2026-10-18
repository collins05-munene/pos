"""
Help desk section of the platform admin: work the client queue, write articles,
post video tutorials and manage feature requests. Same pattern as the Overview
page: one HTML shell plus JSON endpoints. Business rules live in support.services
so replies, status changes and release emails behave exactly as they do elsewhere.
"""
import statistics
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views import View
from django.views.decorators.cache import never_cache

from support import services as support_services
from support.forms import StaffReplyForm, TicketManageForm
from support.models import FeatureRequest, HelpArticle, Severity, Ticket

CLOSED = [Ticket.Status.RESOLVED, Ticket.Status.CLOSED]


class HelpdeskAccessMixin(UserPassesTestMixin):
    """Superusers, plus staff accounts you create for support agents.
    To restrict to superusers only (like the Overview page), drop `or u.is_staff`."""

    raise_exception = True

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and u.is_active and (u.is_superuser or u.is_staff)


@method_decorator(never_cache, name="dispatch")
class API(HelpdeskAccessMixin, View):
    pass

def _iso(dt):
    return dt.isoformat() if dt else None


def _person(u):
    return (u.get_full_name() or u.get_username()) if u else None


def _errors(form):
    return {k: [str(e) for e in v] for k, v in form.errors.items()}


def _bad(errors, status=400):
    return JsonResponse({"errors": errors}, status=status)


def _wa_customer(phone):
    """Kenyan numbers are often stored as 07xx...; wa.me needs 2547xx..."""
    d = "".join(c for c in (phone or "") if c.isdigit())
    if d.startswith("0") and len(d) == 10:
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "71":
        d = "254" + d
    return f"https://wa.me/{d}" if d else ""


def _attachment(a):
    return {
        "id": a.pk, "name": a.original_name, "size": a.size, "is_image": a.is_image,
        "is_video": a.is_video, "url": reverse("support:attachment", args=[a.pk]),
    }


def _ticket_row(t):
    return {
        "number": t.number, "reference": t.reference, "subject": t.subject,
        "tenant": str(t.tenant), "kind": t.kind, "kind_label": t.get_kind_display(),
        "priority": t.priority, "priority_label": t.get_priority_display(),
        "status": t.status, "status_label": t.get_status_display(),
        "agent": t.agent_name if t.assigned_to_id else None,
        "created_at": _iso(t.created_at), "updated_at": _iso(t.updated_at),
    }


def _ticket_detail(t):
    t = Ticket._base_manager.select_related("tenant", "created_by", "assigned_to").get(pk=t.pk)
    first = [_attachment(a) for a in t.attachments.filter(message__isnull=True)]
    messages = [
        {
            "id": m.pk, "author": m.author_full_name if m.author else ("Support" if m.is_staff_reply else "Former user"),
            "is_staff_reply": m.is_staff_reply, "is_internal": m.is_internal, "body": m.body,
            "created_at": _iso(m.created_at), "attachments": [_attachment(a) for a in m.attachments.all()],
        }
        for m in t.messages.select_related("author").prefetch_related("attachments")
    ]
    tenant, user = t.tenant, t.created_by
    data = _ticket_row(t)
    data.update({
        "severity": t.severity, "severity_label": t.get_severity_display(),
        "category_label": t.get_category_display() if t.category else "",
        "assigned_to": t.assigned_to_id, "resolution_summary": t.resolution_summary,
        "first_response_at": _iso(t.first_response_at),
        "tenant_id": tenant.pk,
        "customer": {
            "name": t.customer_name, "email": getattr(user, "email", "") if user else "",
            "shop": str(tenant), "shop_phone": getattr(tenant, "phone", "") or "",
            "shop_email": getattr(tenant, "email", "") or "",
            "whatsapp": _wa_customer(getattr(tenant, "phone", "")),
        },
        "report": {"description": t.description, "steps": t.steps_to_reproduce, "attachments": first},
        "environment": t.environment or {},
        "messages": messages,
        "events": [
            {"detail": e.detail, "internal": not e.visible_to_customer, "created_at": _iso(e.created_at)}
            for e in t.events.all()
        ],
    })
    return data


def _article_row(a):
    return {
        "id": a.pk, "title": a.title, "slug": a.slug, "topic": a.topic, "topic_label": a.get_topic_display(),
        "kind": a.kind, "kind_label": a.get_kind_display(), "is_published": a.is_published,
        "is_featured": a.is_featured, "updated_at": _iso(a.updated_at),
    }


def _article_detail(a):
    d = _article_row(a)
    d.update({
        "summary": a.summary, "body": a.body, "video_url": a.video_url, "keywords": a.keywords,
        "sort_order": a.sort_order, "embed_url": a.embed_url,
        "public_url": a.get_absolute_url() if a.is_published else "",
    })
    return d


def _feature_row(f):
    return {
        "id": f.pk, "title": f.title, "description": f.description, "module": f.module,
        "module_label": f.get_module_display(), "status": f.status, "status_label": f.get_status_display(),
        "status_note": f.status_note, "release_notes": f.release_notes, "votes": f.vote_count,
        "is_visible": f.is_visible, "shop": str(f.tenant) if f.tenant else None,
        "submitted_by": _person(f.submitted_by), "ticket": f.ticket.number if f.ticket else None,
        "created_at": _iso(f.created_at), "released_at": _iso(f.released_at),
    }


def _choices(request):
    User = get_user_model()
    agents = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True), is_active=True).order_by("first_name", "username")
    return {
        "ticket_status": list(Ticket.Status.choices),
        "priority": list(Severity.choices),
        "ticket_kind": list(Ticket.Kind.choices),
        "topic": list(HelpArticle.Topic.choices),
        "article_kind": list(HelpArticle.Kind.choices),
        "feature_status": list(FeatureRequest.Status.choices),
        "module": list(FeatureRequest.Module.choices),
        "agents": [{"id": u.pk, "name": _person(u)} for u in agents],
        "me": request.user.pk,
    }



class HelpdeskView(HelpdeskAccessMixin, View):
    def get(self, request):
        return render(request, "platform_admin/helpdesk.html", {
            "choices": _choices(request),
            "api_base": reverse("helpdesk:api-summary").rsplit("summary/", 1)[0],
            "tenant_base": getattr(settings, "PLATFORM_TENANTS_PATH", "/platform/tenants/"),
            "landing": reverse("admin-dashboard"),
        })



class SummaryView(API):
    def get(self, request):
        live = ~Q(status__in=CLOSED)
        agg = Ticket._base_manager.aggregate(
            open=Count("pk", filter=live),
            unassigned=Count("pk", filter=live & Q(assigned_to__isnull=True)),
            urgent=Count("pk", filter=live & Q(priority__in=["HIGH", "CRITICAL"])),
        )
        since = timezone.now() - timedelta(days=30)
        pairs = Ticket._base_manager.filter(
            created_at__gte=since, first_response_at__isnull=False
        ).values_list("created_at", "first_response_at")[:1000]
        waits = [(r - c).total_seconds() for c, r in pairs]
        agg["median_first_reply_seconds"] = statistics.median(waits) if waits else None
        agg["draft_articles"] = HelpArticle.objects.filter(is_published=False).count()
        agg["features_to_review"] = FeatureRequest.objects.filter(status=FeatureRequest.Status.UNDER_REVIEW, is_visible=True).count()
        return JsonResponse(agg)



class TicketListView(API):
    def get(self, request):
        g = request.GET
        qs = Ticket._base_manager.select_related("tenant", "assigned_to", "created_by")
        view = g.get("view", "active")
        if view == "mine":
            qs = qs.filter(assigned_to=request.user).exclude(status__in=CLOSED)
        elif view == "unassigned":
            qs = qs.filter(assigned_to__isnull=True).exclude(status__in=CLOSED)
        elif view == "closed":
            qs = qs.filter(status__in=CLOSED)
        elif view != "all":
            view = "active"
            qs = qs.exclude(status__in=CLOSED)
        for field in ("priority", "kind"):
            if g.get(field):
                qs = qs.filter(**{field: g[field]})
        q = g.get("q", "").strip()[:100]
        if q:
            digits = q.upper().removeprefix("TKT-").strip()
            cond = (Q(subject__icontains=q) | Q(tenant__slug__icontains=q) | Q(tenant__name__icontains=q)
                    | Q(created_by__username__icontains=q))
            if digits.isdigit():
                cond |= Q(number=int(digits))
            qs = qs.filter(cond)
        if view in ("active", "mine", "unassigned"):
            rank = Case(When(priority="CRITICAL", then=0), When(priority="HIGH", then=1),
                        When(priority="MEDIUM", then=2), default=3, output_field=IntegerField())
            qs = qs.annotate(rank=rank).order_by("rank", "created_at")
        else:
            qs = qs.order_by("-updated_at")
        page = Paginator(qs, 25).get_page(g.get("page"))
        return JsonResponse({"page": page.number, "pages": page.paginator.num_pages,
                             "results": [_ticket_row(t) for t in page]})


class TicketMixin:
    def ticket(self, number):
        return get_object_or_404(Ticket._base_manager.select_related("tenant", "assigned_to"), number=number)


class TicketDetailView(TicketMixin, API):
    def get(self, request, number):
        return JsonResponse(_ticket_detail(self.ticket(number)))


class TicketReplyView(TicketMixin, API):
    def post(self, request, number):
        t = self.ticket(number)
        form = StaffReplyForm(request.POST, request.FILES)
        if not form.is_valid():
            return _bad(_errors(form))
        d = form.cleaned_data
        if t.status == Ticket.Status.CLOSED and not d["is_internal"]:
            return _bad({"body": ["This ticket is closed. Reopen it before replying to the customer."]})
        support_services.add_message(
            t, author=request.user, body=d["body"], files=d["attachments"],
            internal=d["is_internal"], from_staff=True,
        )
        return JsonResponse({"ok": True, "ticket": _ticket_detail(t)})


class TicketUpdateView(TicketMixin, API):
    def post(self, request, number):
        t = self.ticket(number)
        form = TicketManageForm(request.POST)
        if not form.is_valid():
            return _bad(_errors(form))
        d = form.cleaned_data
        support_services.update_ticket(
            t, actor=request.user, status=d["status"], priority=d["priority"],
            assigned_to=d["assigned_to"], resolution_summary=d["resolution_summary"].strip(),
        )
        return JsonResponse({"ok": True, "ticket": _ticket_detail(t)})


class TicketAssignMeView(TicketMixin, API):
    def post(self, request, number):
        t = self.ticket(number)
        support_services.update_ticket(t, actor=request.user, assigned_to=request.user)
        return JsonResponse({"ok": True, "ticket": _ticket_detail(t)})


class ArticleForm(forms.ModelForm):
    class Meta:
        model = HelpArticle
        fields = ["title", "slug", "topic", "kind", "summary", "body", "video_url",
                  "keywords", "is_published", "is_featured", "sort_order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        self.fields["sort_order"].required = False

    def clean(self):
        data = super().clean()
        if not data.get("slug") and data.get("title"):
            base = slugify(data["title"])[:150] or "article"
            slug, n = base, 2
            others = HelpArticle.objects.exclude(pk=self.instance.pk)
            while others.filter(slug=slug).exists():
                slug, n = f"{base}-{n}", n + 1
            data["slug"] = slug
        if data.get("sort_order") is None:
            data["sort_order"] = self.instance.sort_order if self.instance.pk else 100
        if data.get("kind") == HelpArticle.Kind.VIDEO and not data.get("video_url"):
            self.add_error("video_url", "Add a YouTube or Vimeo link for a video tutorial.")
        if data.get("is_published") and not (data.get("body") or data.get("video_url")):
            self.add_error("body", "Add some content before publishing.")
        return data


class ArticleListView(API):
    def get(self, request):
        g = request.GET
        qs = HelpArticle.objects.all()
        if g.get("status") == "published":
            qs = qs.filter(is_published=True)
        elif g.get("status") == "draft":
            qs = qs.filter(is_published=False)
        for field in ("kind", "topic"):
            if g.get(field):
                qs = qs.filter(**{field: g[field]})
        q = g.get("q", "").strip()[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(summary__icontains=q) | Q(keywords__icontains=q))
        page = Paginator(qs.order_by("-updated_at"), 25).get_page(g.get("page"))
        return JsonResponse({"page": page.number, "pages": page.paginator.num_pages,
                             "results": [_article_row(a) for a in page]})


class ArticleDetailView(API):
    def get(self, request, pk):
        return JsonResponse(_article_detail(get_object_or_404(HelpArticle, pk=pk)))


class ArticleSaveView(API):
    """POST with no `id` creates; with `id` updates."""

    def post(self, request):
        pk = request.POST.get("id")
        instance = get_object_or_404(HelpArticle, pk=pk) if pk else None
        form = ArticleForm(request.POST, instance=instance)
        if not form.is_valid():
            return _bad(_errors(form))
        return JsonResponse({"ok": True, "article": _article_detail(form.save())})


class ArticlePublishView(API):
    def post(self, request, pk):
        a = get_object_or_404(HelpArticle, pk=pk)
        publish = request.POST.get("published") == "1"
        if publish and not (a.body or a.video_url):
            return _bad({"body": ["Add some content before publishing."]})
        a.is_published = publish
        a.save(update_fields=["is_published", "updated_at"])
        return JsonResponse({"ok": True, "article": _article_detail(a)})


class ArticleDeleteView(API):
    def post(self, request, pk):
        get_object_or_404(HelpArticle, pk=pk).delete()
        return JsonResponse({"ok": True})


class FeatureListView(API):
    def get(self, request):
        g = request.GET
        qs = FeatureRequest.objects.select_related("tenant", "submitted_by", "ticket")
        if g.get("status") in FeatureRequest.Status.values:
            qs = qs.filter(status=g["status"])
        if g.get("visibility") == "hidden":
            qs = qs.filter(is_visible=False)
        q = g.get("q", "").strip()[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        page = Paginator(qs.order_by("-vote_count", "-created_at"), 25).get_page(g.get("page"))
        return JsonResponse({"page": page.number, "pages": page.paginator.num_pages,
                             "results": [_feature_row(f) for f in page]})


class FeatureUpdateView(API):
    def post(self, request, pk):
        f = get_object_or_404(FeatureRequest, pk=pk)
        p = request.POST
        status = p.get("status", f.status)
        if status not in FeatureRequest.Status.values:
            return _bad({"status": ["Choose a valid status."]})
        release_notes = p.get("release_notes", f.release_notes).strip()
        if status == FeatureRequest.Status.RELEASED and not release_notes:
            return _bad({"release_notes": [
                "Add release notes. They are emailed to everyone who voted for this feature."]})
        changed = status != f.status
        f.status = status
        f.status_note = p.get("status_note", f.status_note).strip()[:200]
        f.release_notes = release_notes
        f.is_visible = p.get("is_visible") == "on"
        f.save()
        if changed:
            support_services.feature_status_changed(f)   # stamps date, emails voters once, resolves author's ticket
        f.refresh_from_db()
        return JsonResponse({"ok": True, "feature": _feature_row(f)})
