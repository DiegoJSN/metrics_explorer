import os
from datetime import timedelta
from pathlib import Path

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR

# -----------------------------------------------------------------------------
# Helpers (leer booleanos y listas desde variables de entorno)
# -----------------------------------------------------------------------------
def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")

def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default or []
    return [x.strip() for x in raw.split(",") if x.strip()]

# -----------------------------------------------------------------------------
# Core settings
# -----------------------------------------------------------------------------
ENV = os.getenv("ENV", "development").strip().lower()
DEMO_MODE = env_bool("DEMO_MODE", default=True)
DEBUG = env_bool("DEBUG", default=(ENV != "production"))

SECRET_KEY = os.getenv("SECRET_KEY", "")
if ENV == "production":
    # En producción NO permitimos una SECRET_KEY vacía o "de ejemplo"
    if (not SECRET_KEY) or ("REEMPLAZA" in SECRET_KEY.upper()) or SECRET_KEY.startswith("dev-"):
        raise RuntimeError(
            "SECRET_KEY no está configurada. "
            "Pon una SECRET_KEY real en tu archivo .env.txt (y NO la subas a GitHub)."
        )
else:
    # En local, si no la defines, ponemos una por defecto (solo para desarrollo)
    SECRET_KEY = SECRET_KEY or "dev-secret-key-change-me"

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", default=[])

# Si tu app va detrás de Nginx (reverse proxy), estas opciones evitan problemas comunes.
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", default=True)

# -----------------------------------------------------------------------------
# App config
# -----------------------------------------------------------------------------
RUN_ETL_SYNCHRONOUSLY = env_bool("RUN_ETL_SYNCHRONOUSLY", default=False)
LOCAL_ETL_MODE = env_bool("LOCAL_ETL_MODE", default=False)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "profiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise sirve estáticos desde Django (útil en despliegues simples con Docker)
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEMO_MODE:
    MIDDLEWARE.insert(3, "profiles.middleware.DailyVisitorTrackingMiddleware")

LANGUAGES = [
    ("es", "Español"),
    ("en", "English"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]

ROOT_URLCONF = "authorsite.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "profiles" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "authorsite.wsgi.application"
ASGI_APPLICATION = "authorsite.asgi.application"

# -----------------------------------------------------------------------------
# Databases (PostgreSQL)
# -----------------------------------------------------------------------------
# IMPORTANTE:
# - Cuando Django corre dentro de Docker, el host NO es 127.0.0.1
#   sino el nombre del servicio en docker-compose (por ejemplo: db).
if DEMO_MODE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.getenv("DEMO_DEFAULT_DB", str(BASE_DIR / "demo.sqlite3"))),
        },
        "authors": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.getenv("DEMO_AUTHORS_DB", str(BASE_DIR / "demo_authors.sqlite3"))),
        },
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("PG_DB", "authorsite"),
            "USER": os.getenv("PG_USER", "authorsite"),
            "PASSWORD": os.getenv("PG_PASSWORD", ""),
            "HOST": os.getenv("PG_HOST", "db"),
            "PORT": os.getenv("PG_PORT", "5432"),
        },
        "authors": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("PG_AUTHORS_DB", os.getenv("PG_DB", "authorsite")),
            "USER": os.getenv("PG_AUTHORS_USER", os.getenv("PG_USER", "authorsite")),
            "PASSWORD": os.getenv("PG_AUTHORS_PASSWORD", os.getenv("PG_PASSWORD", "")),
            "HOST": os.getenv("PG_AUTHORS_HOST", os.getenv("PG_HOST", "db")),
            "PORT": os.getenv("PG_AUTHORS_PORT", os.getenv("PG_PORT", "5432")),
        },
    }

DATABASE_ROUTERS = ["authorsite.dbrouters.AuthorsRouter"]

# -----------------------------------------------------------------------------
# Password validation (recomendado en producción)
# -----------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# -----------------------------------------------------------------------------
# Internationalization
# -----------------------------------------------------------------------------
LANGUAGE_CODE = "es"
LANGUAGES = [
    ("es", "Español"),
]
TIME_ZONE = "Europe/Madrid"
USE_I18N = True
USE_TZ = True

# -----------------------------------------------------------------------------
# Static / Media
# -----------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# En desarrollo, evita Manifest (más simple). En producción, usa manifest + compresión.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Opcional: ayuda a desarrollo si alguna vez quieres que Django sirva estáticos
WHITENOISE_USE_FINDERS = DEBUG or DEMO_MODE
WHITENOISE_AUTOREFRESH = DEBUG

MEDIA_URL = "/media/"
MEDIA_ROOT = PROJECT_ROOT / "media"


# -----------------------------------------------------------------------------
# Security toggles (valores controlados por .env.txt)
# -----------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", default=False)

# Si estás detrás de Nginx y terminas HTTPS ahí, Django necesita saberlo:
# En .env.txt pon: SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
raw_proxy_ssl_header = os.getenv("SECURE_PROXY_SSL_HEADER", "")
if raw_proxy_ssl_header:
    parts = [p.strip() for p in raw_proxy_ssl_header.split(",")]
    if len(parts) == 2 and parts[0] and parts[1]:
        SECURE_PROXY_SSL_HEADER = (parts[0], parts[1])

# -----------------------------------------------------------------------------
# Celery / Redis
# -----------------------------------------------------------------------------
# Preferimos usar URLs completas (más simples) y caemos a host/port si no están.
REDIS_URL = os.getenv("REDIS_URL", "")
if not REDIS_URL:
    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = os.getenv("REDIS_PORT", "6379")
    redis_password = os.getenv("REDIS_PASSWORD", "")
    if redis_password:
        REDIS_URL = f"redis://:{redis_password}@{redis_host}:{redis_port}/0"
    else:
        REDIS_URL = f"redis://{redis_host}:{redis_port}/0"

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

CELERY_TASK_TRACK_STARTED = True
CELERY_TIMEZONE = "Europe/Madrid"

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# Concurrencia y reciclado de workers (mitigación rápida de conexiones abiertas).
CELERY_WORKER_CONCURRENCY = int(os.getenv("CELERY_WORKER_CONCURRENCY", "4"))
CELERY_WORKER_MAX_TASKS_PER_CHILD = int(
    os.getenv("CELERY_WORKER_MAX_TASKS_PER_CHILD", "100")
)

# Límites de tiempo (segundos), controlables desde .env.txt
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "3600"))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "3000"))

CELERY_BEAT_SCHEDULE = {
    "cleanup-db-tables-every-14-days": {
        "task": "profiles.tasks.cleanup_tables_every_14_days",
        "schedule": timedelta(days=14),
    },
    "cleanup-daily-visitors-summaries": {
        "task": "profiles.tasks.cleanup_daily_visitors_summaries",
        "schedule": timedelta(days=1),
    },
}

# -----------------------------------------------------------------------------
# Sentry (opcional)
# -----------------------------------------------------------------------------
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
    )
