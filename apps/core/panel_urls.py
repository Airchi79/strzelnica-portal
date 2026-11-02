from django.urls import path

from apps.association import views as association_views
from apps.bookings import views as bookings_views
from apps.credentials import views as credentials_views
from apps.facilities import views as facilities_views
from apps.logbook import views as logbook_views
from apps.payments import views as payments_views
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
    path("rezerwacje/dniami/", bookings_views.panel_days, name="reservations_days"),
    path("rezerwacje/nowa/", bookings_views.panel_new, name="reservation_new"),
    path("rezerwacje/ustawienia/", bookings_views.panel_settings, name="booking_settings"),
    path("rezerwacje/<int:pk>/", bookings_views.panel_detail, name="reservation"),
    path("blokady/", bookings_views.panel_blocks, name="blocks"),
    path("blokady/nowa/", bookings_views.panel_block_new, name="block_new"),
    path("blokady/<int:pk>/usun/", bookings_views.panel_block_delete, name="block_delete"),
    path("ksiazka/", logbook_views.panel_list, name="logbook"),
    path("ksiazka/druk/", logbook_views.panel_print, name="logbook_print"),
    path("ksiazka/eksport.xlsx", logbook_views.panel_export, name="logbook_export"),
    path("ksiazka/nowy-wpis/", logbook_views.panel_add, name="logbook_add"),
    path("ksiazka/ustawienia/", logbook_views.panel_settings, name="logbook_settings"),
    path("ksiazka/ustawienia/nowy-kod/", logbook_views.panel_rotate_gate, name="logbook_rotate"),
    path("ksiazka/plakat/", logbook_views.panel_gate_poster, name="logbook_poster"),
    path("ksiazka/<int:pk>/", logbook_views.panel_detail, name="logbook_entry"),
    path("platnosci/", payments_views.panel_payments, name="payments"),
    path("platnosci/wplata/", payments_views.panel_manual, name="payment_manual"),
    path("platnosci/import/", payments_views.panel_import, name="payment_import"),
    path("platnosci/<uuid:pk>/", payments_views.panel_payment, name="payment"),
    path("salda/", payments_views.panel_wallets, name="wallets"),
    path("salda/<int:user_pk>/", payments_views.panel_wallet, name="wallet"),
    path("czlonkowie/", association_views.panel_members, name="members"),
    path("czlonkowie/nowy/", association_views.panel_member_new, name="member_new"),
    path("czlonkowie/<int:pk>/", association_views.panel_member, name="member"),
    path("deklaracje/", association_views.panel_applications, name="applications"),
    path("skladki/", association_views.panel_fees, name="fees"),
    path("skladki/<int:pk>/wplata/", association_views.panel_fee_pay, name="fee_pay"),
    path("uchwaly/", association_views.panel_resolutions, name="resolutions"),
    path("uchwaly/nowa/", association_views.panel_resolution_new, name="resolution_new"),
    path("uchwaly/historyczna/", association_views.panel_resolution_historical, name="resolution_historical"),
    path("uchwaly/<uuid:pk>/", association_views.panel_resolution, name="resolution"),
    path("uchwaly/<uuid:pk>/druk/", association_views.panel_resolution_print, name="resolution_print"),
    path("uchwaly/<uuid:pk>/skan/", association_views.panel_resolution_attachment, name="resolution_attachment"),
    path("zebrania/", association_views.panel_meetings, name="meetings"),
    path("zebrania/<int:pk>/", association_views.panel_meeting, name="meeting"),
    path("kluby/", association_views.panel_clubs, name="clubs"),
    path("kluby/<int:pk>/", association_views.panel_club, name="club"),
    path("raporty/", association_views.panel_reports, name="reports"),
    path("powiadomienia/", bookings_views.panel_notifications, name="notifications"),
    path("powiadomienia/<int:pk>/", bookings_views.panel_notification_open, name="notification"),
    path("dziennik/", views.panel_audit, name="audit"),
]
