"""Statystyki miesiąca dla zarządu: obłożenie osi (godziny i % dostępnych), rezerwacje, książka pobytu, przychód, kluby.
Porównanie z poprzednim miesiącem. Obłożenie liczone wobec godzin otwarcia osi minus blokady administracyjne."""

from datetime import date, datetime, timedelta

from django.db.models import Count, Q
from django.utils import timezone

WEEKDAYS_SHORT = ["pn", "wt", "śr", "cz", "pt", "so", "nd"]
ACTIVE_EXCLUDE = Q(status="anulowana", late_cancellation=False)


def month_bounds(year, month):
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    tz = timezone.get_current_timezone()
    return first, nxt, timezone.make_aware(datetime.combine(first, datetime.min.time()), tz), timezone.make_aware(
        datetime.combine(nxt, datetime.min.time()), tz)


def prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _lane_capacity(lane, first, nxt, blocks):
    """Dostępne godziny osi w miesiącu: suma godzin otwarcia minus godziny objęte blokadami tej osi."""
    from apps.bookings.services import local_dt

    hours = {oh.weekday: (oh.opens.hour, oh.closes.hour) for oh in lane.opening_hours.all()}
    lane_blocks = [b for b in blocks if lane.pk in b.lane_ids]
    total, d = 0, first
    while d < nxt:
        span = hours.get(d.weekday())
        if span:
            for h in range(span[0], span[1]):
                start, end = local_dt(d, h), local_dt(d, h + 1)
                if not any(b.starts_at < end and b.ends_at > start for b in lane_blocks):
                    total += 1
        d += timedelta(days=1)
    return total


def month_stats(year, month):
    from apps.bookings.models import Block, Reservation
    from apps.facilities.models import Lane
    from apps.logbook.models import Entry
    from apps.payments.models import Payment

    first, nxt, start, end = month_bounds(year, month)
    reservations = list(
        Reservation.objects.filter(starts_at__gte=start, starts_at__lt=end).exclude(ACTIVE_EXCLUDE)
        .select_related("lane", "access_code__club")
    )
    blocks = list(Block.objects.filter(starts_at__lt=end, ends_at__gt=start).prefetch_related("lanes"))
    for b in blocks:
        b.lane_ids = {lane.pk for lane in b.lanes.all()}

    lanes = []
    for lane in Lane.objects.filter(is_active=True).prefetch_related("opening_hours").order_by("facility__order", "order"):
        booked = sum(r.hours for r in reservations if r.lane_id == lane.pk)
        capacity = _lane_capacity(lane, first, nxt, blocks)
        lanes.append({"lane": lane, "booked": booked, "capacity": capacity,
                      "pct": round(100 * booked / capacity) if capacity else None})

    heat = [[0] * 24 for _ in range(7)]
    for r in reservations:
        t = timezone.localtime(r.starts_at)
        for i in range(r.hours):
            h = (t.hour + i) % 24
            heat[t.weekday()][h] += 1
    from apps.facilities.models import OpeningHours

    open_hours = [(oh.opens.hour, oh.closes.hour) for oh in OpeningHours.objects.filter(lane__is_active=True)]
    used_hours = sorted({h for row in heat for h, v in enumerate(row) if v}
                        | {h for a, b in open_hours for h in range(a, b)}) or list(range(8, 18))
    hmin, hmax = min(used_hours), max(used_hours)
    heat_max = max((v for row in heat for v in row), default=0)

    clubs = {}
    for r in reservations:
        if r.access_code_id:
            c = clubs.setdefault(r.access_code.club_id, {"club": r.access_code.club, "hours": 0, "reservations": 0})
            c["hours"] += r.hours
            c["reservations"] += 1

    entries = Entry.objects.filter(entered_at__gte=start, entered_at__lt=end)
    entry_stats = entries.aggregate(
        n=Count("pk"), shooters=Count("pk", filter=Q(purpose=Entry.Purpose.SHOOTING)),
        people=Count("user", distinct=True),
    )
    # osoby bez konta liczone po imieniu i nazwisku
    guests = entries.filter(user__isnull=True).values("first_name", "last_name").distinct().count()

    payments = [p for p in Payment.objects.filter(status=Payment.Status.COMPLETED, completed_at__gte=start, completed_at__lt=end)
                if not p.is_test]
    by_method = {}
    for p in payments:
        by_method[p.method] = by_method.get(p.method, 0) + p.amount
    labels = dict(Payment.Method.choices)

    booked_total = sum(x["booked"] for x in lanes)
    capacity_total = sum(x["capacity"] for x in lanes)
    return {
        "year": year, "month": month, "first": first,
        "kpi": {
            "reservations": len(reservations),
            "hours": booked_total,
            "occupancy": round(100 * booked_total / capacity_total) if capacity_total else None,
            "persons": sum(r.persons for r in reservations),
            "officers": len({r.user_id for r in reservations}),
            "entries": entry_stats["n"],
            "shooters": entry_stats["shooters"],
            "people": (entry_stats["people"] or 0) + guests,
            "income": sum(by_method.values()),
        },
        "lanes": lanes,
        "heat": [(WEEKDAYS_SHORT[d], [(h, heat[d][h], round(100 * heat[d][h] / heat_max) if heat_max else 0)
                                      for h in range(hmin, hmax + 1)]) for d in range(7)],
        "heat_hours": list(range(hmin, hmax + 1)),
        "heat_max": heat_max,
        "clubs": sorted(clubs.values(), key=lambda c: -c["hours"]),
        "income": sorted(((labels.get(m, m), v) for m, v in by_method.items()), key=lambda x: -x[1]),
        "cancelled_late": sum(1 for r in reservations if r.status == "anulowana"),
        "no_show": sum(1 for r in reservations if r.status == "nieodbyta"),
    }


