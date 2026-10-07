"""Приёмка этапа 1 ROADMAP.md («Безопасность перед вебом») — по требованиям, а не по коду.

Независимая проверка тестировщиком. Всё делается на КОПИЯХ импортированной базы
во временной папке (tmp_path — pytest создаёт новую пустую папку для каждого теста).

Новые понятия:
- monkeypatch — фикстура pytest, которая временно подменяет функцию или атрибут
  (например, input(), чтобы «ответить» на вопрос программы) и возвращает всё
  на место после теста;
- capsys — фикстура, перехватывающая то, что программа напечатала (stdout/stderr);
- threading.Timer — запустить функцию в другом потоке через N секунд: так
  имитируется «второй человек», который правит базу одновременно с нами.
"""

import hashlib
import shutil
import sqlite3
import threading

import pytest
from conftest import SOURCE, count

import biobank.edit
from biobank.__main__ import main
from biobank.checks import DESC_NO_TRAWL, DESC_SL_GT_TL, run_checks
from biobank.db import backup_database, connect, connect_read_only
from biobank.edit import (
    EditError,
    NotFoundError,
    apply_edit,
    find_specimen,
    identify,
    link_trawl,
    resolve_issue,
)

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


@pytest.fixture
def copy_path(db_path, tmp_path):
    """Копия импортированной базы в отдельной папке (рядом появятся backups/)."""
    path = tmp_path / "work" / "biobank.db"
    path.parent.mkdir()
    shutil.copy(db_path, path)
    return path


