from django.urls import path

from . import views

app_name = "logbook"

urlpatterns = [
    path("wpis/r/<str:token>/", views.entry_by_reservation, name="by_reservation"),
    path("wpis/<str:token>/", views.entry_by_gate, name="by_gate"),
    path("wpis/<str:token>/podpowiedz/", views.gate_suggest, name="gate_suggest"),
    path("wpisano/<int:pk>/", views.done, name="done"),
    path("dolacz/<str:token>/", views.entry_by_companion, name="companion"),
    path("wyjscie/<str:token>/", views.exit_view, name="exit"),
    path("moj-kod/", views.my_qr, name="my_qr"),
    path("moje-wyjscie/<int:pk>/", views.my_exit, name="my_exit"),
    path("dziennik/", views.my_diary, name="diary"),
    path("podpis/<int:pk>/", views.sign_remote, name="sign_remote"),
    path("osoba/<str:token>/", views.personal_add, name="personal"),
]
