from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", Path.home())) / "MetricsExplorerDemo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def free_port() -> int:
    for port in (8000, 5050, 8080, 5000):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


base = application_dir()
storage = data_dir()
os.chdir(base)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authorsite.settings")
os.environ["DEMO_MODE"] = "true"
os.environ["LOCAL_ETL_MODE"] = "true"
os.environ["ENV"] = "development"
os.environ["DEBUG"] = "false"
os.environ.setdefault("SECRET_KEY", "local-executable-session-key")
os.environ["ALLOWED_HOSTS"] = "localhost,127.0.0.1,[::1]"
os.environ["DEMO_DEFAULT_DB"] = str(storage / "django-live.sqlite3")
os.environ["DEMO_AUTHORS_DB"] = str(storage / "researchers-live.sqlite3")
os.environ.setdefault("OPENALEX_MAILTO", "info@metricsschool.com")
os.environ.setdefault("HTTP_UA", "MetricsExplorerDemo/1.0 (+https://metricsschool.com)")

import etl_runtime  # Ensures the dynamically loaded ETL is fully packaged.
import django

django.setup()

from django.core.management import call_command
from waitress import serve

call_command("migrate", interactive=False, verbosity=0)
call_command("collectstatic", interactive=False, verbosity=0)

from authorsite.wsgi import application

requested_port = os.getenv("METRICS_EXPLORER_PORT", "").strip()
port = int(requested_port) if requested_port else free_port()
url = f"http://127.0.0.1:{port}/es/"

print("=" * 68)
print(" Metrics Explorer — local full-flow demo")
print("=" * 68)
print(f"Opening {url}")
print("Internet access is required for live researcher analysis.")
print("Keep this window open. Press Ctrl+C to stop the application.")
print("=" * 68)

threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
serve(application, host="127.0.0.1", port=port, threads=6)
