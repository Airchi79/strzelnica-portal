from django.urls import path

from apps.credentials import views as credentials_views
from apps.facilities import views as facilities_views
from apps.regulations import views as regulations_views

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.panel_home, name="home"),
    path("weryfikacja/", credentials_views.panel_list, name="credentials"),
    path("weryfikacja/<int:pk>/", credentials_views.panel_detail, name="credential"),
    path("weryfikacja/<int:pk>/skan/", credentials_views.panel_scan, name="credential_scan"),
    path("uzytkownicy/", views.panel_users, name="users"),
    path("uzytkownicy/<int:pk>/", views.panel_user, name="user"),
    path("regulaminy/", regulations_views.panel_list, name="regulations"),
    path("regulaminy/<int:doc_pk>/nowa/", regulations_views.panel_version_edit, name="regulation_new"),
    path("regulaminy/<int:doc_pk>/wersja/<int:pk>/", regulations_views.panel_version_edit, name="regulation_edit"),
    path("regulaminy/wersja/<int:pk>/publikuj/", regulations_views.panel_version_publish, name="regulation_publish"),
    path("regulaminy/wersja/<int:pk>/usun/", regulations_views.panel_version_delete, name="regulation_delete"),
    path("osie/", facilities_views.panel_list, name="lanes"),
    path("osie/<int:pk>/", facilities_views.panel_lane, name="lane"),
    path("osie/<int:pk>/cennik/", facilities_views.panel_price_add, name="price_add"),
    path("ustawienia/", views.panel_settings, name="settings"),
    path("dziennik/", views.panel_audit, name="audit"),
]
