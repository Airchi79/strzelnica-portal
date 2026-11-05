import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.audit.services import log_event
from apps.bookings.models import Reservation
from apps.core.models import AssociationSettings
from apps.core.permissions import panel_required
from apps.facilities.models import format_pln

from . import services, tpay
from .forms import AssignForm, BankImportForm, CorrectionForm, ManualPaymentForm, PaymentSettingsForm, TopupForm
from .models import BankImport, LedgerEntry, Payment, PaymentSettings, Wallet

logger = logging.getLogger(__name__)
User = get_user_model()
PERM = "payments.manage_payments"


@login_required
def wallet(request):
    from apps.association.models import Fee

    due = request.user.reservations.filter(payment_status=Reservation.Payment.DUE).order_by("starts_at")
    fees = Fee.objects.filter(member__user=request.user, status=Fee.Status.DUE).order_by("due_on")
    ledger = Paginator(request.user.ledger.select_related("reservation"), 30).get_page(request.GET.get("page"))
    pending = request.user.payments.filter(status=Payment.Status.PENDING, method=Payment.Method.TPAY)[:5]
    return render(
        request,
        "payments/wallet.html",
        {
            "balance": services.balance_of(request.user),
            "due": due,
            "due_total": sum(r.amount - r.paid_amount for r in due),
            "fees": fees,
            "fees_total": sum(f.amount for f in fees),
            "page": ledger,
            "pending": pending,
            "topup_code": services.topup_code(request.user),
            "association": AssociationSettings.load(),
            "tpay_enabled": settings.TPAY_ENABLED,
            "form": TopupForm(),
        },
    )


@login_required
@require_POST
def tpay_start(request):
    """Rozpoczęcie płatności Tpay: doładowanie salda albo zapłata za konkretną rezerwację (kwota trafia na saldo)."""
    if not settings.TPAY_ENABLED:
        messages.error(request, "Płatności online są chwilowo niedostępne.")
        return redirect("payments:wallet")
    reservation = fee = registration = None
    if request.POST.get("registration"):
        from apps.events.models import Registration

        registration = get_object_or_404(Registration, pk=request.POST["registration"], user=request.user,
                                         status=Registration.Status.REGISTERED, payment_status=Registration.Payment.DUE)
        amount = max(registration.amount - registration.paid_amount - max(services.balance_of(request.user), 0), 0)
        if amount == 0:
            services.apply_wallet(request.user, targets=[registration])
            messages.success(request, "Opłata startowa została pokryta z nadpłaty.")
            return redirect("events:detail", pk=registration.event_id)
        amount = max(amount, 100)
    elif request.POST.get("fee"):
        from apps.association.models import Fee

        fee = get_object_or_404(Fee, pk=request.POST["fee"], member__user=request.user, status=Fee.Status.DUE)
        amount = fee.amount
    elif request.POST.get("reservation"):
        reservation = get_object_or_404(
            Reservation, pk=request.POST["reservation"], user=request.user, payment_status=Reservation.Payment.DUE, status=Reservation.Status.CONFIRMED
        )
        from apps.bookings.services import payment_options

        if not payment_options(reservation)["online"]:
            messages.error(request, "Dla tej rezerwacji płatność online nie jest dostępna — skorzystaj z instrukcji płatności.")
            return redirect("bookings:detail", pk=reservation.pk)
        amount = max(reservation.amount - reservation.paid_amount - max(services.balance_of(request.user), 0), 0)
        if amount == 0:
            services.apply_wallet(request.user, targets=[reservation])
            messages.success(request, "Rezerwacja została opłacona z salda.")
            return redirect("bookings:detail", pk=reservation.pk)
        amount = max(amount, 100)  # minimalna kwota transakcji
    else:
        if not services.wallet_enabled():
            messages.error(request, "Doładowanie salda jest wyłączone — zapłać za konkretną rezerwację lub składkę.")
            return redirect("payments:wallet")
        form = TopupForm(request.POST)
        if not form.is_valid():
            messages.error(request, form.errors["amount"][0])
            return redirect("payments:wallet")
        amount = form.cleaned_data["amount"]
    payment = Payment.objects.create(
        user=request.user, amount=amount, method=Payment.Method.TPAY, reservation=reservation, fee=fee, event_registration=registration,
        title=reservation.number if reservation else (fee.label if fee else (registration.label if registration else services.topup_code(request.user))),
        created_by=request.user,
    )
    base = settings.SITE_URL
    try:
        tx = tpay.create_transaction(
            amount_grosze=amount,
            description="Strzelnica Sątok — " + (f"rezerwacja {reservation.number}" if reservation else (fee.label if fee else (registration.label if registration else "doładowanie salda")))[:100],
            hidden_description=str(payment.pk),
            payer_email=request.user.email,
            payer_name=request.user.full_name,
            notification_url=base + reverse("payments:tpay_notify"),
            success_url=base + reverse("payments:tpay_return", args=[payment.pk]) + "?wynik=ok",
            error_url=base + reverse("payments:tpay_return", args=[payment.pk]) + "?wynik=blad",
        )
    except tpay.TpayError as e:
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.FAILED, note=str(e)[:255])
        messages.error(request, "Nie udało się połączyć z Tpay. Spróbuj ponownie później lub zapłać przelewem.")
        return redirect("payments:wallet")
    Payment.objects.filter(pk=payment.pk).update(
        external_id=f"tpay:{tx['transaction_id']}", raw={"tpay_title": tx["title"], "sandbox": bool(settings.TPAY_SANDBOX)}
    )
    log_event("platnosc.tpay_start", payment, f"{amount / 100:.2f} zł")
    return redirect(tx["payment_url"])


