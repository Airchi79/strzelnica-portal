from django.urls import path

from . import views

app_name = "regulations"

urlpatterns = [
    path("", views.public_list, name="list"),
    path("wersja/<int:pk>/", views.version_detail, name="version"),
    path("akceptacja/", views.accept, name="accept"),
]
