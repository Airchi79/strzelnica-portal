from django.urls import path

from . import views

app_name = "kiosk"

urlpatterns = [
    path("", views.kiosk_home, name="home"),
    path("parowanie/", views.kiosk_pair, name="pair"),
    path("manifest.webmanifest", views.kiosk_manifest, name="manifest"),
    path("api/stan/", views.api_state, name="api_state"),
    path("api/wpis/", views.api_entry, name="api_entry"),
    path("api/osoba/<str:token>/", views.api_person, name="api_person"),
    path("api/wyjscie/", views.api_exit_search, name="api_exit_search"),
    path("api/wyjscie/<int:pk>/", views.api_exit, name="api_exit"),
    path("api/odrzucony/", views.api_report_failed, name="api_failed"),
    path("api/na-miejscu/terminy/", views.api_onsite_slots, name="api_onsite_slots"),
    path("api/na-miejscu/cena/", views.api_onsite_price, name="api_onsite_price"),
    path("api/na-miejscu/start/", views.api_onsite_start, name="api_onsite_start"),
    path("api/na-miejscu/<str:step>/", views.api_onsite_step, name="api_onsite_step"),
]