def with_comparison(year, month):
    cur = month_stats(year, month)
    py, pm = prev_month(year, month)
    prev = month_stats(py, pm)
    deltas = {}
    for k, v in cur["kpi"].items():
        p = prev["kpi"].get(k)
        if v is None or p is None:
            deltas[k] = None
        elif k == "occupancy":
            deltas[k] = {"abs": v - p, "pct": None}
        else:
            deltas[k] = {"abs": v - p, "pct": round(100 * (v - p) / p) if p else None}
    prev_lanes = {x["lane"].pk: x for x in prev["lanes"]}
    for x in cur["lanes"]:
        p = prev_lanes.get(x["lane"].pk)
        x["prev_pct"] = p["pct"] if p else None
        x["delta"] = (x["pct"] - p["pct"]) if p and p["pct"] is not None and x["pct"] is not None else None
    cur["prev"] = prev
    cur["deltas"] = deltas
    return cur


WEEKDAYS_FULL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
OCCUPANCY_PERIODS = {"30": ("ostatnie 30 dni", -30), "90": ("ostatnie 90 dni", -90), "365": ("ostatni rok", -365),
                     "plan": ("najbliższe 30 dni (już zarezerwowane)", 30)}


def occupancy(first, nxt):
    """Obłożenie w okresie [first, nxt): % zarezerwowanych godzin osi wobec godzin otwarcia (minus blokady) —
    dla każdej pary dzień tygodnia × godzina, każdej osi i każdego dnia tygodnia; najbardziej oblegane terminy."""
    from apps.bookings.models import Block, Reservation
    from apps.bookings.services import local_dt
    from apps.facilities.models import Lane

    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, datetime.min.time()), tz)
    end = timezone.make_aware(datetime.combine(nxt, datetime.min.time()), tz)
    lanes = list(Lane.objects.filter(is_active=True, facility__is_active=True).prefetch_related("opening_hours")
                 .order_by("facility__order", "order"))
    blocks = list(Block.objects.filter(starts_at__lt=end, ends_at__gt=start).prefetch_related("lanes"))
    for b in blocks:
        b.lane_ids = {lane.pk for lane in b.lanes.all()}
    cap = [[0] * 24 for _ in range(7)]
    booked = [[0] * 24 for _ in range(7)]
    lane_rows = {lane.pk: {"lane": lane, "booked": 0, "capacity": 0} for lane in lanes}
    open_slots = set()  # (lane, dzień, godzina) — godziny, w których oś była czynna
    for lane in lanes:
        hours = {oh.weekday: (oh.opens.hour, oh.closes.hour) for oh in lane.opening_hours.all()}
        lane_blocks = [b for b in blocks if lane.pk in b.lane_ids]
        d = first
        while d < nxt:
            span = hours.get(d.weekday())
            if span:
                for h in range(span[0], span[1]):
                    s, e = local_dt(d, h), local_dt(d, h + 1)
                    if not any(b.starts_at < e and b.ends_at > s for b in lane_blocks):
                        cap[d.weekday()][h] += 1
                        lane_rows[lane.pk]["capacity"] += 1
                        open_slots.add((lane.pk, d, h))
            d += timedelta(days=1)
    reservations = Reservation.objects.filter(starts_at__gte=start, starts_at__lt=end, lane__in=lanes).exclude(ACTIVE_EXCLUDE)
    count = 0
    for r in reservations.only("lane_id", "starts_at", "ends_at"):
        count += 1
        t = timezone.localtime(r.starts_at)
        for i in range(r.hours):
            h = t.hour + i
            if h > 23:
                break
            if (r.lane_id, t.date(), h) in open_slots:  # godzina poza otwarciem / w blokadzie nie zawyża obłożenia
                booked[t.weekday()][h] += 1
                lane_rows[r.lane_id]["booked"] += 1

    def pct(b, c):
        return round(100 * b / c) if c else None

    hours_used = [h for h in range(24) if any(cap[w][h] for w in range(7))] or list(range(8, 18))
    top_pct = max((pct(booked[w][h], cap[w][h]) or 0 for w in range(7) for h in range(24)), default=0)
    heat = []
    for w in range(7):
        # (godzina, zarezerwowane, dostępne, %, natężenie koloru 0–100 względem najwyższej wartości w okresie)
        cells = [(h, booked[w][h], cap[w][h], pct(booked[w][h], cap[w][h]),
                  round(100 * (pct(booked[w][h], cap[w][h]) or 0) / top_pct) if top_pct else 0)
                 for h in range(hours_used[0], hours_used[-1] + 1)]
        heat.append((WEEKDAYS_SHORT[w], WEEKDAYS_FULL[w], cells))
    days = [{"name": WEEKDAYS_FULL[w], "booked": sum(booked[w]), "capacity": sum(cap[w]), "pct": pct(sum(booked[w]), sum(cap[w]))}
            for w in range(7)]
    top = sorted(((booked[w][h], pct(booked[w][h], cap[w][h]), w, h) for w in range(7) for h in range(24) if booked[w][h]),
                 key=lambda x: (-(x[1] or 0), -x[0], x[2], x[3]))[:6]
    total_b = sum(map(sum, booked))
    total_c = sum(map(sum, cap))
    lanes_out = [{**x, "pct": pct(x["booked"], x["capacity"])} for x in lane_rows.values()]
    busiest_day = max((d for d in days if d["booked"]), key=lambda d: d["pct"] or 0, default=None)
    return {
        "first": first, "last": nxt - timedelta(days=1), "reservations": count, "booked": total_b, "capacity": total_c,
        "pct": pct(total_b, total_c), "heat": heat, "heat_hours": list(range(hours_used[0], hours_used[-1] + 1)),
        "days": days, "lanes": lanes_out, "busiest_day": busiest_day,
        "top": [{"day": WEEKDAYS_FULL[w], "hour": h, "booked": b, "pct": p} for b, p, w, h in top],
    }


