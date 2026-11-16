from django.urls import path

from . import views

app_name = "credentials"

urlpatterns = [
    path("", views.my_list, name="my_list"),
    path("dodaj/", views.my_edit, name="my_add"),
    path("odczyt-zdjecia/", views.my_read_photo, name="my_read_photo"),
    path("<int:pk>/", views.my_edit, name="my_edit"),
    path("<int:pk>/usun/", views.my_delete, name="my_delete"),
]
