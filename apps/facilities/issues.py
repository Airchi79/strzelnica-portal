"""Zgłoszenia usterek: przyjęcie zgłoszenia (ze zdjęciem), zmiany statusu, powiadomienia."""

import io
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user

from .models import Issue

PERM = "facilities.manage_facilities"
MAX_PHOTO_SIDE = 2000


def clean_photo(f):
    """Zdjęcie: tylko obraz (sprawdzany przez Pillow), zmniejszone do 2000 px, zapisane jako JPG bez metadanych (GPS)."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(f)  # najpierw tylko nagłówek: wymiary przed rozpakowaniem (ochrona przed „bombą” w pliku)
        if img.size[0] * img.size[1] > 50_000_000:
            raise ValidationError("Zdjęcie ma za dużą rozdzielczość.")
        img.load()
    except ValidationError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValidationError("To nie jest zdjęcie (dozwolone JPG i PNG).") from e
    if img.format not in ("JPEG", "PNG", "WEBP"):
        raise ValidationError("Dozwolone są zdjęcia JPG i PNG.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return ContentFile(out.getvalue(), name="zdjecie.jpg")


MAX_REPORTS_PER_DAY = 10


@transaction.atomic
def report(user, title, description="", lane=None, urgent=False, photo=None):
    if user and Issue.objects.filter(reported_by=user, created_at__gte=timezone.now() - timedelta(days=1)).count() >= MAX_REPORTS_PER_DAY:
        raise ValidationError("Wysłano już wiele zgłoszeń w ciągu doby — jeśli sprawa jest pilna, zadzwoń do zarządu.")
    issue = Issue(reported_by=user, title=title.strip(), description=description.strip(), lane=lane, urgent=urgent)
    if photo:
        issue.photo.save("zdjecie.jpg", photo, save=False)
    issue.save()
    log_event("usterka.zgloszenie", issue, str(issue), changes={"pilne": urgent}, actor=user)
    notify_board(
        ("PILNE — usterka: " if urgent else "Usterka: ") + str(issue),
        "emails/issue_reported.txt",
        PERM,
        url=reverse("panel:issue", args=[issue.pk]),
        body=f"{user.full_name if user else 'obsługa'}: {issue.description[:200]}",
        issue=issue,
    )
    return issue


@transaction.atomic
def change_status(issue, status, by, note=""):
    issue = Issue.objects.select_for_update().get(pk=issue.pk)
    if status not in Issue.Status.values:
        raise ValidationError("Nieznany status.")
    if status in (Issue.Status.FIXED, Issue.Status.REJECTED) and not note.strip() and not issue.resolution_note:
        raise ValidationError("Opisz naprawę albo powód odrzucenia — zgłaszający dostanie tę informację.")
    before = issue.status
    issue.status = status
    if note.strip():
        issue.resolution_note = note.strip()[:500]
    if status in (Issue.Status.FIXED, Issue.Status.REJECTED):
        issue.resolved_at, issue.resolved_by = timezone.now(), by
    else:
        issue.resolved_at = issue.resolved_by = None
    issue.save()
    log_event("usterka.status", issue, f"{issue}: {before} → {status}", changes={"uwagi": issue.resolution_note}, actor=by)
    if issue.reported_by and issue.reported_by != by and status in (Issue.Status.IN_PROGRESS, Issue.Status.FIXED, Issue.Status.REJECTED):
        notify_user(issue.reported_by, f"Zgłoszenie usterki: {issue.get_status_display().lower()}", "emails/issue_status.txt", issue=issue)
    return issue
