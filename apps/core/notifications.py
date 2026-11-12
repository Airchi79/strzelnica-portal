"""Wysyłka powiadomień e-mail. Wiadomości wychodzą dopiero po zatwierdzeniu transakcji."""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _send(subject, template, context, recipients):
    """Zapisuje wiadomość w skrzynce nadawczej (w transakcji operacji) i wysyła ją po zatwierdzeniu transakcji.
    Wycofana operacja = brak wiadomości; błąd serwera poczty = ponowienie przez harmonogram (send_outbox)."""
    from apps.core.models import OutgoingEmail

    subject = " ".join(str(subject).split())[:200]  # jedna linia: treści od użytkowników nie mogą zepsuć nagłówka
    recipients = sorted({r for r in recipients if r})
    if not recipients:
        return
    from apps.payments.gateway import active_name

    context = {"site_url": settings.SITE_URL, "online_provider": active_name(), **context}
    body = render_to_string(template, context)
    msg = OutgoingEmail.objects.create(recipients=recipients, subject=f"[Strzelnica Sątok] {subject}", body=body)
    transaction.on_commit(lambda: deliver(msg.pk))


# Ponowienia: po 1, 5, 15, 60 min, 3 h, 6 h, 12 h — potem wiadomość oznaczana jako nieudana (log błędu → alarm dla zarządu).
RETRY_DELAYS = [timedelta(minutes=m) for m in (1, 5, 15, 60, 180, 360, 720)]
OUTBOX_RETENTION_DAYS = 30


def deliver(pk, now=None):
    """Wysyła jedną wiadomość ze skrzynki nadawczej. Błąd poczty nie cofa operacji użytkownika — wiadomość czeka na ponowienie."""
    from django.utils import timezone

    from apps.core.models import OutgoingEmail

    now = now or timezone.now()
    with transaction.atomic():
        msg = OutgoingEmail.objects.select_for_update(skip_locked=True).filter(pk=pk, status=OutgoingEmail.Status.PENDING).first()
        if msg is None:
            return None
        try:
            send_mail(msg.subject, msg.body, settings.DEFAULT_FROM_EMAIL, msg.recipients)
        except Exception as e:  # noqa: BLE001 — każdy błąd poczty: ponowienie
            msg.attempts += 1
            msg.last_error = f"{type(e).__name__}: {e}"[:255]
            if msg.attempts > len(RETRY_DELAYS):
                msg.status, msg.next_attempt_at = OutgoingEmail.Status.FAILED, None
                logger.error("E-mail %s (%r do %s) nie wysłany po %s próbach: %s", msg.pk, msg.subject, msg.recipients,
                             msg.attempts, msg.last_error)
            else:
                msg.next_attempt_at = now + RETRY_DELAYS[msg.attempts - 1]
                logger.warning("E-mail %s (%r): błąd wysyłki, ponowienie o %s", msg.pk, msg.subject, msg.next_attempt_at)
            msg.save(update_fields=["attempts", "last_error", "status", "next_attempt_at"])
            return False
        msg.status, msg.sent_at, msg.next_attempt_at = OutgoingEmail.Status.SENT, now, None
        msg.save(update_fields=["status", "sent_at", "next_attempt_at"])
        return True


def send_outbox(now=None):
    """Harmonogram (co minutę): wiadomości czekające na ponowienie oraz niewysłane po zatwierdzeniu (np. restart).
    Usuwa wysłane starsze niż 30 dni. Zwraca (wysłane, nieudane_teraz)."""
    from django.utils import timezone

    from apps.core.models import OutgoingEmail

    now = now or timezone.now()
    due = OutgoingEmail.objects.filter(status=OutgoingEmail.Status.PENDING).filter(
        Q(next_attempt_at__lte=now) | Q(next_attempt_at__isnull=True, created_at__lte=now - timedelta(minutes=2))
    ).order_by("created_at").values_list("pk", flat=True)[:200]
    sent = failed = 0
    for pk in list(due):
        result = deliver(pk, now)
        sent += result is True
        failed += result is False
    OutgoingEmail.objects.filter(status=OutgoingEmail.Status.SENT, sent_at__lt=now - timedelta(days=OUTBOX_RETENTION_DAYS)).delete()
    return sent, failed


def _portal_link(template, context):
    """Strona w portalu, której dotyczy wiadomość (powiadomienie pod dzwonkiem prowadzi prosto do niej)."""
    from django.urls import reverse

    if context.get("link"):
        return context["link"]
    if context.get("reservation") is not None:
        return reverse("bookings:detail", args=[context["reservation"].pk])
    if context.get("reservations") is not None:
        return reverse("bookings:my_list")
    if context.get("payment") is not None or "balance" in context:
        return reverse("payments:wallet")
    if context.get("credential") is not None:
        return reverse("credentials:my_list")
    if context.get("reg") is not None:
        return reverse("events:detail", args=[context["reg"].event_id])
    if context.get("issue") is not None:
        return reverse("core:issues")
    if template.startswith(("emails/application", "emails/member", "emails/fee")):
        return reverse("association:membership")
    return reverse("core:dashboard")


def notify_user(user, subject, template, portal=True, **context):
    """E-mail do użytkownika oraz powiadomienie pod dzwonkiem w portalu (`portal=False` — tylko e-mail)."""
    _send(subject, template, {"user": user, **context}, [user.email])
    if portal and user.pk and user.is_active:
        from apps.core.models import Notification

        from . import webpush

        link = _portal_link(template, context)[:255]
        Notification.objects.create(recipient=user, channel=Notification.Channel.PORTAL,
                                    title=" ".join(str(subject).split())[:160], url=link)
        webpush.send([user], subject, url=link, channel="portal")


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
        from . import webpush

        subject = " ".join(str(subject).split())
        users = list(board_users(permission_codename))
        Notification.objects.bulk_create(
            Notification(recipient=u, channel=Notification.Channel.PANEL, title=subject[:160], body=body[:500], url=url[:255])
            for u in users
        )
        webpush.send(users, subject, body=body, url=url, channel="panel")
    _send(subject, template, context, board_recipients(permission_codename))


NOTIFICATION_RETENTION_DAYS = 365


def purge_old_notifications(now=None):
    """Usuwa powiadomienia starsze niż rok (minimalizacja danych) — treść i tak została wysłana e-mailem."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.core.models import Notification

    now = now or timezone.now()
    return Notification.objects.filter(created_at__lt=now - timedelta(days=NOTIFICATION_RETENTION_DAYS)).delete()[0]
