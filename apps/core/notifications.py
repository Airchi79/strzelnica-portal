"""Wysyłka powiadomień e-mail. Wiadomości wychodzą dopiero po zatwierdzeniu transakcji."""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _send(subject, template, context, recipients):
    subject = " ".join(str(subject).split())[:200]  # jedna linia: treści od użytkowników nie mogą zepsuć nagłówka
    recipients = sorted({r for r in recipients if r})
    if not recipients:
        return
    context = {"site_url": settings.SITE_URL, **context}
    body = render_to_string(template, context)

    def deliver():
        try:
            send_mail(f"[Strzelnica Sątok] {subject}", body, settings.DEFAULT_FROM_EMAIL, recipients)
        except Exception:  # błąd poczty nie może cofnąć operacji użytkownika
            logger.exception("Nie udało się wysłać wiadomości %r do %s", subject, recipients)

    transaction.on_commit(deliver)


def notify_user(user, subject, template, **context):
    _send(subject, template, {"user": user, **context}, [user.email])


def board_users(permission_codename=None):
    """Aktywni członkowie zarządu z danym uprawnieniem (właściciel zawsze)."""
    User = get_user_model()
    staff = User.objects.filter(is_active=True, is_staff=True)
    if permission_codename:
        app_label, codename = permission_codename.split(".")
        perm_q = Q(user_permissions__codename=codename, user_permissions__content_type__app_label=app_label) | Q(
            groups__permissions__codename=codename, groups__permissions__content_type__app_label=app_label
        )
        staff = staff.filter(Q(is_superuser=True) | perm_q)
    return staff.distinct()


def board_recipients(permission_codename=None):
    """Adres powiadomień stowarzyszenia oraz aktywni członkowie zarządu z danym uprawnieniem."""
    from apps.core.models import AssociationSettings

    emails = {AssociationSettings.load().notify_email}
    emails.update(board_users(permission_codename).values_list("email", flat=True))
    return emails


def notify_board(subject, template, permission_codename=None, url="", body="", **context):
    """E-mail do zarządu oraz powiadomienie w panelu (gdy podano `url` lub `body`)."""
    from apps.core.models import Notification

    if url or body:
        subject = " ".join(str(subject).split())
        Notification.objects.bulk_create(
            Notification(recipient=u, title=subject[:160], body=body[:500], url=url[:255])
            for u in board_users(permission_codename)
        )
    _send(subject, template, context, board_recipients(permission_codename))
