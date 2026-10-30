from django.urls import path

from apps.bookings import views as bookings_views
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
    path("rezerwacje/", bookings_views.panel_calendar, name="bookings"),
    path("rezerwacje/lista/", bookings_views.panel_list, name="reservations"),
    path("rezerwacje/nowa/", bookings_views.panel_new, name="reservation_new"),
    path("rezerwacje/ustawienia/", bookings_views.panel_settings, name="booking_settings"),
    path("rezerwacje/<int:pk>/", bookings_views.panel_detail, name="reservation"),
    path("blokady/", bookings_views.panel_blocks, name="blocks"),
    path("blokady/nowa/", bookings_views.panel_block_new, name="block_new"),
    path("blokady/<int:pk>/usun/", bookings_views.panel_block_delete, name="block_delete"),
    path("powiadomienia/", bookings_views.panel_notifications, name="notifications"),
    path("powiadomienia/<int:pk>/", bookings_views.panel_notification_open, name="notification"),
    path("dziennik/", views.panel_audit, name="audit"),
]
