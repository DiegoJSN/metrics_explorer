# profiles/tasks.py
from __future__ import annotations
from typing import List  # si usas Python 3.9+, puedes usar list[str] directamente
from celery import shared_task
from dateutil.relativedelta import relativedelta
from django.db import close_old_connections, connections
from django.utils import timezone
from .models import DailyVisitorsSummary, Job
from .runner import _job_retention_days, _purge_old_job_data, _run_script_with_inputs
from django.utils.translation import gettext as _

@shared_task(bind=True)
def run_etl_task(self, job_id: int, inputs: List[str], **kwargs) -> None:
    """
    Tarea Celery que ejecuta tu ETL y va actualizando el modelo Job.
    Sustituye el bloque 'simulado' por tus pasos reales de ETL,
    llamando a updates parciales de percent/status.
    """
    print("DEBUG_ARCS: ", job_id, inputs)
    print("DEBUG_KWARGS: ", kwargs)

    close_old_connections()  # higiene conexiones fuera del ciclo request
    job = Job.objects.get(pk=job_id)

    try:
      # start
        job.status = "running"; job.percent = 1
        job.save(update_fields=["status", "percent"])

        # normaliza inputs para el script:
        # [orcid, researcher, github, abs_idx?]
        orcid      = (inputs[0] if len(inputs) > 0 else "") or ""
        researcher = (inputs[1] if len(inputs) > 1 else "") or ""
        github     = (inputs[2] if len(inputs) > 2 else "") or ""
        abs_idx    = (inputs[3] if len(inputs) > 3 else "") or ""
        real_inputs = [orcid, researcher, github]
        if abs_idx:
            real_inputs.append(abs_idx)


        # ejecuta TU script (bloquea hasta terminar)
        _run_script_with_inputs(job_id, real_inputs)

        # fin
        finished_at = timezone.now()
        elapsed_seconds = max(0, int((finished_at - job.created_at).total_seconds()))
        with connections["authors"].cursor() as cursor:
            cursor.execute(
                """
                UPDATE author_summary
                SET profile_load_time_seconds = %s
                WHERE job_id = %s
                """,
                [elapsed_seconds, job_id],
            )

        job.status = "done"
        job.percent = 100
        job.finished_at = finished_at
        job.save(update_fields=["status", "percent", "finished_at"])

    except Exception as e:
        job.refresh_from_db(fields=["error", "percent"])
        job.status = "error"
        if not job.error:
            job.error = _("Error interno al ejecutar el proceso: %(err)s") % {
                "err": f"{type(e).__name__}: {e}"
            }
        job.save(update_fields=["status", "error"])
        raise


@shared_task
def cleanup_tables_every_14_days() -> None:
    """Purga filas antiguas por job_id según la ventana de retención."""
    close_old_connections()
    retention_days = _job_retention_days()
    _purge_old_job_data(retention_days)


@shared_task
def cleanup_daily_visitors_summaries() -> None:
    """Elimina resúmenes de visitas diarios antiguos."""
    close_old_connections()
    cutoff = timezone.localdate() - relativedelta(months=13)
    DailyVisitorsSummary.objects.filter(day__lt=cutoff).delete()
