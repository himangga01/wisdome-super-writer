from django.conf import settings

from wisdome_writer.infrastructure.event_routes import EVENT_ROUTES
from wisdome_writer.infrastructure.queues import CELERY_QUEUE_NAMES


def test_every_static_event_queue_is_declared():
    routed = {route.queue for route in EVENT_ROUTES.values()}
    assert routed <= set(CELERY_QUEUE_NAMES)


def test_queue_registry_matches_django_settings():
    assert tuple(queue.name for queue in settings.CELERY_TASK_QUEUES) == CELERY_QUEUE_NAMES


def test_invalidation_events_use_consumed_maintenance_queue():
    assert EVENT_ROUTES[("evidence.profile_decided", 1)].queue == "maintenance"
    assert EVENT_ROUTES[("topics.registry_decided", 1)].queue == "maintenance"
