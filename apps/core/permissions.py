from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.views import SESSION_2FA_NEXT

# Uprawnienia, które można nadawać w panelu zarządu (właściciel systemu ma wszystkie jako superużytkownik).
PANEL_PERMISSIONS = [
    ("credentials.review_credential", "Weryfikacja dokumentów i uprawnień"),
    ("accounts.view_users", "Przeglądanie kont użytkowników"),
    ("regulations.publish_regulation", "Regulaminy: tworzenie i publikacja wersji"),
    ("facilities.manage_facilities", "Osie, godziny otwarcia i cennik"),
    ("core.manage_settings", "Dane stowarzyszenia"),
    ("audit.view_auditlog", "Dziennik zdarzeń"),
    ("accounts.manage_permissions", "Nadawanie uprawnień innym osobom"),
]


def panel_required(perm=None):
    """Dostęp do panelu zarządu: zalogowany, `is_staff`, po weryfikacji dwuskładnikowej i z danym uprawnieniem."""

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
            if perm and not user.has_perm(perm):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
