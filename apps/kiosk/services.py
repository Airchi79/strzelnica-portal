"""Parowanie i uwierzytelnianie urządzeń kiosku."""

from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event

from .models import KioskDevice, token_hash

PAIRING_MINUTES = 15
COOKIE = "satok_kiosk"
MAX_PAIR_ATTEMPTS = 10  # na adres IP w ciągu godziny


@transaction.atomic
def create_device(name, by):
    device = KioskDevice.objects.create(name=name.strip()[:80], created_by=by)
    code = new_pairing_code(device, by)
    log_event("kiosk.dodanie", device, device.name, actor=by)
    return device, code


@transaction.atomic
def new_pairing_code(device, by):
    """Nowy jednorazowy kod parowania. Dotychczasowe sparowanie (token) przestaje działać."""
    code = KioskDevice.new_pairing_code()
    device.pairing_code_hash = token_hash(code)
    device.pairing_expires_at = timezone.now() + timedelta(minutes=PAIRING_MINUTES)
    device.token_hash = ""
    device.paired_at = None
    device.is_active = True
    device.save()
    log_event("kiosk.kod_parowania", device, device.name, actor=by)
    return code


def check_pair_rate(ip):
    key = f"kiosk-pair:{ip or '-'}"
    attempts = cache.get(key, 0)
    if attempts >= MAX_PAIR_ATTEMPTS:
        raise ValidationError("Zbyt wiele prób parowania. Spróbuj za godzinę.")
    cache.set(key, attempts + 1, 3600)


@transaction.atomic
def pair(code, ip=None):
    """Zamienia kod parowania na stały token urządzenia (zwracany raz — trafia do ciasteczka)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    device = (
        KioskDevice.objects.select_for_update()
        .filter(pairing_code_hash=token_hash(code), is_active=True, pairing_expires_at__gt=timezone.now())
        .first()
        if len(code) == 8 else None
    )
    if device is None:
        raise ValidationError("Nieprawidłowy lub wygasły kod parowania. Wygeneruj nowy w panelu: Książka pobytu → Kioski.")
    raw = KioskDevice.new_token()
    device.token_hash = token_hash(raw)
    device.pairing_code_hash = ""
    device.pairing_expires_at = None
    device.paired_at = timezone.now()
    device.last_ip = ip
    device.save()
    log_event("kiosk.sparowanie", device, device.name)
    return device, raw


def device_from_request(request):
    raw = request.COOKIES.get(COOKIE, "")
    if not raw:
        return None
    device = KioskDevice.objects.filter(token_hash=token_hash(raw), is_active=True).first()
    if device and (device.last_seen_at is None or timezone.now() - device.last_seen_at > timedelta(minutes=1)):
        from apps.audit.context import client_ip

        KioskDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now(), last_ip=client_ip(request))
    return device


@transaction.atomic
def revoke(device, by):
    device.is_active = False
    device.token_hash = ""
    device.pairing_code_hash = ""
    device.save()
    log_event("kiosk.wylaczenie", device, device.name, actor=by)


@transaction.atomic
def set_camera(device, camera, by):
    if camera not in KioskDevice.Camera.values:
        raise ValidationError("Nieznany wybór kamery.")
    if device.camera != camera:
        before = device.camera
        device.camera = camera
        device.save(update_fields=["camera"])
        log_event("kiosk.kamera", device, f"{device.name}: {device.get_camera_display()}", changes={"bylo": before, "jest": camera}, actor=by)
