import base64

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.audit.context import client_ip
from apps.audit.services import diff, log_event
from apps.core.notifications import notify_user
from apps.core.qr import qr_svg_data_uri

from . import ratelimit
from .backends import EmailBackend
from .forms import DeactivateForm, LoginForm, OTPTokenForm, PanelProfileForm, ProfileForm, RegistrationForm, ResendVerificationForm
from .models import User
from .tokens import email_verification_token

SESSION_2FA_USER = "pending_2fa_user"
SESSION_2FA_NEXT = "pending_2fa_next"
STAFF_SESSION_SECONDS = 12 * 3600


def _safe_next(request, default="core:dashboard"):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return reverse(default)


def send_verification_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)
    notify_user(user, "Potwierdź adres e-mail", "emails/verify_email.txt", portal=False, path=reverse("accounts:verify", args=[uid, token]))


def register(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid() and not ratelimit.allow(
            "register-ip", client_ip(request), ratelimit.REGISTRATIONS_PER_IP_PER_HOUR):
        form.add_error(None, "Zbyt wiele rejestracji z tego adresu. Spróbuj za godzinę.")
    if request.method == "POST" and form.is_valid():
        user = form.save()
        log_event("konto.rejestracja", user, f"Rejestracja {user.email}", actor=user)
        send_verification_email(user)
        return render(request, "accounts/register_done.html", {"email": user.email})
    return render(request, "accounts/register.html", {"form": form})


def send_welcome_email(user):
    """E-mail powitalny: co zostało do pierwszej rezerwacji (ta sama lista kontrolna co na pulpicie) i dane strzelnicy."""
    from apps.bookings.services import booking_readiness
    from apps.core.models import AssociationSettings

    notify_user(user, "Witamy — co dalej, żeby zarezerwować oś", "emails/welcome.txt",
                readiness=booking_readiness(user), association=AssociationSettings.load())


def verify_email(request, uidb64, token):
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        user = None
    if user is None or not email_verification_token.check_token(user, token):
        messages.error(request, "Link jest nieprawidłowy lub wygasł. Wyślij nowy.")
        return redirect("accounts:resend_verification")
    if not user.email_verified:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])
        log_event("konto.potwierdzenie_email", user, actor=user)
        send_welcome_email(user)  # tylko przy pierwszym potwierdzeniu — ponowne kliknięcie linku nie wysyła drugi raz
    messages.success(request, "Adres e-mail potwierdzony. Możesz się zalogować.")
    return redirect("accounts:login")


