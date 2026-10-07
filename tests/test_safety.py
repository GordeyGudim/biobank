"""Тесты этапа 1 из ROADMAP.md («безопасность перед вебом») и подготовки к вебу.

Все правки — на копии импортированной базы во временной папке (фикстура path).
"""

import shutil
import sqlite3

import pytest
from conftest import SOURCE, count

from biobank import edit
from biobank.__main__ import main
from biobank.checks import collapse_issues, run_checks
from biobank.db import backup_database, connect, connect_read_only, create_database
from biobank.excel_reader import Issue, SheetData, SpecimenRow
from biobank.importer import (
    ImportContext,
    TableDifference,
    _event_site,
    _import_references,
    _import_specimens,
    prepare_import,
)
from biobank.parsing import parse_label
from biobank.reports import REPORTS, report_tables
from biobank.tables import CardSection

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture
def path(db_path, tmp_path):
    p = tmp_path / "output" / "biobank.db"
    p.parent.mkdir()
    shutil.copy(db_path, p)
    return p


@pytest.fixture
def conn(path):
    c = connect(path)
    yield c
    c.close()


def dump(p):
    c = sqlite3.connect(p)
    try:
        return list(c.iterdump())
    finally:
        c.close()


def backups(p):
    folder = p.parent / "backups"
    return sorted(folder.iterdir()) if folder.exists() else []


# ---------------------------------------------------------------------------
# 1. Только чтение: authorizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "ATTACH DATABASE 'other.db' AS w",
        "VACUUM INTO 'copy.db'",
        "PRAGMA foreign_keys = OFF",
        "PRAGMA journal_mode = WAL",
        "BEGIN IMMEDIATE",
    ],
)
def test_cli_sql_refuses_attach_vacuum_pragma(path, tmp_path, monkeypatch, capsys, query):
    monkeypatch.chdir(tmp_path)  # относительные имена файлов — во временной папке
    before = dump(path)
    assert main(["--db", str(path), "sql", query]) == 1
    assert "только читает данные" in capsys.readouterr().err
    assert not (tmp_path / "copy.db").exists() and not (tmp_path / "other.db").exists()
    assert dump(path) == before


@pytest.mark.parametrize(
    "query",
    [
        "PRAGMA foreign_key_check",
        "PRAGMA integrity_check",
        "PRAGMA table_info(specimen)",
        "PRAGMA foreign_keys",
        "SELECT name FROM pragma_table_info('specimen')",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 3) "
        "SELECT x FROM n",
    ],
)
def test_cli_sql_allows_read_pragmas(path, capsys, query):
    assert main(["--db", str(path), "sql", query]) == 0


