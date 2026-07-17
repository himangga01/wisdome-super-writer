from django.urls import path

from .health import api_root

urlpatterns = [path("", api_root, name="api-root")]

