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

    return {
        "WALLET_ENABLED": PaymentSettings.load().wallet_enabled,
        "SITE_NAME": "Strzelnica Sątok",
        "SITE_URL": settings.SITE_URL,
        "pending_regulations": pending,
        "unread_notifications": unread,
    }
