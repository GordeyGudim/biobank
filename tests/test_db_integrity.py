"""Защита базы данных: внешние ключи, составной ключ особь → траление, NOT NULL / UNIQUE / CHECK,
подключение только на чтение и откат правок при сбое.

Все тесты, которые пытаются что-то записать, работают с КОПИЕЙ импортированной базы
во временной папке (фикстура copy_path), а не с общей базой db_path: если защита
вдруг не сработает, испорчена будет только копия этого теста.

Как сравнить «база не изменилась»: conn.iterdump() выдаёт базу целиком в виде
SQL-команд (CREATE TABLE …, INSERT …). Если дамп до и после совпадает — не изменилось ничего.
"""

import shutil
import sqlite3

import pytest
from conftest import SOURCE, count

from biobank import edit
from biobank.__main__ import main
from biobank.db import connect, connect_read_only

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture
def copy_path(db_path, tmp_path):
    """Копия импортированной базы — своя для каждого теста."""
    path = tmp_path / "copy.db"
    shutil.copy(db_path, path)
    return path


@pytest.fixture
def conn(copy_path):
    """Обычное подключение (на запись) к копии базы."""
    c = connect(copy_path)
    yield c
    c.close()


def dump(path):
    """Вся база как список SQL-команд — для сравнения «до» и «после»."""
    c = sqlite3.connect(path)
    try:
        return list(c.iterdump())
    finally:
        c.close()


def two_trawl_events(conn):
    """Два разных выезда-траления: (event_a, trawling_a, event_b, trawling_b).

    Берём выезд особи «5 pc» (Точка 4, Донг-хап) и любой другой выезд с тралениями.
    """
    event_a = count(conn, "SELECT event_id FROM specimen WHERE label = '5 pc'")
    trawl_a = count(conn, "SELECT min(trawling_id) FROM trawling WHERE event_id = ?", event_a)
    trawl_b, event_b = conn.execute(
        "SELECT trawling_id, event_id FROM trawling WHERE event_id != ? ORDER BY trawling_id",
        (event_a,),
    ).fetchone()
    return event_a, trawl_a, event_b, trawl_b


# ---------------------------------------------------------------------------
# 1. Внешние ключи включены в обоих подключениях проекта
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("opener", [connect, connect_read_only], ids=["connect", "read_only"])
def test_foreign_keys_enabled_in_project_connections(copy_path, opener):
    c = opener(copy_path)
    try:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        c.close()


def test_plain_sqlite_connection_has_foreign_keys_off(copy_path):
    """Почему все модули обязаны открывать базу через biobank.db.connect:
    «голое» sqlite3.connect по умолчанию внешние ключи НЕ проверяет."""
    c = sqlite3.connect(copy_path)
    try:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 2. Нельзя удалить запись, на которую ссылаются другие (ON DELETE не задан)
# ---------------------------------------------------------------------------

# (таблица-родитель, её ключ, таблица-потомок, колонка-ссылка)
PARENT_CHILD = [
    ("specimen", "specimen_id", "sample", "specimen_id"),  # особь с образцами
    ("specimen", "specimen_id", "species_identification", "specimen_id"),
    ("sampling_event", "event_id", "specimen", "event_id"),  # выезд с особями
    ("sampling_event", "event_id", "trawling", "event_id"),
    ("trawling", "trawling_id", "water_measurement", "trawling_id"),
    ("expedition", "expedition_id", "sampling_event", "expedition_id"),
    ("sampling_site", "site_id", "sampling_event", "site_id"),
    ("species", "species_id", "specimen", "species_id"),  # вид, к которому отнесены особи
    ("sample", "sample_id", "sample_shipment", "sample_id"),
    ("lab", "lab_id", "sample_shipment", "lab_id"),
    ("region", "region_id", "city", "region_id"),
]


