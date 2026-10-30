from django.contrib import admin
from django.urls import include, path
from django_otp.admin import OTPAdminSite

from apps.core import views as core_views

# Panel techniczny Django tylko dla superużytkownika i tylko po weryfikacji dwuskładnikowej.
admin.site.__class__ = OTPAdminSite
admin.site.site_header = "Strzelnica Sątok — panel techniczny"

urlpatterns = [
    path("", include("apps.core.urls")),
    path("konto/", include("apps.accounts.urls")),
    path("dokumenty/", include("apps.credentials.urls")),
    path("regulamin/", include("apps.regulations.urls")),
    path("panel/", include("apps.core.panel_urls")),
    path("techniczny/", admin.site.urls),
    path("manifest.webmanifest", core_views.manifest, name="manifest"),
    path("sw.js", core_views.service_worker, name="service_worker"),
]
