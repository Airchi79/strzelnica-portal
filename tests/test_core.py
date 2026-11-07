import pytest
from django.db import DatabaseError, transaction

from apps.audit.models import AuditLog
from apps.audit.services import log_event
from apps.core.models import AssociationSettings

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("url", ["/", "/osie/", "/regulamin/", "/konto/logowanie/", "/konto/rejestracja/", "/offline/"])
def test_public_pages(client, url):
    assert client.get(url).status_code == 200


def test_pwa_files(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest["Content-Type"] == "application/manifest+json"
    assert '"display": "standalone"' in manifest.content.decode()
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw["Content-Type"]


def test_dashboard_requires_login(client):
    assert client.get("/pulpit/").url.startswith("/konto/logowanie/")


def test_audit_log_is_append_only():
    entry = log_event("test.zdarzenie", summary="x")
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditLog.objects.filter(pk=entry.pk).update(summary="zmienione")
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditLog.objects.filter(pk=entry.pk).delete()
    assert AuditLog.objects.get(pk=entry.pk).summary == "x"


def test_settings_validation(owner_client):
    base = {"name": "Stowarzyszenie „STRZELNICA SĄTOK”", "address": "", "regon": "", "contact_email": "", "contact_phone": "", "notify_email": ""}
    response = owner_client.post("/panel/ustawienia/", {**base, "nip": "1234567890", "bank_account": ""})
    assert "nip" in response.context["form"].errors
    response = owner_client.post("/panel/ustawienia/", {**base, "nip": "", "bank_account": "12 1020 1026 0000 0000 0000 0000"})
    assert "bank_account" in response.context["form"].errors
    # Poprawny NIP (suma kontrolna) i poprawny numer rachunku (IBAN mod 97).
    response = owner_client.post("/panel/ustawienia/", {**base, "nip": "526-000-12-46", "bank_account": "61 1090 1014 0000 0712 1981 2874"})
    assert response.status_code == 302, response.context["form"].errors
    s = AssociationSettings.load()
    assert s.nip == "5260001246" and s.bank_account == "PL61109010140000071219812874"


def test_security_headers(client):
    response = client.get("/")
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_range_location_used_for_directions(client, db):
    from apps.core.models import AssociationSettings

    a = AssociationSettings.load()
    assert a.map_url == "https://www.google.com/maps/search/?api=1&query=51.085222,17.448874"
    assert a.directions == "Sątok, 56-420 Bierutów"
    body = client.get("/").content.decode()
    assert "51.085222,17.448874" in body and "Dojazd: Sątok, 56-420 Bierutów" in body
    a.range_lat = a.range_lon = None
    a.save()
    assert "query=S%C4%85tok" in a.map_url  # bez współrzędnych — opis miejsca
