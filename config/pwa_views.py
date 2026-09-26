"""
PWA support views.

These live outside any business app on purpose: they carry no models,
no business logic, and no permissions, so they can't affect the
existing apps (users, products, supplier, sales, payments, inventory).

- service_worker_view:
    Serves the service worker JS at whatever URL it's mounted at in
    urls.py (mount it at the DOMAIN ROOT, e.g. "/service-worker.js",
    NOT under /static/). A service worker's default scope is
    everything "below" the URL it's served from, so it must be served
    from the root to be able to control the whole site. It's rendered
    as a Django template (rather than a plain static file) so it isn't
    fingerprinted/hashed by WhiteNoise's ManifestStaticFilesStorage —
    the browser needs to keep re-requesting the exact same URL to pick
    up updates. Cache-Control: no-cache ensures browsers always
    revalidate it instead of holding on to a stale copy.

- offline_view:
    Renders the offline fallback page the service worker shows when a
    page navigation fails with no network connection.
"""

from django.template.loader import render_to_string
from django.http import HttpResponse
from django.views.generic import TemplateView


def service_worker_view(request):
    content = render_to_string("static/service-worker.js")
    response = HttpResponse(content, content_type="application/javascript")
    # Let this worker (served from an arbitrary root-level path) control
    # the whole origin. Harmless if it's already served from "/".
    response["Service-Worker-Allowed"] = "/"
    # Never let a CDN/browser cache an old service worker version.
    response["Cache-Control"] = "no-cache"
    return response


class OfflineView(TemplateView):
    template_name = "pwa/offline.html"