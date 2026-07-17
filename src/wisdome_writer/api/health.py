from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis import Redis


@require_GET
def live(request):
    return JsonResponse({"status": "ok", "service": "wisdome-super-writer"})


@require_GET
def ready(request):
    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"

    try:
        client = Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        checks["redis"] = "ok" if client.ping() else "unavailable"
    except Exception:
        checks["redis"] = "unavailable"

    is_ready = all(value == "ok" for value in checks.values())
    return JsonResponse(
        {"status": "ok" if is_ready else "unavailable", "checks": checks},
        status=200 if is_ready else 503,
    )


@require_GET
def api_root(request):
    return JsonResponse(
        {
            "service": "wisdome-super-writer",
            "apiVersion": "v1",
            "authenticatedAdmin": str(request.user.pk),
        }
    )

