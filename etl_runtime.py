"""Imports required by the ETL executed through runpy.

PyInstaller cannot inspect imports inside a script loaded dynamically with
runpy.run_path. Importing them here makes the portable build deterministic and
also validates the complete runtime during executable startup.
"""

import bs4  # noqa: F401
import openpyxl  # noqa: F401
import pandas  # noqa: F401
import plotly.express  # noqa: F401
import psycopg  # noqa: F401
import requests  # noqa: F401
import tabulate  # noqa: F401
import urllib3  # noqa: F401