# --- rezerwacje i osie: przychód osi, rezerwacje bez wpisu w książce pobytu ----------------------------------------------

REPORT_PERIODS = {"7": ("ostatnie 7 dni", 7), "30": ("ostatnie 30 dni", 30), "90": ("ostatnie 90 dni", 90), "365": ("ostatni rok", 365)}
ENTRY_WINDOW = timedelta(hours=1)  # wpis bez powiązania z rezerwacją, ale tej osoby, od godziny przed rozpoczęciem


def _lane_revenue(r):
    """Przychód osi z rezerwacji (bez dopłaty za prowadzącego od strzelnicy); zwolniona z opłaty — 0."""
    if r.payment_status == "zwolniona":
        return 0
    return max(0, r.amount - r.officer_amount)


def _period_core(first, nxt, now):
    from apps.bookings.models import Reservation
    from apps.logbook.models import Entry

    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, datetime.min.time()), tz)
    end = timezone.make_aware(datetime.combine(nxt, datetime.min.time()), tz)
    occ = occupancy(first, nxt)
    reservations = list(Reservation.objects.filter(starts_at__gte=start, starts_at__lt=end)
                        .exclude(ACTIVE_EXCLUDE).exclude(status__in=["oczekuje", "do_zatwierdzenia"])
                        .select_related("lane", "user", "officer"))
    lanes = {x["lane"].pk: {**x, "revenue": 0, "officer_fees": 0, "count": 0, "hours": 0} for x in occ["lanes"]}
    for r in reservations:
        row = lanes.get(r.lane_id)
        if row is None:
            continue
        row["revenue"] += _lane_revenue(r)
        row["officer_fees"] += r.officer_amount if r.payment_status != "zwolniona" else 0
        row["count"] += 1
        row["hours"] += r.hours
    for row in lanes.values():
        row["per_open_hour"] = round(row["revenue"] / row["capacity"]) if row["capacity"] else None
        row["per_booked_hour"] = round(row["revenue"] / row["hours"]) if row["hours"] else None
        row["avg_hours"] = round(row["hours"] / row["count"], 1) if row["count"] else None

    # rezerwacje zakończone, odbyte (potwierdzone) — czy jest wpis w książce pobytu
    done = [r for r in reservations if r.status == "potwierdzona" and r.ends_at <= now]
    with_link = set(Entry.objects.filter(reservation__in=done).values_list("reservation_id", flat=True))
    by_user = {}
    for e in Entry.objects.filter(entered_at__gte=start - ENTRY_WINDOW, entered_at__lt=end, user__isnull=False).only("user_id", "entered_at"):
        by_user.setdefault(e.user_id, []).append(e.entered_at)
    missing = []
    for r in done:
        if r.pk in with_link:
            continue
        people = {r.user_id, r.officer_id} - {None}
        if any(r.starts_at - ENTRY_WINDOW <= t < r.ends_at for uid in people for t in by_user.get(uid, [])):
            continue
        missing.append(r)
    return {"occ": occ, "reservations": reservations, "lanes": list(lanes.values()), "done": done, "missing": missing}


