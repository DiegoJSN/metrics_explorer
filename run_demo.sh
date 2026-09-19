#!/bin/sh
set -eu
cd "$(dirname "$0")"

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-demo.txt

export DEMO_MODE=true
export ENV=development
export DEBUG=false
export SECRET_KEY=demo-local-secret-not-for-production
python manage.py migrate --noinput
python manage.py seed_demo

echo "Demo ready: http://127.0.0.1:8000/es/"
python manage.py runserver 127.0.0.1:8000
