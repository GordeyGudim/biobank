"""Общие фикстуры тестов.

pytest автоматически находит conftest.py и делает его фикстуры доступными
во всех тестах этой папки — импортировать их не нужно.
"""

import pytest

from biobank.db import PROJECT_ROOT, connect
from biobank.importer import import_workbook

SOURCE = PROJECT_ROOT / "data" / "source.xlsx"


def count(db, sql, *params):
    """Первое значение первой строки запроса — для SELECT count(*) …"""
    return db.execute(sql, params).fetchone()[0]


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    """Импорт настоящей книги — один раз на весь прогон тестов (scope="session")."""
    if not SOURCE.exists():
        pytest.skip("нет data/source.xlsx")
    path = tmp_path_factory.mktemp("import") / "biobank.db"
    import_workbook(SOURCE, path)
    return path


@pytest.fixture
def db(db_path):
    conn = connect(db_path)
    yield conn
    conn.close()
