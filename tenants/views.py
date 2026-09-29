import json

from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit

from billing.pricing import quote_table
from .forms import SignupForm
from .services import register_business


@method_decorator(ratelimit(key="ip", rate="5/h", method="POST", block=True), name="post")
class SignupView(View):
    def get(self, request):
        """Pricing for the landing/sign-up page."""
        return JsonResponse({"pricing": quote_table(include_install_fee=True)})
    

    def post(self, request):
        if request.content_type == "application/json":
            try:
                data = json.loads(request.body or b"{}")
            except ValueError:
                return JsonResponse({"error": "invalid_json"}, status=400)
        else:
            data = request.POST

        form = SignupForm(data)
        if not form.is_valid():
            return JsonResponse({"errors": form.errors.get_json_data()}, status=400)

        try:
            tenant, owner, invoice = register_business(**form.cleaned_data)
        except ValidationError as exc:
            return JsonResponse({"errors": {"__all__": exc.messages}}, status=400)

        login(request, owner, backend="django.contrib.auth.backends.ModelBackend")
        return JsonResponse({
            "tenant": {"name": tenant.name, "business_code": tenant.slug},
            "invoice": {"number": invoice.number, "total": str(invoice.total),
                        "term_months": invoice.term_months},
            "next": "/billing/",   # owner is logged in; billing pages are exempt from the lock
        }, status=201)