@login_required
def tpay_return(request, pk):
    payment = get_object_or_404(Payment, pk=pk, user=request.user)
    return render(request, "payments/tpay_return.html", {"payment": payment, "ok": request.GET.get("wynik") == "ok"})


@csrf_exempt
@require_POST
@transaction.non_atomic_requests
def tpay_notify(request):
    """Powiadomienie Tpay o płatności. Odpowiedź „TRUE” kończy ponawianie; błąd weryfikacji → 400 (Tpay ponowi)."""
    if not settings.TPAY_ENABLED:
        return HttpResponse(status=404)
    fields = request.POST.dict()
    if not tpay.verify_jws(request.headers.get("X-JWS-Signature", ""), request.body) or not tpay.verify_md5(fields):
        logger.warning("Odrzucono powiadomienie Tpay (podpis lub md5sum): tr_id=%s", fields.get("tr_id"))
        return HttpResponseBadRequest("INVALID")
    try:
        payment = Payment.objects.get(pk=fields.get("tr_crc"), method=Payment.Method.TPAY)
    except (Payment.DoesNotExist, ValidationError, ValueError):
        logger.warning("Powiadomienie Tpay dla nieznanej płatności: %s", fields.get("tr_crc"))
        return HttpResponse("TRUE")
    status = fields.get("tr_status", "").lower()
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        payment.raw = {**payment.raw, "powiadomienie": {k: v for k, v in fields.items() if k != "md5sum"}}
        payment.save(update_fields=["raw"])
        expected_title = (payment.raw or {}).get("tpay_title")
        mismatch = None
        if expected_title and fields.get("tr_id") != expected_title:
            mismatch = f"Numer transakcji z Tpay ({fields.get('tr_id', '')[:40]}) różni się od zapisanego ({expected_title})"
        elif not settings.TPAY_SANDBOX and fields.get("test_mode") == "1":
            mismatch = "Powiadomienie testowe Tpay na koncie produkcyjnym — nie zaksięgowano"
        if mismatch and status == "true":
            if payment.status != Payment.Status.COMPLETED:
                payment.note, payment.status = mismatch[:255], Payment.Status.UNMATCHED
                payment.save(update_fields=["note", "status"])
            logger.warning("Powiadomienie Tpay do sprawdzenia: %s", mismatch)
        elif status == "true":
            paid = services_amount(fields.get("tr_paid") or fields.get("tr_amount"))
            if paid != payment.amount:
                payment.note = f"Kwota z Tpay ({paid / 100:.2f} zł) różni się od oczekiwanej"
                payment.status = Payment.Status.UNMATCHED
                payment.save(update_fields=["note", "status"])
            else:
                services.complete_payment(payment)
        elif status == "chargeback" and payment.status == Payment.Status.COMPLETED and not payment.ledger_entries.filter(kind=LedgerEntry.Kind.CHARGEBACK).exists():
            services.post(payment.user, -payment.amount, LedgerEntry.Kind.CHARGEBACK, f"Zwrot płatności Tpay {fields.get('tr_id', '')}", payment=payment)
            log_event("platnosc.zwrot_tpay", payment, str(payment))
    return HttpResponse("TRUE")


