import os
import glob
import json
import runpy
import shutil
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
from collections import defaultdict
from types import SimpleNamespace
import requests
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render, redirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render, redirect
from django.db import DatabaseError, connections
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse, HttpRequest
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.db import close_old_connections
from urllib.parse import urlencode

from .forms import SearchForm
from .models import AuthorSummary, Work, Job, ZenodoGithub
from .runner import _run_script_with_inputs
#from __future__ import annotations
import time
import uuid
import threading

# Added for real health check: use Redis library
import redis


# Celery task import (if exists)
try:
    from .tasks import run_etl_task  # tarea Celery real, si existe
except Exception:
    run_etl_task = None

@contextmanager
def _cwd(tmp_path: Path):
    prev = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield
    finally:
        os.chdir(prev)

# Localiza la ruta del script
def _get_script_path() -> str:
    candidates = [
        Path(settings.BASE_DIR) / "zz_tool_researchers_merged.py",
        Path(settings.BASE_DIR).parent / "zz_tool_researchers_merged.py",
        Path.cwd() / "zz_tool_researchers_merged.py",
    ]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    raise FileNotFoundError(
        _("No se encontró zz_tool_researchers_merged.py en: ")
        + " | ".join(str(p) for p in candidates)
    )


def _run_etl_task_stub(job_id, inputs):
    close_old_connections()  # higiene de conexiones

    job = Job.objects.using("default").get(pk=job_id)
    try:
        job.status = "running"; job.percent = 5
        job.save(update_fields=["status", "percent"]); time.sleep(1.0)

        for pct in (15, 30, 45, 60, 75, 90):
            job.percent = pct
            job.save(update_fields=["percent"])
            time.sleep(1.0)

        job.status = "done"; job.percent = 100
        job.save(update_fields=["status", "percent"])
    except Exception as e:
        job.status = "error"; job.error = f"{type(e).__name__}: {e}"
        job.save(update_fields=["status", "error"])

    """Simula progreso y/o ejecuta tu script real, actualizando Job.percent."""
    '''import time
    job = Job.objects.get(pk=job_id)
    try:
        job.status = "running"; job.percent = 5; job.save(update_fields=["status","percent"])
        time.sleep(1); job.percent = 15; job.save(update_fields=["percent"])
        time.sleep(1); job.percent = 30; job.save(update_fields=["percent"])
        time.sleep(1); job.percent = 45; job.save(update_fields=["percent"])

        # Si ya quieres disparar tu script real aquí, hazlo
        # _run_script_with_inputs(job, inputs=inputs)

        time.sleep(1); job.percent = 60; job.save(update_fields=["percent"])
        time.sleep(1); job.percent = 75; job.save(update_fields=["percent"])
        time.sleep(1); job.percent = 90; job.save(update_fields=["percent"])

        job.status = "done"; job.percent = 100
        job.save(update_fields=["status","percent"])
        except Exception as e:
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.save(update_fields=["status","error"])'''

def _run_etl_task_stub_start(job_id: int, inputs: list[str]) -> None:
    """
    Simulación de ejecución si el worker Celery no está disponible.
    No bloquea la request (usa Celery? No. Pero deja la UX operable).
    """
    # NOTA: este stub se ejecuta de forma rápida y no requiere threads aquí,
    # porque solo lo usamos en entornos sin Celery. Si quieres, puedes
    # convertirlo en una tarea síncrona y verás progreso al hacer polling.


def _normalize_orcid_variants(orcid: str | None) -> list[str]:
    if not orcid:
        return []
    raw = (orcid or "").strip()
    if not raw:
        return []
    raw_no_prefix = raw.replace("https://orcid.org/", "").strip()
    raw_no_prefix = raw_no_prefix.replace("http://orcid.org/", "").strip()
    no_dash = raw_no_prefix.replace("-", "")
    variants = [
        raw,
        raw_no_prefix,
        no_dash,
        f"https://orcid.org/{raw_no_prefix}" if raw_no_prefix else "",
    ]
    seen = []
    for item in variants:
        if item and item not in seen:
            seen.append(item)
    return seen


def _github_match_filter(github: str | None, *, allow_any_if_blank: bool = False) -> Q:
    github_clean = (github or "").strip()
    if not github_clean:
        if allow_any_if_blank:
            return Q()
        return Q(github_name__isnull=True) | Q(github_name__exact="") | Q(github_name__iexact="skip")
    return Q(github_name__iexact=github_clean)


def _find_existing_author_by_orcid(orcid: str | None, github: str | None) -> AuthorSummary | None:
    orcid_candidates = _normalize_orcid_variants(orcid)
    if not orcid_candidates:
        return None
    try:
        github_filter = _github_match_filter(github, allow_any_if_blank=True)
        return (
            AuthorSummary.objects.using("authors")
            .filter(Q(orcid__in=orcid_candidates) & github_filter)
            .order_by("-id")
            .first()
        )
    except DatabaseError:
        return None


def _find_existing_author_by_profile(
    name: str | None,
    institution: str | None,
    github: str | None,
    orcid: str | None,
) -> AuthorSummary | None:
    name_clean = (name or "").strip()
    institution_clean = (institution or "").strip()
    if not any([name_clean, institution_clean, (github or "").strip(), (orcid or "").strip()]):
        return None
    filters = Q()
    if name_clean:
        filters &= Q(name__iexact=name_clean)
    if institution_clean:
        filters &= Q(institution__iexact=institution_clean)
    if github:
        filters &= _github_match_filter(github)
    if orcid:
        orcid_candidates = _normalize_orcid_variants(orcid)
        if orcid_candidates:
            filters &= Q(orcid__in=orcid_candidates)
    try:
        return (
            AuthorSummary.objects.using("authors")
            .filter(filters)
            .order_by("-id")
            .first()
        )
    except DatabaseError:
        return None


def _related_author_ids(author: AuthorSummary, db_alias: str) -> list[int]:
    """Return current author id plus historic ids for the same ORCID/name profile."""
    ids = [author.id]
    try:
        qs = AuthorSummary.objects.using(db_alias)
        orcid_variants = _normalize_orcid_variants(author.orcid)
        if orcid_variants:
            related_ids = list(
                qs.filter(orcid__in=orcid_variants).values_list("id", flat=True)
            )
        else:
            name_clean = (author.name or "").strip()
            if not name_clean:
                return ids
            related_ids = list(
                qs.filter(name__iexact=name_clean).values_list("id", flat=True)
            )
        for related_id in related_ids:
            if related_id not in ids:
                ids.append(related_id)
    except DatabaseError:
        return ids
    return ids


