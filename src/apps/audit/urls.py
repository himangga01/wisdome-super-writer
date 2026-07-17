from django.urls import path

from .api import approve_retention, execute_retention, list_audit_events, retention_batches

app_name = "audit"

urlpatterns = [
    path("audit-events", list_audit_events, name="event-list"),
    path("retention/batches", retention_batches, name="retention-batches"),
    path("retention/batches/<uuid:batch_id>/approve", approve_retention, name="retention-approve"),
    path("retention/batches/<uuid:batch_id>/execute", execute_retention, name="retention-execute"),
]
