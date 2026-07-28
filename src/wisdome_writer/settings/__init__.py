import json
import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from kombu import Queue

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPOSITORY_ROOT / "src"
BASE_DIR = SRC_ROOT
load_dotenv(REPOSITORY_ROOT / ".env")


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


WISDOME_ENVIRONMENT = env_value("WISDOME_ENVIRONMENT")
if WISDOME_ENVIRONMENT not in {"development", "production"}:
    raise ImproperlyConfigured(
        "WISDOME_ENVIRONMENT must be explicitly set to 'development' or 'production'."
    )

IS_PRODUCTION = WISDOME_ENVIRONMENT == "production"
SECRET_KEY = env_value(
    "DJANGO_SECRET_KEY",
    "development-only-secret-key" if not IS_PRODUCTION else "",
)
DEBUG = env_bool("DJANGO_DEBUG", not IS_PRODUCTION)
ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1" if not IS_PRODUCTION else "",
)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

DATABASE_URL = env_value(
    "DATABASE_URL",
    f"sqlite:///{REPOSITORY_ROOT / 'db.sqlite3'}" if not IS_PRODUCTION else "",
)
REDIS_URL = env_value("REDIS_URL", "redis://localhost:6379/0" if not IS_PRODUCTION else "")
CELERY_BROKER_URL = env_value("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = env_value(
    "CELERY_RESULT_BACKEND",
    "redis://localhost:6379/1" if not IS_PRODUCTION else "",
)

AWS_ACCESS_KEY_ID = env_value("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = env_value("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = env_value(
    "AWS_STORAGE_BUCKET_NAME",
    "wisdome-writer" if not IS_PRODUCTION else "",
)
AWS_S3_ENDPOINT_URL = env_value("AWS_S3_ENDPOINT_URL") or None
AWS_S3_REGION_NAME = env_value(
    "AWS_S3_REGION_NAME",
    "ap-northeast-2" if not IS_PRODUCTION else "",
)
AWS_S3_ADDRESSING_STYLE = env_value("AWS_S3_ADDRESSING_STYLE", "path")
AUDIT_CURSOR_SIGNING_KEY = env_value(
    "AUDIT_CURSOR_SIGNING_KEY",
    SECRET_KEY if not IS_PRODUCTION else "",
)


def validate_production_configuration() -> None:
    if not IS_PRODUCTION:
        return

    errors: list[str] = []
    unsafe_values = {
        "change-me",
        "changeme",
        "development-only-secret-key",
        "minioadmin",
        "replace-me",
        "replace-with-a-dedicated-audit-cursor-key",
        "replace-with-a-long-random-value",
        "wisdome",
    }

    def require_safe_secret(name: str, value: str) -> None:
        if not value:
            errors.append(f"{name} is required")
        elif len(value) < 32 or value.lower() in unsafe_values:
            errors.append(f"{name} must be a non-placeholder value of at least 32 characters")

    require_safe_secret("DJANGO_SECRET_KEY", SECRET_KEY)
    require_safe_secret("AUDIT_CURSOR_SIGNING_KEY", AUDIT_CURSOR_SIGNING_KEY)
    if SECRET_KEY and AUDIT_CURSOR_SIGNING_KEY == SECRET_KEY:
        errors.append("AUDIT_CURSOR_SIGNING_KEY must be distinct from DJANGO_SECRET_KEY")
    if DEBUG:
        errors.append("DJANGO_DEBUG must be false")
    if not ALLOWED_HOSTS or any(
        host in {"*", "localhost", "127.0.0.1", "::1"} for host in ALLOWED_HOSTS
    ):
        errors.append("DJANGO_ALLOWED_HOSTS must contain explicit non-local production hosts")
    if not CSRF_TRUSTED_ORIGINS or any(
        not origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS
    ):
        errors.append("DJANGO_CSRF_TRUSTED_ORIGINS must contain only HTTPS origins")

    database = urlparse(DATABASE_URL)
    if (
        database.scheme not in {"postgres", "postgresql"}
        or not database.hostname
        or not database.username
        or not database.password
        or database.username.lower() in unsafe_values
        or database.password.lower() in unsafe_values
    ):
        errors.append("DATABASE_URL must be an authenticated PostgreSQL URL without defaults")

    for name, value in (
        ("REDIS_URL", REDIS_URL),
        ("CELERY_BROKER_URL", CELERY_BROKER_URL),
        ("CELERY_RESULT_BACKEND", CELERY_RESULT_BACKEND),
    ):
        parsed = urlparse(value)
        internal_redis = parsed.scheme == "redis" and parsed.hostname == "redis"
        if (parsed.scheme != "rediss" and not internal_redis) or not parsed.password:
            errors.append(
                f"{name} must use authenticated TLS or the internal Compose Redis service"
            )

    for name, value in (
        ("AWS_ACCESS_KEY_ID", AWS_ACCESS_KEY_ID),
        ("AWS_SECRET_ACCESS_KEY", AWS_SECRET_ACCESS_KEY),
        ("AWS_STORAGE_BUCKET_NAME", AWS_STORAGE_BUCKET_NAME),
        ("AWS_S3_REGION_NAME", AWS_S3_REGION_NAME),
    ):
        if not value or value.lower() in unsafe_values:
            errors.append(f"{name} is required and must not use a committed default")
    object_endpoint = urlparse(AWS_S3_ENDPOINT_URL or "")
    internal_object_storage = (
        object_endpoint.scheme == "http" and object_endpoint.hostname == "minio"
    )
    if AWS_S3_ENDPOINT_URL and not (
        AWS_S3_ENDPOINT_URL.startswith("https://") or internal_object_storage
    ):
        errors.append(
            "AWS_S3_ENDPOINT_URL must use HTTPS or the internal Compose object-storage service"
        )

    if errors:
        raise ImproperlyConfigured(
            "Unsafe production configuration: " + "; ".join(errors)
        )


validate_production_configuration()

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.audit.apps.AuditConfig",
    "wisdome_writer.infrastructure.apps.InfrastructureConfig",
    "apps.topics.apps.TopicsConfig",
    "apps.collection.apps.CollectionConfig",
    "apps.evidence.apps.EvidenceConfig",
    "apps.editorial.apps.EditorialConfig",
    "apps.publishing.apps.PublishingConfig",
    "apps.scheduling.apps.SchedulingConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wisdome_writer.observability.CorrelationIdMiddleware",
    "wisdome_writer.api.middleware.AdminApiSecurityMiddleware",
    "wisdome_writer.api.middleware.ProblemDetailsMiddleware",
]