def _build_results_context(
    request: HttpRequest,
    task_id: str | None,
    job_pk: int | None = None,
    author_id: int | None = None,
    requested_orcid: str | None = None,
    requested_name: str | None = None,
) -> dict:
    job = None
    if job_pk is not None:
        job = Job.objects.using("default").filter(pk=job_pk).first()
    if job is None and task_id:
        job = Job.objects.using("default").filter(celery_id=str(task_id)).first()
    job_id = job.id if job else None
    error_message = None

    try:
        author = None
        use_raw_works = False
        raw_all_works = []

        def _fetch_raw_author(alias: str, job_id_value: int | None) -> SimpleNamespace | None:
            with connections[alias].cursor() as cursor:
                if job_id_value is None:
                    cursor.execute(
                        """
                        SELECT id, name, institution, zenodo_name, github_name, orcid,
                               h_index, i10_index, n_citas, n_publicaciones,
                               n_publicaciones_oa, publicaciones_citas_avrg,
                               profile_load_time_seconds, created_at
                        FROM author_summary
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, name, institution, zenodo_name, github_name, orcid,
                               h_index, i10_index, n_citas, n_publicaciones,
                               n_publicaciones_oa, publicaciones_citas_avrg,
                               profile_load_time_seconds, created_at
                        FROM author_summary
                        WHERE job_id = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        [job_id_value],
                    )
                row = cursor.fetchone()
            if not row:
                return None
            keys = [
                "id",
                "name",
                "institution",
                "zenodo_name",
                "github_name",
                "orcid",
                "h_index",
                "i10_index",
                "n_citas",
                "n_publicaciones",
                "n_publicaciones_oa",
                "publicaciones_citas_avrg",
                "profile_load_time_seconds",
                "created_at",
            ]
            return SimpleNamespace(**dict(zip(keys, row)))

        def _fetch_raw_works(alias: str, author_id: int, job_id_value: int | None) -> list[SimpleNamespace]:
            with connections[alias].cursor() as cursor:
                if job_id_value is None:
                    cursor.execute(
                        """
                        SELECT id, author_id, type, doi, title, published_in, authors, year,
                               cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                               authors_institutions, institutions_country_code
                        FROM works
                        WHERE author_id = %s
                        ORDER BY cited_by DESC NULLS LAST, year
                        """,
                        [author_id],
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, author_id, type, doi, title, published_in, authors, year,
                               cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                               authors_institutions, institutions_country_code
                        FROM works
                        WHERE author_id = %s AND job_id = %s
                        ORDER BY cited_by DESC NULLS LAST, year
                        """,
                        [author_id, job_id_value],
                    )
                rows = cursor.fetchall()
            keys = [
                "id",
                "author_id",
                "type",
                "doi",
                "title",
                "published_in",
                "authors",
                "year",
                "cited_by",
                "api_source",
                "wikipedia_mentions",
                "newsfeed_mentions",
                "authors_institutions",
                "institutions_country_code",
            ]
            return [SimpleNamespace(**dict(zip(keys, row))) for row in rows]

        def _fetch_raw_top_works(alias: str, job_id_value: int | None, limit: int = 5) -> list[SimpleNamespace]:
            with connections[alias].cursor() as cursor:
                if job_id_value is None:
                    cursor.execute(
                        """
                        SELECT id, author_id, type, doi, title, published_in, authors, year,
                               cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                               authors_institutions, institutions_country_code
                        FROM works
                        ORDER BY cited_by DESC NULLS LAST, year
                        LIMIT %s
                        """,
                        [limit],
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, author_id, type, doi, title, published_in, authors, year,
                               cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                               authors_institutions, institutions_country_code
                        FROM works
                        WHERE job_id = %s
                        ORDER BY cited_by DESC NULLS LAST, year
                        LIMIT %s
                        """,
                        [job_id_value, limit],
                    )
                rows = cursor.fetchall()
            keys = [
                "id",
                "author_id",
                "type",
                "doi",
                "title",
                "published_in",
                "authors",
                "year",
                "cited_by",
                "api_source",
                "wikipedia_mentions",
                "newsfeed_mentions",
                "authors_institutions",
                "institutions_country_code",
            ]
            return [SimpleNamespace(**dict(zip(keys, row))) for row in rows]

        def _fetch_raw_works_all(alias: str, job_id_value: int | None) -> list[SimpleNamespace]:
            with connections[alias].cursor() as cursor:
                if job_id_value is None:
                    cursor.execute(
                        """
                        SELECT id, author_id, type, doi, title, published_in, authors, year,
                               cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                               authors_institutions, institutions_country_code
                        FROM works
                        """
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, author_id, type, doi, title, published_in, authors, year,
                               cited_by, api_source, wikipedia_mentions, newsfeed_mentions,
                               authors_institutions, institutions_country_code
                        FROM works
                        WHERE job_id = %s
                        """,
                        [job_id_value],
                    )
                rows = cursor.fetchall()
            keys = [
                "id",
                "author_id",
                "type",
                "doi",
                "title",
                "published_in",
                "authors",
                "year",
                "cited_by",
                "api_source",
                "wikipedia_mentions",
                "newsfeed_mentions",
                "authors_institutions",
                "institutions_country_code",
            ]
            return [SimpleNamespace(**dict(zip(keys, row))) for row in rows]
        def _table_count(alias: str, table: str, job_id_value: int | None) -> int:
            try:
                with connections[alias].cursor() as cursor:
                    if job_id_value is None:
                        cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    else:
                        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id = %s", [job_id_value])
                    row = cursor.fetchone()
                return int(row[0] or 0) if row else 0
            except Exception:
                return 0

        def _table_exists(alias: str, table: str) -> bool:
            try:
                return table in connections[alias].introspection.table_names()
            except Exception:
                return False

        authors_count = _table_count("authors", "author_summary", job_id)
        default_count = _table_count("default", "author_summary", job_id)
        db_alias = "authors" if authors_count >= default_count else "default"
        if job and job.result_author_id:
            author_qs = AuthorSummary.objects.using(db_alias).filter(id=job.result_author_id)
            if job_id is not None:
                author_qs = author_qs.filter(job_id=job_id)
            author = author_qs.first()
        if author is None and author_id:
            author_qs = AuthorSummary.objects.using(db_alias).filter(id=author_id)
            if job_id is not None:
                author_qs = author_qs.filter(job_id=job_id)
            author = author_qs.first()
        if author is None:
            db_alias = "default"
            if job and job.result_author_id:
                author_qs = AuthorSummary.objects.using(db_alias).filter(id=job.result_author_id)
                if job_id is not None:
                    author_qs = author_qs.filter(job_id=job_id)
                author = author_qs.first()
            if author is None and author_id:
                author_qs = AuthorSummary.objects.using(db_alias).filter(id=author_id)
                if job_id is not None:
                    author_qs = author_qs.filter(job_id=job_id)
                author = author_qs.first()
        if author is None:
            for candidate_alias in ("authors", "default"):
                with connections[candidate_alias].cursor() as cursor:
                    if job_id is None:
                        cursor.execute("SELECT id FROM author_summary ORDER BY id DESC LIMIT 1")
                    else:
                        cursor.execute(
                            "SELECT id FROM author_summary WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                            [job_id],
                        )
                    row = cursor.fetchone()
                if row:
                    db_alias = candidate_alias
                    author_qs = AuthorSummary.objects.using(db_alias).filter(id=row[0])
                    if job_id is not None:
                        author_qs = author_qs.filter(job_id=job_id)
                    author = author_qs.first()
                    if author:
                        break

        works_qs = Work.objects.using(db_alias)
        if author:
            works_qs = works_qs.filter(author_id=author.id)
            if job_id is not None:
                works_qs = works_qs.filter(job_id=job_id)
        works_qs = works_qs.order_by("-cited_by", "year")
        top5 = list(works_qs[:5])
        if author and not top5:
            fallback_work = Work.objects.using(db_alias)
            if job_id is not None:
                fallback_work = fallback_work.filter(job_id=job_id)
            fallback_work = fallback_work.order_by("-id").first()
            if fallback_work:
                author_qs = AuthorSummary.objects.using(db_alias).filter(id=fallback_work.author_id)
                if job_id is not None:
                    author_qs = author_qs.filter(job_id=job_id)
                author = author_qs.first()
                if author:
                    works_qs = Work.objects.using(db_alias).filter(author_id=author.id)
                    if job_id is not None:
                        works_qs = works_qs.filter(job_id=job_id)
                    works_qs = works_qs.order_by("-cited_by", "year")
                    top5 = list(works_qs[:5])
        if author is None and not top5:
            fallback_work = Work.objects.using(db_alias)
            if job_id is not None:
                fallback_work = fallback_work.filter(job_id=job_id)
            fallback_work = fallback_work.order_by("-id").first()
            if fallback_work:
                author_qs = AuthorSummary.objects.using(db_alias).filter(id=fallback_work.author_id)
                if job_id is not None:
                    author_qs = author_qs.filter(job_id=job_id)
                author = author_qs.first()
        if author is None or not top5:
            for candidate_alias in ("authors", "default"):
                raw_author = _fetch_raw_author(candidate_alias, job_id)
                if not raw_author:
                    continue
                raw_works = _fetch_raw_works(candidate_alias, raw_author.id, job_id)
                if not raw_works:
                    continue
                author = raw_author
                db_alias = candidate_alias
                raw_all_works = raw_works
                top5 = raw_works[:5]
                use_raw_works = True
                break

        if author is None:
            requested_orcid_variants = _normalize_orcid_variants(requested_orcid)
            requested_name_clean = (requested_name or "").strip()
            for candidate_alias in ("authors", "default"):
                author_qs = AuthorSummary.objects.using(candidate_alias)
                recovered_author = None
                if requested_orcid_variants:
                    recovered_author = author_qs.filter(orcid__in=requested_orcid_variants).order_by("-id").first()
                if recovered_author is None and requested_name_clean:
                    recovered_author = author_qs.filter(name__iexact=requested_name_clean).order_by("-id").first()
                if recovered_author is None:
                    continue
                author = recovered_author
                db_alias = candidate_alias
                if not top5:
                    works_qs = Work.objects.using(candidate_alias).filter(author_id=recovered_author.id)
                    top5 = list(works_qs.order_by("-cited_by", "year")[:5])
                break
        if not top5:
            for candidate_alias in ("authors", "default"):
                raw_top5 = _fetch_raw_top_works(candidate_alias, job_id)
                if not raw_top5:
                    continue
                top5 = raw_top5
                raw_all_works = _fetch_raw_works_all(candidate_alias, job_id)
                use_raw_works = True
                db_alias = candidate_alias
                if not author:
                    author = _fetch_raw_author(candidate_alias, job_id)
                if author is None:
                    first_authors = (raw_top5[0].authors or "").split(";")[0].strip()
                    author = SimpleNamespace(
                        id=raw_top5[0].author_id,
                        name=first_authors or _("Autor desconocido"),
                        institution=None,
                        zenodo_name=None,
                        github_name=None,
                        orcid=None,
                        h_index=None,
                        i10_index=None,
                        n_citas=None,
                        n_publicaciones=None,
                        n_publicaciones_oa=None,
                        publicaciones_citas_avrg=None,
                        created_at=None,
                    )
                break

        top5_wikipedia = []
        if author:
            related_author_ids = _related_author_ids(author, db_alias)
            wikipedia_qs = Work.objects.using(db_alias).filter(author_id=author.id)
            if job_id is not None:
                wikipedia_qs = wikipedia_qs.filter(job_id=job_id)
            wikipedia_qs = (
                wikipedia_qs
                .exclude(wikipedia_mentions__isnull=True)
                .exclude(wikipedia_mentions=0)
                .order_by("-wikipedia_mentions", "year")
            )
            raw_wikipedia = list(wikipedia_qs[:5])
            using_historic_wikipedia = False

            if not raw_wikipedia and len(related_author_ids) > 1:
                wikipedia_qs = (
                    Work.objects.using(db_alias)
                    .filter(author_id__in=related_author_ids)
                    .exclude(wikipedia_mentions__isnull=True)
                    .exclude(wikipedia_mentions=0)
                    .order_by("-wikipedia_mentions", "year")
                )
                raw_wikipedia = list(wikipedia_qs[:5])
                using_historic_wikipedia = bool(raw_wikipedia)

            wikipedia_links = {}
            if _table_exists(db_alias, "events_clean"):
                with connections[db_alias].cursor() as cursor:
                    placeholders = ", ".join(["%s"] * len(related_author_ids))
                    if job_id is not None and not using_historic_wikipedia:
                        cursor.execute(
                            f"""
                            SELECT work_id, source_url
                            FROM events_clean
                            WHERE author_id IN ({placeholders}) AND source_id = 'wikipedia' AND job_id = %s
                            """,
                            [*related_author_ids, job_id],
                        )
                    else:
                        cursor.execute(
                            f"""
                            SELECT work_id, source_url
                            FROM events_clean
                            WHERE author_id IN ({placeholders}) AND source_id = 'wikipedia'
                            """,
                            related_author_ids,
                        )
                    for work_id, source_url in cursor.fetchall():
                        wikipedia_links.setdefault(work_id, source_url)

            for work in raw_wikipedia:
                work.wikipedia_url = wikipedia_links.get(work.id)
                if work.wikipedia_url:
                    mention_check = _wikipedia_page_mentions_work(work.wikipedia_url, work)
                    if mention_check is False:
                        continue
                top5_wikipedia.append(work)

        repos: list[ZenodoGithub] = []
        if author and _table_exists(db_alias, "zenodo_github"):
            repos_qs = ZenodoGithub.objects.using(db_alias).filter(author_id=author.id)
            if job_id is not None:
                repos_qs = repos_qs.filter(job_id=job_id)
            repos = list(repos_qs.order_by("-cited_by", "title"))

            has_zenodo_repo = any((repo.api_source or "").lower() == "zenodo" for repo in repos)
            if not has_zenodo_repo:
                related_ids = _related_author_ids(author, db_alias)
                if len(related_ids) > 1:
                    fallback_zenodo = list(
                        ZenodoGithub.objects.using(db_alias)
                        .filter(author_id__in=related_ids, api_source__iexact="zenodo")
                        .order_by("-cited_by", "title")
                    )
                    seen = {(repo.doi or "", repo.github_url or "", repo.title or "") for repo in repos}
                    for repo in fallback_zenodo:
                        fingerprint = (repo.doi or "", repo.github_url or "", repo.title or "")
                        if fingerprint in seen:
                            continue
                        repos.append(repo)
                        seen.add(fingerprint)

            for repo in repos:
                api_source = (repo.api_source or "").lower()
                if api_source == "github" or repo.github_url:
                    repo.display_type = "Github"
                elif api_source == "zenodo" or repo.doi:
                    repo.display_type = "Zenodo"
                else:
                    repo.display_type = "-"

        job_institutions = request.session.get("job_institutions", {})
        if not isinstance(job_institutions, dict):
            job_institutions = {}
        institution = None
        if task_id:
            institution = cache.get(_job_institution_cache_key(str(task_id)))
        if not institution:
            institution = job_institutions.get(str(task_id)) if task_id else None
        if institution is None:
            institution = request.session.get("selected_institution")
        if institution:
            institutions_display = institution
        else:
            institutions_ranked = False
            if author:
                institutions = []
                institution_rows = Work.objects.using(db_alias).filter(author_id=author.id)
                if job_id is not None:
                    institution_rows = institution_rows.filter(job_id=job_id)
                institution_rows = (
                    institution_rows
                    .exclude(authors_institutions__isnull=True)
                    .exclude(authors_institutions__exact="")
                    .values_list("authors_institutions", flat=True)
                )
                for institution_str in institution_rows:
                    institutions.extend(_collect_institutions(institution_str))
                if not institutions:
                    institutions = _fetch_institutions_by_orcid(author.orcid)
                    institutions_ranked = bool(institutions)
                if not institutions:
                    institutions = _collect_institutions(_fetch_institution_by_orcid(author.orcid))
            else:
                institutions = []
            institutions_display = _format_institutions(institutions, ranked=institutions_ranked)

        oa_pct = None
        if author and author.n_publicaciones and author.n_publicaciones_oa is not None:
            try:
                total = max(1, int(author.n_publicaciones))
                oa_pct = round(100 * int(author.n_publicaciones_oa) / total, 1)
            except Exception:
                oa_pct = None

        counts = defaultdict(int)
        types_set = set()
        years_set = set()
        all_works = raw_all_works if use_raw_works else Work.objects.using(db_alias)
        if author and not use_raw_works:
            all_works = all_works.filter(author_id=author.id)
            if job_id is not None:
                all_works = all_works.filter(job_id=job_id)
        for w in all_works:
            y_raw = w.year  # int, str o None
            if y_raw is None:
                continue
            try:
                y_int = int(str(y_raw).replace(",", ""))
            except Exception:
                continue
            t = (w.type or _("Unknown"))
            counts[(y_int, t)] += 1
            types_set.add(t)
            years_set.add(y_int)

        years = sorted(years_set)
        types = sorted(types_set)
        series = []
        for t in types:
            series.append({
                "type": t,
                "y": [counts.get((yr, t), 0) for yr in years],
            })
        chart_context = {
            "years": years,
            "series": series,
            "title": _("Publications per year and type"),
        }
    except DatabaseError:
        error_message = _("No se pudieron cargar los resultados en este momento.")
        author = None
        top5 = []
        top5_wikipedia = []
        repos = []
        institutions_display = "—"
        oa_pct = None
        chart_context = {
            "years": [],
            "series": [],
            "title": _("Publications per year and type"),
        }

    return {
        "author": author,
        "top5": top5,
        "top5_wikipedia": top5_wikipedia,
        "repos": repos,
        "institution": institutions_display,
        "institutions_display": institutions_display,
        "oa_pct": oa_pct,
        "chart_json": mark_safe(json.dumps(chart_context)),
        "task_id": task_id,
        "error_message": error_message,
    }
    _run_etl_task_stub(job_id, inputs)