def sha(path):
    """Отпечаток файла: если поменялся хоть один байт — отпечаток другой."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path):
    """Содержимое базы как текст SQL. Резервная копия через backup() совпадает
    с исходником по содержимому, но не обязательно байт в байт (служебный заголовок)."""
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def files_in(folder):
    return sorted(p.name for p in folder.iterdir())


def execute(path, *statements):
    """Записать в копию базы напрямую (как будто данные ввели через веб)."""
    conn = connect(path)
    with conn:
        for sql in statements:
            conn.execute(sql)
    conn.close()


def first_open_issue(path):
    conn = connect(path)
    issue_id = count(conn, "SELECT min(issue_id) FROM data_issue WHERE resolved = 0")
    conn.close()
    return issue_id


def no_questions(monkeypatch):
    """Если программа что-то спросит через input() — тест упадёт."""

    def fail(prompt=""):
        raise AssertionError(f"не должно быть вопроса: {prompt}")

    monkeypatch.setattr("builtins.input", fail)


# ---------------------------------------------------------------------------
# 1. Подключение только на чтение и команда sql
# ---------------------------------------------------------------------------

# {db} — путь к самой базе, {out} — путь к файлу, которого быть не должно
FORBIDDEN_SQL = {
    "insert": "INSERT INTO region (name) VALUES ('Анзянг')",
    "update": "UPDATE specimen SET tl_cm = 1",
    "delete": "DELETE FROM data_issue",
    "drop": "DROP TABLE analysis",
    "create_table": "CREATE TABLE z (x)",
    "alter": "ALTER TABLE specimen ADD COLUMN z TEXT",
    "replace": "REPLACE INTO region (region_id, name) VALUES (1, 'x')",
    "insert_or_ignore": "INSERT OR IGNORE INTO region (name) VALUES ('x')",
    "cte_delete": "WITH x AS (SELECT 1) DELETE FROM specimen",
    "cte_update": "WITH x AS (SELECT 1) UPDATE specimen SET tl_cm = 1",
    "attach_same_file_rw": "ATTACH 'file:{db}?mode=rw' AS w",
    "attach_new_file": "ATTACH '{out}' AS n",
    "attach_memory": "ATTACH ':memory:' AS m",
    "vacuum": "VACUUM",
    "vacuum_into": "VACUUM INTO '{out}'",
    "pragma_fk_off": "PRAGMA foreign_keys = OFF",
    "pragma_fk_off_call": "PRAGMA foreign_keys(0)",
    "pragma_fk_off_schema": "PRAGMA main.foreign_keys = OFF",
    "pragma_user_version": "PRAGMA user_version = 7",
    "pragma_journal_mode": "PRAGMA journal_mode = WAL",
    "pragma_writable_schema": "PRAGMA writable_schema = 1",
    "pragma_query_only_off": "PRAGMA query_only = 0",
    "pragma_ignore_check": "PRAGMA ignore_check_constraints = 1",
    "two_statements": "SELECT 1; DELETE FROM specimen",
    "two_statements_no_space": "SELECT 1;DROP TABLE analysis",
    "temp_table": "CREATE TEMP TABLE t (x)",
    "temp_view": "CREATE TEMP VIEW v AS SELECT 1",
    "temp_trigger": "CREATE TEMP TRIGGER tr AFTER INSERT ON specimen BEGIN SELECT 1; END",
    "virtual_table": "CREATE VIRTUAL TABLE temp.v USING json_each('[1]')",
    "begin": "BEGIN IMMEDIATE",
    "savepoint": "SAVEPOINT a",
    "analyze": "ANALYZE",
    "reindex": "REINDEX",
    "load_extension": "SELECT load_extension('/tmp/evil')",
}


@pytest.mark.parametrize("sql", FORBIDDEN_SQL.values(), ids=FORBIDDEN_SQL.keys())
def test_read_only_connection_refuses_writes(copy_path, tmp_path, sql):
    """Ни один из запросов не проходит; файл базы, папка и foreign_keys не меняются.

    parametrize: один и тот же тест запускается для каждой строки словаря.
    """
    out = tmp_path / "stolen.db"
    before = sha(copy_path)
    conn = connect_read_only(copy_path)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute(sql.format(db=copy_path, out=out)).fetchall()
        assert count(conn, "PRAGMA foreign_keys") == 1
        assert count(conn, "SELECT count(*) FROM specimen") == 462
    finally:
        conn.close()
    assert sha(copy_path) == before
    assert not out.exists()
    assert files_in(copy_path.parent) == ["biobank.db"]  # ни журнала, ни WAL


ALLOWED_SQL = {
    "select": ("SELECT count(*) FROM specimen", 462),
    "join": (
        "SELECT count(*) FROM specimen s JOIN sampling_event e USING (event_id) "
        "JOIN expedition x USING (expedition_id)",
        462,
    ),
    "left_join_group": (
        "SELECT count(*) FROM (SELECT e.event_id FROM sampling_event e "
        "LEFT JOIN trawling t USING (event_id) GROUP BY e.event_id)",
        24,
    ),
    "aggregate": ("SELECT count(DISTINCT expedition_id) FROM sampling_event", 3),
    "with_recursive": (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) "
        "SELECT sum(x) FROM n",
        15,
    ),
    "with_cte": ("WITH t AS (SELECT * FROM trawling) SELECT count(*) FROM t", 94),
    "integrity_check": ("PRAGMA integrity_check", "ok"),
    "quick_check": ("PRAGMA quick_check", "ok"),
    "foreign_keys_read": ("PRAGMA foreign_keys", 1),
    "pragma_function": ("SELECT count(*) FROM pragma_table_info('specimen')", 19),
    "trailing_semicolon": ("SELECT count(*) FROM gear;", 2),
}


@pytest.mark.parametrize(("sql", "expected"), ALLOWED_SQL.values(), ids=ALLOWED_SQL.keys())
def test_read_only_connection_allows_reading(db_path, sql, expected):
    conn = connect_read_only(db_path)
    try:
        assert conn.execute(sql).fetchone()[0] == expected
    finally:
        conn.close()


def test_read_only_foreign_key_check_is_empty(db_path):
    conn = connect_read_only(db_path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_read_only_table_info(db_path):
    conn = connect_read_only(db_path)
    try:
        names = [r["name"] for r in conn.execute("PRAGMA table_info(specimen)")]
    finally:
        conn.close()
    assert names[:2] == ["specimen_id", "label"]
    assert "tl_cm" in names


def test_read_only_connect_to_missing_file_creates_nothing(tmp_path):
    missing = tmp_path / "нет.db"
    with pytest.raises(sqlite3.OperationalError):
        connect_read_only(missing)
    assert files_in(tmp_path) == []


@pytest.mark.parametrize(
    "key",
    ["update", "attach_same_file_rw", "vacuum_into", "pragma_fk_off", "two_statements",
     "cte_delete", "temp_table"],
)  # fmt: skip
def test_cli_sql_refuses_writes(copy_path, tmp_path, capsys, key):
    """Команда sql: код 1, сообщение без traceback, база и папка не меняются."""
    out = tmp_path / "stolen.db"
    before = sha(copy_path)
    query = FORBIDDEN_SQL[key].format(db=copy_path, out=out)
    assert main(["--db", str(copy_path), "sql", query]) == 1
    err = capsys.readouterr().err
    assert "Ошибка SQL" in err
    assert "Traceback" not in err
    assert sha(copy_path) == before
    assert not out.exists()
    assert files_in(copy_path.parent) == ["biobank.db"]


def test_cli_sql_explains_forbidden_query(copy_path, capsys):
    assert main(["--db", str(copy_path), "sql", "DELETE FROM specimen"]) == 1
    assert "только читает данные" in capsys.readouterr().err


def test_cli_sql_select_works(db_path, capsys):
    query = "SELECT count(*) AS n FROM specimen WHERE label = '155 pc'"
    assert main(["--db", str(db_path), "sql", query]) == 0
    assert "строк: 1" in capsys.readouterr().out


def test_cli_sql_missing_db_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "нет.db"
    assert main(["--db", str(missing), "sql", "SELECT 1"]) == 1
    assert files_in(tmp_path) == []


# ---------------------------------------------------------------------------
# 2. import поверх существующей базы
# ---------------------------------------------------------------------------

# Данные, которых нет в Excel: повторный импорт их сотрёт.
# Первая группа перечислена в ROADMAP явно, вторая — «и т.п.»: то, что исследователи
# будут вводить на этапе 4 (траления, замеры воды, прилов, справочники, свои заметки в журнале).
SHEET_EVENT = "(SELECT event_id FROM sampling_event WHERE source_sheet = 'Точка 1')"
MANUAL_DATA = {
    "new_event": [
        "INSERT INTO sampling_event (expedition_id, site_id, event_date, capture_method_id) "
        "SELECT 1, site_id, '2027-01-10', capture_method_id FROM sampling_event "
        "WHERE source_sheet = 'Точка 1'"
    ],
    "new_specimen": [
        f"INSERT INTO specimen (label, event_id, tl_cm) VALUES ('300 pc', {SHEET_EVENT}, 20.5)"
    ],
    "new_sample": [
        "INSERT INTO sample (specimen_id, sample_type, organ) "
        "SELECT specimen_id, 'гистология', 'жабры' FROM specimen WHERE label = '155 pc'"
    ],
    "analysis": [
        "INSERT INTO analysis (sample_id, analysis_type, result) "
        "SELECT min(sample_id), 'секвенирование COI', 'Plotosus canius' FROM sample"
    ],
    "shipment_with_date": [
        "UPDATE sample_shipment SET sent_on = '2026-09-01' "
        "WHERE rowid = (SELECT min(rowid) FROM sample_shipment)"
    ],
    "trawl_link": [
        "UPDATE specimen SET trawling_id = (SELECT trawling_id FROM trawling "
        "WHERE event_id = specimen.event_id AND trawl_no = 1) WHERE label = '5 pc'"
    ],
    "extra_identification": [
        "INSERT INTO species_identification (specimen_id, species_id, method, confidence) "
        "SELECT specimen_id, species_id, 'ДНК', 'точно' FROM specimen WHERE label = '155 pc'"
    ],
    "resolved_issue": [
        "UPDATE data_issue SET resolved = 1, resolution = 'сверили с журналом' "
        "WHERE issue_id = (SELECT min(issue_id) FROM data_issue)"
    ],
    "new_species": ["INSERT INTO species (scientific_name) VALUES ('Arius maculatus')"],
    "new_lab": ["INSERT INTO lab (name) VALUES ('Ханой')"],
    "new_expedition": ["INSERT INTO expedition (name) VALUES ('Анзянг 2027')"],
    "new_site": [
        "INSERT INTO sampling_site (name, site_type, lat, lon) "
        "VALUES ('Рынок Лонгсюен', 'рынок', 10.38, 105.43)"
    ],
    # --- «и т.п.»
    "new_trawling": [f"INSERT INTO trawling (event_id, trawl_no) VALUES ({SHEET_EVENT}, 99)"],
    "new_water_measurement": [
        "INSERT INTO water_measurement (event_id, point_type, lat, lon, ph) "
        f"VALUES ({SHEET_EVENT}, 'точка', 10.8, 105.2, 7.1)"
    ],
    "new_catch_record": [
        "INSERT INTO catch_record (event_id, raw_text, count_n) "
        f"VALUES ({SHEET_EVENT}, '3 краба', 3)"
    ],
    "shipment_without_date": [
        "INSERT INTO sample_shipment (sample_id, lab_id) "
        "SELECT (SELECT min(sample_id) FROM sample WHERE sample_id NOT IN "
        "(SELECT sample_id FROM sample_shipment)), (SELECT min(lab_id) FROM lab)"
    ],
    "new_city": [
        "INSERT INTO city (region_id, name) SELECT region_id, 'Лонгсюен' FROM region LIMIT 1"
    ],
    "new_region": ["INSERT INTO region (name) VALUES ('Анзянг')"],
    "new_gear": ["INSERT INTO gear (gear_type, mesh_cm, size_m) VALUES ('трал', '2x2', '5x8')"],
    "manual_issue": [
        "INSERT INTO data_issue (category, table_name, record_id, description) "
        "VALUES ('промеры', 'specimen', 1, 'Сверить TL с полевым журналом Иванова')"
    ],
}


def test_manual_data_statements_are_valid(copy_path):
    """Самопроверка: каждая «ручная правка» выше действительно что-то меняет в базе."""
    for name, statements in MANUAL_DATA.items():
        conn = connect(copy_path)
        changes = 0
        for sql in statements:
            changes += conn.execute(sql).rowcount
        conn.rollback()
        conn.close()
        assert changes >= 1, name


@pytest.mark.parametrize("case", MANUAL_DATA)
def test_import_refuses_db_with_manual_data(copy_path, monkeypatch, capsys, case):
    """Без --force: отказ с кодом 1, без вопросов, база не меняется ни на байт."""
    execute(copy_path, *MANUAL_DATA[case])
    before = sha(copy_path)
    no_questions(monkeypatch)
    assert main(["--db", str(copy_path), "import", str(SOURCE)]) == 1
    assert "--force" in capsys.readouterr().err
    assert sha(copy_path) == before


def test_import_force_answer_no_keeps_db(copy_path, monkeypatch, capsys):
    execute(copy_path, *MANUAL_DATA["trawl_link"])
    before = sha(copy_path)
    questions = []
    monkeypatch.setattr("builtins.input", lambda prompt="": questions.append(prompt) or "n")
    assert main(["--db", str(copy_path), "import", str(SOURCE), "--force"]) == 1
    assert len(questions) == 1  # спросили ровно один раз
    assert sha(copy_path) == before


def test_import_force_answer_yes_recreates_and_backs_up(copy_path, monkeypatch, capsys):
    execute(copy_path, *MANUAL_DATA["trawl_link"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["--db", str(copy_path), "import", str(SOURCE), "--force"]) == 0
    conn = connect(copy_path)
    assert count(conn, "SELECT count(*) FROM specimen WHERE trawling_id IS NOT NULL") == 0
    conn.close()
    [backup] = (copy_path.parent / "backups").iterdir()
    conn = connect(backup)
    assert count(conn, "SELECT count(*) FROM specimen WHERE trawling_id IS NOT NULL") == 1
    conn.close()


def test_import_force_yes_flag_recreates_without_questions(copy_path, monkeypatch, capsys):
    execute(copy_path, *MANUAL_DATA["new_specimen"])
    no_questions(monkeypatch)
    assert main(["--db", str(copy_path), "import", str(SOURCE), "--force", "-y"]) == 0
    conn = connect(copy_path)
    assert count(conn, "SELECT count(*) FROM specimen") == 462
    conn.close()
    [backup] = (copy_path.parent / "backups").iterdir()
    conn = connect(backup)
    assert count(conn, "SELECT count(*) FROM specimen WHERE label = '300 pc'") == 1
    conn.close()


def test_import_into_missing_file_works_without_questions(tmp_path, monkeypatch, capsys):
    path = tmp_path / "new" / "biobank.db"
    path.parent.mkdir()
    no_questions(monkeypatch)
    assert main(["--db", str(path), "import", str(SOURCE)]) == 0
    conn = connect(path)
    assert count(conn, "SELECT count(*) FROM specimen") == 462
    conn.close()


def test_import_over_clean_db_backs_up_without_questions(copy_path, monkeypatch, capsys):
    """Чистая база из Excel: импорт без вопросов, но резервная копия — всегда."""
    original = dump(copy_path)
    no_questions(monkeypatch)
    assert main(["--db", str(copy_path), "import", str(SOURCE)]) == 0
    assert "Резервная копия" in capsys.readouterr().out
    [backup] = (copy_path.parent / "backups").iterdir()
    assert dump(backup) == original  # в копии — база до импорта


def test_import_refuses_file_that_is_not_a_database(tmp_path, monkeypatch, capsys):
    path = tmp_path / "biobank.db"
    path.write_bytes("это не база SQLite, а чьи-то важные данные".encode() * 100)
    before = sha(path)
    no_questions(monkeypatch)
    assert main(["--db", str(path), "import", str(SOURCE)]) == 1
    assert sha(path) == before


# ---------------------------------------------------------------------------
# 3. identify: вид + «не определён» — ошибка
# ---------------------------------------------------------------------------


def test_identify_species_with_not_determined_is_rejected(copy_path):
    conn = connect(copy_path)
    before_ids = count(conn, "SELECT count(*) FROM species_identification")
    before_species = count(conn, "SELECT species_id FROM specimen WHERE label = '155 pc'")
    with pytest.raises(EditError, match="не определён"):
        identify(conn, "155 pc", "Plotosus canius", "ДНК", "не определён")
    assert count(conn, "SELECT count(*) FROM species_identification") == before_ids
    assert count(conn, "SELECT species_id FROM specimen WHERE label = '155 pc'") == before_species
    conn.close()


def test_cli_identify_species_with_not_determined(copy_path, capsys):
    before = sha(copy_path)
    argv = ["--db", str(copy_path), "identify", "155 pc", "Plotosus canius",
            "--method", "ДНК", "--confidence", "не определён"]  # fmt: skip
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert "не определён" in err
    assert "Traceback" not in err
    assert sha(copy_path) == before


def test_identify_question_mark_with_not_determined_is_allowed(copy_path):
    """Контрольный случай: «?» + «не определён» — допустимо, вид становится NULL."""
    conn = connect(copy_path)
    identify(conn, "155 pc", None, "морфология", "не определён")
    assert count(conn, "SELECT species_id FROM specimen WHERE label = '155 pc'") is None
    last = conn.execute(
        "SELECT species_id, confidence FROM species_identification "
        "ORDER BY identification_id DESC LIMIT 1"
    ).fetchone()
    assert tuple(last) == (None, "не определён")
    conn.close()


# ---------------------------------------------------------------------------
# 4. Правки: двойное закрытие, одновременная работа
# ---------------------------------------------------------------------------


def test_resolve_issue_twice_keeps_first_resolution(copy_path):
    issue_id = first_open_issue(copy_path)
    conn = connect(copy_path)
    resolve_issue(conn, issue_id, "первое решение")
    with pytest.raises(EditError, match="уже решена"):
        resolve_issue(conn, issue_id, "второе решение")
    row = conn.execute(
        "SELECT resolved, resolution FROM data_issue WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    assert tuple(row) == (1, "первое решение")
    conn.close()


def other_writer(path):
    """Второе подключение («другой человек»); можно закрыть из другого потока."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_resolve_issue_waits_for_other_writer_and_sees_its_result(copy_path):
    """Гонка: пока мы ждём, другой человек закрыл ту же проблему.

    После его COMMIT наша правка должна увидеть закрытие и отказать,
    а не перезаписать его решение своим.
    """
    issue_id = first_open_issue(copy_path)
    other = other_writer(copy_path)
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "UPDATE data_issue SET resolved = 1, resolution = 'решил коллега' WHERE issue_id = ?",
        (issue_id,),
    )
    timer = threading.Timer(0.3, other.commit)
    timer.start()
    conn = connect(copy_path)
    try:
        with pytest.raises(EditError, match="уже решена"):
            resolve_issue(conn, issue_id, "решил я")
        resolution = count(conn, "SELECT resolution FROM data_issue WHERE issue_id = ?", issue_id)
    finally:
        timer.join()
        conn.close()
        other.close()
    assert resolution == "решил коллега"


