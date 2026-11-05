from datetime import timedelta

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
    if changed_fields and "valid_until" in changed_fields:
        credential.expiry_notice_days = None  # nowa data ważności — przypomnienia od nowa
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


@transaction.atomic
def revoke(credential, actor, reason):
    """Cofnięcie weryfikacji (np. utrata uprawnień, pomyłka przy weryfikacji). Dokument wraca jako odrzucony,
    strzelec może go poprawić i wysłać ponownie. Istniejące rezerwacje nie są anulowane automatycznie."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Podaj powód cofnięcia weryfikacji.")
    credential = Credential.objects.select_for_update().get(pk=credential.pk)
    if credential.status != Credential.Status.VERIFIED:
        raise ValueError("Cofnąć można tylko weryfikację dokumentu o statusie „Zweryfikowany”.")
    previous = {"zweryfikowal": credential.reviewed_by.full_name if credential.reviewed_by else "",
                "data": credential.reviewed_at.isoformat() if credential.reviewed_at else ""}
    credential.status = Credential.Status.REJECTED
    credential.review_note = f"Weryfikacja cofnięta: {reason}"[:255]
    credential.reviewed_by = actor
    credential.reviewed_at = timezone.now()
    credential.save()
    log_event(
        "dokument.cofniecie_weryfikacji",
        credential,
        f"{credential} → weryfikacja cofnięta",
        changes={"powod": reason, "poprzednia_weryfikacja": previous},
        actor=actor,
    )
    notify_user(credential.user, "Cofnięto weryfikację dokumentu", "emails/credential_revoked.txt", credential=credential, reason=reason)
    return credential


EXPIRY_NOTICES = (30, 7)  # dni przed końcem ważności


def remind_expiring(today=None):
    """E-mail do właściciela 30 i 7 dni przed końcem ważności zweryfikowanego dokumentu (każde przypomnienie raz).
    Zwraca liczbę wysłanych przypomnień."""
    today = today or timezone.localdate()
    sent = 0
    horizon = today + timedelta(days=max(EXPIRY_NOTICES))
    qs = Credential.objects.filter(status=Credential.Status.VERIFIED, valid_until__gte=today, valid_until__lte=horizon).select_related("user")
    for credential in qs:
        days_left = (credential.valid_until - today).days
        due = min((d for d in EXPIRY_NOTICES if days_left <= d), default=None)  # najbliższy próg, który już minął
        if due is None or (credential.expiry_notice_days is not None and credential.expiry_notice_days <= due):
            continue
        credential.expiry_notice_days = due
        credential.save(update_fields=["expiry_notice_days", "updated_at"])
        log_event("dokument.przypomnienie", credential, f"{credential} — wygasa za {days_left} dni")
        notify_user(credential.user, f"Dokument wygasa za {days_left} dni", "emails/credential_expiring.txt",
                    credential=credential, days_left=days_left)
        sent += 1
    return sent


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
