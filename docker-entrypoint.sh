#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py seed_demo
python manage.py collectstatic --noinput
exec gunicorn authorsite.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 120
