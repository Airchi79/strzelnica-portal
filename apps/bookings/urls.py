from django.urls import path

from . import views

app_name = "bookings"

urlpatterns = [
    path("", views.calendar, name="calendar"),
    path("nowa/", views.new, name="new"),
    path("moje/", views.my_list, name="my_list"),
    path("<int:pk>/", views.detail, name="detail"),
]
