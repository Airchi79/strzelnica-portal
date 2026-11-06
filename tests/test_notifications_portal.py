"""Powiadomienia klienta pod dzwonkiem: tworzone przy wysyłce e-maili, licznik, otwieranie, rozdzielenie od panelu."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.models import Notification
from apps.core.notifications import notify_board, notify_user, purge_old_notifications

from .conftest import make_user


@pytest.fixture
def client_user(db):
    return make_user("klient@example.com")


def test_notify_user_creates_portal_notification_with_link(client_user):
    notify_user(client_user, "Wynik weryfikacji\ndokumentu", "emails/credential_reviewed.txt", credential=None)
    n = Notification.objects.get(recipient=client_user)
    assert n.channel == "portal" and n.title == "Wynik weryfikacji dokumentu" and n.url == "/pulpit/"
    notify_user(client_user, "Termin", "emails/waitlist_offer.txt", link="/rezerwacje/nowa/?os=1")
    assert Notification.objects.filter(recipient=client_user, url="/rezerwacje/nowa/?os=1").exists()


def test_verify_email_not_in_portal(client_user):
    notify_user(client_user, "Potwierdź adres e-mail", "emails/verify_email.txt", portal=False, path="/x/")
    assert not Notification.objects.filter(recipient=client_user).exists()


def test_bell_count_open_and_mark_all(client, client_user):
    notify_user(client_user, "Pierwsze", "emails/welcome.txt")
    notify_user(client_user, "Drugie", "emails/welcome.txt")
    client.force_login(client_user)
    body = client.get("/pulpit/").content.decode()
    assert 'class="bell"' in body and '<span class="dot-count">2</span>' in body and 'data-unread="2"' in body
    n = Notification.objects.filter(recipient=client_user).first()
    r = client.get(f"/powiadomienia/{n.pk}/")
    assert r.status_code == 302 and r["Location"] == "/pulpit/"
    n.refresh_from_db()
    assert n.read_at
    assert '<span class="dot-count">1</span>' in client.get("/powiadomienia/").content.decode()
    client.post("/powiadomienia/")
    assert not Notification.objects.filter(recipient=client_user, read_at__isnull=True).exists()
    assert "dot-count" not in client.get("/pulpit/").content.decode()


def test_cannot_open_foreign_or_panel_notification(client, client_user, owner):
    other = make_user("inny@example.com")
    notify_user(other, "Cudze", "emails/welcome.txt")
    notify_board("Dla zarządu", "emails/welcome.txt", url="/panel/")
    client.force_login(client_user)
    assert client.get(f"/powiadomienia/{Notification.objects.get(recipient=other).pk}/").status_code == 404
    client.force_login(owner)
    panel_n = Notification.objects.get(recipient=owner, channel="panel")
    assert client.get(f"/powiadomienia/{panel_n.pk}/").status_code == 404
    assert "Dla zarządu" not in client.get("/powiadomienia/").content.decode()


def test_unsafe_url_not_followed(client, client_user):
    n = Notification.objects.create(recipient=client_user, channel="portal", title="x", url="//zla.strona/")
    client.force_login(client_user)
    assert client.get(f"/powiadomienia/{n.pk}/")["Location"] == "/powiadomienia/"


def test_reservation_flow_notifies_client(client_user):
    """Rzeczywista ścieżka: zatwierdzenie rezerwacji tworzy powiadomienie z odnośnikiem do rezerwacji."""
    from apps.bookings.models import Reservation
    from apps.bookings.services import approve_reservation
    from apps.facilities.models import Lane

    lane = Lane.objects.filter(is_active=True).first()
    start = timezone.now() + timedelta(days=3)
    r = Reservation.objects.create(number="SATOK-R-N-1", lane=lane, user=client_user, starts_at=start, ends_at=start + timedelta(hours=1),
                                   persons=1, price_rule=lane.price_rule_for(start.date()), hourly_amount=100, amount=100,
                                   status=Reservation.Status.PENDING_APPROVAL)
    approve_reservation(r, None)
    n = Notification.objects.get(recipient=client_user)
    assert n.url == f"/rezerwacje/{r.pk}/" and "potwierdzona" in n.title


def test_purge_old(client_user):
    n = Notification.objects.create(recipient=client_user, channel="portal", title="stare")
    Notification.objects.filter(pk=n.pk).update(created_at=timezone.now() - timedelta(days=400))
    Notification.objects.create(recipient=client_user, channel="portal", title="nowe")
    assert purge_old_notifications() == 1
    assert list(Notification.objects.values_list("title", flat=True)) == ["nowe"]
