from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from typing import Any

from core.config import DB_PATH


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))


class HybridRow:
    """Row compatible with both sqlite Row access styles used by the app."""

    def __init__(self, columns: list[str], values: tuple[Any, ...]):
        self._columns = columns
        self._values = values
        self._mapping = dict(zip(columns, values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._mapping[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self):
        return self._columns


def _hybrid_row_factory(cursor):
    if cursor.description is None:
        return lambda values: values
    columns = [column.name for column in cursor.description]
    return lambda values: HybridRow(columns, values)


class PostgresConnection:
    def __init__(self):
        import psycopg

        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        self._conn = psycopg.connect(url, row_factory=_hybrid_row_factory)

    @staticmethod
    def _sql(query: str) -> str:
        # psycopg uses percent-style binding; escape literal SQL percentages first.
        return query.replace("%", "%%").replace("?", "%s")

    @property
    def row_factory(self):
        return _hybrid_row_factory

    @row_factory.setter
    def row_factory(self, _value):
        pass

    def execute(self, query: str, params=()):
        return self._conn.execute(self._sql(query), tuple(params))

    def executemany(self, query: str, params):
        cursor = self._conn.cursor()
        cursor.executemany(self._sql(query), params)
        return cursor

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


def connect_db():
    if IS_POSTGRES:
        return PostgresConnection()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


try:
    import psycopg
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
except ImportError:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
