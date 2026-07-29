from django.urls import path

from . import api


urlpatterns = [
    path("topics", api.topics, name="topics"),
    path("sources", api.sources, name="sources"),
    path(
        "sources/<uuid:source_id>",
        api.source_detail,
        name="source-detail",
    ),
    path(
        "sources/<uuid:source_id>/check",
        api.source_check,
        name="source-check",
    ),
    path(
        "source-registries",
        api.source_registries,
        name="source-registries",
    ),
    path(
        "source-registries/<uuid:registry_id>",
        api.source_registry_detail,
        name="source-registry-detail",
    ),
    path(
        "source-registries/<uuid:registry_id>/memberships/<uuid:source_id>",
        api.source_registry_membership,
        name="source-registry-membership",
    ),
    path(
        "source-registries/<uuid:registry_id>/decisions",
        api.source_registry_decisions,
        name="source-registry-decisions",
    ),
]
