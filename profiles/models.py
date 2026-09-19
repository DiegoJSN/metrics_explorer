import uuid
from django.db import models


class AuthorSummary(models.Model):
    id = models.IntegerField(primary_key=True)
    job_id = models.IntegerField(null=True)
    name = models.TextField()
    institution = models.TextField(null=True)
    zenodo_name = models.TextField(null=True)
    github_name = models.TextField(null=True)
    orcid = models.TextField(null=True)
    h_index = models.IntegerField(null=True)
    i10_index = models.IntegerField(null=True)
    n_citas = models.IntegerField(null=True)
    n_publicaciones = models.IntegerField(null=True)
    n_publicaciones_oa = models.IntegerField(null=True)
    publicaciones_citas_avrg = models.FloatField(null=True)
    profile_load_time_seconds = models.FloatField(null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "author_summary"
        app_label = "profiles"

class Work(models.Model):
    id = models.IntegerField(primary_key=True)
    job_id = models.IntegerField(null=True)
    author_id = models.IntegerField()
    type = models.TextField(null=True)
    doi = models.TextField(null=True)
    title = models.TextField(null=True)
    published_in = models.TextField(null=True)
    authors = models.TextField(null=True)
    year = models.TextField(null=True)
    cited_by = models.IntegerField(null=True)
    api_source = models.TextField(null=True)
    wikipedia_mentions = models.IntegerField(null=True)
    newsfeed_mentions = models.IntegerField(null=True)
    authors_institutions = models.TextField(null=True)
    institutions_country_code = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "works"
        app_label = "profiles"


class ZenodoGithub(models.Model):
    id = models.IntegerField(primary_key=True)
    job_id = models.IntegerField(null=True)
    author_id = models.IntegerField()
    type = models.TextField(null=True)
    doi = models.TextField(null=True)
    github_url = models.TextField(null=True)
    title = models.TextField(null=True)
    authors = models.TextField(null=True)
    year = models.TextField(null=True)
    cited_by = models.IntegerField(null=True)
    api_source = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "zenodo_github"
        app_label = "profiles"

class Job(models.Model):
    status = models.CharField(max_length=255, default="pending")
    percent = models.IntegerField(default=0)
    error = models.TextField(null=True, blank=True)
    result_author_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    celery_id = models.CharField(
        max_length=255, null=True, blank=True, unique=True, db_index=True,
        help_text="ID de la tarea de Celery (task_id)."
    )

    class Meta:
        managed = True
        db_table = "job"


class DailyVisitorsSummary(models.Model):
    day = models.DateField(unique=True)
    unique_visitors = models.IntegerField()
    total_visitors = models.IntegerField()

    class Meta:
        managed = True
        db_table = "daily_visitors_summary"
