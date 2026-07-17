from django.urls import path

from . import api

urlpatterns = [
    path("schedules", api.schedules, name="schedules"),
    path("schedules/<uuid:schedule_id>", api.schedule_detail, name="schedule-detail"),
    path("operations/kill-switch", api.kill_switch, name="kill-switch"),
]
