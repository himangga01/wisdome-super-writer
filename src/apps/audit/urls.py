from django.urls import path

from .api import (
    execute_retention,
    list_audit_events,
    retention_batch_detail,
    retention_batch_items,
    retention_previews,
)

app_name = "audit"

urlpatterns = [
    path("audit-events", list_audit_events, name="event-list"),
    path("retention/previews", retention_previews, name="retention-preview"),
    path(
        "retention/batches/<uuid:retention_batch_id>",
        retention_batch_detail,
        name="retention-batch-detail",
    ),
    path(
        "retention/batches/<uuid:retention_batch_id>/items",
        retention_batch_items,
        name="retention-batch-items",
    ),
    path(
        "retention/batches/<uuid:retention_batch_id>/execute",
        execute_retention,
        name="retention-execute",
    ),
]