def services_amount(text):
    from .services import _parse_amount

    return _parse_amount(text or "") or 0


# --- panel zarządu ----------------------------------------------------------------------


@panel_required(PERM)
def panel_payments(request):
    status = request.GET.get("status", "")
    qs = Payment.objects.select_related("user", "reservation")
    if status in Payment.Status.values:
        qs = qs.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(payer_name__icontains=q) | Q(user__last_name__icontains=q) | Q(user__email__icontains=q))
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    return render(
        request,
        "panel/payments_list.html",
        {"page": page, "status": status, "statuses": Payment.Status.choices, "q": q,
         "unmatched": Payment.objects.filter(status=Payment.Status.UNMATCHED).count()},
    )


@panel_required(PERM)
def panel_payment(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("user", "reservation", "created_by"), pk=pk)
    form = AssignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            if request.POST.get("action") == "assign":
                if not form.cleaned_data["user"]:
                    raise ValidationError("Wybierz strzelca.")
                services.assign_unmatched(payment, form.cleaned_data["user"], request.user)
                messages.success(request, "Wpłata przypisana i zaksięgowana na saldzie.")
            elif request.POST.get("action") == "ignore":
                if not form.cleaned_data["reason"].strip():
                    raise ValidationError("Podaj powód pominięcia.")
                services.ignore_unmatched(payment, form.cleaned_data["reason"], request.user)
                messages.success(request, "Wpłatę oznaczono jako pominiętą.")
        except ValidationError as e:
            messages.error(request, e.messages[0])
        return redirect("panel:payment", pk=payment.pk)
    return render(request, "panel/payment_detail.html", {"p": payment, "form": form, "ledger": payment.ledger_entries.all()})


@panel_required((PERM, "payments.accept_onsite"))
def panel_manual(request):
    full = request.user.has_perm(PERM)  # dyżurny (accept_onsite): tylko gotówka/BLIK i tylko za rezerwację
    initial = {}
    if request.GET.get("user", "").isdigit():
        initial["user"] = request.GET["user"]
    if request.GET.get("kwota"):
        initial["amount"] = request.GET["kwota"]
    if request.GET.get("rezerwacja", "").isdigit():
        initial["reservation"] = request.GET["rezerwacja"]
    form = ManualPaymentForm(request.POST or None, initial=initial, onsite_only=not full)
    reservation = None
    rid = form.data.get("reservation") or initial.get("reservation")
    if rid and str(rid).isdigit():
        reservation = Reservation.objects.filter(pk=int(rid)).first()
    if not full and reservation is None:
        messages.error(request, "Wpłatę na miejscu przyjmujesz z karty rezerwacji (przycisk „Przyjmij wpłatę”).")
        return redirect("panel:today")
    if not full:
        form.fields["user"].queryset = form.fields["user"].queryset.filter(pk=reservation.user_id)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if reservation is not None and reservation.user_id != d["user"].pk:
            reservation = None
        payment = services.register_manual_payment(d["user"], d["amount"], d["method"], request.user, note=d["note"], reservation=reservation)
        if not full:
            messages.success(request, f"Przyjęto wpłatę {format_pln(d['amount'])} za rezerwację {reservation.number}.")
            return redirect("panel:reservation", pk=reservation.pk)
        messages.success(request, f"Przyjęto wpłatę {format_pln(d['amount'])}. Saldo: {format_pln(services.balance_of(d['user']))}.")
        return redirect("panel:wallet", user_pk=payment.user_id)
    return render(request, "panel/payment_manual.html", {"form": form, "reservation": reservation, "full": full})


