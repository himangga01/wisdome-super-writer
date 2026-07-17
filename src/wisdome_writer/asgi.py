import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")
application = get_asgi_application()

