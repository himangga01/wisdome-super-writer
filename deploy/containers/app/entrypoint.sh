#!/bin/sh
set -eu

python src/manage.py collectstatic --noinput

exec gunicorn wisdome_writer.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${WEB_CONCURRENCY:-3}" \
    --timeout "${GUNICORN_TIMEOUT_SECONDS:-60}" \
    --access-logfile - \
    --error-logfile -
