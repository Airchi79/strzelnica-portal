from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user

from .models import Credential

KEY_FIELDS = ["kind", "number", "issuer", "issued_on", "valid_until", "details"]


def delete_scan(credential):
    if credential.scan:
        credential.scan.delete(save=False)
        credential.scan = ""


def submit(credential, changed_fields=None):
    """Zapis dokumentu przez strzelca. Zmiana danych już zweryfikowanego dokumentu przywraca weryfikację."""
    is_new = credential.pk is None
    if is_new or (changed_fields and set(changed_fields) & set(KEY_FIELDS + ["scan"])):
        credential.status = Credential.Status.PENDING
        credential.reviewed_by = None
        credential.reviewed_at = None
        credential.review_note = ""
    credential.save()
    log_event(
        "dokument.dodanie" if is_new else "dokument.zmiana",
        credential,
        str(credential),
        changes={"pola": sorted(changed_fields or [])},
    )
    if credential.status == Credential.Status.PENDING:
        notify_board(
            "Nowy dokument do weryfikacji",
            "emails/credential_submitted.txt",
            permission_codename="credentials.review_credential",
            url=reverse("panel:credential", args=[credential.pk]),
            body=f"{credential.user.full_name}: {credential.get_kind_display()}",
            credential=credential,
        )


@transaction.atomic
def review(credential, reviewer, approve, note=""):
    credential = Credential.objects.select_for_update().get(pk=credential.pk)
    if credential.status != Credential.Status.PENDING:
        raise ValueError("Ten dokument został już rozpatrzony.")
    credential.status = Credential.Status.VERIFIED if approve else Credential.Status.REJECTED
    if approve and credential.is_expired_by_date:
        credential.status = Credential.Status.EXPIRED
    credential.review_note = note.strip()
    credential.reviewed_by = reviewer
    credential.reviewed_at = timezone.now()
    # Minimalizacja danych: skan służy wyłącznie do weryfikacji i jest usuwany po decyzji.
    had_scan = bool(credential.scan)
    path = credential.scan.name if had_scan else None
    credential.scan = ""
    credential.save()
    if had_scan:
        transaction.on_commit(lambda: Credential.scan.field.storage.delete(path))
    log_event(
        "dokument.weryfikacja",
        credential,
        f"{credential} → {credential.get_status_display()}",
        changes={"status": credential.status, "uwagi": credential.review_note, "skan_usuniety": had_scan},
        actor=reviewer,
    )
    notify_user(credential.user, "Wynik weryfikacji dokumentu", "emails/credential_reviewed.txt", credential=credential)
    return credential


def expire_outdated(today=None):
    """Oznacza jako wygasłe zweryfikowane dokumenty po terminie ważności. Zwraca liczbę zmienionych."""
    today = today or timezone.localdate()
    count = 0
    for credential in Credential.objects.filter(status=Credential.Status.VERIFIED, valid_until__lt=today).select_related("user"):
        credential.status = Credential.Status.EXPIRED
        credential.save(update_fields=["status", "updated_at"])
        log_event("dokument.wygasniecie", credential, str(credential))
        notify_user(credential.user, "Dokument stracił ważność", "emails/credential_expired.txt", credential=credential)
        count += 1
    return count
