"""Тесты этапа 1: база создаётся из схемы, внешние ключи включены, init работает.

tmp_path — встроенная фикстура pytest: временная папка, своя для каждого теста.
Благодаря ей тесты не трогают output/.
"""

import sqlite3

import pytest

from biobank.__main__ import main
from biobank.db import connect, create_database, list_tables

EXPECTED_TABLES = {
    "region",
    "city",
    "sampling_site",
    "capture_method",
    "gear",
    "species",
    "lab",
    "expedition",
    "sampling_event",
    "trawling",
    "water_measurement",
    "specimen",
    "species_identification",
    "sample",
    "sample_shipment",
    "analysis",
    "catch_record",
    "data_issue",
    # новое в схеме v3
    "person",
    "app_user",
    "user_role",
    "expedition_member",
    "change_log",
}


@pytest.fixture
def db(tmp_path):
    """Фикстура: свежая пустая база во временной папке, подключение закрывается после теста."""
    path = create_database(tmp_path / "test.db")
    conn = connect(path)
    yield conn
    conn.close()


def test_schema_has_23_tables(db):
    tables = list_tables(db)
    assert len(tables) == 23
    assert set(tables) == EXPECTED_TABLES


def test_schema_version_is_3(db):
    """PRAGMA user_version — номер версии схемы внутри файла (для будущих миграций)."""
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_all_tables_strict(db):
    """Схема v3: все таблицы STRICT — текст в числовую колонку база не примет."""
    rows = db.execute("SELECT name, strict FROM pragma_table_list WHERE schema = 'main'")
    loose = [r["name"] for r in rows if not r["name"].startswith("sqlite_") and not r["strict"]]
    assert loose == []


def test_foreign_keys_enabled(db):
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_key_violation_rejected(db):
    # Город с несуществующей провинцией должен быть отвергнут
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO city (region_id, name) VALUES (999, 'Нигде')")


def test_integrity_ok(db):
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_create_refuses_to_overwrite(tmp_path):
    path = create_database(tmp_path / "test.db")
    with pytest.raises(FileExistsError):
        create_database(path)


def test_cli_init(tmp_path, capsys):
    path = tmp_path / "cli.db"
    assert main(["--db", str(path), "init"]) == 0
    assert path.exists()
    assert "Таблиц: 23" in capsys.readouterr().out

    # Повторно с --yes — пересоздаёт без вопросов
    assert main(["--db", str(path), "init", "--yes"]) == 0
