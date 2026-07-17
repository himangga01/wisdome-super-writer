from django.urls import path

from . import api

urlpatterns = [
    path("runs", api.runs, name="runs"),
    path("runs/<uuid:run_id>", api.run_detail, name="run-detail"),
    path("runs/<uuid:run_id>/stop", api.stop_run, name="run-stop"),
    path("runs/<uuid:run_id>/retry", api.retry_run, name="run-retry"),
]
