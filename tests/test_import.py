"""Приёмочные тесты импорта (этап 3): справочники, места, выезды, траления, вода.

Импорт настоящей книги занимает пару секунд, поэтому делаем его один раз
на весь файл тестов: фикстура со scope="module" создаётся один раз и
переиспользуется всеми тестами ниже.
"""

import sqlite3

import pytest

from biobank import importer
from biobank.db import PROJECT_ROOT, connect
from biobank.excel_reader import ExcelReadError
from biobank.importer import import_workbook

SOURCE = PROJECT_ROOT / "data" / "source.xlsx"

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("import") / "biobank.db"
    import_workbook(SOURCE, path)
    return path


@pytest.fixture
def db(db_path):
    conn = connect(db_path)
    yield conn
    conn.close()


def count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


def test_counts(db):
    assert count(db, "SELECT count(*) FROM region") == 3
    assert count(db, "SELECT count(*) FROM expedition") == 3
    assert count(db, "SELECT count(*) FROM sampling_event") == 24
    assert count(db, "SELECT count(*) FROM trawling") == 94
    assert count(db, "SELECT count(*) FROM gear") == 2
    assert count(db, "SELECT count(*) FROM capture_method") == 4
    assert count(db, "SELECT count(*) FROM species") == 8


def test_water_measurements(db):
    assert count(db, "SELECT count(*) FROM water_measurement WHERE trawling_id IS NOT NULL") == 188
    fisher = db.execute(
        "SELECT w.point_type, e.source_sheet FROM water_measurement w "
        "JOIN sampling_event e USING (event_id) WHERE w.trawling_id IS NULL"
    ).fetchall()
    assert [tuple(row) for row in fisher] == [("точка", "Точка 6.")]


def test_every_trawling_has_start_and_finish(db):
    assert (
        count(
            db,
            "SELECT count(*) FROM trawling t WHERE "
            "(SELECT count(*) FROM water_measurement w WHERE w.trawling_id = t.trawling_id) != 2",
        )
        == 0
    )


def test_integrity(db):
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_expedition_dates(db):
    rows = db.execute("SELECT name, date_start, date_end FROM expedition ORDER BY date_start")
    assert [tuple(r) for r in rows] == [
        ("Донг-хап 2025", "2025-09-25", "2025-10-12"),
        ("Виньлонг 2025", "2025-10-22", "2025-11-01"),
        ("Кантхо 2026", "2026-08-11", "2026-08-22"),
    ]


def test_market_dates_from_previous_event(db):
    rows = db.execute(
        "SELECT source_sheet, event_date FROM sampling_event "
        "WHERE source_sheet LIKE 'Рынок%' ORDER BY event_date"
    )
    assert [tuple(r) for r in rows] == [
        ("Рынок.Бенче", "2025-10-22"),
        ("Рынок. Чавинь", "2025-10-26"),
    ]
    assert count(db, "SELECT count(*) FROM data_issue WHERE category = 'дата'") == 2


def test_fisher_site_deduplicated(db):
    """Точка 9.5187059 / 106.2082146 (20, 21, 22.08.2026) — одно место, к нему 2 выезда."""
    sites = db.execute(
        "SELECT site_id FROM sampling_site WHERE abs(lat - 9.5187059) < 0.001 "
        "AND abs(lon - 106.2082146) < 0.001"
    ).fetchall()
    assert len(sites) == 1
    events = db.execute(
        "SELECT source_sheet FROM sampling_event WHERE site_id = ? ORDER BY event_date",
        (sites[0][0],),
    ).fetchall()
    assert [r[0] for r in events] == [
        "Рыбак (самостоятельно 21.08)",
        "Рыбак (самостоятельно 22.08)",
    ]


def test_site_names(db):
    names = [
        r[0] for r in db.execute("SELECT name FROM sampling_site WHERE site_type = 'участок реки'")
    ]
    assert "Участок у Хонг-нгу" in names
    assert all(n.startswith("Участок") for n in names)
    assert not any("Меконг" in n for n in names)


def test_city_spellings_merged(db):
    # «Хонг-нга» (Точка 1) и «Хонг-на» (Аквахозяйство) — один город
    assert count(db, "SELECT count(*) FROM city WHERE name LIKE 'Хонг%'") == 1


