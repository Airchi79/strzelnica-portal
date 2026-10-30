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
    assert set(RegulationDocument.objects.values_list("slug", flat=True)) == {"regulamin-strzelnicy", "regulamin-rezerwacji", "rodo"}


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