def resend_verification(request):
    form = ResendVerificationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = User.objects.filter(email__iexact=form.cleaned_data["email"], is_active=True, email_verified_at__isnull=True).first()
        if user and _mail_allowed(request, "verify", user.email):
            send_verification_email(user)
        # Ta sama odpowiedź niezależnie od tego, czy konto istnieje (brak wycieku informacji o kontach).
        messages.info(request, "Jeśli konto istnieje i nie jest potwierdzone, wysłaliśmy nowy link.")
        return redirect("accounts:login")
    return render(request, "accounts/resend_verification.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request))
    form = LoginForm(request.POST or None)
    ip = client_ip(request)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        if ratelimit.is_blocked(email, ip):
            form.add_error(None, "Zbyt wiele nieudanych prób. Spróbuj ponownie za 15 minut.")
        else:
            user = EmailBackend().authenticate(request, email=email, password=form.cleaned_data["password"])
            if user is None:
                ratelimit.register_failure(email, ip)
                log_event("konto.logowanie_nieudane", summary=email[:255])
                form.add_error(None, "Nieprawidłowy e-mail lub hasło.")
            elif not user.email_verified:
                form.add_error(None, "Najpierw potwierdź adres e-mail (sprawdź skrzynkę lub wyślij link ponownie).")
            else:
                ratelimit.reset(email, ip)
                if user.requires_2fa:
                    request.session[SESSION_2FA_USER] = user.pk
                    request.session[SESSION_2FA_NEXT] = _safe_next(request)
                    has_device = TOTPDevice.objects.filter(user=user, confirmed=True).exists()
                    return redirect("accounts:otp_verify" if has_device else "accounts:otp_setup")
                login(request, user, backend="apps.accounts.backends.EmailBackend")
                log_event("konto.logowanie", user)
                return redirect(_safe_next(request))
    return render(request, "accounts/login.html", {"form": form, "next": request.GET.get("next", "")})


def _pending_2fa_user(request):
    pk = request.session.get(SESSION_2FA_USER)
    return User.objects.filter(pk=pk, is_active=True).first() if pk else None


def _finish_2fa_login(request, user, device):
    nxt = request.session.pop(SESSION_2FA_NEXT, reverse("core:dashboard"))
    request.session.pop(SESSION_2FA_USER, None)
    if not request.user.is_authenticated:
        login(request, user, backend="apps.accounts.backends.EmailBackend")
    otp_login(request, device)
    if user.requires_2fa:
        # Konta z dostępem do panelu (dane osobowe, płatności): sesja 12 h zamiast 14 dni.
        request.session.set_expiry(STAFF_SESSION_SECONDS)
    log_event("konto.logowanie", user, "z weryfikacją dwuskładnikową")
    return redirect(nxt)


def otp_setup(request):
    """Konfiguracja aplikacji uwierzytelniającej (np. Google Authenticator, Aegis)."""
    user = request.user if request.user.is_authenticated else _pending_2fa_user(request)
    if user is None:
        return redirect("accounts:login")
    if TOTPDevice.objects.filter(user=user, confirmed=True).exists() and not request.user.is_authenticated:
        return redirect("accounts:otp_verify")
    device = TOTPDevice.objects.filter(user=user, confirmed=False).first() or TOTPDevice.objects.create(
        user=user, name="Aplikacja", confirmed=False
    )
    form = OTPTokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            TOTPDevice.objects.filter(user=user).exclude(pk=device.pk).delete()
            log_event("konto.2fa_wlaczone", user, actor=user)
            messages.success(request, "Logowanie dwuskładnikowe włączone.")
            if request.user.is_authenticated and SESSION_2FA_NEXT not in request.session:
                otp_login(request, device)
                return redirect("accounts:profile")
            return _finish_2fa_login(request, user, device)
        form.add_error("token", "Kod nieprawidłowy. Sprawdź czas w telefonie i spróbuj ponownie.")
    secret = base64.b32encode(device.bin_key).decode()
    return render(
        request,
        "accounts/otp_setup.html",
        {"form": form, "qr": qr_svg_data_uri(device.config_url), "secret": secret, "required": user.requires_2fa},
    )


def otp_verify(request):
    """Drugi krok logowania; także dla zalogowanego członka zarządu, który wchodzi do panelu bez weryfikacji."""
    user = request.user if request.user.is_authenticated else _pending_2fa_user(request)
    if user is None:
        return redirect("accounts:login")
    form = OTPTokenForm(request.POST or None)
    ip = client_ip(request)
    if request.method == "POST" and form.is_valid():
        if ratelimit.is_blocked(f"otp:{user.email}", ip):
            form.add_error(None, "Zbyt wiele nieudanych prób. Spróbuj ponownie za 15 minut.")
        else:
            for device in TOTPDevice.objects.filter(user=user, confirmed=True):
                if device.verify_token(form.cleaned_data["token"]):
                    ratelimit.reset(f"otp:{user.email}", ip)
                    return _finish_2fa_login(request, user, device)
            ratelimit.register_failure(f"otp:{user.email}", ip)
            form.add_error("token", "Kod nieprawidłowy.")
    return render(request, "accounts/otp_verify.html", {"form": form})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        log_event("konto.wylogowanie", request.user)
    logout(request)
    return redirect("core:home")


@login_required
def profile(request):
    before = User.objects.get(pk=request.user.pk)
    from apps.core.middleware import is_panel_account

    form_class = PanelProfileForm if is_panel_account(request.user) else ProfileForm
    form = form_class(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        log_event("konto.profil_zmiana", user, changes=diff(before, user, form.Meta.fields))
        messages.success(request, "Zapisano dane profilu.")
        return redirect("accounts:profile")
    has_2fa = TOTPDevice.objects.filter(user=request.user, confirmed=True).exists()
    return render(request, "accounts/profile.html", {"form": form, "has_2fa": has_2fa})


def _active_commitments(user):
    """Przeszkody w dezaktywacji: przyszłe rezerwacje (także czekające na płatność lub zatwierdzenie) i zapisy na wydarzenia."""
    from apps.bookings.models import Reservation
    from apps.events.models import Registration

    now = timezone.now()
    reasons = []
    n = Reservation.objects.filter(user=user, ends_at__gt=now, status__in=[
        Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL, Reservation.Status.HOLD]).count()
    if n:
        reasons.append(f"Masz {n} nadchodzące rezerwacje — anuluj je w „Moich rezerwacjach” (albo poproś obsługę) przed dezaktywacją konta.")
    if Registration.objects.filter(user=user, status__in=[Registration.Status.REGISTERED, Registration.Status.RESERVE],
                                   event__ends_at__gt=now).exists():
        reasons.append("Jesteś zapisany na nadchodzące wydarzenie — wycofaj zapis przed dezaktywacją konta.")
    return reasons


@login_required
def deactivate(request):
    """Dezaktywacja konta przez użytkownika. Dane objęte obowiązkiem przechowywania pozostają w systemie."""
    form = DeactivateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not request.user.check_password(form.cleaned_data["password"]):
            form.add_error("password", "Nieprawidłowe hasło.")
        elif request.user.is_superuser and not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=request.user.pk).exists():
            form.add_error(None, "Jesteś jedynym administratorem. Najpierw nadaj uprawnienia innej osobie.")
        elif _active_commitments(request.user):
            form.add_error(None, " ".join(_active_commitments(request.user)))
        else:
            user = request.user
            user.is_active = False
            user.deactivated_at = timezone.now()
            user.save(update_fields=["is_active", "deactivated_at"])
            log_event("konto.dezaktywacja", user)
            logout(request)
            messages.info(request, "Konto zostało dezaktywowane.")
            return redirect("core:home")
    return render(request, "accounts/deactivate.html", {"form": form})


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "accounts/password_change.html"
    extra_context = {"heading": "Zmiana hasła"}
    success_url = reverse_lazy("accounts:profile")

    def form_valid(self, form):
        response = super().form_valid(form)
        update_session_auth_hash(self.request, form.user)
        log_event("konto.zmiana_hasla", form.user)
        messages.success(self.request, "Hasło zostało zmienione.")
        return response


def _mail_allowed(request, kind, email):
    """Limit e-maili z formularzy publicznych: na adres odbiorcy i na IP. Odpowiedź dla użytkownika jest taka sama
    niezależnie od wyniku (bez ujawniania, czy konto istnieje i czy zadziałał limit)."""
    ok_ip = ratelimit.allow(f"{kind}-ip", client_ip(request), ratelimit.MAIL_PER_IP_PER_HOUR)
    ok_mail = ratelimit.allow(f"{kind}-mail", email, ratelimit.MAIL_PER_ADDRESS_PER_HOUR)
    return ok_ip and ok_mail


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    extra_context = {"heading": "Reset hasła"}
    email_template_name = "emails/password_reset.txt"
    extra_email_context = {"site_url": settings.SITE_URL}
    subject_template_name = "emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")

    def form_valid(self, form):
        if not _mail_allowed(self.request, "reset", form.cleaned_data["email"]):
            return redirect(self.success_url)  # ta sama strona „wysłaliśmy” — bez wysyłki
        return super().form_valid(form)


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    extra_context = {"heading": "Ustaw nowe hasło"}
    success_url = reverse_lazy("accounts:login")

    def form_valid(self, form):
        response = super().form_valid(form)
        log_event("konto.reset_hasla", form.user, actor=form.user)
        messages.success(self.request, "Hasło ustawione. Zaloguj się.")
        return response


@login_required
def my_data(request):
    """RODO: pobranie kompletu własnych danych (JSON)."""
    import json

    from . import gdpr

    if request.method == "POST":
        if not ratelimit.allow("gdpr-export", request.user.email, 5, 24 * 3600):
            messages.error(request, "Eksport danych można pobrać najwyżej 5 razy na dobę.")
            return redirect("accounts:my_data")
        payload = json.dumps(gdpr.export_data(request.user), ensure_ascii=False, indent=2)
        response = HttpResponse(payload, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="moje-dane-{timezone.localdate():%Y-%m-%d}.json"'
        response["Cache-Control"] = "no-store"
        return response
    from apps.core.models import AssociationSettings

    return render(request, "accounts/my_data.html", {"association": AssociationSettings.load()})

