from django.urls import path
from . import views

app_name = "profiles"

urlpatterns = [
    path("", views.search, name="search"),
    path("demo/", views.demo, name="demo"),
    path("candidates/", views.candidates, name="candidates"),
    path("choose/", views.choose_candidate, name="choose_candidate"),

    path("run-etl/", views.run_etl, name="run_etl"),
    path("progress/<uuid:job_id>/", views.progress, name="progress"),  # UUID
    path("loading/", views.loading, name="loading"),

    path("results/", views.results, name="results"),
]
