from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.wallet, name="wallet"),
    path("tpay/start/", views.tpay_start, name="tpay_start"),
    path("potwierdzenie/<uuid:pk>.pdf", views.receipt, name="receipt"),
    path("tpay/powrot/<uuid:pk>/", views.tpay_return, name="tpay_return"),
    path("tpay/powiadomienie/", views.tpay_notify, name="tpay_notify"),
]
