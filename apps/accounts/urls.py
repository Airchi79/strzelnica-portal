from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("rejestracja/", views.register, name="register"),
    path("potwierdz/<uidb64>/<token>/", views.verify_email, name="verify"),
    path("potwierdz/ponownie/", views.resend_verification, name="resend_verification"),
    path("logowanie/", views.login_view, name="login"),
    path("wyloguj/", views.logout_view, name="logout"),
    path("2fa/konfiguracja/", views.otp_setup, name="otp_setup"),
    path("2fa/kod/", views.otp_verify, name="otp_verify"),
    path("profil/", views.profile, name="profile"),
    path("profil/haslo/", views.PasswordChangeView.as_view(), name="password_change"),
    path("profil/dezaktywuj/", views.deactivate, name="deactivate"),
    path("profil/moje-dane/", views.my_data, name="my_data"),
    path("haslo/reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path(
        "haslo/reset/wyslano/",
        auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_done.html"),
        name="password_reset_done",
    ),
    path("haslo/reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
]