# Función para extraer la institución del autor (aparece en el html)
def _extract_institution_aff0(author_obj: dict) -> str:
    try:
        return author_obj["affiliations"][0]["institution"]["display_name"]
    except Exception:
        return _("Not found")


def _fetch_institution_by_orcid(orcid: str) -> str | None:
    """
    Look up the author's institution on OpenAlex using the ORCID so the name
    shown on the results page matches the candidate listing.
    """
    clean_orcid = (orcid or "").replace("https://orcid.org/", "").strip()
    if not clean_orcid:
        return None

    try:
        url = f"https://api.openalex.org/authors/ORCID:{clean_orcid}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        institution = _extract_institution_aff0(data)
        return institution if institution and institution != _("Not found") else None
    except Exception:
        return None


def _orcid_exists_in_openalex(orcid: str) -> bool:
    clean_orcid = (orcid or "").strip()
    clean_orcid = clean_orcid.replace("https://orcid.org/", "")
    clean_orcid = clean_orcid.replace("http://orcid.org/", "")
    if not clean_orcid:
        return False
    try:
        resp = requests.get(
            "https://api.openalex.org/authors",
            params={
                "filter": f"orcid:https://orcid.org/{clean_orcid}",
                "per-page": 1,
            },
            timeout=20,
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        count = ((data.get("meta") or {}).get("count")) if isinstance(data, dict) else 0
        if isinstance(count, int):
            return count > 0
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            return len(results) > 0
        return True
    except Exception:
        # Si hay error de red/API, no bloqueamos el flujo para evitar falsos negativos.
        return True


def _normalize_openalex_id(author_id: str) -> str:
    if not author_id:
        return ""
    clean_id = str(author_id).strip()
    for prefix in ("https://openalex.org/", "http://openalex.org/"):
        if clean_id.startswith(prefix):
            clean_id = clean_id[len(prefix):]
            break
    return clean_id


def _fetch_institutions_by_openalex_id(author_id: str) -> list[dict]:
    clean_id = _normalize_openalex_id(author_id)
    if not clean_id:
        return []
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "filter": f"author.id:{clean_id}",
                "group_by": "institutions.id",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        institutions = []
        for group in data.get("group_by", []) or []:
            name = group.get("key_display_name") or group.get("key")
            if not name:
                continue
            institutions.append({
                "display_name": name,
                "works_count": group.get("count") or 0,
            })
        if institutions:
            return institutions
        resp = requests.get(f"https://api.openalex.org/authors/{clean_id}", timeout=20)
        resp.raise_for_status()
        data = resp.json()
        institutions = data.get("institutions")
        return institutions if isinstance(institutions, list) else []
    except Exception:
        return []


def _fetch_institutions_by_orcid(orcid: str) -> list[dict]:
    clean_orcid = (orcid or "").replace("https://orcid.org/", "").strip()
    if not clean_orcid:
        return []
    try:
        url = f"https://api.openalex.org/authors/ORCID:{clean_orcid}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        author_id = data.get("id")
        institutions = _fetch_institutions_by_openalex_id(author_id)
        if institutions:
            return institutions
        institutions = data.get("institutions")
        return institutions if isinstance(institutions, list) else []
    except Exception:
        return []


def _wikipedia_page_mentions_work(url: str, work: Work) -> bool | None:
    """
    Best-effort validation to reduce false positives from the events table by
    confirming the Wikipedia page references the work's DOI or title.
    """
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
    except Exception:
        # Si no podemos acceder a la página, mantenemos la mención para no
        # descartar resultados válidos por problemas de red o bloqueos.
        return None

    page_text = resp.text.lower()
    needles = []
    if work.doi:
        needles.append(str(work.doi).replace("https://doi.org/", "").lower())
    if work.title:
        needles.append(str(work.title).lower())

    return any(n and n in page_text for n in needles)


def _collect_institutions(raw_institutions) -> list[str]:
    if raw_institutions is None:
        return []
    if isinstance(raw_institutions, (list, tuple, set)):
        raw_items = raw_institutions
    else:
        raw_items = str(raw_institutions).split(";")
    institutions: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            name = item.get("display_name") or item.get("name") or ""
        else:
            name = str(item).strip()
        if not name or name.lower() == "none":
            continue
        institutions.append(name)
    return institutions


def _top_institutions_by_frequency(raw_institutions, max_items: int = 4) -> list[str]:
    institutions = _collect_institutions(raw_institutions)
    if not institutions:
        return []
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for idx, name in enumerate(institutions):
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
        if key not in first_seen:
            first_seen[key] = idx
            display_names[key] = name
    ranked = sorted(
        counts.keys(),
        key=lambda k: (-counts[k], first_seen[k]),
    )
    return [display_names[key] for key in ranked[:max_items]]


def _top_institutions_by_works_count(raw_institutions, max_items: int = 4) -> list[str]:
    if raw_institutions is None:
        return []
    raw_items = raw_institutions if isinstance(raw_institutions, (list, tuple)) else []
    ranked: list[tuple[int, int, str]] = []
    for idx, item in enumerate(raw_items):
        if isinstance(item, dict):
            name = item.get("display_name") or item.get("name")
            works_count = item.get("works_count") or 0
        else:
            name = str(item).strip()
            works_count = 0
        if not name:
            continue
        ranked.append((int(works_count), idx, name))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    seen = set()
    top = []
    for _, _, name in ranked:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        top.append(name)
        if len(top) >= max_items:
            break
    return top


def _format_institutions(
    raw_institutions,
    max_items: int = 4,
    ranked: bool = False,
) -> str:
    if ranked:
        institutions = _top_institutions_by_works_count(raw_institutions, max_items=max_items)
    else:
        institutions = _top_institutions_by_frequency(raw_institutions, max_items=max_items)
    return "; ".join(institutions) if institutions else "—"


def _extract_institutions(author_obj: dict) -> tuple[list[dict] | list[str], bool]:
    if not isinstance(author_obj, dict):
        return [], False
    institutions_field = author_obj.get("institutions")
    if isinstance(institutions_field, list) and institutions_field:
        return institutions_field, True
    affiliations = author_obj.get("affiliations")
    if not isinstance(affiliations, list):
        return [], False
    institutions = []
    for aff in affiliations:
        if not isinstance(aff, dict):
            continue
        institution = aff.get("institution")
        if isinstance(institution, dict):
            name = institution.get("display_name")
        else:
            name = None
        if name:
            institutions.append(name)
    return _collect_institutions(institutions), False

#Función para extraer los candidatos que aparecen de la búsqueda en OpenAlex (aparece en el html)
def _fetch_openalex_candidates(researcher: str, cursor: str = "*", per_page: int = 25):
    url = "https://api.openalex.org/authors"
    params = {
        "search": researcher,
        "per_page": per_page,
        "cursor": cursor,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    results = data.get("results", [])
    next_cursor = data.get("meta", {}).get("next_cursor")

    cand = []
    for it in results:
        institutions_ranked = False
        institutions = []
        if it.get("id"):
            institutions = _fetch_institutions_by_openalex_id(it.get("id"))
            institutions_ranked = bool(institutions)
        if not institutions:
            institutions, institutions_ranked = _extract_institutions(it)
        cand.append({
            "id": it.get("id"),
            "display_name": it.get("display_name"),
            "orcid": it.get("orcid") or "",
            "institutions": institutions,
            "institutions_ranked": institutions_ranked,
            "institutions_display": _format_institutions(
                institutions,
                ranked=institutions_ranked,
            ),
        })
    return cand, next_cursor


PENDING_REQUEST_TTL_SECONDS = 60 * 60
SEARCH_CONTEXT_TTL_SECONDS = 60 * 60
CANDIDATE_MAP_TTL_SECONDS = 60 * 60
JOB_INSTITUTION_TTL_SECONDS = 60 * 60
LOADING_PROFILE_TTL_SECONDS = 60 * 60


def _pending_request_cache_key(token: str) -> str:
    return f"pending_request:{token}"


def _search_context_cache_key(token: str) -> str:
    return f"search_context:{token}"


def _loading_profile_cache_key(token: str) -> str:
    return f"loading_profile:{token}"


def _candidate_map_cache_key(token: str) -> str:
    return f"candidate_map:{token}"


def _job_institution_cache_key(task_id: str) -> str:
    return f"job_institution:{task_id}"


def search(request):
    if request.method == "POST":
        form = SearchForm(request.POST or None)
        if form.is_valid():
            cleaned = form.cleaned_data
            orcid = (form.cleaned_data.get("orcid") or "").strip()
            researcher = (form.cleaned_data.get("researcher_name") or "").strip()
            github = (form.cleaned_data.get("github_name") or "").strip()
            if github.lower() == "skip":
                github = ""

            # The hosted portfolio demo is deterministic: each example
            # opens its own preloaded snapshot without calling third-party APIs.
            if (
                getattr(settings, "DEMO_MODE", False)
                and not getattr(settings, "LOCAL_ETL_MODE", False)
            ):
                researcher_key = " ".join(
                    researcher.casefold().replace("-", " ").split()
                )
                is_name_github_example = (
                    researcher_key == "diego j. soler navarro"
                    or github.casefold() == "diegojsn"
                )
                author_id = 2 if is_name_github_example else 1
                return redirect(
                    f"{reverse('profiles:results')}?author_id={author_id}"
                )

            if orcid:
                if not _orcid_exists_in_openalex(orcid):
                    return render(
                        request,
                        "profiles/search.html",
                        {
                            "form": form,
                            "orcid_not_found": True,
                            "local_etl_mode": getattr(settings, "LOCAL_ETL_MODE", False),
                        },
                    )
                if not github:
                    github = ""
                existing_author = _find_existing_author_by_orcid(orcid, github)
                if existing_author:
                    request.session["selected_institution"] = None
                    return redirect(
                        f"{reverse('profiles:results')}?author_id={existing_author.id}"
                    )
                # En vez de ejecutar aquí, guardamos inputs y vamos a la página de carga
                token = str(uuid.uuid4())
                cache.set(_pending_request_cache_key(token), {
                    "inputs": [orcid, "", github],
                    "institution": None,
                    "profile": {
                        "name": researcher,
                        "institution": None,
                        "orcid": orcid,
                        "github": github,
                    },
                }, timeout=PENDING_REQUEST_TTL_SECONDS)
                pending_requests = request.session.get("pending_requests", {})
                if not isinstance(pending_requests, dict):
                    pending_requests = {}
                pending_requests[token] = {
                    "inputs": [orcid, "", github],
                    "institution": None,
                    "profile": {
                        "name": researcher,
                        "institution": None,
                        "orcid": orcid,
                        "github": github,
                    },
                }
                request.session["pending_requests"] = pending_requests
                return redirect(f"{reverse('profiles:loading')}?token={token}")

            if researcher:
                search_token = str(uuid.uuid4())
                cache.set(_search_context_cache_key(search_token), {
                    "orcid": "",
                    "researcher": researcher,
                    "github": github,
                }, timeout=SEARCH_CONTEXT_TTL_SECONDS)
                search_contexts = request.session.get("search_contexts", {})
                if not isinstance(search_contexts, dict):
                    search_contexts = {}
                search_contexts[search_token] = {
                    "orcid": "",
                    "researcher": researcher,
                    "github": github,
                }
                request.session["search_contexts"] = search_contexts
                return redirect(f"{reverse('profiles:candidates')}?token={search_token}")
            
        form.add_error(None,_("Please provide at least an name or ORCID."))
    else:
        initial = request.session.get("search_form_data", {})
        form = SearchForm(initial=initial)

    return render(request, "profiles/search.html", {
        "form": form,
        "local_etl_mode": getattr(settings, "LOCAL_ETL_MODE", False),
    })

def candidates(request):
    search_token = request.GET.get("token", "")
    stored = cache.get(_search_context_cache_key(search_token)) if search_token else None
    if not isinstance(stored, dict):
        search_contexts = request.session.get("search_contexts", {})
        if not isinstance(search_contexts, dict):
            search_contexts = {}
        stored = search_contexts.get(search_token, {}) if search_token else {}
    researcher = stored.get("researcher", "")
    if not researcher:
        return redirect("profiles:search")

    per_page = 25
    cursor = request.GET.get("cursor", "*")
    page = int(request.GET.get("page", 1))

    candidate_cursors = request.session.get("candidate_cursors", {})
    if not isinstance(candidate_cursors, dict):
        candidate_cursors = {}
    token_cursors = candidate_cursors.get(search_token, {})
    if not isinstance(token_cursors, dict):
        token_cursors = {}
    token_cursors[str(page)] = cursor
    candidate_cursors[search_token] = token_cursors
    request.session["candidate_cursors"] = candidate_cursors

    cand, next_cursor = _fetch_openalex_candidates(researcher, cursor=cursor, per_page=per_page)

    offset = (page - 1) * per_page
    candidates = []
    for i, it in enumerate(cand, start=1):
        abs_idx = offset + i
        value = f"{abs_idx}|{it.get('orcid','')}"
        institutions = it.get("institutions") or []
        institutions_ranked = bool(it.get("institutions_ranked"))
        institutions_display = it.get("institutions_display") or _format_institutions(
            institutions,
            ranked=institutions_ranked,
        )
        candidates.append({
            "value": value,
            "abs_idx": abs_idx,
            "display_name": it.get("display_name") or "",
            "orcid": it.get("orcid") or "",
            "institutions": institutions,
            "institutions_display": institutions_display,
            "has_institutions": bool(_collect_institutions(institutions)),
            "institutions_ranked": institutions_ranked,
        })

    # Guardamos un mapa de índices -> institución por token para evitar
    # colisiones entre búsquedas concurrentes.
    candidate_map_key = _candidate_map_cache_key(search_token)
    candidate_map = cache.get(candidate_map_key)
    if not isinstance(candidate_map, dict):
        candidate_maps = request.session.get("candidate_maps", {})
        if not isinstance(candidate_maps, dict):
            candidate_maps = {}
        candidate_map = candidate_maps.get(search_token, {})
    for cand in candidates:
        candidate_map[str(cand.get("abs_idx"))] = {
            "institutions": cand.get("institutions") if cand.get("has_institutions") else None,
            "institutions_ranked": cand.get("institutions_ranked"),
            "display_name": cand.get("display_name"),
            "orcid": cand.get("orcid"),
        }
    cache.set(candidate_map_key, candidate_map, timeout=CANDIDATE_MAP_TTL_SECONDS)
    candidate_maps = request.session.get("candidate_maps", {})
    if not isinstance(candidate_maps, dict):
        candidate_maps = {}
    candidate_maps[search_token] = candidate_map
    request.session["candidate_maps"] = candidate_maps

    context = {
        "candidates": candidates,
        "researcher": researcher,
        "next_cursor": next_cursor,
        "prev_cursor": token_cursors.get(str(page - 1)),
        "page": page,
        "per_page": per_page,
        "token": search_token,
    }
    return render(request, "profiles/candidates.html", context)

@require_POST
def choose_candidate(request):
    raw = request.POST.get("choice", "")
    search_token = (request.POST.get("token") or "").strip()
    if not search_token:
        return redirect("profiles:search")
    parts = (raw or "").split("|", 1)
    abs_idx = parts[0].strip() if parts else ""
    selected_orcid = parts[1].strip() if len(parts) > 1 else ""

    stored = cache.get(_search_context_cache_key(search_token)) if search_token else None
    if not isinstance(stored, dict):
        search_contexts = request.session.get("search_contexts", {})
        if not isinstance(search_contexts, dict):
            search_contexts = {}
        stored = search_contexts.get(search_token, {}) if search_token else {}
    researcher = (stored.get("researcher") or "").strip()
    github = (stored.get("github") or "").strip()
    if not github or github.lower() == "skip":
        github = ""

    selected_institution = None
    selected_primary_institution = None
    selected_name = ""
    selected_orcid_from_map = ""
    if search_token:
        candidate_map = cache.get(_candidate_map_cache_key(search_token))
        if isinstance(candidate_map, dict):
            candidate_data = candidate_map.get(str(abs_idx))
            if isinstance(candidate_data, dict):
                candidate_institutions = candidate_data.get("institutions")
                selected_institution = _format_institutions(
                    candidate_institutions,
                    ranked=bool(candidate_data.get("institutions_ranked")),
                )
                selected_primary_institution = (
                    _collect_institutions(candidate_institutions)[0]
                    if candidate_institutions
                    else None
                )
                selected_name = candidate_data.get("display_name") or ""
                selected_orcid_from_map = candidate_data.get("orcid") or ""
            else:
                selected_institution = candidate_data
        if selected_institution is None and not selected_name:
            candidate_maps = request.session.get("candidate_maps", {})
            if not isinstance(candidate_maps, dict):
                candidate_maps = {}
            candidate_data = candidate_maps.get(search_token, {}).get(str(abs_idx))
            if isinstance(candidate_data, dict):
                candidate_institutions = candidate_data.get("institutions")
                selected_institution = _format_institutions(
                    candidate_institutions,
                    ranked=bool(candidate_data.get("institutions_ranked")),
                )
                selected_primary_institution = (
                    _collect_institutions(candidate_institutions)[0]
                    if candidate_institutions
                    else None
                )
                selected_name = candidate_data.get("display_name") or ""
                selected_orcid_from_map = candidate_data.get("orcid") or ""
            else:
                selected_institution = candidate_data

    if not selected_name:
        selected_name = researcher
    if not selected_orcid:
        selected_orcid = selected_orcid_from_map

    existing_author = None
    if selected_orcid:
        try:
            existing_author = _find_existing_author_by_orcid(selected_orcid, github)
        except DatabaseError:
            existing_author = None
    if existing_author is None:
        institution_for_match = selected_primary_institution
        if not institution_for_match and selected_institution:
            institution_for_match = _collect_institutions(selected_institution)
            institution_for_match = institution_for_match[0] if institution_for_match else None
        try:
            existing_author = _find_existing_author_by_profile(
                selected_name,
                institution_for_match,
                github,
                selected_orcid,
            )
        except DatabaseError:
            existing_author = None
    if existing_author:
        request.session["selected_institution"] = selected_institution
        return redirect(
            f"{reverse('profiles:results')}?author_id={existing_author.id}"
        )

    if selected_orcid:
        # ORCID claro
        inputs = [selected_orcid, "", github]
    else:
        # Sin ORCID, usamos nombre + índice absoluto de candidato
        inputs = ["", researcher, github, abs_idx]

    token = str(uuid.uuid4())
    profile_payload = {
        "name": selected_name,
        "institution": selected_institution,
        "orcid": selected_orcid,
        "github": github,
    }
    cache.set(_pending_request_cache_key(token), {
        "inputs": inputs,
        "institution": selected_institution,
        "profile": profile_payload,
    }, timeout=PENDING_REQUEST_TTL_SECONDS)
    cache.set(
        _loading_profile_cache_key(token),
        profile_payload,
        timeout=LOADING_PROFILE_TTL_SECONDS,
    )
    pending_requests = request.session.get("pending_requests", {})
    if not isinstance(pending_requests, dict):
        pending_requests = {}
    pending_requests[token] = {
        "inputs": inputs,
        "institution": selected_institution,
        "profile": profile_payload,
    }
    loading_profiles = request.session.get("loading_profiles", {})
    if not isinstance(loading_profiles, dict):
        loading_profiles = {}
    loading_profiles[token] = profile_payload
    request.session["loading_profiles"] = loading_profiles
    request.session["pending_requests"] = pending_requests
    institution_display = _format_institutions(selected_institution)
    loading_params = urlencode({
        "token": token,
        "name": selected_name or "",
        "orcid": selected_orcid or "",
        "github": github or "",
        "institution_display": institution_display,
    })
    return redirect(f"{reverse('profiles:loading')}?{loading_params}")


def loading(request):
    """
    Página intermedia con barra de progreso. El JS de la página llama a /run-etl/,
    que ejecuta el script; mientras tanto, mostramos una barra que avanza.
    Al terminar, redirige a /results.
    """
    token = request.GET.get("token", "")
    payload = cache.get(_pending_request_cache_key(token)) if token else None
    if not isinstance(payload, dict):
        pending_requests = request.session.get("pending_requests", {})
        if not isinstance(pending_requests, dict):
            pending_requests = {}
        payload = pending_requests.get(token)
    profile = payload.get("profile") if isinstance(payload, dict) else None
    if not isinstance(profile, dict):
        loading_profile = cache.get(_loading_profile_cache_key(token)) if token else None
        if not isinstance(loading_profile, dict):
            loading_profiles = request.session.get("loading_profiles", {})
            if not isinstance(loading_profiles, dict):
                loading_profiles = {}
            loading_profile = loading_profiles.get(token)
        profile = loading_profile
    if not isinstance(profile, dict):
        profile = None
    fallback_name = request.GET.get("name") or ""
    fallback_orcid = request.GET.get("orcid") or ""
    fallback_github = request.GET.get("github") or ""
    if not profile:
        if fallback_name or fallback_orcid or fallback_github:
            profile = {
                "name": fallback_name,
                "institution": None,
                "orcid": fallback_orcid,
                "github": fallback_github,
            }
    elif isinstance(profile, dict):
        if not profile.get("name") and fallback_name:
            profile["name"] = fallback_name
        if not profile.get("orcid") and fallback_orcid:
            profile["orcid"] = fallback_orcid
        if not profile.get("github") and fallback_github:
            profile["github"] = fallback_github
    profile_name = (profile.get("name") if profile else "") or ""
    profile_orcid = (profile.get("orcid") if profile else "") or ""
    profile_github = (profile.get("github") if profile else "") or ""
    author = None
    if profile_orcid:
        try:
            orcid_candidates = _normalize_orcid_variants(profile_orcid)
            if orcid_candidates:
                author = (
                    AuthorSummary.objects.using("authors")
                    .filter(orcid__in=orcid_candidates)
                    .order_by("-id")
                    .first()
                )
        except DatabaseError:
            author = None
    if author is None and profile_name:
        try:
            author = (
                AuthorSummary.objects.using("authors")
                .filter(name__iexact=profile_name)
                .order_by("-id")
                .first()
            )
        except DatabaseError:
            author = None
    if author:
        profile_name = author.name or profile_name
        profile_orcid = author.orcid or profile_orcid
        profile_github = author.github_name or profile_github

    institutions_display = (profile.get("institution") if profile else None) or ""
    if not institutions_display:
        institutions_display = request.GET.get("institution_display") or "—"
    has_profile_institutions = bool(institutions_display and institutions_display != "—")
    return render(
        request,
        "profiles/loading.html",
        {
            "token": token,
            "selected_profile": profile,
            "selected_profile_institutions": institutions_display,
            "selected_profile_name": profile_name,
            "selected_profile_orcid": profile_orcid,
            "selected_profile_github": profile_github,
            "selected_profile_has_institutions": has_profile_institutions,
        },
    )


@require_POST
def run_etl(request):

    """
    Crea un Job y dispara el ETL en segundo plano con Celery.
    Devuelve inmediatamente el job_id para que el front haga polling.
    """
    token = request.GET.get("token") or request.POST.get("token")
    payload = cache.get(_pending_request_cache_key(token)) if token else None
    if token:
        cache.delete(_pending_request_cache_key(token))
        pending_requests = request.session.get("pending_requests", {})
        if not isinstance(pending_requests, dict):
            pending_requests = {}
        if payload is None:
            payload = pending_requests.get(token)
        pending_requests.pop(token, None)
        request.session["pending_requests"] = pending_requests
    if not payload:
        return JsonResponse({"ok": False, "error": _("Missing pending inputs.")}, status=400)

    inputs = payload.get("inputs", [])
    if not isinstance(inputs, list):
        inputs = []
    selected_institution = payload.get("institution")

    job = Job.objects.create(status="queued", percent=0, error="")

    async_result = None
    local_etl = getattr(settings, "LOCAL_ETL_MODE", False)
    use_stub = run_etl_task is None or getattr(settings, "RUN_ETL_SYNCHRONOUSLY", False)
    if local_etl and run_etl_task is not None:
        job.celery_id = str(uuid.uuid4())
        job.save(update_fields=["celery_id"])
        threading.Thread(
            target=run_etl_task.run,
            args=(job.id, inputs),
            name=f"researcher-etl-{job.id}",
            daemon=True,
        ).start()
    elif use_stub:
        job.celery_id = str(uuid.uuid4())
        _run_etl_task_stub(job.id, inputs)
    else:
        try:
            async_result = run_etl_task.delay(job.id, inputs)
        except Exception as exc:
            job.status = "error"
            job.error = _("No se pudo encolar la tarea ETL: %(err)s") % {
                "err": f"{type(exc).__name__}: {exc}"
            }
            job.save(update_fields=["status", "error"])
            return JsonResponse({"ok": False, "error": job.error}, status=503)
        job.celery_id = async_result.id
    job.save(update_fields=["celery_id"])

    job_task_id = job.celery_id or (async_result.id if async_result else "")
    cache.set(
        _job_institution_cache_key(str(job_task_id)),
        selected_institution,
        timeout=JOB_INSTITUTION_TTL_SECONDS,
    )
    job_institutions = request.session.get("job_institutions", {})
    if not isinstance(job_institutions, dict):
        job_institutions = {}
    job_institutions[str(job_task_id)] = selected_institution
    request.session["job_institutions"] = job_institutions
    return JsonResponse({"ok": True, "job_id": job_task_id})
    '''    try:
        # 2) si tienes Celery y la tarea importada, encola
        if run_etl_task is not None and not getattr(settings, "RUN_ETL_SYNCHRONOUSLY", False):
            # encolado no bloqueante
            run_etl_task.delay(str(job.id), inputs)
        else:
            # fallback: ejecutar en el mismo proceso (para desarrollo local)
            _run_etl_task_stub(job.id, inputs)

        # 3) limpia la sesión para no relanzar el mismo ETL por error
        request.session["pending_inputs"] = []

        # 4) devuelve inmediatamente el job_id
        return JsonResponse({"ok": True, "job_id": str(job.id)})

    except Exception as e:
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.save(update_fields=["status","error"])
        return JsonResponse({"ok": False, "error": job.error}, status=500)'''
    
@require_GET
def progress(request: HttpRequest, job_id: uuid.UUID) -> JsonResponse:
    """
    Endpoint de polling (opción B UUID): busca por celery_id.
    """
    job = get_object_or_404(Job, celery_id=str(job_id))
    return JsonResponse({
        "job_pk": job.id,
        "status": job.status,
        "percent": int(job.percent or 0),
        "error": job.error or ""
    })
    

def demo(request):
    """Open the deterministic portfolio profile without calling external APIs."""
    if not getattr(settings, "DEMO_MODE", False):
        raise Http404
    return redirect(f"{reverse('profiles:results')}?author_id=1")


def results(request):
    task_id = request.GET.get("task")
    job_param = request.GET.get("job")
    job_pk = int(job_param) if job_param and str(job_param).isdigit() else None
    requested_orcid = (request.GET.get("orcid") or "").strip() or None
    requested_name = (request.GET.get("name") or "").strip() or None
    author_id = request.GET.get("author_id")
    author_id_value = None
    if author_id and str(author_id).isdigit():
        author_id_value = int(author_id)
    context = _build_results_context(
        request,
        task_id,
        job_pk,
        author_id_value,
        requested_orcid,
        requested_name,
    )
    return render(request, "profiles/results.html", context)


# ----------------------------------------------------------------------------
# Real health check endpoint
# ----------------------------------------------------------------------------
@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """
    Real health check endpoint.

    This view verifies that the default PostgreSQL database and the Redis
    broker are reachable. It returns a JSON object with boolean keys
    ``db`` and ``redis`` indicating the status of each component. If
    either check fails, error messages are included in the response and
    the HTTP status code is set to 500. Otherwise, the status code is
    200.
    """
    db_ok = True
    redis_ok = True
    errors: dict[str, str] = {}
    demo_mode = getattr(settings, "DEMO_MODE", False)
    # Check DB connection
    try:
        # Using a lightweight SELECT to ensure the connection is alive
        connections["default"].cursor().execute("SELECT 1")
    except Exception as exc:
        db_ok = False
        errors["db_error"] = str(exc)
    # Redis is deliberately optional in the zero-configuration demo.
    try:
        if demo_mode:
            raise StopIteration
        redis_url = os.getenv("REDIS_URL", "")
        if redis_url:
            client = redis.Redis.from_url(redis_url)
        else:
            redis_host = os.getenv("REDIS_HOST", "redis")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
            redis_password = os.getenv("REDIS_PASSWORD", "")
            if redis_password:
                client = redis.Redis(host=redis_host, port=redis_port, password=redis_password)
            else:
                client = redis.Redis(host=redis_host, port=redis_port)
        client.ping()
    except StopIteration:
        redis_ok = True
    except Exception as exc:
        redis_ok = False
        errors["redis_error"] = str(exc)
    status_code = 200 if (db_ok and redis_ok) else 500
    response: dict[str, object] = {"db": db_ok, "redis": redis_ok}
    if errors:
        response["errors"] = errors
    return JsonResponse(response, status=status_code)
