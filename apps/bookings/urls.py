from django.urls import path

from . import views

app_name = "bookings"

urlpatterns = [
    path("", views.calendar, name="calendar"),
    path("nowa/", views.new, name="new"),
    path("sloty/", views.slots_json, name="slots"),
    path("moje/", views.my_list, name="my_list"),
    path("lista-oczekujacych/", views.waitlist, name="waitlist"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/kalendarz.ics", views.ics, name="ics"),
]
