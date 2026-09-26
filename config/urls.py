"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from users.views import HomepageView
from config.pwa_views import service_worker_view, OfflineView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('users/', include('users.urls')),
    path('products/', include('products.urls')),
    path('suppliers/', include('supplier.urls')),
    path('pos/', include('sales.urls')),
    path('payments/', include('payments.urls')),
    path('inventory/', include('inventory.urls')),

    # --- PWA support -------------------------------------------------
    # Must stay at the domain root (not under /static/) so the service
    # worker's default scope covers the entire site.
    path('service-worker.js', service_worker_view, name='service_worker'),
    path('offline/', OfflineView.as_view(), name='offline'),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)