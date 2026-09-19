from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from django.test import SimpleTestCase

from profiles import runner


@contextmanager
def _noop_atomic(using: str | None = None):
    yield


class _CursorRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple | list | None]] = []

    def execute(self, sql: str, params=None) -> None:
        self.calls.append((sql, params))


class _ConnectionRecorder:
    def __init__(self, cursor: _CursorRecorder) -> None:
        self._cursor = cursor

    @contextmanager
    def cursor(self):
        yield self._cursor


class CleanupTablesTests(SimpleTestCase):
    def test_no_cleanup_when_under_limit(self) -> None:
        authors_cursor = _CursorRecorder()
        default_cursor = _CursorRecorder()
        fake_connections = {
            "authors": _ConnectionRecorder(authors_cursor),
            "default": _ConnectionRecorder(default_cursor),
        }

        with patch.object(runner, "connections", fake_connections), patch.object(
            runner.transaction, "atomic", _noop_atomic
        ), patch.object(runner.AuthorSummary.objects, "using") as using_mock:
            using_mock.return_value.count.return_value = 9
            runner._cleanup_tables_if_needed(job_id=42)

        self.assertEqual(authors_cursor.calls, [])
        self.assertEqual(default_cursor.calls, [])

    def test_cleanup_when_limit_reached(self) -> None:
        authors_cursor = _CursorRecorder()
        default_cursor = _CursorRecorder()
        fake_connections = {
            "authors": _ConnectionRecorder(authors_cursor),
            "default": _ConnectionRecorder(default_cursor),
        }

        with patch.object(runner, "connections", fake_connections), patch.object(
            runner.transaction, "atomic", _noop_atomic
        ), patch.object(runner.AuthorSummary.objects, "using") as using_mock:
            using_mock.return_value.count.return_value = 10
            with patch.dict("os.environ", {"AUTHOR_SUMMARY_MAX_ROWS": "10"}):
                runner._cleanup_tables_if_needed(job_id=7)

        expected_author_tables = [
            "events_clean",
            "events",
            "works",
            "zenodo_github",
            "author_summary",
        ]
        expected_author_calls = [(f"DELETE FROM {table}", None) for table in expected_author_tables]
        self.assertEqual(authors_cursor.calls, expected_author_calls)
        self.assertEqual(default_cursor.calls, [("DELETE FROM job WHERE id <> %s", [7])])
