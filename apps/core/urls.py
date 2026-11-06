from django.urls import path

from apps.facilities import views as facilities_views
from apps.logbook import views as logbook_views

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("pulpit/", views.dashboard, name="dashboard"),
    path("osie/", facilities_views.public_lanes, name="lanes"),
    path("offline/", views.offline, name="offline"),
    path("teraz/", logbook_views.now_public, name="now"),
    path("usterki/", facilities_views.my_issues, name="issues"),
    path("usterki/nowa/", facilities_views.issue_new, name="issue_new"),
]
