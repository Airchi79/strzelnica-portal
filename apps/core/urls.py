from django.urls import path

from apps.facilities import views as facilities_views

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("pulpit/", views.dashboard, name="dashboard"),
    path("osie/", facilities_views.public_lanes, name="lanes"),
    path("offline/", views.offline, name="offline"),
]