def test_read_only_keeps_foreign_keys_on(path):
    c = connect_read_only(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            c.execute("PRAGMA foreign_keys = OFF")
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 2. Защита импорта
# ---------------------------------------------------------------------------


def guard(db):
    """prepare_import без замены: {таблица: (только в базе, только в Excel)}, unreadable."""
    with prepare_import(SOURCE, db) as prepared:
        assert prepared.new_path.exists()  # новая база уже собрана
        found = {d.table: (d.only_old, d.only_new) for d in prepared.differences}
        result = prepared.exists, found, prepared.unreadable, prepared.needs_force
    assert not prepared.new_path.exists()  # with удалил временный файл
    return result


def test_guard_missing_and_fresh(path, tmp_path):
    assert guard(tmp_path / "nope.db") == (False, {}, None, False)
    assert not (tmp_path / "nope.db").exists()
    assert guard(path) == (True, {}, None, False)


def test_guard_sees_records_not_from_excel(path, conn):
    event = conn.execute("SELECT * FROM sampling_event LIMIT 1").fetchone()
    with conn:
        conn.execute(
            "INSERT INTO sampling_event (expedition_id, site_id, event_date, capture_method_id) "
            "VALUES (?, ?, '2027-01-01', ?)",
            (event["expedition_id"], event["site_id"], event["capture_method_id"]),
        )
        conn.execute("INSERT INTO lab (name) VALUES ('Новая лаборатория')")
        conn.execute(
            "INSERT INTO analysis (sample_id, analysis_type) "
            "VALUES ((SELECT min(sample_id) FROM sample), 'секвенирование COI')"
        )
    exists, found, unreadable, needs_force = guard(path)
    assert needs_force and unreadable is None
    assert found == {"sampling_event": (1, 0), "lab": (1, 0), "analysis": (1, 0)}


def test_guard_sees_changed_and_deleted_excel_records(path, conn):
    """Изменённое значение в записи из Excel и удалённая запись тоже видны."""
    with conn:
        conn.execute("UPDATE specimen SET tl_cm = 13.1 WHERE label = '160'")
        conn.execute(
            "DELETE FROM catch_record WHERE catch_id = (SELECT min(catch_id) FROM catch_record)"
        )
    _, found, _, needs_force = guard(path)
    assert needs_force
    assert found == {"specimen": (1, 1), "catch_record": (0, 1)}


def test_guard_sees_resolved_issue(path):
    """Вручную закрытая проблема (resolved + resolution) — тоже правка."""
    assert main(["--db", str(path), "resolve-issue", "30", "TL 3,1 — опечатка"]) == 0
    _, found, _, _ = guard(path)
    assert found == {"data_issue": (1, 1)}


def test_guard_after_check_on_clean_db(path):
    """check на нетронутой базе ничего не меняет — импорт по-прежнему без --force."""
    assert main(["--db", str(path), "check"]) == 0
    assert guard(path) == (True, {}, None, False)


def test_guard_other_schema(path, conn):
    conn.execute("DROP TABLE analysis")
    _, found, unreadable, needs_force = guard(path)
    assert needs_force and "analysis" in unreadable


def test_guard_unreadable_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("это не база")
    _, _, unreadable, needs_force = guard(bad)
    assert unreadable and needs_force


def test_difference_is_readable():
    text = TableDifference("specimen", 1, 2).describe()
    assert text == (
        "особи (specimen): 1 — только в текущей базе (пропадут при импорте); "
        "2 — только в Excel (удалены или изменены в базе)"
    )


def test_import_refuses_without_force(path, capsys):
    assert main(["--db", str(path), "link-trawl", "5 pc", "3"]) == 0
    before = dump(path)
    assert main(["--db", str(path), "import", str(SOURCE), "--yes"]) == 1
    out = capsys.readouterr()
    assert "особи (specimen): 1 — только в текущей базе" in out.out
    assert "--force" in out.err
    assert dump(path) == before
    assert len(backups(path)) == 1  # только копия до правки link-trawl
    assert sorted(f.name for f in path.parent.iterdir()) == ["backups", "biobank.db"]  # нет .tmp


def test_import_with_force_asks(path, monkeypatch, capsys):
    assert main(["--db", str(path), "link-trawl", "5 pc", "3"]) == 0
    monkeypatch.setattr("builtins.input", lambda _: "n")
    before = dump(path)
    assert main(["--db", str(path), "import", str(SOURCE), "--force"]) == 1
    assert dump(path) == before


def test_import_always_backs_up_existing_db(path, capsys):
    """Даже без ручных правок: файл базы есть — перед импортом делается копия."""
    assert main(["--db", str(path), "import", str(SOURCE)]) == 0
    assert "Резервная копия текущей базы" in capsys.readouterr().out
    assert len(backups(path)) == 1


# ---------------------------------------------------------------------------
# 3. identify: вид при «не определён»
# ---------------------------------------------------------------------------


def test_identify_species_with_undetermined_refused(conn):
    before = count(conn, "SELECT count(*) FROM species_identification")
    with pytest.raises(edit.EditError, match="не определён"):
        edit.identify(conn, "1 e", "Pangasius sp.", "морфология", "не определён")
    assert count(conn, "SELECT count(*) FROM species_identification") == before


# ---------------------------------------------------------------------------
# 4. Гонки: проверка и запись — в одной транзакции BEGIN IMMEDIATE
# ---------------------------------------------------------------------------

EDITS = {
    "link_trawl": lambda c: edit.link_trawl(c, "5 pc", 3),
    "identify": lambda c: edit.identify(c, "1 e", "Pangasius elongatus", "ДНК", "точно"),
    "resolve_issue": lambda c: edit.resolve_issue(c, 1, "сверено"),
}


@pytest.mark.parametrize("name", list(EDITS))
def test_edit_checks_inside_immediate_transaction(conn, name):
    """set_trace_callback печатает каждую команду SQL: первой должна идти BEGIN IMMEDIATE,
    то есть и проверки (SELECT), и запись — уже внутри транзакции."""
    statements = []
    conn.set_trace_callback(statements.append)
    EDITS[name](conn)
    conn.set_trace_callback(None)
    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"


@pytest.mark.parametrize("name", list(EDITS))
def test_edit_waits_for_other_writer(path, name):
    """Пока другой пользователь пишет (держит блокировку), правка не проверяет старые данные,
    а ждёт; не дождалась — ошибка, база не изменена."""
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    before = dump(path)
    c = sqlite3.connect(path, timeout=0.05)
    c.row_factory = sqlite3.Row
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            EDITS[name](c)
    finally:
        c.close()
        other.rollback()
        other.close()
    assert dump(path) == before


def test_resolve_twice_second_refused(conn):
    edit.resolve_issue(conn, 1, "сверено")
    with pytest.raises(edit.EditError, match="уже решена"):
        edit.resolve_issue(conn, 1, "ещё раз")


# ---------------------------------------------------------------------------
# 5. run_checks: записи без листа Excel
# ---------------------------------------------------------------------------


def _add_web_specimens(conn, n):
    """n особей «из веба» (выезд без листа Excel), у каждой SL > TL."""
    event = conn.execute("SELECT * FROM sampling_event LIMIT 1").fetchone()
    with conn:
        event_id = conn.execute(
            "INSERT INTO sampling_event (expedition_id, site_id, event_date, capture_method_id) "
            "VALUES (?, ?, '2027-01-01', ?)",
            (event["expedition_id"], event["site_id"], event["capture_method_id"]),
        ).lastrowid
        for i in range(n):
            conn.execute(
                "INSERT INTO specimen (label, event_id, tl_cm, sl_cm) VALUES (?, ?, 10, 12)",
                (f"W{i}", event_id),
            )


def test_web_records_not_collapsed_and_not_duplicated(conn):
    _add_web_specimens(conn, 5)
    with conn:
        result = run_checks(conn)
    assert result.added == 5  # не одна свёрнутая запись на «лист None»
    rows = conn.execute(
        "SELECT record_id FROM data_issue WHERE sheet_name IS NULL AND description = 'SL больше TL'"
    ).fetchall()
    assert len({r[0] for r in rows}) == 5
    with conn:
        again = run_checks(conn)
    assert again.added == 0 and again.closed == 0


def test_record_id_is_part_of_key(conn):
    """Две записи с одинаковым текстом, но разными record_id — разные проблемы."""
    issues = [
        Issue("промеры", "SL больше TL", None, None, "W", "x", "specimen", 1),
        Issue("промеры", "SL больше TL", None, None, "W", "x", "specimen", 2),
    ]
    assert len(collapse_issues(issues * 3)) == 6  # без листа — не сворачиваются


# ---------------------------------------------------------------------------
# 6. apply_edit и CLI: ошибки SQLite, резервная копия
# ---------------------------------------------------------------------------


def test_apply_edit_sqlite_error_becomes_edit_error(path):
    def broken(c):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(edit.EditError, match="Ошибка базы данных"):
        edit.apply_edit(path, broken)
    assert backups(path) == []  # правки не было — копия удалена


def test_apply_edit_returns_backup(path):
    result = edit.apply_edit(path, lambda c: edit.link_trawl(c, "5 pc", 3))
    assert result.backup == backups(path)[0]


def test_apply_edit_missing_db(tmp_path):
    with pytest.raises(edit.NotFoundError, match="Базы нет"):
        edit.apply_edit(tmp_path / "nope.db", lambda c: None)


def test_cli_edit_on_broken_db(tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_text("это не база")
    assert main(["--db", str(bad), "link-trawl", "5 pc", "3"]) == 1
    assert "База не изменена" in capsys.readouterr().err
    assert backups(bad) == []


def test_backup_of_non_database_copies_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("это не база")
    assert backup_database(bad).read_text() == "это не база"


def test_backup_does_not_change_source(path):
    before = path.read_bytes()
    backup_database(path)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 7. Мелочи импорта
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    create_database(tmp_path / "unit.db")
    c = connect(tmp_path / "unit.db")
    context = ImportContext(c)
    _import_references(context)
    yield context
    c.close()


def _sheet(name, method, specimens=(), issues=()):
    return SheetData(name, method, None, "2025-09-25", None,
                     specimens=list(specimens), issues=list(issues))  # fmt: skip


def _spec(row, raw, species):
    return SpecimenRow(row, parse_label(raw), raw, {"label": raw, "species": species}, {}, {})


def test_market_without_city(ctx):
    region_id = ctx.insert("region", name="Тест")
    sheet = _sheet("Рынок", "рынок")
    site_id = _event_site(ctx, sheet, region_id)
    name = count(ctx.conn, "SELECT name FROM sampling_site WHERE site_id = ?", site_id)
    assert name == "Рынок (город не указан, лист «Рынок»)"
    assert [i.category for i in sheet.issues] == ["место"]


def test_unnumbered_specimen_issue_goes_to_itself(ctx):
    """Проблема особи «ХХ» не должна достаться первой особи листа."""
    region_id = ctx.insert("region", name="Тест")
    exp_id = ctx.insert("expedition", name="Тест")
    site_id = ctx.insert("sampling_site", name="Участок", site_type="участок реки")
    event_id = ctx.insert(
        "sampling_event", expedition_id=exp_id, site_id=site_id, event_date="2025-09-25",
        capture_method_id=ctx.method_ids["траление"],
    )  # fmt: skip
    issue = Issue("промеры", "что-то не так", "Точка 1", "G4", "ХХ", None, "specimen")
    sheet = _sheet(
        "Точка 1", "траление", [_spec(3, "1", "Pangasius sp"), _spec(4, "ХХ", "?")], [issue]
    )
    _import_specimens(ctx, sheet, event_id, site_id)
    assert region_id
    xx = count(ctx.conn, "SELECT specimen_id FROM specimen WHERE label LIKE 'ХХ%'")
    assert issue.record_id == xx


def test_not_found_hint_by_number(conn):
    with pytest.raises(edit.NotFoundError, match="Похожие: 13, 13 e, 13 pc"):
        edit.find_specimen(conn, "13ph")


def test_not_found_issue(conn):
    with pytest.raises(edit.NotFoundError):
        edit.resolve_issue(conn, 99999, "x")


# ---------------------------------------------------------------------------
# Подготовка к вебу: отчёт как данные
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(REPORTS))
def test_report_tables(path, name):
    c = connect_read_only(path)
    try:
        sections = report_tables(c, name)
    finally:
        c.close()
    assert len(sections) == len(REPORTS[name])
    assert all(isinstance(s, CardSection) and s.columns for s in sections)


# ---------------------------------------------------------------------------
# 8. Доработки по проверке этапа 1
# ---------------------------------------------------------------------------

# Номера проблем на свежем импорте — как до этапа 1 (коммит a864a50):
# на них ссылаются README (resolve-issue 30) и заметки пользователя.
EXPECTED_ISSUE_IDS = {
    1: ("Точка 1", None, "Чисел, записанных текстом"),
    2: ("Точка 2", "траление 1 старт", "Точка в 50 км"),
    25: ("Рыбак (самостоятельно 22.08)", "155 pc", "Число превратилось в дату"),
    26: ("Точка 1", "1", "Длина и масса не согласуются"),
    27: ("Рынок.Бенче", "21 pc", "Массы совпадают с длинами"),
    28: ("Рынок.Бенче", "21 pc", "Длина и масса не согласуются"),
    29: ("Точка 4.", "47 pc", "Масса без внутренних органов больше"),
    30: ("Точка 5.", "160", "SL больше TL"),
    33: ("Точка 5 2026", "262", "SL больше TL"),
    34: ("Рыбак (самостоятельно 21.08)", "149 pc", "SL слишком мала"),
    35: ("Точка 2", "5 e, 6 e, 7 e, 8 e", "Суффикс в номере пробы"),
    37: (None, "серия Pangasius", "Номера повторяются"),
    38: (None, "серия Pangasius", "Номера пропущены"),
    39: (None, "серия pc", "Номера пропущены"),
    40: ("Точка 1", None, "Особи не привязаны к тралениям"),
    56: ("Точка 6 2026", None, "Особи не привязаны к тралениям"),
}


@pytest.mark.parametrize("issue_id", list(EXPECTED_ISSUE_IDS))
def test_issue_ids_stable(db, issue_id):
    sheet, label, description = EXPECTED_ISSUE_IDS[issue_id]
    row = db.execute(
        "SELECT sheet_name, object_label, description FROM data_issue WHERE issue_id = ?",
        (issue_id,),
    ).fetchone()
    assert (row["sheet_name"], row["object_label"]) == (sheet, label)
    assert row["description"].startswith(description)


def test_issue_count_unchanged(db):
    assert count(db, "SELECT count(*) FROM data_issue") == 56


def test_collapse_keeps_order():
    """Запись без листа — на своём месте; группа — там, где была её первая запись."""

    def issue(sheet, label, description="SL больше TL"):
        return Issue("промеры", description, sheet, None, label, None, "specimen")

    items = [
        issue("A", "1"),
        issue(None, "web1"),
        issue("B", "x", "другое"),
        issue("A", "2"),
        issue(None, "web2"),
        issue("A", "3"),
        issue("A", "4"),
    ]
    result = collapse_issues(items)
    assert [(i.sheet, i.object_label) for i in result] == [
        ("A", "1, 2, 3, 4"),
        (None, "web1"),
        ("B", "x"),
        (None, "web2"),
    ]


def test_check_database_uses_immediate_transaction(path, monkeypatch):
    statements = []
    real_connect = edit.connect

    def traced_connect(p):
        c = real_connect(p)
        c.set_trace_callback(statements.append)
        return c

    monkeypatch.setattr(edit, "connect", traced_connect)
    result = edit.check_database(path)
    assert result.added == 0 and result.closed == 0
    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"


def test_check_database_waits_for_other_writer(path, monkeypatch):
    """Пока другой пишет, check не читает старые данные, а ждёт; не дождался — ошибка."""
    real_connect = edit.connect
    monkeypatch.setattr(edit, "connect", lambda p: _with_timeout(real_connect(p)))
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    before = dump(path)
    try:
        with pytest.raises(edit.EditError, match="журнал не изменён"):
            edit.check_database(path)
    finally:
        other.rollback()
        other.close()
    assert dump(path) == before


def _with_timeout(c):
    c.execute("PRAGMA busy_timeout = 50")  # ждать блокировку 50 мс, а не 5 с
    return c


def test_check_database_missing(tmp_path):
    with pytest.raises(edit.NotFoundError, match="Базы нет"):
        edit.check_database(tmp_path / "nope.db")
    assert list(tmp_path.iterdir()) == []


def test_cli_check_on_broken_db(tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_text("это не база")
    assert main(["--db", str(bad), "check"]) == 1
    assert "журнал не изменён" in capsys.readouterr().err
    assert bad.read_text() == "это не база"


def test_cli_check_after_edit_closes_issue(path, capsys):
    """Привязали траление напрямую (как из веба) — check закрывает проблему выезда."""
    c = connect(path)
    with c:
        c.execute(
            "UPDATE specimen SET trawling_id = (SELECT min(trawling_id) FROM trawling "
            "WHERE event_id = specimen.event_id) WHERE event_id = "
            "(SELECT event_id FROM sampling_event WHERE source_sheet = 'Точка 5')"
        )
    c.close()
    assert main(["--db", str(path), "check"]) == 0
    assert "закрыто (больше не находятся) 1" in capsys.readouterr().out


@pytest.mark.parametrize("name", list(EDITS))
def test_edit_inside_open_transaction_refused(conn, name):
    """Правка внутри чужой транзакции — EditError; чужие изменения не сохранены."""
    conn.execute("UPDATE specimen SET tl_cm = 1 WHERE label = '160'")  # открыла транзакцию
    assert conn.in_transaction
    with pytest.raises(edit.EditError, match="уже в транзакции"):
        EDITS[name](conn)
    assert conn.in_transaction  # транзакцию вызывающего кода не тронули
    conn.rollback()
    assert count(conn, "SELECT tl_cm FROM specimen WHERE label = '160'") == 3.1


def test_backup_of_missing_file_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError, match="файла базы нет"):
        backup_database(tmp_path / "nope.db")
    assert list(tmp_path.iterdir()) == []
