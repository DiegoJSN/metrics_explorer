"""Django URL configuration for Authorsite project.

This version includes a health check endpoint outside of the i18n patterns.
The health check (`/healthz`) validates connectivity to the PostgreSQL
database and Redis broker by delegating to the `profiles.views.healthz`
view.
"""

from django.contrib import admin
from django.urls import path, include
from django.conf.urls.i18n import i18n_patterns

# Import the health check view.  We import from profiles.views rather than
# profiles.urls to avoid circular imports and to make it clear where the
# endpoint is defined.
from profiles.views import healthz

urlpatterns = [
    # Real health check endpoint.  This route is outside of the i18n
    # patterns to ensure that liveness probes do not depend on the
    # language prefix.
    path("healthz/", healthz, name="healthz"),

    # URL para cambiar de idioma (set_language)
    path("i18n/", include("django.conf.urls.i18n")),
]

urlpatterns += i18n_patterns(
    # Django admin
    path("admin/", admin.site.urls),

    # Todas las rutas de la app `profiles` con el namespace "profiles"
    path("", include(("profiles.urls", "profiles"), namespace="profiles")),
)