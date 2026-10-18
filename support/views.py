import mimetypes
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Case, Count, IntegerField, Q, When
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, FormView, ListView, TemplateView
from django_ratelimit.core import is_ratelimited

from users.mixins import AdminRequiredMixin, CashierRequiredMixin, TenantScopedQuerysetMixin
from users.utils import log_action

from . import services
from .availability import get_availability, whatsapp_url
from .forms import (
    ContactForm, FeatureRequestForm, IssueReportForm, ReplyForm,
    StaffReplyForm, TicketManageForm,
)
from .mixins import (
    PostRateLimitMixin, SupportStaffRequiredMixin, TenantRequiredMixin, is_management,
)
from .models import FeatureRequest, FeatureVote, HelpArticle, Ticket, TicketAttachment
from .search import search_articles, search_features


def _wa_message(request, ticket=None):
    who = request.user.get_full_name() or request.user.get_username()
    shop = getattr(request, "tenant", None)
    text = f"Hello, this is {who}" + (f" from {shop}" if shop else "") + "."
    if ticket is not None:
        text += f" I need help with {ticket.reference}."
    return whatsapp_url(text)


class SupportHomeView(CashierRequiredMixin, TenantRequiredMixin, TemplateView):
    template_name = "support/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tickets = Ticket._base_manager.filter(tenant=self.request.tenant)
        if not is_management(self.request.user):
            tickets = tickets.filter(created_by=self.request.user)
        ctx["recent_tickets"] = tickets.order_by("-updated_at")[:5]
        ctx["featured"] = HelpArticle.objects.filter(is_published=True, is_featured=True)[:6]
        ctx["availability"] = get_availability()
        ctx["whatsapp_url"] = _wa_message(self.request)
        return ctx


class ReportIssueView(CashierRequiredMixin, TenantRequiredMixin, PostRateLimitMixin, FormView):
    template_name = "support/report_issue.html"
    form_class = IssueReportForm

    def form_valid(self, form):
        d = form.cleaned_data
        ticket = services.create_ticket(
            tenant=self.request.tenant, user=self.request.user, kind=Ticket.Kind.ISSUE,
            category=d["category"], severity=d["severity"], subject=d["subject"],
            description=d["description"], steps=d["steps_to_reproduce"],
            environment=services.capture_environment(self.request, d["client_env"]),
            files=d["attachments"],
        )
        log_action(self.request.user, "Support Ticket", f"{ticket.reference}: {ticket.subject}", self.request)
        messages.success(self.request, f"Thanks. We've logged {ticket.reference} and will reply here.")
        return redirect(ticket)


class ContactSupportView(CashierRequiredMixin, TenantRequiredMixin, PostRateLimitMixin, FormView):
    template_name = "support/contact.html"
    form_class = ContactForm

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["availability"] = get_availability()
        ctx["whatsapp_url"] = _wa_message(self.request)
        return ctx

    def form_valid(self, form):
        d = form.cleaned_data
        ticket = services.create_ticket(
            tenant=self.request.tenant, user=self.request.user, kind=Ticket.Kind.QUESTION,
            subject=d["subject"], description=d["message"],
            environment=services.capture_environment(self.request, d["client_env"]),
            files=d["attachments"],
        )
        log_action(self.request.user, "Support Ticket", f"{ticket.reference}: {ticket.subject}", self.request)
        messages.success(self.request, f"Message sent as {ticket.reference}. We'll email you when we reply.")
        return redirect(ticket)


class TicketListView(CashierRequiredMixin, TenantRequiredMixin, TenantScopedQuerysetMixin, ListView):
    model = Ticket
    template_name = "support/ticket_list.html"
    context_object_name = "tickets"
    paginate_by = 15

    def get_queryset(self):
        qs = super().get_queryset().select_related("assigned_to")
        if not is_management(self.request.user):
            qs = qs.filter(created_by=self.request.user)
        self.filter = self.request.GET.get("show", "open")
        done = [Ticket.Status.RESOLVED, Ticket.Status.CLOSED]
        if self.filter == "closed":
            qs = qs.filter(status__in=done)
        elif self.filter != "all":
            self.filter = "open"
            qs = qs.exclude(status__in=done)
        return qs.order_by("-updated_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show"] = self.filter
        return ctx