@panel_required(PERM)
def panel_import(request):
    form = BankImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        f = form.cleaned_data["file"]
        try:
            result = services.import_bank_csv(f.read(), f.name, request.user)
        except ValidationError as e:
            form.add_error("file", e.messages[0])
        else:
            messages.success(request, f"Wczytano wpływów: {result.rows_total}; nowych: {result.rows_new}; przypisanych automatycznie: {result.rows_matched}.")
            return redirect(reverse("panel:payments") + ("?status=nieprzypisana" if result.rows_new > result.rows_matched else ""))
    return render(request, "panel/payment_import.html", {"form": form, "imports": BankImport.objects.select_related("created_by")[:20]})


@panel_required(PERM)
def panel_wallets(request):
    q = request.GET.get("q", "").strip()
    qs = Wallet.objects.select_related("user").order_by("user__last_name")
    if q:
        qs = qs.filter(Q(user__last_name__icontains=q) | Q(user__email__icontains=q))
    if request.GET.get("filtr") == "ujemne":
        qs = qs.filter(balance__lt=0)
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    return render(request, "panel/wallets.html", {"page": page, "q": q})


@panel_required(PERM)
def panel_wallet(request, user_pk):
    subject = get_object_or_404(User, pk=user_pk)
    form = CorrectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.correction(subject, form.cleaned_data["signed"], form.cleaned_data["reason"], request.user)
        except ValidationError as e:
            messages.error(request, e.messages[0])
        else:
            messages.success(request, "Zapisano korektę salda.")
        return redirect("panel:wallet", user_pk=subject.pk)
    ledger = Paginator(subject.ledger.select_related("reservation", "created_by"), 40).get_page(request.GET.get("page"))
    return render(
        request,
        "panel/wallet_detail.html",
        {
            "subject": subject,
            "balance": services.balance_of(subject),
            "page": ledger,
            "form": form,
            "due": subject.reservations.filter(payment_status=Reservation.Payment.DUE).order_by("starts_at"),
            "topup_code": services.topup_code(subject),
            "consistent": services.ledger_consistent(subject),
        },
    )


@panel_required(PERM)
def panel_settings(request):
    obj = PaymentSettings.load()
    before = {f: getattr(obj, f) for f in PaymentSettingsForm.Meta.fields}
    form = PaymentSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        changes = {f: [before[f], getattr(obj, f)] for f in before if before[f] != getattr(obj, f)}
        if changes:
            log_event("platnosci.ustawienia", obj, changes=changes)
        msg = "Zapisano ustawienia płatności."
        turned_on = (not before["overpayment_pays_reservations"] and obj.overpayment_pays_reservations) or (
            not before["wallet_enabled"] and obj.wallet_enabled)
        if turned_on:
            paid = services.apply_all_overpayments(by=request.user)
            msg += f" Z istniejących nadpłat opłacono należności: {paid}."
        messages.success(request, msg)
        return redirect("panel:payment_settings")
    overpaid = Wallet.objects.filter(balance__gt=0).count()
    return render(request, "panel/payment_settings.html", {"form": form, "overpaid": overpaid})
