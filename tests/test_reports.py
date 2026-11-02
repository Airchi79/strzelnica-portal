from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.clients import services as client_services
from apps.payments import services as payment_services
from apps.payments.models import Payment
from apps.reports import services
from apps.reports.models import SalesReport

from .conftest import grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_user(db):
    return make_user("klient@example.com", first_name="Kasia", last_name="Klientka")


def _at(day, hour=12):
    return timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time())) + timedelta(hours=hour)


def _pay(user, owner, amount, method, day, hour=12):
    p = payment_services.register_manual_payment(user, amount, method, owner)
    Payment.objects.filter(pk=p.pk).update(completed_at=_at(day, hour))
    return p


def test_daily_report_contents_numbering_and_immutability(client_user, owner):
    day = timezone.localdate() - timedelta(days=2)
    _pay(client_user, owner, 7000, Payment.Method.CASH, day, 10)
    _pay(client_user, owner, 3500, Payment.Method.TRANSFER, day, 15)
    _pay(client_user, owner, 1000, Payment.Method.CASH, day + timedelta(days=1))  # inny dzień
    cp = client_services.issue_pass(client_user, kind="karnet", entries_limit=5, valid_from=day, valid_until=day + timedelta(days=30),
                                    by=owner, price=15000, paid_with="karta")
    type(cp).objects.filter(pk=cp.pk).update(paid_at=_at(day, 11))
    client_services.issue_pass(client_user, kind="karnet", entries_limit=5, valid_from=day, valid_until=day + timedelta(days=30),
                               by=owner, price=0, paid_with="bezplatny")  # bez wartości sprzedaży
    report, new = services.generate(SalesReport.Kind.DAILY, day, by=owner)
    assert new and report.number == f"RS/D/{day.year}/0001" and report.integrity_ok
    s = report.data["summary"]
    assert s["count"] == 3 and s["inflow"] == 25500 and s["net"] == 25500
    assert s["by_method"] == {"gotowka": 7000, "przelew": 3500, "karta": 15000}
    assert s["by_category"] == {"inne": 10500, "kod_wejscia": 15000}
    again, new = services.generate(SalesReport.Kind.DAILY, day)
    assert not new and again.pk == report.pk
    second, _ = services.generate(SalesReport.Kind.DAILY, day + timedelta(days=1))
    assert second.number == f"RS/D/{day.year}/0002"
    with pytest.raises(DatabaseError), transaction.atomic():
        SalesReport.objects.filter(pk=report.pk).update(data={})
    with pytest.raises(DatabaseError), transaction.atomic():
        SalesReport.objects.filter(pk=report.pk).delete()


def test_open_period_rejected():
    with pytest.raises(ValidationError, match="zakończeniu"):
        services.generate(SalesReport.Kind.DAILY, timezone.localdate())
    today = timezone.localdate()
    with pytest.raises(ValidationError):
        services.generate(SalesReport.Kind.MONTHLY, today.replace(day=1))


def test_generate_missing_and_monthly_lists_dailies(client_user, owner):
    today = timezone.localdate()
    last_month_end = today.replace(day=1) - timedelta(days=1)
    d1, d2 = last_month_end.replace(day=3), last_month_end.replace(day=20)
    _pay(client_user, owner, 5000, Payment.Method.CASH, d1)
    _pay(client_user, owner, 2500, Payment.Method.TRANSFER, d2)
    created = services.generate_missing()
    kinds = [r.kind for r in created]
    assert kinds.count("D") == 2 and kinds.count("M") == 1
    monthly = SalesReport.objects.get(kind="M")
    assert [d["day"] for d in monthly.data["daily"]] == [d1.isoformat(), d2.isoformat()]
    assert monthly.data["summary"]["net"] == 7500 and monthly.number == f"RS/M/{d1.year}/0001"
    assert services.generate_missing() == []  # idempotentnie


def test_pdf_and_csv(owner_client, client_user, owner):
    day = timezone.localdate() - timedelta(days=1)
    _pay(client_user, owner, 7000, Payment.Method.CASH, day)
    report, _ = services.generate(SalesReport.Kind.DAILY, day)
    pdf = owner_client.get(f"/panel/raporty-sprzedazy/{report.pk}/pdf/")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and len(pdf.content) > 1000
    csv_body = owner_client.get(f"/panel/raporty-sprzedazy/{report.pk}/csv/").content.decode("utf-8")
    assert "Kasia Klientka" in csv_body and "70,00" in csv_body and report.sha256 in csv_body
    page = owner_client.get(f"/panel/raporty-sprzedazy/{report.pk}/").content.decode()
    assert report.number in page and "dane bez zmian" in page
    assert report.number in owner_client.get("/panel/raporty-sprzedazy/").content.decode()


def test_panel_generate_and_permissions(owner_client, client_user, owner):
    from django.test import Client

    day = timezone.localdate() - timedelta(days=3)
    _pay(client_user, owner, 1000, Payment.Method.CASH, day)
    response = owner_client.post("/panel/raporty-sprzedazy/generuj/", {"rodzaj": "D", "dzien": day.isoformat()})
    assert response.status_code == 302 and SalesReport.objects.filter(kind="D", period_start=day).exists()
    response = owner_client.post("/panel/raporty-sprzedazy/generuj/", {"rodzaj": "D", "dzien": timezone.localdate().isoformat()}, follow=True)
    assert "po zakończeniu okresu" in response.content.decode()
    staff = grant(make_user("s@example.com"), "logbook.view_logbook")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/raporty-sprzedazy/").status_code == 403
    viewer = grant(make_user("r@example.com"), "reports.view_salesreport")
    c = Client()
    login_with_otp(c, viewer)
    assert c.get("/panel/raporty-sprzedazy/").status_code == 200
