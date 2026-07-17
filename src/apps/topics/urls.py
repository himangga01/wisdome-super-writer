from django.urls import path

from . import api

urlpatterns = [
    path("topics", api.topics, name="topics"),
    path("sources", api.sources, name="sources"),
    path("source-registries/<uuid:registry_id>", api.registry_detail, name="registry-detail"),
]
