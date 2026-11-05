import re

import pytest

from apps.bookings import feed
from apps.bookings.models import CalendarFeed

from .conftest import add_credential, approve, grant, make_user

pytestmark = pytest.mark.django_db


def test_feed_link_lifecycle_and_content(owner_client, owner, client):
    from apps.bookings import services
    from apps.facilities.models import Lane
    from django.utils import timezone
    import datetime as dt

    ps = make_user("ps-feed@example.com", first_name="Tajny", last_name="Nazwiskowski", phone="600999888")
    add_credential(ps)
    approve(ps)
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + dt.timedelta(days=4)
    while lane.hours_for(day) is None:
        day += dt.timedelta(days=1)
    r = services.create_reservation(ps, lane, day, lane.hours_for(day).opens.hour, lane.hours_for(day).opens.hour + 1, 3)
    page = owner_client.post("/panel/rezerwacje/kalendarz-w-telefonie/")
    url = page.context["url"]
    token = re.search(r"/kalendarz/(.+)\.ics$", url).group(1)
    assert CalendarFeed.objects.get(user=owner).token_hash != token  # przechowujemy tylko skrót
    body = client.get(f"/kalendarz/{token}.ics").content.decode()
    assert body.startswith("BEGIN:VCALENDAR") and r.number in body and "3 os." in body
    assert "Nazwiskowski" not in body and "600999888" not in body and ps.email not in body  # bez danych osobowych
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    # nowy link unieważnia stary, unieważnienie wyłącza
    owner_client.post("/panel/rezerwacje/kalendarz-w-telefonie/")
    assert client.get(f"/kalendarz/{token}.ics").status_code == 404
    owner_client.post("/panel/rezerwacje/kalendarz-w-telefonie/", {"action": "revoke"})
    assert not CalendarFeed.objects.filter(user=owner).exists()


def test_feed_stops_when_permission_removed(client):
    staff = grant(make_user("z-feed@example.com"), "bookings.manage_reservations")
    token = feed.new_token(staff)
    assert client.get(f"/kalendarz/{token}.ics").status_code == 200
    staff.user_permissions.clear()
    staff.is_staff = False
    staff.save()
    assert client.get(f"/kalendarz/{token}.ics").status_code == 404
    assert client.get("/kalendarz/zgadywany-token.ics").status_code == 404