def test_gear_only_on_trawl_events(db):
    assert (
        count(
            db,
            "SELECT count(*) FROM sampling_event e JOIN capture_method m USING (capture_method_id) "
            "WHERE (m.name = 'траление') != (e.gear_id IS NOT NULL)",
        )
        == 0
    )


def test_outlier_point_reported(db):
    row = db.execute(
        "SELECT object_label FROM data_issue "
        "WHERE category = 'координаты' AND sheet_name = 'Точка 2'"
    ).fetchone()
    assert row[0] == "траление 1 старт"


def test_reimport_is_identical(db_path, tmp_path):
    """Идемпотентность: повторный импорт даёт ту же базу до последней строки."""
    again = tmp_path / "again.db"
    import_workbook(SOURCE, again)
    first, second = connect(db_path), connect(again)
    try:
        assert list(first.iterdump()) == list(second.iterdump())
    finally:
        first.close()
        second.close()


def test_failed_import_keeps_old_db(tmp_path):
    """Если книга не читается, старая база остаётся на месте."""
    path = tmp_path / "keep.db"
    path.write_bytes(b"old")
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not an excel file")
    with pytest.raises(ExcelReadError):
        import_workbook(bad, path)
    assert path.read_bytes() == b"old"


def test_error_inside_transaction_keeps_old_db(tmp_path, monkeypatch):
    """Ошибка посреди записи в базу: временный файл удалён, старая база цела.

    monkeypatch — фикстура pytest, которая временно подменяет функцию
    (здесь запись журнала проблем) на время одного теста.
    """
    path = tmp_path / "keep.db"
    path.write_bytes(b"old")

    def broken(ctx):
        raise sqlite3.IntegrityError("искусственная ошибка")

    monkeypatch.setattr(importer, "_write_issues", broken)
    with pytest.raises(sqlite3.IntegrityError):
        import_workbook(SOURCE, path)
    assert path.read_bytes() == b"old"
    assert not (tmp_path / "keep.db.tmp").exists()


def test_same_city_river_section_is_one_site(db):
    """«Участок у Кантхо» (Точка 5. в 2025 и Точка 2 2026) — одно место, два выезда."""
    sites = db.execute(
        "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'"
    ).fetchall()
    assert len(sites) == 1
    sheets = db.execute(
        "SELECT source_sheet FROM sampling_event WHERE site_id = ? ORDER BY event_date",
        (sites[0][0],),
    ).fetchall()
    assert [r[0] for r in sheets] == ["Точка 5.", "Точка 2 2026"]


def test_river_site_coordinates_from_first_trawl(db):
    """Координаты участка = старт первого траления первого выезда на него (реальный замер)."""
    site = db.execute(
        "SELECT lat, lon, description FROM sampling_site WHERE name = 'Участок у Кантхо'"
    ).fetchone()
    first_start = db.execute(
        "SELECT w.lat, w.lon FROM water_measurement w "
        "JOIN trawling t USING (trawling_id) JOIN sampling_event e ON e.event_id = t.event_id "
        "WHERE e.source_sheet = 'Точка 5.' AND t.trawl_no = 1 AND w.point_type = 'старт'"
    ).fetchone()
    assert (site["lat"], site["lon"]) == tuple(first_start)
    assert site["description"].count("координаты места — старт траления 1 (лист «Точка 5.»") == 1
    assert "«Точка 2 2026»" in site["description"]


def test_outlier_start_not_used_as_site_coordinates(db):
    """Точка 2 (Донг-хап): старт 1-го траления — выброс в 50 км, берётся финиш того же траления."""
    site = db.execute(
        "SELECT s.lat, s.lon, s.description FROM sampling_site s "
        "JOIN sampling_event e USING (site_id) WHERE e.source_sheet = 'Точка 2'"
    ).fetchone()
    finish = db.execute(
        "SELECT w.lat, w.lon FROM water_measurement w "
        "JOIN trawling t USING (trawling_id) JOIN sampling_event e ON e.event_id = t.event_id "
        "WHERE e.source_sheet = 'Точка 2' AND t.trawl_no = 1 AND w.point_type = 'финиш'"
    ).fetchone()
    assert (site["lat"], site["lon"]) == tuple(finish)
    assert "финиш траления 1" in site["description"]
