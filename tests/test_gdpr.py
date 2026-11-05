import json

import pytest
from django.utils import timezone

from apps.accounts import gdpr
from apps.accounts.models import User
from apps.audit.models import AuditLog, SensitiveAccessLog
from apps.credentials.models import Credential

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def test_user_exports_own_data(client, owner):
    u = make_user("eksport@example.com", first_name="Ewa", last_name="Eksportowa", phone="600111222")
    add_credential(u, number="PS-9")
    SensitiveAccessLog.objects.create(actor=owner, subject=u, resource="konto:x", reason="weryfikacja")
    client.force_login(u)
    assert client.get("/konto/profil/moje-dane/").status_code == 200
    response = client.post("/konto/profil/moje-dane/")
    assert response["Content-Type"].startswith("application/json")
    data = json.loads(response.content)
    assert data["konto"]["telefon"] == "600111222" and data["dokumenty"][0]["numer"] == "PS-9"
    assert data["kto_oglądał_moje_dane"][0]["kto"] == owner.full_name
    assert AuditLog.objects.filter(action="rodo.eksport").exists()


def test_anonymize_flow_keeps_legal_records(owner_client, owner):
    from apps.bookings import services
    from apps.facilities.models import Lane
    from apps.payments.services import register_manual_payment
    import datetime as dt

    u = make_user("rodo@example.com", first_name="Robert", last_name="Rodowski", phone="600222333", city="Oleśnica")
    add_credential(u)
    approve(u)
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + dt.timedelta(days=3)
    while lane.hours_for(day) is None:
        day += dt.timedelta(days=1)
    r = services.create_reservation(u, lane, day, lane.hours_for(day).opens.hour, lane.hours_for(day).opens.hour + 1, 1)
    register_manual_payment(u, r.amount, "gotowka", owner, reservation=r)
    page = owner_client.get(f"/panel/rodo/{u.pk}/")
    assert page.status_code == 200 and page.context["blockers"]  # aktywne konto + przyszła rezerwacja
    owner_client.post(f"/panel/rodo/{u.pk}/", {"action": "deactivate", "reason": "żądanie e-mail"})
    u.refresh_from_db()
    assert not u.is_active
    assert any("przyszłe rezerwacje" in b for b in gdpr.anonymize_blockers(u))
    services.cancel_reservation(r, owner, "RODO", as_board=True)
    owner_client.post(f"/panel/rodo/{u.pk}/", {"action": "anonymize", "confirm": "zle"})
    assert User.objects.get(pk=u.pk).anonymized_at is None
    owner_client.post(f"/panel/rodo/{u.pk}/", {"action": "anonymize", "confirm": f"USUŃ {u.pk}"})
    u = User.objects.get(pk=u.pk)
    assert u.anonymized_at and u.first_name == "Usunięte" and "Rodowski" not in u.last_name
    assert u.email.endswith("@anonim.invalid") and not u.phone and not u.city and not u.has_usable_password()
    assert not Credential.objects.filter(user=u).exists()
    assert u.reservations.count() == 1 and u.payments.count() == 1  # rozliczenia zostają
    assert AuditLog.objects.filter(action="rodo.anonimizacja").exists()
    assert "Rodowski" not in owner_client.get("/panel/rodo/").content.decode()


def test_regular_staff_cannot_anonymize(client):
    from .conftest import grant, login_with_otp

    staff = grant(make_user("v@example.com"), "accounts.view_users")
    target = make_user("t@example.com")
    target.is_active = False
    target.save()
    login_with_otp(client, staff)
    assert client.post(f"/panel/rodo/{target.pk}/", {"action": "anonymize", "confirm": f"USUŃ {target.pk}"}).status_code == 403
