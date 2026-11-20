from django.urls import path

from apps.facilities import views as facilities_views
from apps.logbook import views as logbook_views

from . import public_api, views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("api/publiczne/osie/", public_api.lanes, name="api_lanes"),
    path("api/publiczne/wolne-terminy/", public_api.free_slots, name="api_free_slots"),
    path("api/publiczne/wydarzenia/", public_api.events, name="api_events"),
    path("pulpit/", views.dashboard, name="dashboard"),
    path("powiadomienia/", views.my_notifications, name="notifications"),
    path("powiadomienia/push/", views.push_subscribe, name="push_subscribe"),
    path("powiadomienia/<int:pk>/", views.notification_open, name="notification"),
    path("osie/", facilities_views.public_lanes, name="lanes"),
    path("offline/", views.offline, name="offline"),
    path("teraz/", logbook_views.now_public, name="now"),
    path("pytania/", views.faq, name="faq"),
    path("usterki/", facilities_views.my_issues, name="issues"),
    path("usterki/nowa/", facilities_views.issue_new, name="issue_new"),
]
