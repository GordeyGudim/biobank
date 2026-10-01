"""Тесты проверок (этап 5).

Две части:
1. Модульные — на маленькой искусственной базе: видно, что именно ловит каждая проверка
   и как журнал ведёт себя при повторном запуске.
2. Приёмочные — на импортированной книге: все известные проблемы из CLAUDE.md найдены.
"""

import pytest
from conftest import SOURCE, count

from biobank.checks import (
    DESC_CONDITION,
    DESC_COPIED,
    DESC_GUTTED_GT_MASS,
    DESC_SL_GT_TL,
    DESC_SL_TOO_SMALL,
    find_issues,
    run_checks,
)
from biobank.db import connect, create_database

# ---------------------------------------------------------------------------
# Маленькая база для модульных тестов
# ---------------------------------------------------------------------------


@pytest.fixture
def small_db(tmp_path):
    """Пустая база с одним выездом-тралением; особей добавляет сам тест."""
    conn = connect(create_database(tmp_path / "small.db"))
    conn.executescript(
        """
        INSERT INTO region (region_id, name) VALUES (1, 'Тест');
        INSERT INTO sampling_site (site_id, name, site_type)
            VALUES (1, 'Участок у Теста', 'участок реки');
        INSERT INTO capture_method (capture_method_id, name) VALUES (1, 'траление');
        INSERT INTO expedition (expedition_id, name) VALUES (1, 'Тест 2025');
        INSERT INTO sampling_event
            (event_id, expedition_id, site_id, event_date, capture_method_id, source_sheet)
            VALUES (1, 1, 1, '2025-01-01', 1, 'Лист');
        INSERT INTO trawling (trawling_id, event_id, trawl_no) VALUES (1, 1, 1);
        INSERT INTO species (species_id, scientific_name, code) VALUES (1, 'Plotosus canius', 'pc');
        """
    )  # fmt: skip
    yield conn
    conn.close()


def add_specimen(conn, label, tl, sl, m, mg, series="pc", number=1, species_id=1, trawl=1):
    conn.execute(
        "INSERT INTO specimen (label, series, series_no, event_id, trawling_id, species_id, "
        "tl_cm, sl_cm, mass_g, mass_gutted_g) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (label, series, number, trawl, species_id, tl, sl, m, mg),
    )


def descriptions(conn):
    return [i.description for i in find_issues(conn)]


def test_normal_specimen_has_no_issues(small_db):
    add_specimen(small_db, "1 pc", 30.0, 27.0, 150.0, 140.0)
    assert descriptions(small_db) == []


@pytest.mark.parametrize(
    "tl, sl, m, mg, expected",
    [
        (10.2, 10.3, 11.0, 8.6, DESC_SL_GT_TL),  # 262
        (21.3, 1.8, 46.0, 42.0, DESC_SL_TOO_SMALL),  # 149 pc
        (22.0, 20.4, 28.0, 34.0, DESC_GUTTED_GT_MASS),  # 47 pc
        (43.0, 37.5, 43.0, 37.5, DESC_COPIED),  # 21 pc
        (93.0, 79.0, 141.0, 111.0, DESC_CONDITION),  # особь 1
    ],
)
def test_measurement_checks(small_db, tl, sl, m, mg, expected):
    add_specimen(small_db, "1 pc", tl, sl, m, mg)
    assert expected in descriptions(small_db)


def test_missing_measurements_are_not_errors(small_db):
    add_specimen(small_db, "1 pc", None, None, None, None)  # целая особь — промеров нет
    assert descriptions(small_db) == []


def test_suffix_mismatch(small_db):
    add_specimen(small_db, "1 ph", 30.0, 27.0, 150.0, 140.0, series="Pangasius")
    assert [i.object_label for i in find_issues(small_db) if i.category == "вид"] == ["1 ph"]


def test_numbering_gaps_and_duplicates(small_db):
    for label, n in (("1", 1), ("1 e", 1), ("4 sp", 4)):
        add_specimen(small_db, label, None, None, None, None, series="Pangasius", number=n,
                     species_id=None)  # fmt: skip
    issues = {i.description: i.raw_value for i in find_issues(small_db) if i.category == "номера"}
    assert issues["Номера повторяются в сквозной серии (разные суффиксы)"] == "1 = 1 e"
    assert issues["Номера пропущены в сквозной серии"] == "2–3"


def test_trawl_link_issue(small_db):
    add_specimen(small_db, "1 pc", None, None, None, None, trawl=None)
    issues = [i for i in find_issues(small_db) if i.category == "связь"]
    assert [i.raw_value for i in issues] == ["особей без траления: 1"]
    small_db.execute("UPDATE specimen SET trawling_id = 1")
    assert [i for i in find_issues(small_db) if i.category == "связь"] == []


