from django.urls import path

from . import views

app_name = "logbook"

urlpatterns = [
    path("wpis/r/<str:token>/", views.entry_by_reservation, name="by_reservation"),
    path("wpis/<str:token>/", views.entry_by_gate, name="by_gate"),
    path("wpisano/<int:pk>/", views.done, name="done"),
    path("wyjscie/<str:token>/", views.exit_view, name="exit"),
    path("moj-kod/", views.my_qr, name="my_qr"),
    path("dziennik/", views.my_diary, name="diary"),
    path("osoba/<str:token>/", views.personal_add, name="personal"),
]
