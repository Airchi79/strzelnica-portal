from django.conf import settings


def site(request):
    pending = []
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from apps.regulations.models import pending_acceptances

        pending = pending_acceptances(user)
    return {"SITE_NAME": "Strzelnica Sątok", "SITE_URL": settings.SITE_URL, "pending_regulations": pending}
