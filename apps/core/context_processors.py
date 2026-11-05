from django.conf import settings


def site(request):
    pending, unread = [], 0
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from apps.regulations.models import pending_acceptances

        pending = pending_acceptances(user)
        if user.is_staff:
            unread = user.notifications.filter(read_at__isnull=True).count()
    from apps.payments.models import PaymentSettings

    from .announcements import active
    from .branding import current_urls

    return {
        "ANNOUNCEMENTS": sorted(active().only("pk", "title", "body", "level", "visible_from")[:5],
                                key=lambda a: ({"zamkniete": 0, "uwaga": 1}.get(a.level, 2), -a.visible_from.timestamp()))[:3],
        "BRAND": current_urls(),
        "WALLET_ENABLED": PaymentSettings.load().wallet_enabled,
        "SITE_NAME": "Strzelnica Sątok",
        "SITE_URL": settings.SITE_URL,
        "pending_regulations": pending,
        "unread_notifications": unread,
    }
