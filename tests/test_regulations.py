import datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.regulations.models import Acceptance, RegulationDocument, RegulationVersion, pending_acceptances

pytestmark = pytest.mark.django_db


def create_and_publish(client, doc, content="§ 1\n\nTreść", effective=None):
    effective = effective or timezone.localtime() - datetime.timedelta(minutes=1)
    client.post(
        f"/panel/regulaminy/{doc.pk}/nowa/",
        {"content": content, "change_note": "", "effective_from": effective.strftime("%Y-%m-%dT%H:%M")},
    )
    version = doc.versions.order_by("-number").first()
    client.post(f"/panel/regulaminy/wersja/{version.pk}/publikuj/")
    version.refresh_from_db()
    return version


def test_seeded_documents():
    assert set(RegulationDocument.objects.values_list("slug", flat=True)) == {"regulamin-strzelnicy", "regulamin-rezerwacji", "rodo", "regulamin-stowarzyszenia"}


def test_publish_requires_acceptance_and_new_version_requires_it_again(owner_client, client, user):
    doc = RegulationDocument.objects.get(slug="regulamin-strzelnicy")
    v1 = create_and_publish(owner_client, doc)
    assert v1.number == 1 and v1.is_published
    assert pending_acceptances(user) == [v1]

    from django.test import Client

    c = Client()
    c.force_login(user)
    assert "Obowiązują nowe dokumenty" in c.get("/pulpit/").content.decode()
    c.post("/regulamin/akceptacja/", {"confirm": "on"})
    assert pending_acceptances(user) == []
    assert Acceptance.objects.get(user=user, version=v1).ip == "127.0.0.1"

    v2 = create_and_publish(owner_client, doc, content="§ 1\n\nNowa treść")
    assert v2.number == 2
    assert pending_acceptances(user) == [v2]


def test_future_version_not_required_yet(owner_client, user):
    doc = RegulationDocument.objects.get(slug="rodo")
    create_and_publish(owner_client, doc, effective=timezone.localtime() + datetime.timedelta(days=3))
    assert pending_acceptances(user) == []


def test_published_version_is_immutable(owner_client):
    doc = RegulationDocument.objects.get(slug="rodo")
    version = create_and_publish(owner_client, doc)
    version.content = "zmiana"
    with pytest.raises(ValidationError):
        version.save()
    with pytest.raises(ValidationError):
        RegulationVersion.objects.get(pk=version.pk).delete()
    response = owner_client.get(f"/panel/regulaminy/{doc.pk}/wersja/{version.pk}/")
    assert response.status_code == 302
    owner_client.post(f"/panel/regulaminy/wersja/{version.pk}/usun/")
    assert RegulationVersion.objects.filter(pk=version.pk).exists()


def test_draft_not_public(client, owner_client):
    doc = RegulationDocument.objects.get(slug="rodo")
    owner_client.post(
        f"/panel/regulaminy/{doc.pk}/nowa/",
        {"content": "szkic", "change_note": "", "effective_from": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
    )
    draft = doc.versions.get()
    assert client.get(f"/regulamin/wersja/{draft.pk}/").status_code == 404


def test_regulation_text_is_escaped(owner_client, client):
    doc = RegulationDocument.objects.get(slug="rodo")
    version = create_and_publish(owner_client, doc, content="<script>alert(1)</script>")
    body = client.get(f"/regulamin/wersja/{version.pk}/").content.decode()
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


def test_regulation_text_links_only_https_and_stays_escaped():
    from apps.core.templatetags.satok import regulation_text

    html = str(regulation_text('DPA: https://www.cloudflare.com/cloudflare-customer-dpa/.\n'
                               'x https://x.pl/"onmouseover=alert(1) javascript:alert(1) http://a.pl <b>b</b>'))
    assert '<a href="https://www.cloudflare.com/cloudflare-customer-dpa/" target="_blank" rel="noopener noreferrer">' in html
    assert "</a>." in html  # kropka kończąca zdanie poza linkiem
    assert 'onmouseover=alert(1)' not in html.replace("&quot;onmouseover=alert(1", "")  # cudzysłów zamieniony na encję
    assert 'href="http://a.pl' not in html and 'href="javascript' not in html and "<b>" not in html


def test_public_list_shows_acceptance_state(client):
    from .conftest import make_user

    doc = RegulationDocument.objects.get(slug="regulamin-strzelnicy")
    v = RegulationVersion.objects.create(document=doc, number=99, content="§ 1. Test", effective_from=timezone.now() - datetime.timedelta(minutes=1),
                                         published_at=timezone.now())
    anon = client.get("/regulamin/").content.decode()
    assert "Do akceptacji" not in anon and "Zaakceptowano" not in anon  # niezalogowany — bez stanów
    u = make_user("akcept@example.com")
    Acceptance.objects.filter(user=u).delete()
    client.force_login(u)
    body = client.get("/regulamin/").content.decode()
    assert "Do akceptacji" in body and "Przeczytaj i zaakceptuj" in body
    assert "Dokument w przygotowaniu." in body or "Do zapoznania" in body
    Acceptance.objects.create(user=u, version=v)
    body = client.get("/regulamin/").content.decode()
    assert "Zaakceptowano" in body
