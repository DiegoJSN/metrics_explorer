# Libraries we need:

import os, time
from pathlib import Path
import math
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import LocationParseError
import django
import json
import re
from bs4 import BeautifulSoup

# -------------------------------------------------------------------------------------
# Helpers for running SQLite-style scripts on PostgreSQL
#
# The ETL code was originally written for SQLite and makes use of `executescript()`
# and SQLite-specific SQL syntax such as `AUTOINCREMENT` and `INSERT OR IGNORE`.  When
# running against PostgreSQL via psycopg, these constructs either do not exist or
# behave differently.  The helpers below implement a minimal SQL splitter,
# normalization routines, and a cursor wrapper that provides an `executescript()`
# method and translates SQLite placeholders (`?`) into psycopg placeholders (`%s`).

import re as _re

def _split_sql_statements(script: str) -> list[str]:
    """Divide a SQL script into individual statements.  It splits on semicolons
    but respects single and double quoted strings, so semicolons inside strings
    are not treated as statement separators."""
    statements = []
    buf = []
    in_single = False
    in_double = False
    i = 0
    while i < len(script):
        ch = script[i]
        # inside a single quoted string
        if in_single:
            buf.append(ch)
            if ch == "'":
                # handle escaped single quote in SQL ('' -> ')
                if i + 1 < len(script) and script[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_single = False
            i += 1
            continue
        # inside a double quoted identifier
        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue
        # start of single quoted string
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        # start of double quoted identifier
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        # split on semicolon
        if ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    last = ''.join(buf).strip()
    if last:
        statements.append(last)
    return statements

def _normalize_sql_for_postgres(sql: str) -> str | None:
    """
    Normalize a single SQL statement from SQLite syntax to PostgreSQL syntax.

    This helper removes SQLite-specific constructs and converts data types to
    equivalents supported by PostgreSQL. If the statement should be skipped
    entirely (e.g., PRAGMA), return None.
    """
    s = sql.strip()
    if not s:
        return None
    # Ignore PRAGMA statements in PostgreSQL
    if _re.match(r"(?is)^\s*pragma\b", s):
        return None
    # Convert autoincrement primary keys to SERIAL PRIMARY KEY.  Handle
    # patterns with or without UNIQUE and AUTOINCREMENT keywords.  We first
    # match the most specific pattern (including AUTOINCREMENT) then fall
    # back to a generic UNIQUE/PRIMARY combination.
    s = _re.sub(r"(?is)\bINTEGER\s+UNIQUE\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "SERIAL PRIMARY KEY", s)
    s = _re.sub(r"(?is)\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "SERIAL PRIMARY KEY", s)
    s = _re.sub(r"(?is)\bINTEGER\s+UNIQUE\s+PRIMARY\s+KEY\b", "SERIAL PRIMARY KEY", s)
    s = _re.sub(r"(?is)\bINTEGER\s+PRIMARY\s+KEY\b", "SERIAL PRIMARY KEY", s)
    # Remove any remaining AUTOINCREMENT tokens to avoid invalid syntax
    s = _re.sub(r"(?is)\bAUTOINCREMENT\b", "", s)
    # Convert common SQLite types to PostgreSQL equivalents
    s = _re.sub(r"(?is)\bDATETIME\b", "TIMESTAMP", s)
    s = _re.sub(r"(?is)\bREAL\b", "DOUBLE PRECISION", s)
    s = _re.sub(r"(?is)\bBLOB\b", "BYTEA", s)
    # Translate INSERT OR IGNORE into INSERT ... ON CONFLICT DO NOTHING
    if _re.match(r"(?is)^\s*insert\s+or\s+ignore\s+into\b", s):
        s = _re.sub(r"(?is)^\s*insert\s+or\s+ignore\s+into\b", "INSERT INTO", s)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip(';') + " ON CONFLICT DO NOTHING"
    return s

class CursorCompat:
    """Wrapper around a psycopg cursor to emulate SQLite's executescript() and
    translate parameter placeholders.  It proxies unknown attributes to the
    underlying cursor."""
    def __init__(self, inner_cursor):
        self._cur = inner_cursor

    def execute(self, query, params=None):
        if isinstance(query, str):
            # translate SQLite placeholders to psycopg's %s
            if '?' in query:
                query = query.replace('?', '%s')
            norm = _normalize_sql_for_postgres(query)
            if norm is None:
                return None
            query = norm
        if params is None:
            return self._cur.execute(query)
        return self._cur.execute(query, params)

    def executemany(self, query, seq_of_params):
        if isinstance(query, str):
            if '?' in query:
                query = query.replace('?', '%s')
            norm = _normalize_sql_for_postgres(query)
            if norm is None:
                return None
            query = norm
        return self._cur.executemany(query, seq_of_params)

    def executescript(self, script: str):
        # execute multiple statements separated by semicolons
        for stmt in _split_sql_statements(script):
            norm = _normalize_sql_for_postgres(stmt)
            if norm is None:
                continue
            self._cur.execute(norm)
    
    def __iter__(self):
        """
        Make the wrapper iterable by delegating iteration to the underlying
        psycopg cursor.  Without this method, attempting to iterate over
        CursorCompat instances (e.g. ``for row in con.execute(...)``) would
        raise a TypeError.  Returning an iterator over the inner cursor
        ensures compatibility with code that expects an iterable cursor.
        """
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)

def _normalize_sql_for_sqlite(sql: str) -> str:
    """Translate the PostgreSQL-flavoured DDL used by the shared ETL to SQLite."""
    statement = sql.strip()
    statement = statement.replace("%s", "?")
    statement = _re.sub(
        r"(?is)\bSERIAL\s+PRIMARY\s+KEY\b",
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        statement,
    )
    statement = _re.sub(r"(?is)\bTIMESTAMPTZ\b", "DATETIME", statement)
    statement = _re.sub(r"(?is)\bDOUBLE\s+PRECISION\b", "REAL", statement)
    statement = _re.sub(r"(?is)\bNOW\(\)", "CURRENT_TIMESTAMP", statement)
    return statement


class SQLiteCursorCompat:
    """Normalize shared PostgreSQL/SQLite SQL before using sqlite3."""

    def __init__(self, inner_cursor):
        self._cur = inner_cursor

    def _skip_existing_column(self, query: str) -> bool:
        match = _re.match(
            r"(?is)^\s*ALTER\s+TABLE\s+([\w\"]+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([\w\"]+)",
            query,
        )
        if not match:
            return False
        table = match.group(1).strip('"')
        column = match.group(2).strip('"')
        self._cur.execute(f'PRAGMA table_info("{table}")')
        columns = {row[1] for row in self._cur.fetchall()}
        return column in columns

    def execute(self, query, params=None):
        if isinstance(query, str):
            lowered = query.lower()
            if "pg_advisory_lock" in lowered or "pg_advisory_unlock" in lowered:
                self._cur.execute("SELECT 1")
                return self
            regclass = _re.search(r"to_regclass\(['\"](?:public\.)?([\w]+)['\"]\)", query, _re.I)
            if regclass:
                table = regclass.group(1)
                self._cur.execute(
                    """
                    SELECT CASE
                        WHEN EXISTS (
                            SELECT 1 FROM sqlite_master
                            WHERE type = 'table' AND name = ?
                        )
                        THEN ?
                        ELSE NULL
                    END
                    """,
                    (table, table),
                )
                return self
            if "from pg_type" in lowered:
                self._cur.execute("SELECT 1 WHERE 0")
                return self
            if _re.match(r"(?is)^\s*DROP\s+TYPE\b", query):
                self._cur.execute("SELECT 1")
                return self
            if self._skip_existing_column(query):
                return self
            query = _re.sub(
                r"(?is)(ADD\s+COLUMN)\s+IF\s+NOT\s+EXISTS",
                r"\1",
                query,
            )
            query = _normalize_sql_for_sqlite(query)
        if params is None:
            self._cur.execute(query)
        else:
            self._cur.execute(query, params)
        return self

    def executemany(self, query, seq_of_params):
        query = _normalize_sql_for_sqlite(query) if isinstance(query, str) else query
        self._cur.executemany(query, seq_of_params)
        return self

    def executescript(self, script: str):
        for statement in _split_sql_statements(script):
            self.execute(statement)
        return self

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", cleaned).strip()

def _safe_int(value, default: int = 0) -> int:
    """Convert values to int while handling None/NaN-like inputs."""
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in {"", "nan", "none"}:
            return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _title_fragment_in_html(reference_title: str, page_html: str, *, words_count: int = 3) -> bool:
    if not reference_title or not page_html:
        return False
    title_norm = _normalize_title(reference_title)
    if not title_norm:
        return False
    words = title_norm.split()
    if not words:
        return False
    first_fragment = " ".join(words[:words_count])
    last_fragment = " ".join(words[-words_count:])
    page_norm = page_html.lower()
    return (first_fragment and first_fragment in page_norm) or (last_fragment and last_fragment in page_norm)

def _normalize_doi_url(doi_value: str) -> str | None:
    if not doi_value:
        return None
    doi_value = doi_value.strip()
    if not doi_value:
        return None
    if doi_value.startswith("http://") or doi_value.startswith("https://"):
        return doi_value
    return f"https://doi.org/{doi_value}"

def _fetch_doi_page_html(doi_url: str, session: requests.Session, timeout: float) -> str | None:
    if not doi_url:
        return None
    headers = {
        "User-Agent": "researcher-tool-doi-check/1.0 (+https://doi.org/)",
        "Accept": "text/html,application/xhtml+xml"
    }
    try:
        response = session.get(doi_url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except requests.exceptions.SSLError:
        try:
            response = session.get(
                doi_url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                verify=False,
            )
            response.raise_for_status()
        except (requests.exceptions.RequestException, LocationParseError):
            return None
    except (requests.exceptions.RequestException, LocationParseError):
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    return str(soup)

def _extract_doi_fragment(doi_value: str) -> str | None:
    if not doi_value:
        return None
    doi_value = doi_value.strip()
    if not doi_value:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_value, flags=re.IGNORECASE) or None

def _wikipedia_page_contains_doi(
    source_url: str,
    doi_fragment: str,
    session: requests.Session,
    timeout=None
) -> bool | None:
    if not source_url or not doi_fragment:
        return False
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    try:
        time.sleep(0.2)
        response = session.get(source_url, headers={"Accept": "text/html"}, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    return doi_fragment.lower() in str(soup).lower()

import pandas as pd
import openpyxl
from tabulate import tabulate
# Replace SQLite with psycopg for PostgreSQL support
import psycopg
import sqlite3
import plotly.express as px
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from itertools import zip_longest

from profiles.models import Job

### Timeouts settings and backoff features
# --- Config por defecto (puedes mover a .env) ---
DEFAULT_TIMEOUT = (10, 30)  # (connect, read) segundos
OPENALEX_MAILTO = os.getenv("OPENALEX_MAILTO", "you@example.com")
UA = os.getenv("HTTP_UA", f"authorsite/1.0 (+mailto:{OPENALEX_MAILTO})")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

def make_session():
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,                # 1s, 2s, 4s, 8s...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD", "OPTIONS")
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": UA})
    return s

SESSION = make_session()

def get(url, *, params=None, extra_headers=None, timeout=DEFAULT_TIMEOUT):
    headers = {}
    if extra_headers:
        headers.update(extra_headers)
    r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
    # Respeta Retry-After si la API lo envía (p.ej., 429)
    if r.status_code == 429 and "Retry-After" in r.headers:
        try:
            delay = _safe_int(r.headers["Retry-After"])
            time.sleep(delay)
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
        except Exception:
            pass
    r.raise_for_status()
    return r

# Activate Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.getenv("DJANGO_SETTINGS_MODULE","authorsite.settings"))
django.setup()

JOB_ID = globals().get("JOB_ID_FROM_RUNNER") or os.getenv("JOB_ID")
JOB_ID_VALUE = _safe_int(JOB_ID) if JOB_ID is not None else None

PHASE_DEFINITIONS = [
    ("phase_openalex", 5, "Extrayendo de OpenAlex…"),
    ("phase_doi_check", 20, "Verificando DOIs…"),
    ("phase_crossref", 40, "Extrayendo de Crossref EventData…"),
    ("phase_wikipedia_check", 60, "Verificando trabajos en Wikipedia…"),
    ("phase_zenodo", 72, "Extrayendo de Zenodo…"),
    ("phase_github", 84, "Extrayendo de GitHub…"),
    ("phase_openaire", 94, "Extrayendo de OpenAIRE…"),
]
PHASE_META = {key: {"percent": pct, "label": label} for key, pct, label in PHASE_DEFINITIONS}
current_phase_key = "phase_openalex"

def update_progress(pct=None, status=None, error=None, result_author_id=None):
    """Actualiza percent/status/error del Job sin usar variables globales 'job'."""
    if not JOB_ID:
        return
    try:
        # Relee el objeto cada vez (robusto ante procesos largos)
        job_obj = Job.objects.using("default").filter(pk=JOB_ID).first()
        if not job_obj:
            return

        fields = []
        if pct is not None:
            job_obj.percent = _safe_int(pct, job_obj.percent or 0)
            fields.append("percent")
        if status is not None:
            status_value = str(status)
            if status_value == "running" and current_phase_key.startswith("phase_"):
                status_value = current_phase_key
            job_obj.status = status_value
            fields.append("status")
        if error is not None:
            job_obj.error = str(error)
            fields.append("error")
        if result_author_id is not None:
            job_obj.result_author_id = _safe_int(result_author_id, job_obj.result_author_id or 0)
            fields.append("result_author_id")

        if fields:
            job_obj.save(update_fields=fields)
    except Exception:
        # No rompas el ETL si falla el update del progreso
        pass

def set_phase(phase_key: str) -> None:
    global current_phase_key
    current_phase_key = phase_key
    meta = PHASE_META.get(phase_key)
    if not meta:
        update_progress(status=phase_key)
        return
    update_progress(pct=meta["percent"], status=phase_key)

def phase_error_message(err: Exception) -> str:
    meta = PHASE_META.get(current_phase_key, {})
    phase_label = meta.get("label", current_phase_key or "Fase desconocida")
    return f"{phase_label} · {type(err).__name__}: {err}"

# Processing input data
while True:
# ORCID
    print()
    orcid_or_name= input("> ORCID (highly recommended): ").strip()
    print()

    # Patron flexible para reconocer ORCID: cualquier cosa que tenga al menos 1 dígito
    orcid_pattern = r"(?:.*\d){1,}"

    orcid_num = None

    if re.match(orcid_pattern, orcid_or_name):
        # print("Se introdujo un input similar a ORCID. Revisando en profundidad...")
        # print()
        orcid_num = orcid_or_name
        if "orcid.org" in orcid_num:
            orcid_num = re.findall("orcid.org/(.*)", orcid_num)[0]
        #print(orcid_num)

        orcid_pattern = r"^\d{4}-\d{4}-\d{4}-\d{4}$|^\d{4}-\d{4}-\d{4}-\d{3}X$"

        if re.match(orcid_pattern, orcid_num):
            print(f"ORCID tiene formato correcto: {orcid_num}")
            print()
            break
        else:
            print("Input error. Please enter your ORCID number (in the following format: XXXX-XXXX-XXXX-XXXX) or researcher name")
            print()
            continue

    else:
        print("ORCID no detectado")
        print()
        break


researcher_name = input("> Researcher name: ").strip()
print()

github_name = input("> GitHub name: ").strip()
print()


# Update job progress to 5%
try:
    update_progress(5, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise


##############################################################################################
##############################################################################################
##############################################################################################
# ___OpenAlex API #####
##############################################################################################
##############################################################################################
##############################################################################################

url = "https://api.openalex.org/authors"

orcid_not_found = False # Ponemos esta variable aqui como "interruptor" para la variable "orcid_not_found" que se usa mas adelante SI no se encuentra ningun resultado usando el orcid
exit_script = False # Ponemos esta variable aqui como "interruptor" para la variable "exit_script" que se usa mas adelante SI no se encuentra ningun resultado

while True:
    if orcid_num is None or orcid_not_found == True:
        per_page = 25
        params =  {"search":researcher_name, "per_page":per_page}
    else:
        orcid_param = "orcid:" + orcid_num
        params = {"filter":orcid_param}

    params = {**params, "mailto": OPENALEX_MAILTO}
    response = get(url, params=params)
    data = response.json()

    # print(json.dumps(data, indent=8, ensure_ascii=False)[:5000])

    # if orcid_num is None:
    #    with open(f"researcher_results_{researcher_name}.txt", "w", encoding="utf-8") as file: file.write(json.dumps(data, indent=8, ensure_ascii=False))
    #else:
    #     with open(f"researcher_results_{data["results"][0]["display_name"]}.txt", "w", encoding="utf-8") as file: file.write(json.dumps(data, indent=8, ensure_ascii=False))

    try:
        results_page = data["meta"]["page"]
        results_count = data["meta"]["count"]

        if results_count == 0 and orcid_not_found == False:
            orcid_not_found = True
            print("ORCID not found. Using the researcher's name instead...")
            print()
            continue

        if results_count == 0 and orcid_not_found == True:
            print(f"Sorry. No results found for the name '{researcher_name}'")
            print()
            exit_script = True
            break

        else:
            if orcid_not_found == True:
                print(f"There were {results_count} results found for the name '{researcher_name}'")
                print()
            else:
                print(f"There were {results_count} results found for the ORCID '{orcid_num}'")
                print()

    except:
        print("The item 'meta' was not found when calling the API. Please contact the administrators to fix this issue.")
        print()
        pass

    try:
        results = data["results"] # results es una lista en la que cada elemento es un diccionario
        break
    except: # Para localizar posibles errores si cambian la estructura de la API en el futuro
        print("ERROR 01: The item 'results' was not found when calling the API. Please contact the administrators to fix this issue.")

if exit_script == True:
    quit()

user_input = None # Ponemos esta variable aqui como "primer" para la variable "user_input" que se usara mas adelante

total_count = list()
unique_count = 0

while True:    
    if user_input == "":
        results_page = results_page + 1

        params = {"search": researcher_name, "per_page": per_page, "page": results_page, "mailto": OPENALEX_MAILTO}
        response = get(url, params=params)
        data = response.json()

        results_page = data["meta"]["page"]

        results = data["results"] # results es una lista en la que cada elemento es un diccionario

    else:
        pass

    try:
        # print("CADA RESULT TIENE LOS SIGUIENTES DICCIONARIOS: ")
        # for result in results[0]: print(result)
        # print()

        print("##############################################################################################################")
        print("##############################################################################################################")
        print()

        count = 0
        for result in results:
            name = result["display_name"]
            try:
                orcid = result["orcid"]
                if orcid == None:
                    orcid = "Not found"
            except:
                orcid = "Not found"
            try:
                institution = result["affiliations"][0]["institution"]["display_name"]
            except:
                institution = "Not found"
    
            print(f"Result ({unique_count + 1}) \nName:        {name} \nORCID:       {orcid} \nInstitution: {institution}")
            count = count + 1
            unique_count = unique_count + 1
            print()

            researcher_info = results[0]

        print("##############################################################################################################")
        print("##############################################################################################################")
        print()
        total_count.append(count)

        if sum(total_count) != results_count:
            print(f"{count} results are shown ({sum(total_count)}/{results_count}). Select one of the results by entering its number to choose a researcher OR press enter to view the next {per_page} results.")
        if orcid_num is None or orcid_not_found == True:
            user_input = input("> ")
        elif sum(total_count) == 0 and orcid_num is not None:
            print("No results found. Please try searching again using your name.")
            print()
            exit_script = True
            break
        elif sum(total_count) == 0:
            print("No results found.")
            print()
            exit_script = True
            break
        else:
            print(f"{count} results are shown ({sum(total_count)}/{results_count}). Select one of the results by entering its number to choose a researcher.")
            if orcid_num is None or orcid_not_found == True:
                user_input = input("> ")
            print()
        print()

    except:  # Para localizar posibles errores si cambian la estructura de la API en el futuro
        if orcid_num is None or orcid_not_found == True:
            print("ERROR 02: The item 'display_name' was not found when calling the API. Please contact the administrators to fix this issue.") 
            break
        else:
            break
    
    if orcid_num is None or orcid_not_found == True:
        try:
            user_input = int(user_input) - sum(total_count) - 1
            researcher_info = results[user_input]
            break

        except:
            print(f"Showing the next {per_page} results...")
            print()
            continue

if exit_script == True:
    quit()

if orcid_num is None or orcid_not_found == True:
    # print(user_input)
    if user_input is not None:
        count_corrected = sum(total_count) + user_input + 1
    else:
        count_corrected = sum(total_count) + 1

    print(f"Has seleccionado el numero {count_corrected}:")
    print()
    print("##############################################################################################################")
    print("##############################################################################################################")
    print()

    ################################################
    ###### FASE 1: Extrayendo de OpenAlex.... ###### Sacando el nombre, orcid, institution del investigador elegido
    ################################################
    name = researcher_info["display_name"]
    try:
        orcid = researcher_info["orcid"]
        if orcid is None:
            orcid = "Not found"
    except:
        orcid = "Not found"
    try:
        institution = researcher_info["affiliations"][0]["institution"]["display_name"]
    except:
        institution = "Not found"

    print(f"Result ({count_corrected}) \nName:        {name} \nORCID:       {orcid} \nInstitution: {institution}")

    print()

    print()

    print("##############################################################################################################")
    print("##############################################################################################################")
    print()


    # COMPLETAR CON ORCID + INSTITUCION
    print()

# with open(f"DEFINITIVE_results_{researcher_info["display_name"]}.txt", "w", encoding="utf-8") as file: file.write(json.dumps(researcher_info, indent=8, ensure_ascii=False))

set_phase("phase_openalex")

###### ADICION_OPT ###### 
###### EN ALGUN LUGAR DEL CODIGO DE ARRIBA PUEDES AÑADIR UNA OPCION PARA QUE BUSQUE TAMBIEN POR INSTITUCION, PERO AHORA MISMO LO DEJAMOS DE LADO

###### ADICION_OPT ######
###### Y TAMBIEN PODRIAS HACER QUE EL USUARIO SELECCIONARA IR A LA PAGINA SIGUIENTE O ANTERIOR (Y QUE SALGA EL NUM DE PAGINA)


if orcid_num is None:
    orcid_url = researcher_info["orcid"]
    try:
        orcid_num = re.findall("https://orcid.org/(.*)", orcid_url)[0]
        #print()
        #print("Si orcid era None: ")
        #print(orcid_num)
        #print()
    except:
        pass
else:
    try:
        orcid_url = "https://orcid.org/" + orcid_num
    except:
        orcid_url = None

########################
# ________Indice h
########################

h_index = researcher_info["summary_stats"]["h_index"]
print(f"Indice h: {h_index}")



########################
# ________Indice i10
########################

i10_index = researcher_info["summary_stats"]["i10_index"]
print(f"Indice i10: {i10_index}")



########################
# ________Total de citas recibidas (N)
########################

total_citas = researcher_info["cited_by_count"]
print(f"Total de citas recibidas: {total_citas}")

openalex_id = researcher_info["id"]
openalex_id = re.findall("openalex.org/(.*)", openalex_id)[0]

## Update job process 10%
try:
    update_progress(10, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise

###### ADICION_OBL (HECHO) #####
###### DEBES AÑADIR UN CONDICIONAL PARA LOS CASOS EN LOS QUE HAYA ALGUIEN CON MAS DE 200 TRABAJOS

url = "https://api.openalex.org/works"
openalex_param = "author.id:" + openalex_id
per_page = 200
page = 1
params = {"filter":openalex_param, "per_page":per_page, "page":page}

params = {**params, "mailto": OPENALEX_MAILTO}
response = get(url, params=params)
data = response.json()

#print(json.dumps(data, indent=8, ensure_ascii=False)[:5000])

########################
# ________Publicaciones (total)
########################

total_publicaciones = _safe_int(data["meta"].get("count"), 0)
print(f"Publicaciones (total): {total_publicaciones}")

# Run if more than 200 publications
count_pages = total_publicaciones // per_page
works_info = list()

if total_publicaciones > per_page:
    for page in range(1, count_pages + 2):
        params = {"filter":openalex_param, "per_page":per_page, "page":page}
        params = {**params, "mailto": OPENALEX_MAILTO}
        response = get(url, params=params)
        data = response.json()
        works_info.extend(data["results"])
else:
    works_info = data["results"]
    #print(f"Only the first {per_page} publications are retrieved.")
# with open(f"DEFINITIVE_results_{researcher_info["display_name"]}_works.txt", "w", encoding="utf-8") as file: file.write(json.dumps(works_info, indent=8, ensure_ascii=False))

########################
# ________Publicaciones (articulos de revista)
########################

# print()
# print("CADA WORK TIENE LOS SIGUIENTES DICCIONARIOS: ")
# for work in works_info[0]: print(work) # el tipo de trabajo se encuentra en el diccionario "type" (works_info["type"]). En teoria, deberiamos obtener 85 items en el diccionario "type"
# print()

works_info_type = dict() # Crear un diccionario en el que las keys sean los tipos y el value sea el n total

for work in works_info: 
    work_type = work["type"]
    works_info_type[work_type] = works_info_type.get(work_type, 0) + 1

# print(works_info_type)
# print()

try:
    print(f"Publicaciones (articulos de revista): {works_info_type["article"]}")
except:
    print(f"Publicaciones (articulos de revista): {0}")


########################
# ________Publicaciones (reviews)
########################

try:
    print(f"Publicaciones (reviews): {works_info_type["review"]}")
except:
    print(f"Publicaciones (reviews): {0}")


########################
# ________Publicaciones (libros)
########################

try:
    print(f"Publicaciones (libros): {works_info_type["book"]}")
except:
    print(f"Publicaciones (libros): {0}")

########################
# ________ Publicaciones (capítulos de libro)
########################

try:
    print(f"Publicaciones (capitulos de libro): {works_info_type["book-chapter"]}")
except:
    print(f"Publicaciones (capitulos de libro): {0}")

########################
# ________Publicaciones (preprints)
########################


try:
    print(f"Publicaciones (preprints): {works_info_type["preprint"]}")
except:
    print(f"Publicaciones (preprints): {0}")

########################
# ________Publicaciones con acceso abierto (N, %)
########################

# esta info se encuentra en works_info["open_access"]["is_oa"]
open_access_state = dict()

for work in works_info:
    open_access = work["open_access"]["is_oa"]
    open_access_state[open_access] = open_access_state.get(open_access, 0) + 1

#print(open_access_state)
#print()

oa_true = _safe_int(open_access_state.get(True, 0))
oa_pct = round((oa_true / max(1, _safe_int(total_publicaciones))) * 100, 1)
print(f"Publicaciones con acceso abierto: {oa_true} ({oa_pct} %)")


# Update job progress to 20%
try:
    update_progress(20, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise

########################
# Creamos db para los autores
########################


# Conectamos a la base de datos PostgreSQL en lugar de SQLite.  Las credenciales se leen de
# las variables de entorno PG_AUTHORS_* (o, en su defecto, PG_*).  Al usar psycopg3
# podemos reutilizar la misma API de cursores y commits que en SQLite.
LOCAL_ETL_MODE = os.getenv("LOCAL_ETL_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
if LOCAL_ETL_MODE:
    local_db_path = os.getenv("DEMO_AUTHORS_DB", str(Path(__file__).resolve().parent / "demo_authors.sqlite3"))
    Path(local_db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(local_db_path, timeout=30)
else:
    con = psycopg.connect(
        dbname=os.getenv("PG_AUTHORS_DB", os.getenv("PG_DB", "authorsite")),
        user=os.getenv("PG_AUTHORS_USER", os.getenv("PG_USER", "authorsite")),
        password=os.getenv("PG_AUTHORS_PASSWORD", os.getenv("PG_PASSWORD", "")),
        host=os.getenv("PG_AUTHORS_HOST", os.getenv("PG_HOST", "db")),
        port=os.getenv("PG_AUTHORS_PORT", os.getenv("PG_PORT", "5432")),
    )
# Use a compatibility wrapper around the psycopg cursor to support
# executescript() and translate SQLite-style SQL to PostgreSQL.  Without
# this wrapper, calls like cur.executescript() would fail because
# psycopg's cursors do not implement that method.  CursorCompat is
# defined near the top of this file and adds executescript() plus
# translates placeholders and common SQLite idioms (e.g. INSERT OR IGNORE).
cur = SQLiteCursorCompat(con.cursor()) if LOCAL_ETL_MODE else CursorCompat(con.cursor())

# --- "Monkey patch" ---
# La lógica original de este script usa métodos específicos de SQLite como
# executescript, así como placeholders de consulta con '?'.  Para mantener la
# mayor parte del código sin cambios, definimos unas funciones de ayuda que
# adaptan las consultas a PostgreSQL.  Estas funciones se asignan como
# atributos del objeto de conexión (con.execute y con.executescript) y
# realizan las siguientes tareas:
#   * Sustituyen los marcadores de posición '?' por '%s' (síntaxis de psycopg).
#   * Sustituyen 'INSERT OR IGNORE INTO' por 'INSERT INTO … ON CONFLICT DO NOTHING'.
#   * Sustituyen 'AUTOINCREMENT' y 'INTEGER UNIQUE PRIMARY KEY' por 'SERIAL PRIMARY KEY'.
def _execute_wrapper(query, params=None):
    """
    Execute a single SQL query on a fresh cursor and return an iterable
    psycopg cursor.  SQLite's ``Connection.execute`` returns a new
    cursor object each call; to emulate this behaviour in PostgreSQL,
    ``_execute_wrapper`` instantiates a new cursor via ``con.cursor()``
    and wraps it with ``CursorCompat`` before executing the query.  This
    prevents state from leaking between successive calls (e.g., an INSERT
    followed by a SELECT) and avoids errors like "the last operation didn't
    produce records" when iterating over the result of a SELECT.
    """
    # Create a fresh cursor for this execution
    local_cursor = CursorCompat(con.cursor())
    # Use the CursorCompat's execute method to handle placeholder and
    # SQL normalization.  CursorCompat will translate '?' to '%s',
    # normalize SQLite syntax, and execute the statement.
    if params is None:
        local_cursor.execute(query)
    else:
        local_cursor.execute(query, params)
    # Return the underlying psycopg cursor so that callers can iterate
    # over it using ``for row in ...``.  Returning the wrapped
    # CursorCompat would require implementing the full iterator
    # protocol, whereas the underlying psycopg cursor already does.
    return local_cursor._cur

def _executescript_wrapper(script):
    # Divide el script por ';' y ejecuta cada sentencia por separado
    statements = script.strip().split(";")
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        # Reemplaza tipos y palabras clave de SQLite que no existen en PostgreSQL
        stmt_pg = stmt
        # AUTOINCREMENT no existe en PostgreSQL; SERIAL ya crea un valor autoincremental
        stmt_pg = stmt_pg.replace("AUTOINCREMENT", "")
        # Convierte 'INTEGER UNIQUE PRIMARY KEY' a 'SERIAL PRIMARY KEY'
        stmt_pg = stmt_pg.replace("INTEGER UNIQUE PRIMARY KEY", "SERIAL PRIMARY KEY")
        # Convierte 'INSERT OR IGNORE' a una inserción con ON CONFLICT DO NOTHING
        stripped_stmt = stmt_pg.lstrip().lower()
        if stripped_stmt.startswith("insert or ignore into"):
            stmt_pg = stmt_pg.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
        # Ejecuta la sentencia
        cur.execute(stmt_pg)
    # Haz commit al finalizar todas las sentencias
    con.commit()

if not LOCAL_ETL_MODE:
    con.execute = _execute_wrapper
    con.executescript = _executescript_wrapper

def _ensure_fk_constraint(table_name: str, constraint_name: str, constraint_sql: str) -> None:
    if LOCAL_ETL_MODE:
        return
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (constraint_name,))
    if cur.fetchone() is None:
        cur.execute(f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} {constraint_sql}")
        con.commit()

def _migrate_works_uniqueness() -> None:
    """Defensive migration for works uniqueness strategy.

    1) Remove legacy single-column uniqueness on ``doi``.
    2) Add composite uniqueness on ``(job_id, author_id, doi)``.
    """
    if LOCAL_ETL_MODE:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS works_job_author_doi_key "
            "ON works (job_id, author_id, doi)"
        )
        con.commit()
        return
    # 1) Remove old UNIQUE constraints that only apply to works.doi
    cur.execute(
        """
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS cols(attnum, ord) ON true
        JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = cols.attnum
        WHERE con.contype = 'u'
          AND nsp.nspname = 'public'
          AND rel.relname = 'works'
        GROUP BY con.conname
        HAVING COUNT(*) = 1 AND MAX(att.attname) = 'doi'
        """
    )
    for row in cur.fetchall():
        constraint_name = row[0]
        cur.execute(f'ALTER TABLE public.works DROP CONSTRAINT IF EXISTS "{constraint_name}"')

    # 1b) Remove old standalone UNIQUE indexes over works(doi)
    cur.execute(
        """
        SELECT idx.indexname
        FROM pg_indexes idx
        WHERE idx.schemaname = 'public'
          AND idx.tablename = 'works'
          AND idx.indexdef ILIKE 'CREATE UNIQUE INDEX%'
          AND idx.indexdef ILIKE '%(doi)%'
        """
    )
    for row in cur.fetchall():
        index_name = row[0]
        cur.execute(f'DROP INDEX IF EXISTS public."{index_name}"')

    # 2) Add new composite key used for dedup + ON CONFLICT
    cur.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s",
        ("works_job_author_doi_key",),
    )
    if cur.fetchone() is None:
        cur.execute(
            """
            ALTER TABLE public.works
            ADD CONSTRAINT works_job_author_doi_key UNIQUE (job_id, author_id, doi)
            """
        )

def _log_works_rows_for_phase(phase_name: str) -> None:
    cur.execute(
        "SELECT COUNT(*) FROM works WHERE author_id = ? AND job_id = ?",
        (author_id, JOB_ID_VALUE),
    )
    works_count = cur.fetchone()[0]
    print(f"[{phase_name}] works rows available for author_id={author_id}, job_id={JOB_ID_VALUE}: {works_count}")

def _verify_works_supports_cross_job_rows() -> None:
    """Validate (defensively) that the same DOI can be inserted for another job_id."""
    cur.execute(
        """
        SELECT doi, type, title, published_in, authors, year, cited_by, api_source
        FROM works
        WHERE author_id = ? AND job_id = ? AND doi IS NOT NULL
        ORDER BY id
        LIMIT 1
        """,
        (author_id, JOB_ID_VALUE),
    )
    sample = cur.fetchone()
    if sample is None:
        print("[works-migration-check] skipped: no DOI rows available to probe cross-job insert.")
        return

    probe_job_id = JOB_ID_VALUE + 10_000_000
    publication_doi, publication_type, publication_title, publication_journal, authors_str, publication_year, publication_cites, api_source = sample
    cur.execute("SAVEPOINT works_migration_probe")
    try:
        cur.execute(
            """
            INSERT INTO works (job_id, author_id, type, doi, title, published_in, authors, year, cited_by, api_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_id, author_id, doi) DO NOTHING
            """,
            (
                probe_job_id,
                author_id,
                publication_type,
                publication_doi,
                publication_title,
                publication_journal,
                authors_str,
                publication_year,
                publication_cites,
                api_source,
            ),
        )
        print(f"[works-migration-check] probe insert rowcount={cur.rowcount} for author_id={author_id}, doi={publication_doi}, probe_job_id={probe_job_id}")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT works_migration_probe")
        cur.execute("RELEASE SAVEPOINT works_migration_probe")

cur.executescript("""
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
""")

cur.execute(
    """
    ALTER TABLE author_summary
    ADD COLUMN IF NOT EXISTS institution TEXT
    """
)

cur.execute(
    """
    ALTER TABLE author_summary
    ADD COLUMN IF NOT EXISTS profile_load_time_seconds DOUBLE PRECISION
    """
)

cur.execute(
    """
    ALTER TABLE author_summary
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    """
)
cur.execute(
    """
    ALTER TABLE author_summary
    ADD COLUMN IF NOT EXISTS job_id INTEGER
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS author_summary_job_id_idx ON author_summary (job_id)")

display_name = researcher_info["display_name"]
orcid_candidates = []
if orcid_url:
    orcid_candidates.append(orcid_url)
if orcid_num:
    orcid_candidates.append(orcid_num)
    orcid_candidates.append(orcid_num.replace("-", ""))
orcid_candidates = [c for i, c in enumerate(orcid_candidates) if c and c not in orcid_candidates[:i]]

if orcid_candidates:
    placeholders = ", ".join(["%s"] * len(orcid_candidates))
    cur.execute(
        f"DELETE FROM author_summary WHERE orcid IN ({placeholders}) AND job_id = %s",
        tuple(orcid_candidates) + (JOB_ID_VALUE,),
    )
else:
    cur.execute(
        """
        DELETE FROM author_summary
        WHERE LOWER(name) = LOWER(?) AND job_id = ?
        """,
        (display_name, JOB_ID_VALUE),
    )

cur.execute(
    """
    INSERT INTO author_summary
        (job_id, name, institution, github_name, orcid, h_index, i10_index, n_citas, n_publicaciones, n_publicaciones_oa, publicaciones_citas_avrg, profile_load_time_seconds, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
    RETURNING id
    """,
    (
        JOB_ID_VALUE,
        display_name,
        institution,
        github_name,
        orcid_url,
        h_index,
        i10_index,
        total_citas,
        total_publicaciones,
        oa_true,  # <- usa el conteo seguro
        round(total_citas / max(1, _safe_int(total_publicaciones))),  # <- evita /0
        None,
    ),
)
author_id = cur.fetchone()[0]
con.commit()

# Update job progress to 30%
try: 
    update_progress(30, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise



########################
# ________Articulo mas citado
########################

cur.executescript("""
CREATE TABLE IF NOT EXISTS works (
                  id SERIAL PRIMARY KEY,
                  job_id INTEGER,
                  author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                  type TEXT,
                  doi TEXT,
                  title TEXT,
                  published_in TEXT,
                  authors TEXT,
                  year INTEGER,
                  cited_by INTEGER,
                  api_source TEXT,
                  wikipedia_mentions INTEGER,
                  newsfeed_mentions INTEGER,
                  authors_institutions TEXT,
                  institutions_country_code TEXT
                  )
""")
_ensure_fk_constraint(
    "works",
    "works_author_id_fkey",
    "FOREIGN KEY (author_id) REFERENCES author_summary(id) ON DELETE CASCADE",
)
cur.execute(
    """
    ALTER TABLE works
    ADD COLUMN IF NOT EXISTS job_id INTEGER
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS works_job_id_idx ON works (job_id)")
_migrate_works_uniqueness()


publication_list = list()

api_source = "openalex"

for work in works_info:
    publication_type = work["type"]
    publication_doi = work["doi"]
    publication_title = work["title"]

    try:
        publication_journal = work["primary_location"]["source"]["display_name"]
    except:
        publication_journal = None

    publication_year = work["publication_year"]
    publication_cites = work["cited_by_count"]

    publication_authors = list()
    for author in work["authorships"]:
        au = author["author"]["display_name"]
        if not au:
            continue
        publication_authors.append(str(au))
    
    authors_str = "; ".join(publication_authors)
    
    #### PARA EXPORTAR A EXCEL ####
    publication_list.append(
        {
            "type":publication_type,
            "doi":publication_doi,
            "title":publication_title,
            "published_in":publication_journal,
            "authors":authors_str,
            "year":publication_year,
            "cited_by":publication_cites,
            "api_source":api_source
         }
    )
    ##############################


    cur.execute(
        """
        INSERT INTO works (job_id, author_id, type, doi, title, published_in, authors, year, cited_by, api_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (job_id, author_id, doi) DO NOTHING
        """,
        (JOB_ID_VALUE, author_id, publication_type, publication_doi, publication_title, publication_journal, authors_str, publication_year, publication_cites, api_source),
    )

    con.commit()

#### PARA EXPORTAR A EXCEL ####
publication_df = pd.DataFrame(publication_list)
if not publication_df.empty and "cited_by" in publication_df.columns:
    publication_df = publication_df.sort_values("cited_by", ascending=False)
# publication_df.to_excel(f"publication_list_author_id_{author_id}_{researcher_info["display_name"]}.xlsx", index=False)
##############################

_verify_works_supports_cross_job_rows()

################################################
###### FASE 2: Verificando DOIs…  ###### 
################################################

set_phase("phase_doi_check")

print()
print("Verificando DOI contra el titulo del landing page...")

doi_session = requests.Session()
doi_retry = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"]
)
doi_adapter = HTTPAdapter(max_retries=doi_retry)
doi_session.mount("https://", doi_adapter)
doi_session.mount("http://", doi_adapter)

query = """
SELECT id, doi, title
FROM works
WHERE author_id = ? AND job_id = ?
  AND doi IS NOT NULL
ORDER BY id
"""

work_rows = list(con.execute(query, (author_id, JOB_ID_VALUE)))
total_works = len(work_rows)

for index, row in enumerate(work_rows, start=1):
    work_id = row[0]
    doi_value = row[1]
    title = row[2]
    if total_works:
        try:
            progress_pct = 30 + _safe_int((index / total_works) * 20)
            update_progress(progress_pct)
        except Exception as e:
            update_progress(status="error", error=phase_error_message(e))
            raise
    doi_url = _normalize_doi_url(doi_value)
    if not doi_url:
        continue
    doi_deadline = time.monotonic() + 10
    remaining = doi_deadline - time.monotonic()
    if remaining <= 0:
        continue
    page_html = _fetch_doi_page_html(doi_url, doi_session, timeout=remaining)
    if time.monotonic() > doi_deadline:
        print(f"Warning: DOI fetch timed out for {doi_value}; skipping.")
        continue
    if page_html is None:
        continue
    if not _title_fragment_in_html(title, page_html):
        print(f"DOI incorrecto detectado (work_id={work_id}): {doi_value}")
        cur.execute("UPDATE works SET doi = NULL WHERE id = ? AND job_id = ?", (work_id, JOB_ID_VALUE))
        con.commit()

print()
print("Articulo mas citado: ")

query = """
SELECT type, doi, title, published_in, authors, year, cited_by
FROM works
WHERE author_id = ? AND job_id = ?
ORDER BY cited_by DESC
LIMIT 1
"""

for row in cur.execute(query, (author_id, JOB_ID_VALUE)):
    print(f"     - DOI: {row[1]}")
    print(f"     - Title: {row[2]}")
    print(f"     - Journal: {row[3]}")
    print(f"     - Year: {row[5]}")
    print(f"     - Authors: {row[4]}")
    print(f"     - Cited by: {row[6]}")

# Update job progress to 50%
try:
    update_progress(50, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise

########################
# ________Citas recibidas por publicacion
########################

print()
print(f"Citas recibidas por publicacion: <ver la db 'author_database.sqlite'>")
print()



########################
# ________Citas promedio (total publicaciones)
########################

query = """
SELECT *
FROM works
where author_id = ? AND job_id = ?
"""

citas_total_publicaciones_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    # print(row)
    cited_by = row[8]
    citas_total_publicaciones_list.append(cited_by)

try:
    citas_total_publicaciones = round(sum(citas_total_publicaciones_list)/len(citas_total_publicaciones_list))
except:
    citas_total_publicaciones = 0

print(f"Citas promedio (total publicaciones): {citas_total_publicaciones}")




########################
# ________Citas promedio (articulos)
########################


citas_articulos_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    #print(row)
    type = row[2]
    if type != "article":
        continue
    cited_by = row[8]
    citas_articulos_list.append(cited_by)

try:
    citas_articulos = round(sum(citas_articulos_list)/len(citas_articulos_list))
except:
    citas_articulos = 0

print(f"Citas promedio (articulos): {citas_articulos}")



# ########################
# # ________Citas promedio (reviews)
# ########################

citas_reviews_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    #print(row)
    type = row[2]
    if type != "review":
        continue
    cited_by = row[8]
    citas_reviews_list.append(cited_by)

try:
    citas_reviews = round(sum(citas_reviews_list)/len(citas_reviews_list))
except:
    citas_reviews = 0

print(f"Citas promedio (reviews): {citas_reviews}")




# ########################
# # ________Citas promedio (libros)
# ########################


citas_libros_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    #print(row)
    type = row[2]
    if type != "book":
        continue
    cited_by = row[8]
    citas_libros_list.append(cited_by)

try:
    citas_libros = round(sum(citas_libros_list)/len(citas_libros_list))
except:
    citas_libros = 0

print(f"Citas promedio (libros): {citas_libros}")





# ########################
# # ________Citas promedio (capitulos de libro)
# ########################

citas_capitulos_libro_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    #print(row)
    type = row[2]
    if type != "book-chapter":
        continue
    cited_by = row[8]
    citas_capitulos_libro_list.append(cited_by)

try:
    citas_capitulos_libro = round(sum(citas_capitulos_libro_list)/len(citas_capitulos_libro_list))
except:
    citas_capitulos_libro = 0
    

print(f"Citas promedio (capitulos de libro): {citas_capitulos_libro}")



# ########################
# # ________Citas promedio (preprints)
# ########################


citas_preprints_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    #print(row)
    type = row[2]
    if type != "preprint":
        continue
    cited_by = row[8]
    citas_preprints_list.append(cited_by)

try:
    citas_preprints = round(sum(citas_preprints_list)/len(citas_preprints_list))
except:
    citas_preprints = 0

print(f"Citas promedio (preprints): {citas_preprints}")


########################
# ________Publicaciones por año (Figura: grafico)
########################

publications_year_list = list()

query = """
SELECT id, type, year
FROM works
WHERE author_id = ? AND job_id = ?
ORDER BY year
DESC
"""

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    
    id = row[0]
    type = row[1]
    year = row[2]

    publications_year_list.append({
        "id":id,
        "type":type,
        "year":year
    })
    
publications_year_df = pd.DataFrame(publications_year_list)

if publications_year_df.empty or "year" not in publications_year_df.columns:
    print()
    print("Publicaciones por año: no hay datos disponibles.")
    print()
else:
    # limpiar year (si viene con comas) y convertir a entero
    publications_year_df["year"] = (
        publications_year_df["year"]
        .astype(str)
        .str.replace(",", "", regex=False)
        .pipe(pd.to_numeric, errors="coerce")
    )
    publications_year_df = publications_year_df.dropna(subset=["year"])
    publications_year_df["year"] = publications_year_df["year"].astype(int)

    # contar por año y tipo
    counts = publications_year_df.groupby(["year", "type"]).size().reset_index(name="count")

    # gráfico interactivo apilado
    fig = px.bar(
        counts.sort_values("year"),
        x="year", y="count", color="type",
        barmode="stack",
        title="Publicaciones por año y tipo",
        labels={"year": "Año", "count": "Número de publicaciones", "type": "Tipo"},
    )
    fig.update_layout(xaxis=dict(type="category", categoryorder="category ascending"))

    # guardar el HTML standalone (sin pathlib)
    # fig.write_html(f"publications_year_author_id_{author_id}_{researcher_info["display_name"]}.html",      include_plotlyjs="inline", full_html=True)

    print()
    print(f"Publicaciones por año: <ver grafica 'publications_year_author_id_{author_id}_{researcher_info["display_name"]}.html'>")
    print()

# Update job progress to 60%
try:
    update_progress(60, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise

########################
# ________Publicaciones con instituciones extranjeras
########################

##### ADICION_OPT #####
##### Ahora mismo esto esta puesto para que coja 1 institucion por autor (aunque un autor puede pertenecer a mas de 1). Entonces, si un autor pertenece a mas de 1 institucion, solo se coge la primera que aparece en openalex, perdiendo las demas.

for work in works_info:
    institution_list = list()
    institution_country_code_list = list()

    #print(json.dumps(work, indent=8, ensure_ascii=False))
    authorships = work["authorships"]

    publication_title = work["title"]
    publication_year = work["publication_year"]
    publication_cites = work["cited_by_count"]

    #print(publication_title)
    for item in authorships:
        author = item["author"]["display_name"]
        try:
            institution = item["institutions"][0]["display_name"]
        except:
            institution = "None"
        
        try:
            institution_country_code = item["institutions"][0]["country_code"]
        except:
            institution_country_code = "None"
        
        institution_list.append(institution)
        institution_country_code_list.append(institution_country_code)

        # print(author)
        # print(institution)
        # print(institution_country_code)

        # print(institution_list)
        # print(institution_country_code_list)
   
        cur.execute(
            "SELECT id FROM works WHERE title = ? AND year = ? AND cited_by = ? AND job_id = ?",
            (publication_title, publication_year, publication_cites, JOB_ID_VALUE),
        )
        
        result = cur.fetchone()
        if result is not None:
            publication_id = result[0]
        else:
            print(f"No se encontró publicación con esos parámetros en la base de datos.")
            continue  
        #print(publication_id)
        #print()
##### ERROR AQUI:expected str instance, NoneType found
        cur.execute(
            "UPDATE works SET authors_institutions = ?, institutions_country_code = ? WHERE id = ? AND job_id = ?",
            (
                "; ".join([str(check_str) if check_str is not None else "" for check_str in institution_list]),
                #"; ".join(institution_list),
                "; ".join([str(check_str) if check_str is not None else "" for check_str in institution_country_code_list]),
                publication_id,
                JOB_ID_VALUE,
            ),
        )
        
        con.commit()


query = """
SELECT id, authors, year, authors_institutions, institutions_country_code
FROM works
WHERE author_id = ? AND job_id = ?
"""

row_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    # print(row)
    id = row[0]
    authors = row[1]
    year = row[2]
    institutions = row[3]
    institutions_country = row[4]

    authors_split = authors.split(";")
    #print(authors_split)

### Error aqui: expected str instance, NoneType found. Si algun elemento es NONE genera errores
    try:
        if institutions is not None:
            institutions_split = institutions.split(";")
        else:
            institutions_split = []
    except KeyError:
        institutions_split = ""
    #print(institutions_split)

    try:
        if institutions_country is not None:
            institutions_country_split = institutions_country.split(";")
        else:
            institutions_country_split = []
    except KeyError:
        institutions_country_split = ""

    # Prevent blank rows if all are empty
    if any([authors_split, institutions_split, institutions_country_split]):
        rows = []
        for a, inst, country in zip_longest(
                authors_split or [],
                institutions_split or [],
                institutions_country_split or [],
                fillvalue=None):
            rows.append({
                "work_id": id,                   
                "author": a,
                "institution": inst,
                "institution_country": country
        })
    df = pd.DataFrame(rows)
    row_list.append(df)


if row_list:
    ins_country_df = pd.concat(row_list)
else:
    ins_country_df = pd.DataFrame(columns=["work_id", "author", "institution", "institution_country"])
# print(ins_country_df)


# Normalizamos los strings de todas las columnas, eliminando los espacios
ins_country_df["work_id"] = ins_country_df["work_id"].astype(str).str.strip()
ins_country_df["author"] = ins_country_df["author"].astype(str).str.strip()
ins_country_df["institution"] = ins_country_df["institution"].astype(str).str.strip()
ins_country_df["institution_country"] = ins_country_df["institution_country"].astype(str).str.strip()

# Eliminando las filas que tienen "None" en Institution
# Reemplazar valores None o la cadena "None" por NaN
ins_country_df["institution"] = ins_country_df["institution"].replace(["None", None], pd.NA)

# Eliminar filas con NA en "institution"
ins_country_df = ins_country_df.dropna(subset=["institution"])
#print()
#print(ins_country_df)



# Hacemos subset con las columnas "institution" e "institution_country"
ins_country_df = ins_country_df[["institution", "institution_country"]]
#print()
#print(ins_country_df)


# Borramos filas repetidas, nos quedamos con la primera ocurrencia
ins_country_df = ins_country_df.drop_duplicates(["institution"], keep="first")
print()
print("Lista de instituciones (propias y de colaboradores)")
print(ins_country_df)


# Contamos los valores unicos por pais
print()
print("Numero de instituciones por pais (propias y de colaboradores)")
print(ins_country_df["institution_country"].value_counts())


# Update job progress to 70%
try:
    update_progress(70, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise


################################################
###### FASE 3: Extrayendo de Crossref EventData…  ###### 
################################################

set_phase("phase_crossref")
_log_works_rows_for_phase("phase_crossref")

##############################################################################################
##############################################################################################
##############################################################################################
# ___Crossref Event Data (CED) API #####
##############################################################################################
##############################################################################################
##############################################################################################

# NOTA: CED va a ser cerrada (noticia del 29 julio 2025) y, a cambio, abriran una nueva API (noticia: https://community.crossref.org/t/an-update-on-event-data-and-data-citations/14203)

##### ADICION_OPT #####
##### QUIZAS PODEMOS USAR LA API "DataCite Event Data" (https://support.datacite.org/docs/eventdata-guide)??

cur.execute("SELECT pg_advisory_lock(2200, 701)")
try:
    cur.execute("SELECT to_regclass('public.events')")
    if cur.fetchone()[0] is None:
        cur.execute(
            """
            SELECT 1
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typname = 'events' AND n.nspname = 'public'
            """
        )
        if cur.fetchone():
            cur.execute("DROP TYPE public.events")
    try:
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS events (
                          id SERIAL PRIMARY KEY,
                          job_id INTEGER,
                          author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                          work_id INTEGER,
                          date TEXT,
                          source_id TEXT,
                          language TEXT,
                          title TEXT,
                          relation_type TEXT,
                          source_url TEXT,
                          object_id TEXT,
                          api_source TEXT
                          )
        """)
    except psycopg.errors.UniqueViolation:
        con.rollback()
        cur.execute("SELECT to_regclass('public.events')")
        if cur.fetchone()[0] is None:
            time.sleep(0.5)
            cur.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                              id SERIAL PRIMARY KEY,
                              job_id INTEGER,
                              author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                              work_id INTEGER,
                              date TEXT,
                              source_id TEXT,
                              language TEXT,
                              title TEXT,
                              relation_type TEXT,
                              source_url TEXT,
                              object_id TEXT,
                              api_source TEXT
                              )
            """)
finally:
    cur.execute("SELECT pg_advisory_unlock(2200, 701)")
_ensure_fk_constraint(
    "events",
    "events_author_id_fkey",
    "FOREIGN KEY (author_id) REFERENCES author_summary(id) ON DELETE CASCADE",
)
cur.execute(
    """
    ALTER TABLE events
    ADD COLUMN IF NOT EXISTS job_id INTEGER
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS events_job_id_idx ON events (job_id)")

url = "https://api.eventdata.crossref.org/v1/events"
rows = 1000
next_cursor = None

api_source = "crossref_event_data"
session = requests.Session()
request_timeout = 2
adapter = HTTPAdapter(max_retries=0)
session.mount("https://", adapter)
session.mount("http://", adapter)

query = """
SELECT id, doi, title
FROM works
WHERE author_id = ? AND job_id = ?
ORDER BY id
"""

work_rows = [row for row in con.execute(query, (author_id, JOB_ID_VALUE)) if row[1] is not None]
total_works = len(work_rows)

for index, row in enumerate(work_rows, start=1):
    work_id = row[0]
    doi = row[1]
    title = row[2]
    obj_id = re.findall("doi.org/(.*)", doi)[0]
    
    print()
    print()
    print(f"Buscando: {work_id}) || Title: {title} || DOI: {obj_id}")
    if total_works:
        try:
            progress_pct = 70 + _safe_int((index / total_works) * 10)
            update_progress(progress_pct, "running")
        except Exception as e:
            update_progress(status="error", error=phase_error_message(e))
            raise

    ##################
    ### WIKIPEDIA ####
    ##################

    source = "wikipedia"
    next_cursor = None
    count = 0
    work_started_at = time.perf_counter()
    page_deadline = None
    work_deadline = None
    skip_work = False

    while True:
        if time.perf_counter() - work_started_at > 2:
            print(f"Warning: Crossref Event Data timed out for {obj_id}; skipping.")
            break
        if work_deadline and time.perf_counter() > work_deadline:
            print(f"Warning: Crossref Event Data timed out for {obj_id}; skipping.")
            break

        if next_cursor is not None:
                params = {"rows":rows, 
                "source":source,
                "obj-id":obj_id,
                "cursor":next_cursor
                }

        else: 
            params = {"rows":rows, 
                    "source":source,
                    "obj-id":obj_id}
            
        count = count + 1

        params = {**params, "mailto": OPENALEX_MAILTO}
        try:
            response = session.get(url, params=params, timeout=request_timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            print(f"Warning: Crossref Event Data request failed for {obj_id}: {exc}")
            break
        data = response.json()
        # print(json.dumps(data, indent=8, ensure_ascii=False))

        next_cursor = data["message"]["next-cursor"]
        total_results = data["message"]["total-results"]

        events = data["message"]["events"]

        paginas = round(total_results/rows) + 1 # He añadido el +1 porque asi se corrige el descabalgamiento entre el "count" y "paginas"

        if count == 1:
            print(f"Wikipedia: {total_results} eventos totales repartidos en {paginas} paginas")
        
        percentage = (count / paginas) * 100
        print(f"Wikipedia: descargando pagina {count} de {paginas} ({round(percentage)} %)...")
        if count == 1 and paginas == 1:
            page_deadline = time.perf_counter() + 2
            work_deadline = time.perf_counter() + 2

        for event in events:
            if page_deadline and time.perf_counter() > page_deadline:
                print(f"Warning: Crossref Event Data page processing timed out for {obj_id}; skipping.")
                skip_work = True
                break
            if work_deadline and time.perf_counter() > work_deadline:
                print(f"Warning: Crossref Event Data timed out for {obj_id}; skipping.")
                skip_work = True
                break
            object_id = event["obj_id"]
            source_id = event["source_id"]
            date = event["occurred_at"]
            subj = event.get("subj") or {}
            source_url = subj.get("url") or event.get("subj_id")
            if source_url:
                try:
                    language = re.findall("https://(.*).wikipedia.org", source_url)[0]
                except Exception:
                    language = "language not detected"
            else:
                language = "language not detected"
            title = subj.get("title") or event.get("subj_id") or "title not available"
            relation_type = event["relation_type_id"]

            cur.execute(
                "INSERT OR IGNORE INTO events (job_id, author_id, work_id, date, source_id, language, title, relation_type, source_url, object_id, api_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (JOB_ID_VALUE, author_id, work_id, date, source_id, language, title, relation_type, source_url, object_id, api_source),
            )

            con.commit() 
            ##### ADICION_OPT #####
            ##### MIRAR AQUI EL TRUCO PARA EVITAR COMETER ERRORES CUANDO SE MANEJAN GRANDES VOLUMENES DE DATOS (COMO PODRIA SER ESTE CASO)

        if skip_work:
            break

        if next_cursor is None:
            break
        
            


    ##################
    ### NEWSFEED ####
    ##################

    source = "newsfeed"
    count = 0
    next_cursor = None
    while True:
        if work_deadline and time.perf_counter() > work_deadline:
            print(f"Warning: Crossref Event Data timed out for {obj_id}; skipping.")
            skip_work = True
            break

        if next_cursor is not None:
                params = {"rows":rows, 
                "source":source,
                "obj-id":obj_id,
                "cursor":next_cursor
                }

        else: 
            params = {"rows":rows, 
                    "source":source,
                    "obj-id":obj_id}

        count = count + 1
        
        params = {**params, "mailto": OPENALEX_MAILTO}
        try:
            response = session.get(url, params=params, timeout=request_timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            print(f"Warning: Crossref Event Data request failed for {obj_id}: {exc}")
            break
        data = response.json()
        # print(json.dumps(data, indent=8, ensure_ascii=False))

        next_cursor = data["message"]["next-cursor"]
        total_results = data["message"]["total-results"]

        events = data["message"]["events"]

        paginas = round(total_results/rows) + 1 # He añadido el +1 porque asi se corrige el descabalgamiento entre el "count" y "paginas"

        if count == 1:
            print(f"Newsfeed: {total_results} eventos totales repartidos en {paginas} paginas")
        
        percentage = (count / paginas) * 100
        print(f"Newsfeed: descargando pagina {count} de {paginas} ({round(percentage)} %)...")

        for event in events:
            if work_deadline and time.perf_counter() > work_deadline:
                print(f"Warning: Crossref Event Data timed out for {obj_id}; skipping.")
                skip_work = True
                break
            object_id = event["obj_id"]
            source_id = event["source_id"]
            date = event["occurred_at"]
            subj = event.get("subj") or {}
            source_url = subj.get("url") or event.get("subj_id")
            if source_url:
                try:
                    language = re.findall("https://(.*).wikipedia.org", source_url)[0]
                except Exception:
                    language = "language not detected"
            else:
                language = "language not detected"
            title = subj.get("title") or event.get("subj_id") or "title not available"
            relation_type = event["relation_type_id"]

            # if "wiki" in event["subj_id"]: continue # Algunos newsfeed son de wikipedia... quizas los podemos ignorar porque ya los hemos incluido antes arriba

            cur.execute(
                "INSERT OR IGNORE INTO events (job_id, author_id, work_id, date, source_id, language, title, relation_type, source_url, object_id, api_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (JOB_ID_VALUE, author_id, work_id, date, source_id, language, title, relation_type, source_url, object_id, api_source),
            )

            con.commit() 
            ##### ADICION_OPT #####
            ##### MIRAR AQUI EL TRUCO PARA EVITAR COMETER ERRORES CUANDO SE MANEJAN GRANDES VOLUMENES DE DATOS (COMO PODRIA SER ESTE CASO)

        if skip_work:
            break

        if next_cursor is None:
            break
    

print()
print("Fin de los resultados")
print(f"Events guardados en 'author_database.sqlite'")
 
# Update job progress to 80%
try:
    update_progress(80, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise


##############################################################################################
##############################################################################################
##############################################################################################
# ___CLEAN DB #####
##############################################################################################
##############################################################################################
##############################################################################################

# En este script vamos a limpiar la tabla "events" DB para quedarnos con las paginas unicas de Wikipedia y Newsfeed


cur.execute("SELECT pg_advisory_lock(2200, 702)")
try:
    cur.execute("SELECT to_regclass('public.events_clean')")
    if cur.fetchone()[0] is None:
        cur.execute(
            """
            SELECT 1
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typname = 'events_clean' AND n.nspname = 'public'
            """
        )
        if cur.fetchone():
            cur.execute("DROP TYPE public.events_clean")
    try:
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS events_clean (
                          id SERIAL PRIMARY KEY,
                          job_id INTEGER,
                          author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                          work_id INTEGER,
                          date TEXT,
                          source_id TEXT,
                          language TEXT,
                          title TEXT,
                          relation_type TEXT,
                          source_url TEXT UNIQUE,
                          object_id TEXT,
                          api_source TEXT
                          )
        """)
    except psycopg.errors.UniqueViolation:
        con.rollback()
        cur.execute("SELECT to_regclass('public.events_clean')")
        if cur.fetchone()[0] is None:
            time.sleep(0.5)
            cur.executescript("""
            CREATE TABLE IF NOT EXISTS events_clean (
                              id SERIAL PRIMARY KEY,
                              job_id INTEGER,
                              author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                              work_id INTEGER,
                              date TEXT,
                              source_id TEXT,
                              language TEXT,
                              title TEXT,
                              relation_type TEXT,
                              source_url TEXT UNIQUE,
                              object_id TEXT,
                              api_source TEXT
                              )
            """)
finally:
    cur.execute("SELECT pg_advisory_unlock(2200, 702)")
_ensure_fk_constraint(
    "events_clean",
    "events_clean_author_id_fkey",
    "FOREIGN KEY (author_id) REFERENCES author_summary(id) ON DELETE CASCADE",
)
cur.execute(
    """
    ALTER TABLE events_clean
    ADD COLUMN IF NOT EXISTS job_id INTEGER
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS events_clean_job_id_idx ON events_clean (job_id)")

api_source = "crossref_event_data"

query = """
SELECT source_id, language, title, relation_type, source_url, date, object_id, work_id
FROM events
WHERE author_id = ? AND job_id = ?
"""


for row in con.execute(query, (author_id, JOB_ID_VALUE)): # Usa cur.execute para trabajar con resultados ya cargados; usa con.execute para iterar sin romper el cursor cuando vas a hacer más queries dentro del bucle.
    source_id = row[0]
    language = row[1]
    title = row[2]
    relation_type = row[3]
    source_url = row[4]
    date = row[5]
    object_id = row[6]
    work_id = row[7]
    
    if source_id == "wikipedia":
        if "&oldid=" not in source_url:
            continue

        clean_url = re.findall("(.*)&oldid=", source_url)[0]
    else:
        clean_url = source_url
    
    cur.execute(
        "INSERT OR IGNORE INTO events_clean (job_id, author_id, work_id, date, source_id, language, title, relation_type, source_url, object_id, api_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (JOB_ID_VALUE, author_id, work_id, date, source_id, language, title, relation_type, clean_url, object_id, api_source),
    )
    con.commit()


################################################
###### FASE 4: Verificando trabajos en Wikipedia... ###### 
################################################

set_phase("phase_wikipedia_check")
_log_works_rows_for_phase("phase_wikipedia_check")

# Eliminar páginas de Wikipedia que no contengan el DOI del trabajo
cleanup_query = """
SELECT id, source_url, object_id
FROM events_clean
WHERE author_id = ? AND job_id = ? AND source_id = 'wikipedia'
"""

for row in con.execute(cleanup_query, (author_id, JOB_ID_VALUE)):
    event_id = row[0]
    source_url = row[1]
    object_id = row[2]
    doi_fragment = _extract_doi_fragment(object_id)
    wiki_contains_doi = _wikipedia_page_contains_doi(source_url, doi_fragment, session=SESSION)
    if wiki_contains_doi is False:
        cur.execute("DELETE FROM events_clean WHERE id = ? AND job_id = ?", (event_id, JOB_ID_VALUE))
        con.commit()

query = """
SELECT source_url, title, language, date, object_id, source_id, relation_type, work_id
FROM events_clean
WHERE author_id = ? AND job_id = ?
"""
# Vamos a crear un excel para comparar con el test
wiki_pages = list()

count = 0
for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    source_url = row[0]
    title = row[1]
    language = row[2]
    date = row[3]
    object_id = row[4]
    source_id = row[5]
    relation_type = row[6]
    work_id = row[7]

    count = count + 1
    print(f"{count}) {source_id} || {work_id} || {title} ({language}): {source_url}")

    ###### NOTA: EN LA VERSION FINAL SE PUEDE BORRAR ESTE BLOQUE DE CODIGO ####
    wiki_pages.append({
        "ID":count,
        "work_id":work_id,
        "date":date,
        "source_id":source_id,
        "relation_type":relation_type,
        "language":language,
        "title":title,
        "url":source_url,
        "doi":object_id,
        "api_source":api_source
    })
    ####################################################################

wiki_pages_df = pd.DataFrame(wiki_pages)
# wiki_pages_df.to_excel(f"event_clean_author_id_{author_id}_{researcher_info["display_name"]}.xlsx", index=False)

##### ADICION_OPT #####: 
##### De las 97 paginas reales del test, 3 son repetidas (me he basado en que tienen el "title" practicamente igual)... se me ocurre que podria añadir un paso mas que sea: filtrar por language, y dentro de cada language, calcular el porcentaje de similitud de los titulos, y aquellos que tengan una similitud muy alta los tratamos como repetidos, y nos quedariamos con uno de los repetidos
##### De estas 97 paginas reales del test, 13 son falsos positivos... se me ocurre que con BeautifulSoup puedo parsear todas las paginas para asegurarse de que el DOI realmente esta, y nos quedamos con las paginas que realmente presentan ese DOI. 
# En resumen: al final, restando los repetidos y los falsos positivos, en el test tenemos 81 paginas unicas reales

print()
print(f"Paginas unicas reales: {count}")
print(f"Paginas disponibles en la tabla 'event_clean' de 'author_database.sqlite' y en el excel 'event_clean_author_id_{author_id}_{researcher_info["display_name"]}.xlsx'")








##############################################################################################
##############################################################################################
##############################################################################################
# ___UPDATE DB #####
##############################################################################################
##############################################################################################
##############################################################################################

# En este script vamos a actualizar la tabla "works" de la DB para rellenar las columnas "wikipedia_mentions" y "newsfeed_mention"
_log_works_rows_for_phase("phase_events_clean")


query = """
SELECT work_id, source_id
FROM events_clean
WHERE author_id = ? AND job_id = ?
"""

wikipedia_events = dict()
newsfeed_events = dict()

for row in con.execute(query, (author_id, JOB_ID_VALUE)): # Usa cur.execute para trabajar con resultados ya cargados; usa con.execute para iterar sin romper el cursor cuando vas a hacer más queries dentro del bucle.
    work_id = row[0]
    source_id = row[1]

    if source_id == "wikipedia":
        wikipedia_events[work_id] = wikipedia_events.get(work_id, 0) + 1
    if source_id == "newsfeed":
        newsfeed_events[work_id] = newsfeed_events.get(work_id, 0) + 1


# print()
# print("Wikipedia counts (work_id:count)")
# print(wikipedia_events)
# print()
# print("Newsfeed counts (work_id:count)")
# print(newsfeed_events)
# print()



query = """
SELECT id, wikipedia_mentions, newsfeed_mentions
FROM works
WHERE author_id = ? AND job_id = ?
"""
for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    id = row[0]

    for key,value in wikipedia_events.items():
        work_id = key
        wikipedia_mentions = value

        # print(work_id, wikipedia_mentions)

        cur.execute("UPDATE works SET wikipedia_mentions = ? WHERE id = ? AND job_id = ?", (wikipedia_mentions, work_id, JOB_ID_VALUE))
        con.commit()

    for key,value in newsfeed_events.items():
        work_id = key
        newsfeed_mentions = value

        # print(work_id, newsfeed_mentions)

        cur.execute("UPDATE works SET newsfeed_mentions = ? WHERE id = ? AND job_id = ?", (newsfeed_mentions, work_id, JOB_ID_VALUE))
        con.commit()



## QUERY DE PRUEBA ##
# Vamos a hacer una tabla que muestre:
# INFO ARTICULO [work_id || doi || title del articulo || autores ||] INFO EVENTS [source_id || date || title || source_url])

query = """
SELECT works.id AS ID,
       works.doi AS DOI,
       works.title AS Title,
       works.authors AS Authors,
       events_clean.source_id AS Event_domain,
       events_clean.date AS Event_date,
       events_clean.title AS Event_title,
       events_clean.source_url AS Event_url
FROM works
JOIN events_clean
ON works.id = events_clean.work_id
WHERE works.author_id = ? AND works.job_id = ? AND events_clean.author_id = ? AND events_clean.job_id = ?
"""

query_list = list()

for row in con.execute(query, (author_id, JOB_ID_VALUE, author_id, JOB_ID_VALUE)):
    work_id = row[0]
    doi = row[1]
    work_title = row[2]
    authors = row[3]
    event_domain = row[4]
    event_date = row[5]
    event_title = row[6]
    event_url = row[7]

    ##### PARA EXPORTAR A EXCEL ####
    query_list.append({
        "ID":work_id,
        "DOI":doi,
        "Title":work_title,
        "Authors":authors,
        "Event_domain":event_domain,
        "Event_date":event_date,
        "Event_title":event_title,
        "Event_url":event_url
    })

query_df = pd.DataFrame(query_list)
# query_df.to_excel(f"works_&_events_{author_id}_{researcher_info["display_name"]}.xlsx")

print(f"Works and events saved in 'works_&_events_{author_id}_{researcher_info["display_name"]}.xlsx'")





################################################
###### FASE 5: Extrayendo de Zenodo…  ###### 
################################################

set_phase("phase_zenodo")

##############################################################################################
##############################################################################################
##############################################################################################
# ________Repositorios (Zenodo)
##############################################################################################
##############################################################################################
##############################################################################################


########################
# ___Zenodo API #####
########################


cur.executescript("""
CREATE TABLE IF NOT EXISTS zenodo_github (
                  id SERIAL PRIMARY KEY,
                  job_id INTEGER,
                  author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                  type TEXT,
                  doi TEXT UNIQUE,
                  github_url TEXT UNIQUE,
                  title TEXT,
                  authors TEXT,
                  year TEXT,
                  cited_by INT,
                  api_source TEXT              
                  )
""")
_ensure_fk_constraint(
    "zenodo_github",
    "zenodo_github_author_id_fkey",
    "FOREIGN KEY (author_id) REFERENCES author_summary(id) ON DELETE CASCADE",
)
cur.execute(
    """
    ALTER TABLE zenodo_github
    ADD COLUMN IF NOT EXISTS job_id INTEGER
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS zenodo_github_job_id_idx ON zenodo_github (job_id)")

if orcid_num:
    search_term = "contributors.orcid: " + orcid_num + " " + "creators.orcid: " + orcid_num
else:
    search_term = f'creators.name:"{researcher_name}" OR contributors.name:"{researcher_name}"'

##### ADICION_OBL (HECHO) #####
# ##### CUIDADO: POR DEFECTO ESTARA PUESTA UNA PAGINA CON 25 RESULTADOS MAXIMO. SI HAY MAS DE 25 RESULTADOS, HABRA 2 PAGINAS O MAS, Y TAL COMO ESTA PUESTO AHORA EL SCRIPT NO PILLARIA LAS CONTRIBUCIONES DE LA 2 PAGINA (TIENES QUE HACER LOOP)

url = "https://zenodo.org/api/records"
#token = "VBt4d9cGkuWMdQisvOwb4f0BSTgpZ1vGbCHSUrUGH7tFqgcL6kRBWVP4aTNR"
token = os.getenv("ZENODO_TOKEN", "")
zenodo_items = []
try:
    params = {
        "q": search_term,
    }
    if token:
        params["access_token"] = token
    params = {**params, "mailto": OPENALEX_MAILTO}
    try:
        response = get(url, params=params)
    except requests.exceptions.HTTPError as exc:
        print(f"Warning: Zenodo request failed for query '{search_term}': {exc}")
        response = None

    if response is None:
        zenodo_data = {"total": 0, "hits": []}
    else:
        zenodo_data = response.json()["hits"]
    zenodo_counts = zenodo_data["total"] # N resultados que devuelve la busqueda
    #print(json.dumps(zenodo_items, indent=8, ensure_ascii=False))

    # Set pages and size to extract all results
    limit_zenodo = 25
    pages_count = zenodo_counts//limit_zenodo
    size = 200

    if zenodo_counts > 25:
        for page in range(1,pages_count + 2):
            params = {"q":search_term, "size":size, "page":page}
            if token:
                params["access_token"] = token
            params = {**params, "mailto": OPENALEX_MAILTO}
            try:
                response = get(url, params=params)
            except requests.exceptions.HTTPError as exc:
                print(f"Warning: Zenodo request failed for query '{search_term}' (page {page}): {exc}")
                break
            zenodo_data = response.json()
            zenodo_items.extend(zenodo_data["hits"]["hits"])
    else:
        zenodo_items = zenodo_data["hits"]
except Exception as exc:
    print(f"Warning: Zenodo phase failed and will be skipped: {exc}")
    zenodo_items = []

count = 0

for item in zenodo_items:
    count = count + 1

    resource_authors_creators_list = list()
    resource_authors_contributors_list = list()
    #resource_year_list = list()

    published_in = list()
    api_source = "zenodo"
    published_in.append(api_source)

    resource_type = item["metadata"]["resource_type"]["type"]
    #resource_type_works[resource_type] = resource_type_works.get(resource_type, 0) + 1
    #resource_type_works.append(resource_type)

    resource_doi = item["doi_url"]
    #resource_doi_works.append(resource_doi)

    try:
        resource_github_url = item["metadata"]["related_identifiers"][0]["identifier"]
        published_in.append("github")
    except:
        resource_github_url = None

    resource_title = item["metadata"]["title"]
    #resource_title_works.append(resource_title)

    resource_authors_creators = item["metadata"]["creators"]
    #resource_authors_creators_list.append(resource_authors_creators)

    for author in resource_authors_creators:
        author_name = author["name"]
        resource_authors_creators_list.append(author_name)
    try:
        resource_authors_contributors = item["metadata"]["contributors"]
        #resource_authors_contributors_list.append(resource_authors_contributors)
        for author in resource_authors_contributors:
            author_name = author["name"]
            resource_authors_contributors_list.append(author_name)
    except:
        pass
    resource_authors = resource_authors_creators_list + resource_authors_contributors_list
    resource_authors_str = "; ".join(str(author) for author in resource_authors if author)
    resource_year = item["metadata"]["publication_date"].split("-")[0]
    #resource_year_list.append(resource_year)

    cur.execute(
        "INSERT OR IGNORE INTO zenodo_github (job_id, author_id, type, doi, github_url, title, authors, year,  api_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (JOB_ID_VALUE, author_id, resource_type, resource_doi, resource_github_url, resource_title, resource_authors_str, resource_year, api_source),
    )
    con.commit()


    # print(f"Result ({count})")
    # print(f"Type:           {resource_type}")
    # print(f"Title:          {resource_title}")
    # print(f"DOI:            {resource_doi}")
    # print(f"Github:         {resource_github_url}")
    # print(f"Published in:   {', '.join(str(pub) for pub in published_in if pub}")
    # print(f"Year:           {resource_year}")
    # print(f"Authors:        {resource_authors_str[:150]}")
    # print(f"Cited by:       {000}")
    # print()



################################################
###### FASE 6: Extrayendo de GitHub...  ###### 
################################################

set_phase("phase_github")

##############################################################################################
##############################################################################################
##############################################################################################
# ________Repositorios (GitHub)
##############################################################################################
##############################################################################################
##############################################################################################

########################
# ___GitHub API #####
########################

# cur.execute("DROP TABLE IF EXISTS zenodo_github")

cur.executescript("""
CREATE TABLE IF NOT EXISTS zenodo_github (
                  id SERIAL PRIMARY KEY,
                  job_id INTEGER,
                  author_id INTEGER REFERENCES author_summary(id) ON DELETE CASCADE,
                  type TEXT,
                  doi TEXT UNIQUE,
                  github_url TEXT UNIQUE,
                  title TEXT,
                  authors TEXT,
                  year TEXT,
                  cited_by INT,
                  api_source TEXT              
                  )
""")
_ensure_fk_constraint(
    "zenodo_github",
    "zenodo_github_author_id_fkey",
    "FOREIGN KEY (author_id) REFERENCES author_summary(id) ON DELETE CASCADE",
)
cur.execute(
    """
    ALTER TABLE zenodo_github
    ADD COLUMN IF NOT EXISTS job_id INTEGER
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS zenodo_github_job_id_idx ON zenodo_github (job_id)")

# GitHub API token (if available).  For personal access tokens (prefix ``ghp_``),
# the recommended Authorization header is ``token <PAT>`` rather than
# ``Bearer``.  If a token is provided in the environment, include it;
# otherwise rely on unauthenticated requests.
gh_headers = {"Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    # Use 'token' prefix for GitHub personal access tokens
    gh_headers["Authorization"] = f"token {GITHUB_TOKEN}"

##### ADICION_OBL (HECHO)#####
##### Por defecto se enseñan 30 resultados en 1 pagina. Hacer un loop en caso de que haya mas de 30 resultados

# Get number of repositories from GitHub.  Use a try/except block to
# gracefully handle authorization errors (401/403).  If the token is
# missing or invalid, skip GitHub retrieval instead of raising an uncaught
# exception.
if github_name and github_name.strip():
    try:
        url = f"https://api.github.com/users/{github_name}"
        response = get(url, extra_headers=gh_headers)
        data = response.json()
        num_repos = data.get("public_repos", 0)
        print(f"Repositories (Github): {num_repos}")

        github_data_list = []

        # If num_repos <= 30, get all repositories in one page
        if num_repos <= 30:
            page1 = get(
                f"https://api.github.com/users/{github_name}/repos?page=1&per_page=30",
                extra_headers=gh_headers,
            ).json()
            github_data_list.extend(page1)
        else:
            # Retrieve repositories information across multiple pages
            per_page_github = 30
            count_pages_github = num_repos // per_page_github
            for page in range(1, count_pages_github + 2):
                url = f"https://api.github.com/users/{github_name}/repos?page={page}&per_page=30"
                resp = get(url, extra_headers=gh_headers)
                github_data_list.extend(resp.json())

        count = 0
    except requests.exceptions.HTTPError as e:
        # Unauthorized or forbidden access to GitHub API; log and skip
        if hasattr(e.response, "status_code") and e.response.status_code in (401, 403):
            print(f"GitHub API authorization failed ({e.response.status_code}); skipping GitHub data.")
            num_repos = 0
            github_data_list = []
            count = 0
        else:
            raise

    for repo in github_data_list:
        count = count + 1

        resource_authors = list()
        
        github_url = repo["html_url"]
        github_title = repo["name"]
        github_year = repo["created_at"].split("-")[0]
        api_source = "github"

        contributors_url = f"https://api.github.com/repos/{github_name}/{github_title}/contributors"
        contributors_response = get(contributors_url, extra_headers=gh_headers)
        contributors_data = contributors_response.json()

        if contributors_data:
            for contributor in contributors_data:
                contributor_name = contributor["login"]
                resource_authors.append(contributor_name)

        cur.execute(
            "INSERT OR IGNORE INTO zenodo_github (job_id, author_id, github_url, title, authors, year, api_source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (JOB_ID_VALUE, author_id, github_url, github_title, "; ".join(resource_authors), github_year, api_source),
        )
        con.commit()
else:
    print("No GitHub username provided. Skipping GitHub data retrieval.")
    # print(github_title, github_url, resource_authors, github_year, api_source)

    # print(f"Result ({count})")
    # print(f"Title:          {github_title}")
    # print(f"Github:         {github_url}")
    # print(f"Published in:   {api_source}")
    # print(f"Year:           {github_year}")
    # print(f"Authors:        {"; ".join(resource_authors)[:150]}")
    # print()


# Update job progress to 90%
try:
    update_progress(90, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise


################################################
###### FASE 7: Extrayendo de OpenAIRE...  ###### 
################################################

set_phase("phase_openaire")

##############################################################################################
##############################################################################################
##############################################################################################
# ___OpenAIRE API #####
##############################################################################################
##############################################################################################
##############################################################################################

# Esta API nos permite extraer las citas de los repositorios de Zenodo

url = "https://api.openaire.eu/search/researchProducts"
format = "json"

api_source = "openaire"

query = """
SELECT id, doi, title
FROM zenodo_github
WHERE author_id = ? AND job_id = ?
ORDER BY id
"""


for row in con.execute(query, (author_id, JOB_ID_VALUE)):
    work_id = row[0]
    doi = row[1]
    title = row[2]

    if doi is None:
        continue

    obj_id = re.findall("doi.org/(.*)", doi)[0]
    
    print()
    print(f"Buscando citas para: {work_id}) || Title: {title} || DOI: {obj_id}")

    params = {
        "format":format,
        "doi":obj_id
    }

    response = get(url, params=params)
    data = response.json()
    # print(json.dumps(data, indent=8, ensure_ascii=False))
    # with open("openaire.txt", "w", encoding="utf-8") as file: file.write(json.dumps(data, indent=8, ensure_ascii=False))
    try:
        cited_by = data["response"]["results"]["result"][0]["metadata"]["oaf:entity"]["oaf:result"]["measure"][2]["@score"]
    except:
        cited_by = None

    print(f"Cited by: {cited_by}")

    cur.execute("UPDATE zenodo_github SET cited_by = ? WHERE id = ? AND job_id = ?", (cited_by, work_id, JOB_ID_VALUE))
    con.commit()

print()
print(f"Archivos en repositorios (Zenodo, GitHub):")
print()


query = """
SELECT id, type, doi, github_url, title, authors, year, cited_by, api_source
FROM zenodo_github
WHERE author_id = ? AND job_id = ?
"""

github_counts = 0
zenodo_counts = 0

# IMPORTANT:
# Do not mark the job as "done" from inside the ETL script.
# The Celery task sets final "done" only after this script returns.
# Marking done here can redirect the UI to results too early while the
# script is still performing DB work below, which is especially visible
# under concurrent runs.
try:
    update_progress(99, "running")
except Exception as e:
    update_progress(status="error", error=phase_error_message(e))
    raise


for row in con.execute(query, (author_id, JOB_ID_VALUE)):

    published_in = list()

    resource_id = row[0]
    resource_type = row[1]
    resource_doi = row[2]
    resource_github_url = row[3]
    resource_title = row[4]
    resource_authors = row[5]
    resource_year = row[6]
    resource_cited_by = row[7]
    resource_api_source = row[8]

    published_in.append(resource_api_source)

    if resource_github_url is not None and resource_api_source != "github":
        published_in.append("github")

    if resource_github_url is not None:
        resource_type = "github project"

    if "zenodo" in published_in:
        zenodo_counts = zenodo_counts + 1

    if "github" in published_in:
        github_counts = github_counts + 1

    print(f"Type:           {resource_type}")
    print(f"Title:          {resource_title}")
    print(f"DOI:            {resource_doi}")
    print(f"Github:         {resource_github_url}")
    print(f"Published in:   {", ".join(published_in)}")
    print(f"Year:           {resource_year}")
    print(f"Authors:        {resource_authors[:150]}")
    print(f"Cited by:       {resource_cited_by}")
    print()

print()
print(f"Total archivos en Zenodo: {zenodo_counts}")
print()
print(f"Total archivos en GitHub: {github_counts}")
print()


cur.close()
con.close()
