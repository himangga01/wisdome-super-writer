from django.urls import path

from .api import reauthenticate

app_name = "accounts"

urlpatterns = [path("auth/reauth", reauthenticate, name="reauthenticate")]

