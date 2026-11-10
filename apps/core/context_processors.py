from django.conf import settings
from django.db.models import Count


def site(request):
    pending, unread, unread_portal = [], 0, 0
    user = getattr(request, "user", None)
    panel_account = bool(user is not None and user.is_authenticated and (user.is_staff or user.is_superuser))
    if user is not None and user.is_authenticated:
        from apps.regulations.models import pending_acceptances

        # Konto panelu nie jest kontem strzelca — regulaminy strzelca go nie dotyczą.
        pending = [] if panel_account else pending_acceptances(user)
        counts = dict(user.notifications.filter(read_at__isnull=True).values_list("channel").annotate(n=Count("pk")))
        unread_portal = counts.get("portal", 0)
        if user.is_staff:
            unread = counts.get("panel", 0)
    from apps.payments.models import PaymentSettings, Wallet

    wallet_on = PaymentSettings.load().wallet_enabled
    header_balance = None
    if wallet_on and user is not None and user.is_authenticated and not panel_account:
        header_balance = Wallet.objects.filter(user=user).values_list("balance", flat=True).first() or 0

    from .announcements import active
    from .branding import current_urls

    return {
        "ANNOUNCEMENTS": sorted(active().only("pk", "title", "body", "level", "visible_from")[:5],
                                key=lambda a: ({"zamkniete": 0, "uwaga": 1}.get(a.level, 2), -a.visible_from.timestamp()))[:3],
        "BRAND": current_urls(),
        "WALLET_ENABLED": wallet_on,
        "header_balance": header_balance,
        "SITE_NAME": "Strzelnica Sątok",
        "TEST_COPY": settings.IS_TEST_COPY,
        "SITE_URL": settings.SITE_URL,
        "pending_regulations": pending,
        "PANEL_ACCOUNT": panel_account,
        # „Moje konto” (profil, zmiana hasła) dla konta panelu — w wyglądzie panelu
        "ACCOUNT_BASE": "panel/base_panel.html" if panel_account else "base.html",
        "unread_notifications": unread,
        "unread_portal": unread_portal,
    }