class TicketDetailView(
    CashierRequiredMixin, TenantRequiredMixin, PostRateLimitMixin,
    TenantScopedQuerysetMixin, DetailView,
):
    model = Ticket
    template_name = "support/ticket_detail.html"
    context_object_name = "ticket"
    slug_field = "number"
    slug_url_kwarg = "number"

    def get_queryset(self):
        qs = super().get_queryset().select_related("assigned_to", "created_by")
        if not is_management(self.request.user):
            qs = qs.filter(created_by=self.request.user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        t = self.object
        ctx["thread"] = t.messages.filter(is_internal=False).select_related("author").prefetch_related("attachments")
        ctx["first_attachments"] = t.attachments.filter(message__isnull=True)
        ctx["events"] = t.events.filter(visible_to_customer=True).select_related("actor")
        ctx.setdefault("reply_form", ReplyForm())
        ctx["can_reply"] = t.status != Ticket.Status.CLOSED
        ctx["availability"] = get_availability()
        ctx["whatsapp_url"] = _wa_message(self.request, t)
        return ctx

    def post(self, request, *args, **kwargs):
        self.object = ticket = self.get_object()
        if ticket.status == Ticket.Status.CLOSED:
            messages.error(request, "This ticket is closed. Please open a new one.")
            return redirect(ticket)
        form = ReplyForm(request.POST, request.FILES)
        if form.is_valid():
            services.add_message(
                ticket, author=request.user, body=form.cleaned_data["body"],
                files=form.cleaned_data["attachments"],
            )
            messages.success(request, "Reply sent.")
            return redirect(ticket)
        return self.render_to_response(self.get_context_data(object=ticket, reply_form=form))


class FeatureListView(AdminRequiredMixin, ListView):
    template_name = "support/feature_list.html"
    context_object_name = "features"
    paginate_by = 20

    def get_queryset(self):
        g = self.request.GET
        qs = FeatureRequest.objects.filter(is_visible=True)
        self.status = g.get("status", "")
        if self.status in FeatureRequest.Status.values:
            qs = qs.filter(status=self.status)
        elif self.status != "all":
            self.status = ""
            qs = qs.exclude(status=FeatureRequest.Status.DECLINED)
        self.module = g.get("module", "")
        if self.module in FeatureRequest.Module.values:
            qs = qs.filter(module=self.module)
        self.q = g.get("q", "").strip()[:100]
        if self.q:
            qs = qs.filter(Q(title__icontains=self.q) | Q(description__icontains=self.q))
        self.sort = "new" if g.get("sort") == "new" else "top"
        return qs.order_by("-created_at") if self.sort == "new" else qs.order_by("-vote_count", "-created_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            status=self.status, module=self.module, q=self.q, sort=self.sort,
            status_choices=FeatureRequest.Status.choices,
            module_choices=FeatureRequest.Module.choices,
            voted_ids=set(FeatureVote.objects.filter(user=self.request.user).values_list("feature_id", flat=True)),
        )
        return ctx


class FeatureCreateView(AdminRequiredMixin, TenantRequiredMixin, PostRateLimitMixin, FormView):
    template_name = "support/feature_form.html"
    form_class = FeatureRequestForm
    ratelimit_rate = "10/h"

    def form_valid(self, form):
        d = form.cleaned_data
        services.create_feature_request(
            tenant=self.request.tenant, user=self.request.user,
            title=d["title"], description=d["description"], module=d["module"],
        )
        messages.success(self.request, "Thanks. Your request is on the board and we've counted your vote.")
        return redirect("support:feature-list")


class FeatureVoteView(AdminRequiredMixin, PostRateLimitMixin, View):
    ratelimit_rate = "60/h"

    def post(self, request, pk):
        try:
            voted, fr = services.toggle_vote(pk, request.user)
            messages.success(request, "Vote added." if voted else "Vote removed.")
        except FeatureRequest.DoesNotExist:
            raise Http404
        except ValueError as exc:
            messages.error(request, str(exc))
        nxt = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
            nxt = reverse("support:feature-list")
        return redirect(nxt)


class HelpCenterView(CashierRequiredMixin, TemplateView):
    template_name = "support/help_center.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()[:100]
        kind = self.request.GET.get("kind", "")
        kind = kind if kind in HelpArticle.Kind.values else ""
        ctx.update(q=q, kind=kind, kind_choices=HelpArticle.Kind.choices)
        if q:
            ctx["results"] = search_articles(q, limit=20, kind=kind or None)
        else:
            articles = HelpArticle.objects.filter(is_published=True)
            if kind:
                articles = articles.filter(kind=kind)
            articles = list(articles)
            ctx["groups"] = [
                (label, [a for a in articles if a.topic == value])
                for value, label in HelpArticle.Topic.choices
                if any(a.topic == value for a in articles)
            ]
        return ctx


class HelpArticleView(CashierRequiredMixin, DetailView):
    template_name = "support/help_article.html"
    context_object_name = "article"
    queryset = HelpArticle.objects.filter(is_published=True)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        a = self.object
        ctx["related"] = HelpArticle.objects.filter(is_published=True, topic=a.topic).exclude(pk=a.pk)[:5]
        return ctx


class SuggestView(CashierRequiredMixin, View):
    """JSON used by the report and feature forms to show help before submitting."""

    def get(self, request):
        if is_ratelimited(request, group="support:suggest", key="user", rate="60/m",
                          method="GET", increment=True):
            return JsonResponse({"items": []}, status=429)
        q = request.GET.get("q", "")[:300]
        if request.GET.get("scope") == "features":
            items = [
                {
                    "title": f.title,
                    "note": f"{f.get_status_display()}, {f.vote_count} vote{'s' if f.vote_count != 1 else ''}",
                    "url": reverse("support:feature-list") + f"?q={quote(f.title)}",
                }
                for f in search_features(q)
            ]
        else:
            items = [
                {"title": a.title, "note": a.summary, "url": a.get_absolute_url()}
                for a in search_articles(q, limit=4, min_score=2)
            ]
        return JsonResponse({"items": items})


class AttachmentDownloadView(LoginRequiredMixin, View):
    """Uploads are private: only the ticket's business (or your staff) can open them."""

    def get(self, request, pk):
        att = get_object_or_404(TicketAttachment.objects.select_related("ticket", "message"), pk=pk)
        user, t = request.user, att.ticket
        if not user.is_staff:
            tenant = getattr(request, "tenant", None)
            allowed = (
                tenant is not None
                and t.tenant_id == tenant.pk
                and (is_management(user) or t.created_by_id == user.pk)
                and not (att.message and att.message.is_internal)
            )
            if not allowed:
                raise Http404
        try:
            handle = att.file.open("rb")
        except FileNotFoundError:
            raise Http404
        inline = att.is_image or att.is_video
        resp = FileResponse(
            handle,
            content_type=mimetypes.guess_type(att.original_name)[0] or "application/octet-stream",
            as_attachment=not inline, filename=att.original_name,
        )
        resp["X-Content-Type-Options"] = "nosniff"
        resp["Cache-Control"] = "private, max-age=3600"
        return resp


_CLOSED = [Ticket.Status.RESOLVED, Ticket.Status.CLOSED]


class StaffTicketListView(SupportStaffRequiredMixin, ListView):
    template_name = "support/staff_ticket_list.html"
    context_object_name = "tickets"
    paginate_by = 25

    def get_queryset(self):
        g = self.request.GET
        qs = Ticket._base_manager.select_related("tenant", "assigned_to", "created_by")
        self.view = g.get("view", "active")
        if self.view == "mine":
            qs = qs.filter(assigned_to=self.request.user).exclude(status__in=_CLOSED)
        elif self.view == "unassigned":
            qs = qs.filter(assigned_to__isnull=True).exclude(status__in=_CLOSED)
        elif self.view == "closed":
            qs = qs.filter(status__in=_CLOSED)
        elif self.view == "all":
            pass
        else:
            self.view = "active"
            qs = qs.exclude(status__in=_CLOSED)
        for field in ("priority", "kind"):
            value = g.get(field, "")
            if value:
                qs = qs.filter(**{field: value})
        self.q = g.get("q", "").strip()[:100]
        if self.q:
            digits = self.q.upper().removeprefix("TKT-").strip()
            cond = (
                Q(subject__icontains=self.q) | Q(tenant__slug__icontains=self.q)
                | Q(created_by__username__icontains=self.q)
            )
            if digits.isdigit():
                cond |= Q(number=int(digits))
            qs = qs.filter(cond)
        if self.view in ("active", "mine", "unassigned"):
            rank = Case(
                When(priority="CRITICAL", then=0), When(priority="HIGH", then=1),
                When(priority="MEDIUM", then=2), default=3, output_field=IntegerField(),
            )
            return qs.annotate(rank=rank).order_by("rank", "created_at")
        return qs.order_by("-updated_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        live = ~Q(status__in=_CLOSED)
        ctx["counts"] = Ticket._base_manager.aggregate(
            active=Count("pk", filter=live),
            unassigned=Count("pk", filter=live & Q(assigned_to__isnull=True)),
            mine=Count("pk", filter=live & Q(assigned_to=self.request.user)),
            critical=Count("pk", filter=live & Q(priority="CRITICAL")),
        )
        ctx.update(
            view=self.view, q=self.q,
            priority=self.request.GET.get("priority", ""), kind=self.request.GET.get("kind", ""),
            priority_choices=Ticket._meta.get_field("priority").choices,
            kind_choices=Ticket.Kind.choices,
        )
        return ctx


class StaffTicketDetailView(SupportStaffRequiredMixin, DetailView):
    template_name = "support/staff_ticket_detail.html"
    context_object_name = "ticket"
    slug_field = "number"
    slug_url_kwarg = "number"

    def get_queryset(self):
        return Ticket._base_manager.select_related("tenant", "assigned_to", "created_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        t = self.object
        ctx["thread"] = t.messages.select_related("author").prefetch_related("attachments")
        ctx["first_attachments"] = t.attachments.filter(message__isnull=True)
        ctx["events"] = t.events.select_related("actor")
        ctx.setdefault("reply_form", StaffReplyForm())
        ctx.setdefault("manage_form", TicketManageForm(initial={
            "status": t.status, "priority": t.priority, "assigned_to": t.assigned_to,
            "resolution_summary": t.resolution_summary,
        }))
        ctx["whatsapp_url"] = whatsapp_url(f"Hello, this is support about {t.reference}.")
        return ctx

    def post(self, request, *args, **kwargs):
        self.object = ticket = self.get_object()
        action = request.POST.get("action")

        if action == "assign_me":
            services.update_ticket(ticket, actor=request.user, assigned_to=request.user)
            messages.success(request, "Assigned to you.")
            return redirect(ticket.get_staff_url())

        if action == "reply":
            form = StaffReplyForm(request.POST, request.FILES)
            if form.is_valid():
                d = form.cleaned_data
                if ticket.status == Ticket.Status.CLOSED and not d["is_internal"]:
                    messages.error(request, "This ticket is closed. Reopen it before replying to the customer.")
                else:
                    services.add_message(
                        ticket, author=request.user, body=d["body"], files=d["attachments"],
                        internal=d["is_internal"], from_staff=True,
                    )
                    messages.success(request, "Internal note added." if d["is_internal"] else "Reply sent.")
                    return redirect(ticket.get_staff_url())
            return self.render_to_response(self.get_context_data(object=ticket, reply_form=form))

        if action == "manage":
            form = TicketManageForm(request.POST)
            if form.is_valid():
                d = form.cleaned_data
                services.update_ticket(
                    ticket, actor=request.user, status=d["status"], priority=d["priority"],
                    assigned_to=d["assigned_to"], resolution_summary=d["resolution_summary"].strip(),
                )
                messages.success(request, "Ticket updated.")
                return redirect(ticket.get_staff_url())
            return self.render_to_response(self.get_context_data(object=ticket, manage_form=form))

        return redirect(ticket.get_staff_url())
