import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

app = Celery("wisdome_writer")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.autodiscover_tasks(["wisdome_writer.infrastructure"])
app.set_default()
