import datetime

from django.db import migrations

# Stan strzelnicy na 30.10.2026: 5 osi, wszystkie otwarte 9–18, cennik ustalony przez zarząd.
LANES = [
    # nazwa, typ, długość, tylko na wyłączność, stawka za osobę/h [gr], ryczałt/h [gr]
    ("Oś 25 m nr 1", "krotka", 25, False, 3500, 10000),
    ("Oś 25 m nr 2", "krotka", 25, False, 3500, 10000),
    ("Oś 35 m", "krotka", 35, False, 3500, 10000),
    ("Oś 100 m", "dluga", 100, False, 6000, 20000),
    ("Oś dynamiczno-myśliwska", "dynamiczna", None, True, None, 15000),
]


def seed(apps, schema_editor):
    Facility = apps.get_model("facilities", "Facility")
    Lane = apps.get_model("facilities", "Lane")
    OpeningHours = apps.get_model("facilities", "OpeningHours")
    PriceRule = apps.get_model("facilities", "PriceRule")
    if Facility.objects.exists():
        return
    facility = Facility.objects.create(name="Strzelnica kulowa", kind="kulowa", order=1)
    Facility.objects.create(name="Strzelnica śrutowa", kind="srutowa", order=2, is_active=False)
    for order, (name, kind, length, exclusive, per_person, flat) in enumerate(LANES, start=1):
        lane = Lane.objects.create(
            facility=facility, name=name, kind=kind, length_m=length, exclusive_only=exclusive, order=order
        )
        for weekday in range(7):
            OpeningHours.objects.create(lane=lane, weekday=weekday, opens=datetime.time(9), closes=datetime.time(18))
        PriceRule.objects.create(lane=lane, per_person_hourly=per_person, flat_hourly=flat, valid_from=datetime.date(2026, 1, 1))


class Migration(migrations.Migration):
    dependencies = [("facilities", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
