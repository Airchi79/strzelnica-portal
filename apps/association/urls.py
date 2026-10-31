from django.urls import path

from . import views

app_name = "association"

urlpatterns = [
    path("", views.membership, name="membership"),
    path("uchwaly/<uuid:pk>/", views.resolution_public, name="resolution"),
]
