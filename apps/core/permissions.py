from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.views import SESSION_2FA_NEXT

# Uprawnienia, które można nadawać w panelu zarządu (właściciel systemu ma wszystkie jako superużytkownik).
PANEL_PERMISSIONS = [
    ("association.manage_members", "Stowarzyszenie: członkowie, deklaracje i składki"),
    ("association.manage_resolutions", "Stowarzyszenie: uchwały i zebrania"),
    ("association.manage_clubs", "Kluby i kody dostępu"),
    ("association.view_reports", "Raporty"),
    ("reports.view_salesreport", "Raporty sprzedaży: przegląd, generowanie, PDF i CSV"),
    ("payments.manage_payments", "Płatności: wpłaty, import wyciągu, salda i korekty"),
    ("logbook.view_logbook", "Książka pobytu: przegląd i wydruk"),
    ("logbook.staff_entry", "Książka pobytu: wpis w imieniu osoby (obsługa)"),
    ("logbook.manage_logbook", "Książka pobytu: ustawienia i kod QR wejścia"),
    ("bookings.manage_reservations", "Rezerwacje, blokady osi i rozliczenia rezerwacji"),
    ("bookings.manage_booking_settings", "Rezerwacje: ustawienia (anulowanie, płatności, grupy klientów) i import archiwum"),
    ("payments.accept_onsite", "Wpłaty na miejscu: gotówka i BLIK za rezerwację (bez importu wyciągów i korekt sald)"),
    ("credentials.review_credential", "Weryfikacja dokumentów i uprawnień"),
    ("accounts.view_users", "Klienci: przeglądanie kartoteki i kont"),
    ("clients.manage_clients", "Klienci: notatki, tagi, zatwierdzanie i kody wejścia"),
    ("regulations.publish_regulation", "Regulaminy: tworzenie i publikacja wersji"),
    ("facilities.manage_facilities", "Osie, godziny otwarcia i cennik"),
    ("core.manage_settings", "Dane stowarzyszenia"),
    ("audit.view_auditlog", "Dziennik zdarzeń"),
    ("accounts.manage_permissions", "Nadawanie uprawnień innym osobom"),
]


# Gotowe zestawy uprawnień (role). Nadawane w karcie osoby jednym przyciskiem; potem można je dowolnie zmienić.
ROLE_PRESETS = {
    "dyzurny": {
        "label": "Dyżurny / obsługa strzelnicy",
        "description": "Dziś na strzelnicy, rezerwacje i rezerwacja na miejscu, książka pobytu (podgląd i wpis za osobę), "
                       "wpłaty gotówką i BLIK za rezerwacje. Bez członków, raportów, ustawień i korekt sald.",
        "permissions": [
            "bookings.manage_reservations",
            "logbook.view_logbook",
            "logbook.staff_entry",
            "payments.accept_onsite",
        ],
    },
}


def panel_required(perm=None):
    """Dostęp do panelu zarządu: zalogowany, `is_staff`, po weryfikacji dwuskładnikowej i z danym uprawnieniem.
    `perm` może być krotką — wystarczy jedno z wymienionych uprawnień."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not user.is_active or not user.is_staff:
                raise PermissionDenied
            if not user.is_verified():
                request.session[SESSION_2FA_NEXT] = request.get_full_path()
                has_device = TOTPDevice.objects.filter(user=user, confirmed=True).exists()
                return redirect("accounts:otp_verify" if has_device else "accounts:otp_setup")
            perms = perm if isinstance(perm, (list, tuple)) else ([perm] if perm else [])
            if perms and not any(user.has_perm(p) for p in perms):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
