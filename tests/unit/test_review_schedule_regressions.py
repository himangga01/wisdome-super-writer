import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.audit.services import AuditContext
from apps.scheduling import services
from apps.scheduling.models import Schedule


def test_invalid_schedule_timezone_is_a_validation_error():
    schedule = SimpleNamespace(timezone="Invalid/Timezone", cron_expression="0 * * * *")
    with pytest.raises(ValueError, match="timezone"):
        services.calculate_next_run(schedule)


@pytest.mark.django_db
def test_ineligible_tick_is_audited_and_does_not_starve_the_next_due_schedule():
    now = timezone.now()
    values = dict(
        topic_code="housing_subscription",
        cron_expression="0 * * * *",
        timezone="Asia/Seoul",
        window_minutes=60,
        target_ids=[],
        enabled=True,
        next_run_at=now - timezone.timedelta(minutes=1),
    )
    bad = Schedule.objects.create(
        name="Expired material", **{**values, "next_run_at": now - timezone.timedelta(minutes=2)}
    )
    good = Schedule.objects.create(name="Healthy", **values)
    context = AuditContext.for_system(
        reason_code="Dispatch due review ticks",
        correlation_id=uuid.uuid4(),
        operation_key="review-due-ticks",
    )
    original = services.dispatch_schedule
    healthy = SimpleNamespace(id=good.id)

    def dispatch(schedule_id, **kwargs):
        return original(schedule_id, **kwargs) if schedule_id == bad.id else healthy

    with (
        patch.object(services, "dispatch_schedule", side_effect=dispatch),
        patch.object(
            services,
            "build_schedule_dispatch_material",
            side_effect=ValueError("schedule material is ineligible"),
        ),
    ):
        result = services.dispatch_due_schedules(now, audit_context=context)
    assert result == [None, healthy]
    bad.refresh_from_db()
    assert bad.next_run_at > now
    assert AuditEvent.objects.filter(action="schedule_dispatch.skipped", entity_id=bad.id).exists()