@pytest.mark.parametrize(
    "parent, key, child, ref", PARENT_CHILD, ids=[f"{p}<-{c}" for p, _, c, _ in PARENT_CHILD]
)
def test_cannot_delete_referenced_row(conn, parent, key, child, ref):
    parent_id = count(conn, f"SELECT {ref} FROM {child} WHERE {ref} IS NOT NULL LIMIT 1")
    assert parent_id is not None  # в импортированной базе такая ссылка есть
    before = count(conn, f"SELECT count(*) FROM {parent}")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:  # транзакция: при ошибке — откат
            conn.execute(f"DELETE FROM {parent} WHERE {key} = ?", (parent_id,))
    assert count(conn, f"SELECT count(*) FROM {parent}") == before


# ---------------------------------------------------------------------------
# 3. Нельзя сослаться на несуществующую запись
# ---------------------------------------------------------------------------

MISSING = 999999  # такого id нет ни в одной таблице

ORPHAN_UPDATES = [
    ("specimen", "event_id"),
    ("specimen", "species_id"),
    ("specimen", "capture_site_id"),
    ("specimen", "capture_method_id"),
    ("sample", "specimen_id"),
    ("species_identification", "specimen_id"),
    ("species_identification", "species_id"),
    ("sample_shipment", "lab_id"),
    ("sampling_event", "site_id"),
    ("sampling_event", "gear_id"),
    ("water_measurement", "event_id"),
    ("catch_record", "event_id"),
]


@pytest.mark.parametrize("table, column", ORPHAN_UPDATES, ids=[".".join(x) for x in ORPHAN_UPDATES])
def test_cannot_reference_missing_row(conn, copy_path, table, column):
    before = dump(copy_path)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE rowid = (SELECT min(rowid) FROM {table})",
                (MISSING,),
            )
    assert dump(copy_path) == before


