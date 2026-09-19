from pathlib import Path
from contextlib import contextmanager
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone
import os, runpy
from unittest.mock import patch
from .models import AuthorSummary, Job


def _ensure_author_summary_exists() -> None:
    """Create the minimum author table for a first local ETL run."""
    connection = connections["authors"]
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS author_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    name TEXT,
                    institution TEXT,
                    zenodo_name TEXT,
                    github_name TEXT,
                    orcid TEXT,
                    h_index INTEGER,
                    i10_index INTEGER,
                    n_citas INTEGER,
                    n_publicaciones INTEGER,
                    n_publicaciones_oa INTEGER,
                    publicaciones_citas_avrg REAL,
                    profile_load_time_seconds REAL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute("PRAGMA table_info(author_summary)")
            columns = {row[1] for row in cursor.fetchall()}
            if "profile_load_time_seconds" not in columns:
                cursor.execute(
                    "ALTER TABLE author_summary ADD COLUMN profile_load_time_seconds REAL"
                )
        else:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS author_summary (
                    id SERIAL PRIMARY KEY,
                    job_id INTEGER,
                    name TEXT,
                    institution TEXT,
                    zenodo_name TEXT,
                    github_name TEXT,
                    orcid TEXT,
                    h_index INTEGER,
                    i10_index INTEGER,
                    n_citas INTEGER,
                    n_publicaciones INTEGER,
                    n_publicaciones_oa INTEGER,
                    publicaciones_citas_avrg INTEGER,
                    profile_load_time_seconds DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE author_summary
                ADD COLUMN IF NOT EXISTS profile_load_time_seconds DOUBLE PRECISION
                """
            )


def _job_retention_days() -> int:
    """Días de retención para limpiar filas antiguas por job_id."""
    raw = os.getenv("ETL_JOB_RETENTION_DAYS", "14")
    try:
        return max(1, int(raw))
    except ValueError:
        return 14


def _purge_old_job_data(retention_days: int) -> None:
    """Purge ETL rows for jobs older than the retention window."""
    _ensure_author_summary_exists()
    cutoff = timezone.now() - timezone.timedelta(days=retention_days)
    old_job_ids = list(
        Job.objects.using("default")
        .filter(created_at__lt=cutoff)
        .exclude(status="running")
        .values_list("id", flat=True)
    )
    if not old_job_ids:
        return
    authors_tables = [
        "events_clean",
        "events",
        "works",
        "zenodo_github",
        "author_summary",
    ]

    with transaction.atomic(using="authors"):
        with connections["authors"].cursor() as cursor:
            placeholders = ",".join(["%s"] * len(old_job_ids))
            existing_tables = set(connections["authors"].introspection.table_names())
            for table in authors_tables:
                if table in existing_tables:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE job_id IN ({placeholders})",
                        old_job_ids,
                    )

    with transaction.atomic(using="default"):
        Job.objects.using("default").filter(id__in=old_job_ids).delete()


def _cleanup_tables_if_needed(job_id: int) -> None:
    """Clean tables using a per-job retention policy."""
    _ensure_author_summary_exists()
    retention_days = _job_retention_days()
    _purge_old_job_data(retention_days)


@contextmanager
def _cwd(tmp: Path):
    prev = Path.cwd(); os.chdir(tmp)
    try: yield
    finally: os.chdir(prev)


def _get_script_path() -> str:
    cands = [
        Path(settings.BASE_DIR) / "zz_tool_researchers_merged.py",
        Path(settings.BASE_DIR).parent / "zz_tool_researchers_merged.py",
        Path.cwd() / "zz_tool_researchers_merged.py",
    ]
    for p in cands:
        if p.exists(): return str(p.resolve())
    raise FileNotFoundError("No se encontró zz_tool_researchers_merged.py")


# Funcion para ejecutar el script
def _run_script_with_inputs(job_id, inputs, expect_fourth=False):
    def _input_feed(prompt: str = ""):
        if not _input_feed.queue:
            raise RuntimeError("Faltan inputs para el script.")
        return _input_feed.queue.pop(0)
    _input_feed.queue = list(inputs)

    _cleanup_tables_if_needed(job_id)

    script_path = _get_script_path()
    base_dir = Path(settings.BASE_DIR)
    with _cwd(base_dir):
        with patch("builtins.input", side_effect=_input_feed):
            runpy.run_path(
                script_path,
                run_name="__main__",
                init_globals={"JOB_ID_FROM_RUNNER": str(job_id)},
            )

            orcid = (inputs[0] or "").strip()
            researcher = (inputs[1] or "").strip()

            qs = AuthorSummary.objects.using("authors")
            author = None
            if orcid:
                orcid_norm = orcid.replace("-", "")
                author = qs.filter(job_id=job_id, orcid__in=[orcid, orcid_norm]).order_by("-id").first()
            elif researcher:
                author = qs.filter(job_id=job_id, name__iexact=researcher).order_by("-id").first()

            Job.objects.using("default").filter(pk=job_id).update(result_author_id=(author.id if author else None))