def reservations_report(days, today=None, now=None):
    """Raport jak w GunTrackerze: przychód osi (na godzinę otwarcia i na zarezerwowaną godzinę), rezerwacje bez wpisu
    w książce pobytu z listą osób, porównanie z poprzednim okresem tej samej długości."""
    today = today or timezone.localdate()
    now = now or timezone.now()
    nxt = today + timedelta(days=1)
    first = nxt - timedelta(days=days)
    cur = _period_core(first, nxt, now)
    prev = _period_core(first - timedelta(days=days), first, now)

    def kpi(core):
        rs = core["reservations"]
        revenue = sum(row["revenue"] for row in core["lanes"])
        hours = sum(r.hours for r in rs)
        return {"revenue": revenue, "reservations": len(rs), "occupancy": core["occ"]["pct"],
                "avg_value": round(revenue / len(rs)) if rs else None, "avg_hours": round(hours / len(rs), 1) if rs else None,
                "missing_pct": round(100 * len(core["missing"]) / len(core["done"])) if core["done"] else None}

    k, p = kpi(cur), kpi(prev)
    deltas = {}
    for key, v in k.items():
        old = p.get(key)
        deltas[key] = None if v is None or old is None else (
            {"abs": v - old, "pct": None} if key in ("occupancy", "missing_pct") else
            {"abs": round(v - old, 1) if isinstance(v, float) or isinstance(old, float) else v - old,
             "pct": round(100 * (v - old) / old) if old else None})
    people = {}
    for r in cur["done"]:
        row = people.setdefault(r.user_id, {"user": r.user, "visits": 0, "missing": 0, "last_missing": None})
        row["visits"] += 1
    for r in cur["missing"]:
        row = people[r.user_id]
        row["missing"] += 1
        row["last_missing"] = max(filter(None, [row["last_missing"], timezone.localtime(r.starts_at).date()]))
    ranking = sorted((x for x in people.values() if x["missing"]), key=lambda x: (-x["missing"], -x["visits"], x["user"].last_name))
    lanes = sorted(cur["lanes"], key=lambda x: -(x["per_open_hour"] or 0))
    best = next((x for x in lanes if x["revenue"]), None)
    return {
        "first": first, "last": today, "kpi": k, "deltas": deltas, "lanes": lanes, "best_lane": best,
        "officer_fees": sum(x["officer_fees"] for x in cur["lanes"]),
        "done_count": len(cur["done"]), "missing": sorted(cur["missing"], key=lambda r: r.starts_at, reverse=True),
        "ranking": ranking[:15],
    }