# ---------------------------------------------------------------------------
# Повторный запуск: без дубликатов, исправленное закрывается, решённое не трогаем
# ---------------------------------------------------------------------------


def test_rerun_adds_nothing(small_db):
    add_specimen(small_db, "1 pc", 10.2, 10.3, 11.0, 8.6)
    first = run_checks(small_db)
    second = run_checks(small_db)
    assert (first.added, second.added, second.known) == (1, 0, 1)
    assert count(small_db, "SELECT count(*) FROM data_issue") == 1


def test_fixed_problem_is_closed(small_db):
    add_specimen(small_db, "1 pc", 10.2, 10.3, 11.0, 8.6)
    run_checks(small_db)
    small_db.execute("UPDATE specimen SET sl_cm = 9.3")  # исправили опечатку
    result = run_checks(small_db, today="2026-10-01")
    assert result.closed == 1
    row = small_db.execute("SELECT resolved, resolution FROM data_issue").fetchone()
    assert row["resolved"] == 1
    assert "2026-10-01" in row["resolution"]


def test_resolved_issue_stays_resolved(small_db):
    add_specimen(small_db, "1 pc", 10.2, 10.3, 11.0, 8.6)
    run_checks(small_db)
    small_db.execute("UPDATE data_issue SET resolved = 1, resolution = 'так и есть, сверено'")
    result = run_checks(small_db)
    assert (result.added, result.known) == (0, 1)
    assert count(small_db, "SELECT resolution FROM data_issue") == "так и есть, сверено"


def test_import_issues_untouched_by_check(small_db):
    small_db.execute(
        "INSERT INTO data_issue (category, description) VALUES ('дата', 'Дата на листе не указана')"
    )
    run_checks(small_db)
    assert count(small_db, "SELECT resolved FROM data_issue WHERE category = 'дата'") == 0


# ---------------------------------------------------------------------------
# Приёмка: известные проблемы книги (CLAUDE.md) есть в журнале после импорта
# ---------------------------------------------------------------------------

needs_source = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


def issue_exists(db, description_start, object_part=None, raw_part=None):
    sql = "SELECT count(*) FROM data_issue WHERE description LIKE ? || '%'"
    params = [description_start]
    if object_part:
        sql += " AND object_label LIKE '%' || ? || '%'"
        params.append(object_part)
    if raw_part:
        sql += " AND raw_value LIKE '%' || ? || '%'"
        params.append(raw_part)
    return count(db, sql, *params) > 0


@needs_source
@pytest.mark.parametrize(
    "description, obj, raw",
    [
        ("Число превратилось в дату", "155 pc", None),
        (DESC_SL_TOO_SMALL, "149 pc", None),
        (DESC_SL_GT_TL, "160", None),
        (DESC_SL_GT_TL, "262", None),
        (DESC_COPIED, "21 pc", None),
        (DESC_GUTTED_GT_MASS, "47 pc", None),
        (DESC_CONDITION, "1", "TL 93"),
        ("Номера повторяются", "Pangasius", "1 = 1 e"),
        ("Номера пропущены", "серия pc", "81–85"),
        ("Для этих номеров нет строк особей", None, "52 - 65"),
        ("Суффикс в номере", "85 ph", None),
        ("Суффикс в номере", "5 e", None),
        ("В метке гистологии стоит номер другой пробы", "27 ph", None),
        ("В метке гистологии стоит номер другой пробы", "3 pc", None),
        ("Точка в 50 км", "траление 1 старт", None),
        ("Чисел, записанных текстом", None, None),
        ("Особи не привязаны к тралениям", None, None),
        ("Дата на листе не указана", None, None),
        ("Время записано числом", None, "10.48"),
        ("В колонке «мазок крови» записан орган", "1 pc", None),
    ],
)
def test_known_problems_found(db, description, obj, raw):
    assert issue_exists(db, description, obj, raw)


@needs_source
def test_text_numbers_one_issue_per_sheet(db):
    assert (
        count(
            db,
            "SELECT max(n) FROM (SELECT count(*) AS n FROM data_issue "
            "WHERE description LIKE 'Чисел, записанных текстом%' GROUP BY sheet_name)",
        )
        == 1
    )


@needs_source
def test_check_after_import_changes_nothing(db_path, tmp_path):
    """check сразу после import: всё уже известно, ничего не добавлено и не закрыто."""
    import shutil

    copy = tmp_path / "copy.db"
    shutil.copy(db_path, copy)
    conn = connect(copy)
    try:
        with conn:
            result = run_checks(conn)
    finally:
        conn.close()
    assert (result.added, result.closed) == (0, 0)
