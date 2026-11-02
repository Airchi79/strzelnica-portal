from django.urls import path

from . import views

app_name = "kiosk"

urlpatterns = [
    path("", views.kiosk_home, name="home"),
    path("parowanie/", views.kiosk_pair, name="pair"),
    path("api/stan/", views.api_state, name="api_state"),
    path("api/wpis/", views.api_entry, name="api_entry"),
    path("api/osoba/<str:token>/", views.api_person, name="api_person"),
    path("api/wyjscie/", views.api_exit_search, name="api_exit_search"),
    path("api/wyjscie/<int:pk>/", views.api_exit, name="api_exit"),
    path("api/odrzucony/", views.api_report_failed, name="api_failed"),
]