ROOT_URLCONF = "wisdome_writer.urls"
WSGI_APPLICATION = "wisdome_writer.wsgi.application"
ASGI_APPLICATION = "wisdome_writer.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [SRC_ROOT / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=60,
        conn_health_checks=True,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "accounts.AdminAccount"
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = REPOSITORY_ROOT / "staticfiles"
STATICFILES_DIRS = [SRC_ROOT / "static"]
MEDIA_ROOT = REPOSITORY_ROOT / "media"
MEDIA_URL = "/media/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 28 * 60
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_CREATE_MISSING_QUEUES = False
CELERY_TASK_QUEUES = tuple(
    Queue(name)
    for name in (
        "default",
        "collect",
        "extract.generic",
        "extract.ocr.paddle",
        "generate",
        "publish",
    )
)
CELERY_BEAT_SCHEDULE = {
    "dispatch-due-schedules": {
        "task": "apps.scheduling.tasks.dispatch_due_schedules_task",
        "schedule": 60.0,
    }
}
CELERY_TASK_ROUTES = {
    "apps.evidence.tasks.process_paddleocr_document": {"queue": "extract.ocr.paddle"},
    "apps.collection.*": {"queue": "collect"},
    "apps.evidence.*": {"queue": "extract.generic"},
    "apps.editorial.*": {"queue": "generate"},
    "apps.publishing.*": {"queue": "publish"},
    "apps.scheduling.*": {"queue": "default"},
    "apps.audit.*": {"queue": "default"},
}

OBJECT_STORAGE_PRESIGN_TTL_SECONDS = int(os.getenv("OBJECT_STORAGE_PRESIGN_TTL_SECONDS", "300"))

REAUTH_PROOF_TTL_SECONDS = min(int(os.getenv("REAUTH_PROOF_TTL_SECONDS", "300")), 300)
REAUTH_MFA_REQUIRED = env_bool("REAUTH_MFA_REQUIRED", False)
REAUTH_MFA_VALIDATOR = os.getenv("REAUTH_MFA_VALIDATOR", "")
BLOGGER_OAUTH_CLIENT_ID_REF = os.getenv("BLOGGER_OAUTH_CLIENT_ID_REF", "")
BLOGGER_OAUTH_CLIENT_SECRET_REF = os.getenv("BLOGGER_OAUTH_CLIENT_SECRET_REF", "")
BLOGGER_OAUTH_TOKEN_STORE = os.getenv("BLOGGER_OAUTH_TOKEN_STORE", "")
SECRET_PROVIDER_CLASSES = json.loads(os.getenv("SECRET_PROVIDER_CLASSES", "{}"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"correlation": {"()": "wisdome_writer.observability.CorrelationIdFilter"}},
    "formatters": {
        "json": {
            "format": '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","correlation_id":"%(correlation_id)s","message":"%(message)s"}',
            "style": "%",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["correlation"],
        }
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
