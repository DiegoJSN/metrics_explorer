# authorsite/celery.py
import os

from celery import Celery
from celery.signals import task_postrun, task_prerun
from django.db import close_old_connections

# Señala a Celery cuál es tu settings de Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authorsite.settings")

# Crea la app Celery
app = Celery("authorsite")

# Lee config desde Django settings con prefijo CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodescubre tasks.py en cada app de INSTALLED_APPS
app.autodiscover_tasks()

# Cierra conexiones obsoletas en cada tarea para evitar acumulación en workers.
@task_prerun.connect(weak=False, dispatch_uid="close_connections_prerun")
def _close_connections_before_task(*args, **kwargs):
    close_old_connections()


@task_postrun.connect(weak=False, dispatch_uid="close_connections_postrun")
def _close_connections_after_task(*args, **kwargs):
    close_old_connections()

# (Opcional) Tarea de prueba
@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