def test_cannot_insert_specimen_into_missing_event(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:
            conn.execute("INSERT INTO specimen (label, event_id) VALUES ('999 pc', ?)", (MISSING,))
    assert count(conn, "SELECT count(*) FROM specimen WHERE label = '999 pc'") == 0


# ---------------------------------------------------------------------------
# 4. Составной внешний ключ: траление — только своего выезда
# ---------------------------------------------------------------------------


def test_specimen_can_be_linked_to_trawl_of_own_event(conn):
    """Контроль: правильная привязка проходит — значит, следующие тесты
    падают именно из-за чужого выезда, а не из-за чего-то ещё."""
    _, trawl_a, _, _ = two_trawl_events(conn)
    with conn:
        conn.execute("UPDATE specimen SET trawling_id = ? WHERE label = '5 pc'", (trawl_a,))
    assert count(conn, "SELECT trawling_id FROM specimen WHERE label = '5 pc'") == trawl_a


def test_specimen_cannot_be_linked_to_trawl_of_other_event(conn):
    """Даже прямой UPDATE в обход edit.link_trawl не даёт привязать особь к чужому тралению."""
    _, _, _, trawl_b = two_trawl_events(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:
            conn.execute("UPDATE specimen SET trawling_id = ? WHERE label = '5 pc'", (trawl_b,))
    assert count(conn, "SELECT trawling_id FROM specimen WHERE label = '5 pc'") is None


def test_linked_specimen_cannot_move_to_other_event(conn):
    """Особь с тралением нельзя перенести на другой выезд, оставив старое траление."""
    event_a, trawl_a, event_b, _ = two_trawl_events(conn)
    with conn:
        conn.execute("UPDATE specimen SET trawling_id = ? WHERE label = '5 pc'", (trawl_a,))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:
            conn.execute("UPDATE specimen SET event_id = ? WHERE label = '5 pc'", (event_b,))
    assert count(conn, "SELECT event_id FROM specimen WHERE label = '5 pc'") == event_a


def test_trawling_with_measurements_cannot_move_to_other_event(conn):
    """Траление нельзя «переселить» на другой выезд: его замеры воды остались бы у старого.

    Номер 99 — чтобы не сработал раньше UNIQUE (event_id, trawl_no)."""
    event_a, trawl_a, event_b, _ = two_trawl_events(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:
            conn.execute(
                "UPDATE trawling SET event_id = ?, trawl_no = 99 WHERE trawling_id = ?",
                (event_b, trawl_a),
            )
    assert count(conn, "SELECT event_id FROM trawling WHERE trawling_id = ?", trawl_a) == event_a


# Замер воды и запись прилова защищены тем же составным ключом.
# Для замера берём «точку лова рыбака» (Точка 6.): у неё trawling_id NULL,
# поэтому UNIQUE (trawling_id, point_type) не сработает раньше внешнего ключа.
OTHER_CHILDREN = {
    "water_measurement": "SELECT measurement_id, event_id FROM water_measurement "
    "WHERE point_type = 'точка'",
    "catch_record": "SELECT catch_id, event_id FROM catch_record ORDER BY catch_id",
}


@pytest.mark.parametrize("table", list(OTHER_CHILDREN))
def test_measurement_and_catch_cannot_point_to_other_event_trawl(conn, table):
    row_id, event_id = conn.execute(OTHER_CHILDREN[table]).fetchone()
    foreign_trawl = count(
        conn, "SELECT min(trawling_id) FROM trawling WHERE event_id != ?", event_id
    )
    key = "measurement_id" if table == "water_measurement" else "catch_id"
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with conn:
            conn.execute(
                f"UPDATE {table} SET trawling_id = ? WHERE {key} = ?", (foreign_trawl, row_id)
            )


# ---------------------------------------------------------------------------
# 5. UNIQUE, NOT NULL, CHECK
# ---------------------------------------------------------------------------

CONSTRAINTS = [
    # --- UNIQUE: дубликаты ---
    (
        "дубликат номера особи",
        "UPDATE specimen SET label = '26 ph' WHERE label = '155 pc'",
        "UNIQUE constraint failed: specimen.label",
    ),
    (
        "второе траление с тем же номером на выезде",
        "INSERT INTO trawling (event_id, trawl_no) "
        "SELECT event_id, trawl_no FROM trawling ORDER BY trawling_id LIMIT 1",
        "UNIQUE constraint failed: trawling.event_id, trawling.trawl_no",
    ),
    (
        "второй «старт» у одного траления",
        "INSERT INTO water_measurement (event_id, trawling_id, point_type) "
        "SELECT event_id, trawling_id, point_type FROM water_measurement "
        "WHERE point_type = 'старт' LIMIT 1",
        "UNIQUE constraint failed: water_measurement.trawling_id, water_measurement.point_type",
    ),
    (
        "дубликат вида в справочнике",
        "INSERT INTO species (scientific_name) VALUES ('Plotosus canius')",
        "UNIQUE constraint failed: species.scientific_name",
    ),
    (
        "повторная отправка образца в ту же лабораторию",
        "INSERT INTO sample_shipment (sample_id, lab_id) "
        "SELECT sample_id, lab_id FROM sample_shipment LIMIT 1",
        "UNIQUE constraint failed: sample_shipment.sample_id, sample_shipment.lab_id",
    ),
    (
        "вторая пробирка с той же меткой у особи (тот же тип и орган)",
        "INSERT INTO sample (specimen_id, sample_type, organ, label) "
        "SELECT specimen_id, sample_type, organ, label FROM sample WHERE organ IS NOT NULL LIMIT 1",
        "UNIQUE constraint failed: index 'ux_sample_label'",
    ),
    (
        "вторая пробирка с той же меткой, орган пустой (ifnull в индексе)",
        "INSERT INTO sample (specimen_id, sample_type, label) "
        "SELECT specimen_id, sample_type, label FROM sample WHERE organ IS NULL "
        "AND label IS NOT NULL LIMIT 1",
        "UNIQUE constraint failed: index 'ux_sample_label'",
    ),
    # --- STRICT (v3): тип значения должен совпадать с типом колонки ---
    (
        "текст в REAL (промер «21,3 см»)",
        "UPDATE specimen SET tl_cm = '21,3 см' WHERE label = '155 pc'",
        "cannot store TEXT value in REAL column specimen.tl_cm",
    ),
    (
        "дробное в INTEGER (номер траления)",
        "UPDATE trawling SET trawl_no = 1.5 WHERE trawling_id = (SELECT min(trawling_id) "
        "FROM trawling)",
        "cannot store REAL value in INTEGER column trawling.trawl_no",
    ),
    (
        "текст в ссылку INTEGER (кто определил — имя вместо номера человека)",
        "UPDATE species_identification SET identified_by = 'Иванов' "
        "WHERE identification_id = (SELECT min(identification_id) FROM species_identification)",
        "cannot store TEXT value in INTEGER column species_identification.identified_by",
    ),
    # --- CHECK (v3): формат дат, времени, флагов, источника записи журнала ---
    (
        "дата не в ISO («25.09.25»)",
        "UPDATE sampling_event SET event_date = '25.09.25' "
        "WHERE event_id = (SELECT min(event_id) FROM sampling_event)",
        "CHECK constraint failed",
    ),
    (
        "несуществующая дата (30 февраля)",
        "UPDATE sampling_event SET event_date = '2025-02-30' "
        "WHERE event_id = (SELECT min(event_id) FROM sampling_event)",
        "CHECK constraint failed",
    ),
    (
        "время вне суток («25:10»)",
        "UPDATE trawling SET time_start = '25:10' "
        "WHERE trawling_id = (SELECT min(trawling_id) FROM trawling)",
        "CHECK constraint failed",
    ),
    (
        "время без двоеточия («10.48»)",
        "UPDATE trawling SET time_start = '10.48' "
        "WHERE trawling_id = (SELECT min(trawling_id) FROM trawling)",
        "CHECK constraint failed",
    ),
    (
        "отметка времени не в формате схемы",
        "UPDATE specimen SET created_at = '2026-10-20T10:00:00' WHERE label = '155 pc'",
        "CHECK constraint failed",
    ),
    (
        "is_dead не 0/1",
        "UPDATE specimen SET is_dead = 2 WHERE label = '155 pc'",
        "CHECK constraint failed",
    ),
    (
        "resolved не 0/1",
        "UPDATE data_issue SET resolved = 2 "
        "WHERE issue_id = (SELECT min(issue_id) FROM data_issue)",
        "CHECK constraint failed",
    ),
    (
        "источник записи журнала вне списка",
        "UPDATE data_issue SET source = 'Excel' "
        "WHERE issue_id = (SELECT min(issue_id) FROM data_issue)",
        "CHECK constraint failed",
    ),
    # --- NOT NULL ---
    (
        "особь без номера",
        "UPDATE specimen SET label = NULL WHERE label = '155 pc'",
        "NOT NULL constraint failed: specimen.label",
    ),
    (
        "особь без выезда",
        "UPDATE specimen SET event_id = NULL WHERE label = '155 pc'",
        "NOT NULL constraint failed: specimen.event_id",
    ),
    (
        "выезд без даты",
        "UPDATE sampling_event SET event_date = NULL "
        "WHERE event_id = (SELECT min(event_id) FROM sampling_event)",
        "NOT NULL constraint failed: sampling_event.event_date",
    ),
    (
        "проблема без описания",
        "INSERT INTO data_issue (category) VALUES ('промеры')",
        "NOT NULL constraint failed: data_issue.description",
    ),
    (
        "запись прилова без исходного текста",
        "UPDATE catch_record SET raw_text = NULL "
        "WHERE catch_id = (SELECT min(catch_id) FROM catch_record)",
        "NOT NULL constraint failed: catch_record.raw_text",
    ),
    # --- CHECK: промеры и списки допустимых значений ---
    ("TL = 0", "UPDATE specimen SET tl_cm = 0 WHERE label = '155 pc'", "CHECK constraint failed"),
    ("SL < 0", "UPDATE specimen SET sl_cm = -1 WHERE label = '155 pc'", "CHECK constraint failed"),
    (
        "масса < 0",
        "UPDATE specimen SET mass_g = -5 WHERE label = '155 pc'",
        "CHECK constraint failed",
    ),
    (
        "масса без внутр. < 0",
        "UPDATE specimen SET mass_gutted_g = -0.1 WHERE label = '155 pc'",
        "CHECK constraint failed",
    ),
    (
        "пол вне списка",
        "UPDATE specimen SET sex = 'самк' WHERE label = '155 pc'",
        "CHECK constraint failed",
    ),
    (
        "тип образца вне списка",
        "UPDATE sample SET sample_type = 'кровь' "
        "WHERE sample_id = (SELECT min(sample_id) FROM sample)",
        "CHECK constraint failed",
    ),
    (
        "метод определения вне списка",
        "UPDATE species_identification SET method = 'ПЦР' "
        "WHERE identification_id = (SELECT min(identification_id) FROM species_identification)",
        "CHECK constraint failed",
    ),
    (
        "уверенность вне списка",
        "UPDATE species_identification SET confidence = 'наверное' "
        "WHERE identification_id = (SELECT min(identification_id) FROM species_identification)",
        "CHECK constraint failed",
    ),
    (
        "тип точки замера вне списка",
        "UPDATE water_measurement SET point_type = 'середина' "
        "WHERE measurement_id = (SELECT min(measurement_id) FROM water_measurement)",
        "CHECK constraint failed",
    ),
    (
        "тип места вне списка",
        "UPDATE sampling_site SET site_type = 'озеро' "
        "WHERE site_id = (SELECT min(site_id) FROM sampling_site)",
        "CHECK constraint failed",
    ),
]


@pytest.mark.parametrize(
    "sql, error", [c[1:] for c in CONSTRAINTS], ids=[c[0] for c in CONSTRAINTS]
)
def test_schema_constraint_rejects_bad_value(conn, copy_path, sql, error):
    before = dump(copy_path)
    with pytest.raises(sqlite3.IntegrityError, match=error):
        with conn:
            conn.execute(sql)
    assert dump(copy_path) == before


def test_unknown_role_rejected(conn, copy_path):
    """Роль пользователя — только из списка схемы: читатель, исследователь, куратор, админ."""
    with conn:
        user = conn.execute(
            "INSERT INTO app_user (login, password_hash, display_name, created_at) "
            "VALUES ('ivanova', 'scrypt$…', 'Иванова А.', '2026-10-20 10:00:00')"
        ).lastrowid
        conn.execute("INSERT INTO user_role (user_id, role) VALUES (?, 'куратор')", (user,))
    before = dump(copy_path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        with conn:
            conn.execute("INSERT INTO user_role (user_id, role) VALUES (?, 'бог')", (user,))
    assert dump(copy_path) == before


def test_same_label_allowed_for_other_sample_types(conn):
    """ДНК, мазок и кишечник одной особи по традиции Excel носят одну метку («26 ph») —
    это не дубль: уникальность — в пределах особи, типа образца и органа."""
    groups = count(
        conn,
        "SELECT count(*) FROM (SELECT specimen_id, label FROM sample WHERE label IS NOT NULL "
        "GROUP BY specimen_id, label HAVING count(DISTINCT sample_type) > 1)",
    )
    assert groups > 0


def test_missing_measurements_are_allowed(conn):
    """Граничный случай: промеры NULL допустимы («целая рыба» — промеров нет),
    CHECK (tl_cm > 0) на NULL не срабатывает."""
    with conn:
        conn.execute(
            "UPDATE specimen SET tl_cm = NULL, sl_cm = NULL, mass_g = NULL, "
            "mass_gutted_g = NULL WHERE label = '155 pc'"
        )
    row = conn.execute(
        "SELECT tl_cm, sl_cm, mass_g, mass_gutted_g FROM specimen WHERE label = '155 pc'"
    ).fetchone()
    assert tuple(row) == (None, None, None, None)


def test_imported_numeric_columns_hold_numbers_not_text(db):
    """SQLite без STRICT молча сохранил бы текст «29,9» в колонку REAL. В схеме v3 таблицы
    STRICT и база такое не примет, но проверка результата импорта остаётся: во всех
    колонках REAL/INTEGER — только числа или NULL (в Excel 2026 сотни чисел текстом)."""
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    text_values = {}
    for table in tables:
        for col in db.execute(f"PRAGMA table_info({table})"):
            if col["type"] in ("REAL", "INTEGER"):
                n = count(
                    db,
                    f"SELECT count(*) FROM {table} "
                    f"WHERE typeof({col['name']}) NOT IN ('real', 'integer', 'null')",
                )
                if n:
                    text_values[f"{table}.{col['name']}"] = n
    assert text_values == {}


# ---------------------------------------------------------------------------
# 6. Только чтение: connect_read_only и команда `sql`
# ---------------------------------------------------------------------------

WRITES = [
    "INSERT INTO lab (name) VALUES ('Новая лаборатория')",
    "UPDATE specimen SET tl_cm = 1 WHERE label = '155 pc'",
    "DELETE FROM data_issue",
    "DROP TABLE lab",
    "CREATE TABLE extra (x)",
    "ALTER TABLE specimen ADD COLUMN extra TEXT",
    "PRAGMA user_version = 5",
]


@pytest.mark.parametrize("sql", WRITES)
def test_read_only_connection_refuses_to_write(copy_path, sql):
    before = dump(copy_path)
    c = connect_read_only(copy_path)
    try:
        # authorizer отклоняет запрос ещё до выполнения: DatabaseError «not authorized»
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            c.execute(sql)
            c.commit()
    finally:
        c.close()
    assert dump(copy_path) == before


def test_read_only_connection_cannot_write_through_attach(copy_path):
    """Обход «только чтения»: ATTACH того же файла с mode=rw даёт второе имя базы,
    через которое можно писать. Подключение только на чтение не должно это позволять
    (веб-интерфейс будет держать такое подключение и выполнять через него запросы)."""
    before = dump(copy_path)
    c = connect_read_only(copy_path)
    try:
        with pytest.raises(sqlite3.Error):
            c.execute(f"ATTACH DATABASE '{copy_path.as_uri()}?mode=rw' AS w")
            c.execute("DELETE FROM w.data_issue")
            c.commit()
    finally:
        c.close()
    assert dump(copy_path) == before


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO lab (name) VALUES ('Новая лаборатория')",
        "CREATE TABLE extra (x)",
        "ALTER TABLE specimen ADD COLUMN extra TEXT",
        "SELECT 1; DELETE FROM lab",  # вторая команда «в хвосте» запроса
    ],
)
def test_cli_sql_refuses_to_write(copy_path, capsys, query):
    """Дополняет test_export_reports.py::test_sql_refuses_to_write (там DELETE/UPDATE/DROP)."""
    before = dump(copy_path)
    assert main(["--db", str(copy_path), "sql", query]) == 1
    assert "Ошибка SQL" in capsys.readouterr().err
    assert dump(copy_path) == before


# ---------------------------------------------------------------------------
# 7. Правки edit.py: сбой посреди транзакции — откат всего
# ---------------------------------------------------------------------------

# Каждая правка сначала меняет данные, затем в той же транзакции перепроверяет
# журнал (run_checks). Подменяем run_checks на функцию с ошибкой — так имитируется
# сбой ПОСЛЕ того, как UPDATE/INSERT уже выполнен.
EDITS = {
    "link_trawl": lambda c: edit.link_trawl(c, "5 pc", 3),
    "identify": lambda c: edit.identify(c, "1 e", "Pangasius elongatus", "ДНК", "точно"),
    "resolve_issue": lambda c: edit.resolve_issue(
        c, count(c, "SELECT min(issue_id) FROM data_issue WHERE resolved = 0"), "сверено"
    ),
}


@pytest.mark.parametrize("name", list(EDITS))
def test_edit_rolls_back_when_failing_midway(conn, copy_path, monkeypatch, name):
    """monkeypatch — фикстура pytest: временно подменяет функцию на время теста."""

    def broken_checks(*args, **kwargs):
        raise sqlite3.OperationalError("сбой посреди правки")

    before = dump(copy_path)
    monkeypatch.setattr(edit, "run_checks", broken_checks)
    with pytest.raises(sqlite3.OperationalError, match="сбой посреди правки"):
        EDITS[name](conn)
    assert dump(copy_path) == before  # ни особь, ни история, ни журнал не изменились


@pytest.mark.parametrize("name", list(EDITS))
def test_edit_on_read_only_connection_changes_nothing(copy_path, name):
    """Если веб-слой по ошибке передаст в правку подключение только на чтение —
    база не меняется (ошибка SQLite «not authorized», а не EditError)."""
    before = dump(copy_path)
    c = connect_read_only(copy_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            EDITS[name](c)
    finally:
        c.close()
    assert dump(copy_path) == before
