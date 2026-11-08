"""Częste pytania: strona publiczna, zarządzanie w panelu, uprawnienia."""

import pytest

from apps.core.models import FaqItem

from .conftest import login_with_otp, make_user

pytestmark = pytest.mark.django_db


def test_public_page_shows_published_only(client):
    assert FaqItem.objects.count() >= 10  # pytania początkowe z migracji
    hidden = FaqItem.objects.create(question="Ukryte pytanie?", answer="x", is_published=False)
    body = client.get("/pytania/").content.decode()
    assert "Kto może zarezerwować oś?" in body and hidden.question not in body
    assert 'href="/pytania/"' in body  # link w menu / stopce


def test_answer_is_escaped(client):
    FaqItem.objects.create(question="Test <b>?", answer="<script>alert(1)</script>\n\nDrugi akapit")
    body = client.get("/pytania/").content.decode()
    assert "<script>alert(1)" not in body and "&lt;script&gt;" in body and "Drugi akapit" in body


def test_panel_crud(owner_client):
    assert owner_client.get("/panel/pytania/").status_code == 200
    r = owner_client.post("/panel/pytania/nowe/", {"question": "Czy jest parking?", "answer": "Tak, przy wjeździe.", "order": 1, "is_published": "on"})
    assert r.status_code == 302
    item = FaqItem.objects.get(question="Czy jest parking?")
    owner_client.post(f"/panel/pytania/{item.pk}/", {"question": "Czy jest parking?", "answer": "Tak.", "order": 1})
    item.refresh_from_db()
    assert item.answer == "Tak." and not item.is_published
    owner_client.post(f"/panel/pytania/{item.pk}/", {"action": "delete"})
    assert not FaqItem.objects.filter(pk=item.pk).exists()


def test_panel_requires_permission(client):
    u = make_user("bezfaq@example.com", is_staff=True)
    login_with_otp(client, u)
    assert client.get("/panel/pytania/").status_code == 403


def test_faq_describes_checkout_flow(db):
    from apps.core.models import FaqItem

    pay = FaqItem.objects.get(question="Jak mogę zapłacić?").answer
    assert "5 minut" in pay and "Przelewem tradycyjnym" in pay and "przypomnimy" in pay
    assert "wybierz sposób płatności" in FaqItem.objects.get(question="Jak zarezerwować oś?").answer