def test_link_trawl_waits_for_other_writer_and_sees_its_result(copy_path):
    """Пока мы ждём, коллега привязал особь к тралению 1; мы видим это и пишем «1 → 2»."""
    other = other_writer(copy_path)
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "UPDATE specimen SET trawling_id = (SELECT trawling_id FROM trawling "
        "WHERE event_id = specimen.event_id AND trawl_no = 1) WHERE label = '5 pc'"
    )
    timer = threading.Timer(0.3, other.commit)
    timer.start()
    conn = connect(copy_path)
    try:
        result = link_trawl(conn, "5 pc", 2)
    finally:
        timer.join()
        conn.close()
        other.close()
    assert "траление 1 → 2" in result.message


@pytest.fixture
def fast_timeout(monkeypatch):
    """Правки ждут занятую базу 0,2 с вместо 5 с — чтобы тест не тянулся."""

    def quick_connect(path):
        conn = sqlite3.connect(path, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(biobank.edit, "connect", quick_connect)


def test_edit_on_locked_db_fails_cleanly(copy_path, fast_timeout):
    """База занята чужой незавершённой записью: понятная ошибка, данные целы."""
    issue_id = first_open_issue(copy_path)
    other = other_writer(copy_path)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(EditError, match="Ошибка базы данных"):
            apply_edit(copy_path, lambda conn: resolve_issue(conn, issue_id, "x"))
    finally:
        other.rollback()
        other.close()
    conn = connect(copy_path)
    assert count(conn, "SELECT resolved FROM data_issue WHERE issue_id = ?", issue_id) == 0
    assert count(conn, "PRAGMA integrity_check") == "ok"
    conn.close()


def test_cli_edit_on_locked_db(copy_path, fast_timeout, capsys):
    issue_id = first_open_issue(copy_path)
    other = other_writer(copy_path)
    other.execute("BEGIN IMMEDIATE")
    try:
        code = main(["--db", str(copy_path), "resolve-issue", str(issue_id), "x"])
    finally:
        other.rollback()
        other.close()
    assert code == 1
    err = capsys.readouterr().err
    assert "locked" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# 5. Журнал проблем: записи без листа Excel
# ---------------------------------------------------------------------------


def add_manual_event(conn, date, *, with_trawl):
    """Выезд, введённый не из Excel (source_sheet NULL), — как будто через веб."""
    event_id = conn.execute(
        "INSERT INTO sampling_event (expedition_id, site_id, event_date, capture_method_id) "
        "SELECT expedition_id, site_id, ?, capture_method_id FROM sampling_event "
        "WHERE source_sheet = 'Точка 1'",
        (date,),
    ).lastrowid
    if with_trawl:
        conn.execute("INSERT INTO trawling (event_id, trawl_no) VALUES (?, 1)", (event_id,))
    return event_id


def add_manual_specimen(conn, label, event_id, tl, sl):
    """Метка без пробела и без серии — чтобы не сработали проверки суффикса и нумерации."""
    return conn.execute(
        "INSERT INTO specimen (label, event_id, tl_cm, sl_cm, mass_g) VALUES (?, ?, ?, ?, ?)",
        (label, event_id, tl, sl, 300.0),
    ).lastrowid


def open_issues(conn, description):
    return {
        r["record_id"]
        for r in conn.execute(
            "SELECT record_id FROM data_issue WHERE description = ? AND sheet_name IS NULL "
            "AND resolved = 0",
            (description,),
        )
    }


@pytest.fixture
def two_manual_events(copy_path):
    """Два выезда без листа Excel, у каждого траление и непривязанная особь."""
    conn = connect(copy_path)
    with conn:
        ev_a = add_manual_event(conn, "2027-01-10", with_trawl=True)
        ev_b = add_manual_event(conn, "2027-01-11", with_trawl=True)
        add_manual_specimen(conn, "Т-A", ev_a, 30.0, 25.0)
        add_manual_specimen(conn, "Т-B", ev_b, 30.0, 25.0)
    yield conn, ev_a, ev_b
    conn.close()


def test_issues_of_two_sheetless_events_are_separate(two_manual_events):
    conn, ev_a, ev_b = two_manual_events
    with conn:
        result = run_checks(conn)
    assert result.added == 2
    assert open_issues(conn, DESC_NO_TRAWL) == {ev_a, ev_b}


def test_rerun_checks_on_sheetless_events_adds_and_closes_nothing(two_manual_events):
    conn, ev_a, ev_b = two_manual_events
    with conn:
        run_checks(conn)
    total = count(conn, "SELECT count(*) FROM data_issue")
    with conn:
        result = run_checks(conn)
    assert (result.added, result.closed) == (0, 0)
    assert count(conn, "SELECT count(*) FROM data_issue") == total
    assert open_issues(conn, DESC_NO_TRAWL) == {ev_a, ev_b}


def test_fixing_one_event_closes_only_its_issue(two_manual_events):
    conn, ev_a, ev_b = two_manual_events
    with conn:
        run_checks(conn)
        conn.execute(
            "UPDATE specimen SET trawling_id = (SELECT trawling_id FROM trawling "
            "WHERE event_id = ?) WHERE label = 'Т-A'",
            (ev_a,),
        )
        result = run_checks(conn)
    assert (result.added, result.closed) == (0, 1)
    assert open_issues(conn, DESC_NO_TRAWL) == {ev_b}


def test_many_sheetless_measurement_issues_are_not_collapsed(copy_path):
    """4 особи с SL > TL на двух ручных выездах — 4 записи, у каждой своя особь."""
    conn = connect(copy_path)
    with conn:
        ev_a = add_manual_event(conn, "2027-01-10", with_trawl=False)
        ev_b = add_manual_event(conn, "2027-01-11", with_trawl=False)
        ids = {
            add_manual_specimen(conn, f"Т-{i}", ev, 20.0, 25.0)
            for i, ev in enumerate([ev_a, ev_a, ev_b, ev_b])
        }
        result = run_checks(conn)
    assert result.added == 4
    assert open_issues(conn, DESC_SL_GT_TL) == ids
    conn.close()


def test_rerun_checks_on_imported_db_changes_nothing(copy_path):
    conn = connect(copy_path)
    before = [tuple(r) for r in conn.execute("SELECT * FROM data_issue ORDER BY issue_id")]
    with conn:
        first = run_checks(conn, today="2030-01-01")
        second = run_checks(conn, today="2030-01-02")
    after = [tuple(r) for r in conn.execute("SELECT * FROM data_issue ORDER BY issue_id")]
    conn.close()
    assert (first.added, first.closed) == (0, 0)
    assert (second.added, second.closed) == (0, 0)
    assert after == before


# ---------------------------------------------------------------------------
# 6. Ошибка SQLite посреди правки; резервная копия только читает исходник
# ---------------------------------------------------------------------------


def test_cli_sqlite_error_in_middle_of_edit(copy_path, capsys):
    """Триггер обрывает UPDATE ошибкой SQLite — CLI сообщает о ней без traceback."""
    execute(
        copy_path,
        "CREATE TRIGGER fail BEFORE UPDATE ON data_issue "
        "BEGIN SELECT RAISE(ABORT, 'сбой записи'); END",
    )
    issue_id = first_open_issue(copy_path)
    before = sha(copy_path)
    code = main(["--db", str(copy_path), "resolve-issue", str(issue_id), "решили"])
    err = capsys.readouterr().err
    assert code == 1
    assert "сбой записи" in err
    assert "Traceback" not in err
    assert sha(copy_path) == before


def test_sqlite_error_in_middle_of_identify_rolls_back(copy_path, capsys):
    """INSERT определения прошёл, UPDATE особи упал — определение тоже откатилось."""
    execute(
        copy_path,
        "CREATE TRIGGER fail BEFORE UPDATE OF species_id ON specimen "
        "BEGIN SELECT RAISE(ABORT, 'сбой записи'); END",
    )
    before = sha(copy_path)
    argv = ["--db", str(copy_path), "identify", "155 pc", "Plotosus canius",
            "--method", "ДНК", "--confidence", "точно"]  # fmt: skip
    assert main(argv) == 1
    assert "сбой записи" in capsys.readouterr().err
    assert sha(copy_path) == before


def test_backup_opens_source_read_only(copy_path, tmp_path, monkeypatch):
    """Каждое подключение к исходному файлу — с mode=ro; исходник не меняется."""
    calls = []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        calls.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    before = sha(copy_path)
    backup = backup_database(copy_path, tmp_path / "backups")
    monkeypatch.undo()
    to_source = [c for c in calls if copy_path.name in c and "backups" not in c]
    assert to_source, calls
    assert all("mode=ro" in c for c in to_source), to_source
    assert sha(copy_path) == before
    assert dump(backup) == dump(copy_path)


def test_backup_of_missing_file_creates_nothing(tmp_path):
    missing = tmp_path / "нет.db"
    with pytest.raises((sqlite3.Error, FileNotFoundError)):
        backup_database(missing, tmp_path / "backups")
    assert not missing.exists()
    assert list((tmp_path / "backups").glob("*")) == []


# ---------------------------------------------------------------------------
# 7. «Не найдено» — NotFoundError, он же EditError
# ---------------------------------------------------------------------------


def test_not_found_is_edit_error():
    assert issubclass(NotFoundError, EditError)


@pytest.mark.parametrize(
    "action",
    [
        lambda c: find_specimen(c, "9999 pc"),
        lambda c: link_trawl(c, "9999 pc", 1),
        lambda c: identify(c, "9999 pc", "Plotosus canius", "ДНК", "точно"),
        lambda c: resolve_issue(c, 999999, "решили"),
    ],
    ids=["find_specimen", "link_trawl", "identify", "resolve_issue"],
)
def test_missing_object_raises_not_found(copy_path, action):
    before = sha(copy_path)
    conn = connect(copy_path)
    with pytest.raises(NotFoundError):
        action(conn)
    conn.close()
    assert sha(copy_path) == before


def test_not_found_is_caught_as_edit_error(copy_path):
    """Код, который ловит EditError (CLI, веб), ловит и «не найдено»."""
    conn = connect(copy_path)
    with pytest.raises(EditError, match="не найдена"):
        find_specimen(conn, "9999 pc")
    conn.close()


def test_apply_edit_on_missing_db_creates_nothing(tmp_path):
    missing = tmp_path / "нет.db"
    with pytest.raises(NotFoundError, match="Базы нет"):
        apply_edit(missing, lambda conn: resolve_issue(conn, 1, "x"))
    assert files_in(tmp_path) == []


def test_cli_edit_missing_specimen(copy_path, capsys):
    before = sha(copy_path)
    assert main(["--db", str(copy_path), "link-trawl", "9999 pc", "1"]) == 1
    err = capsys.readouterr().err
    assert "не найдена" in err
    assert "Traceback" not in err
    assert sha(copy_path) == before
