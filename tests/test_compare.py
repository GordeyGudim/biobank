"""Сравнение баз перед импортом (importer.compare_databases): ссылки — по содержимому.

Номера записей (sample_id, specimen_id, trawling_id…) могут сдвинуться, если импорт
создаст новую запись в начале. Такой сдвиг — не расхождение; а другая лаборатория у
отправки, правка поля или удалённая запись — расхождение.
"""

import shutil

import pytest
from conftest import count

from biobank.db import connect, connect_read_only
from biobank.importer import _table_order, compare_databases

# на сколько сдвинуть номера: заведомо больше числа записей в таблице
SHIFT = 100_000
# кто ссылается на образцы, особей, траления: [(таблица, колонка)]
SAMPLE_REFS = [("sample_shipment", "sample_id"), ("analysis", "sample_id")]
SPECIMEN_REFS = [("sample", "specimen_id"), ("species_identification", "specimen_id")]
TRAWLING_REFS = [
    ("specimen", "trawling_id"), ("water_measurement", "trawling_id"),
    ("catch_record", "trawling_id"),
]  # fmt: skip


@pytest.fixture
def old(db_path, tmp_path):
    """«Текущая база» — копия свежего импорта во временной папке, её можно портить."""
    path = tmp_path / "old.db"
    shutil.copy(db_path, path)
    conn = connect(path)
    yield conn
    conn.close()


def differences(old_conn, db_path):
    """{таблица: (только в текущей, только в Excel)} — как видит защита import."""
    fresh = connect_read_only(db_path)
    try:
        return {d.table: (d.only_old, d.only_new) for d in compare_databases(old_conn, fresh)}
    finally:
        fresh.close()


def shift_ids(conn, table, id_column, references):
    """Сдвинуть номера записей таблицы и все ссылки на них (references: [(таблица, колонка)]).

    PRAGMA defer_foreign_keys откладывает проверку внешних ключей до COMMIT: внутри
    транзакции ссылки на миг «битые», к концу — снова целые. Имена таблиц и колонок —
    константы теста, не данные.
    """
    conn.execute("BEGIN")  # прагма действует только внутри уже открытой транзакции
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute(f"UPDATE {table} SET {id_column} = {id_column} + ?", (SHIFT,))
    for child, column in references:
        conn.execute(f"UPDATE {child} SET {column} = {column} + ?", (SHIFT,))
    conn.execute(
        "UPDATE data_issue SET record_id = record_id + ? WHERE table_name = ?", (SHIFT, table)
    )
    conn.commit()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_identical(old, db_path):
    assert differences(old, db_path) == {}


def test_shifted_ids_are_not_differences(old, db_path):
    """Другие номера образцов, особей и тралений (в том числе составной FK) — 0 расхождений."""
    shift_ids(old, "sample", "sample_id", SAMPLE_REFS)
    shift_ids(old, "specimen", "specimen_id", SPECIMEN_REFS)
    shift_ids(old, "trawling", "trawling_id", TRAWLING_REFS)
    assert count(old, "SELECT min(sample_id) FROM sample") > SHIFT  # номера правда другие
    assert differences(old, db_path) == {}


def first_shipment(conn):
    return conn.execute(
        "SELECT sample_id, lab_id FROM sample_shipment ORDER BY sample_id, lab_id LIMIT 1"
    ).fetchone()


def test_other_lab_is_difference(old, db_path):
    sample_id, lab_id = first_shipment(old)
    other = count(
        old,
        "SELECT min(lab_id) FROM lab WHERE lab_id NOT IN "
        "(SELECT lab_id FROM sample_shipment WHERE sample_id = ?)",
        sample_id,
    )
    with old:
        old.execute(
            "UPDATE sample_shipment SET lab_id = ? WHERE sample_id = ? AND lab_id = ?",
            (other, sample_id, lab_id),
        )
    assert differences(old, db_path) == {"sample_shipment": (1, 1)}


def test_other_lab_found_even_with_shifted_ids(old, db_path):
    """Сдвиг номеров не прячет настоящую правку."""
    sample_id, lab_id = first_shipment(old)
    with old:
        old.execute(
            "DELETE FROM sample_shipment WHERE sample_id = ? AND lab_id = ?", (sample_id, lab_id)
        )
    shift_ids(old, "sample", "sample_id", SAMPLE_REFS)
    assert differences(old, db_path) == {"sample_shipment": (0, 1)}


def test_deleted_shipment_is_difference(old, db_path):
    sample_id, lab_id = first_shipment(old)
    with old:
        old.execute(
            "DELETE FROM sample_shipment WHERE sample_id = ? AND lab_id = ?", (sample_id, lab_id)
        )
    assert differences(old, db_path) == {"sample_shipment": (0, 1)}


def test_edited_parent_does_not_cascade(old, db_path):
    """Правка поля выезда — изменён только выезд, а не все его особи и траления."""
    with old:
        old.execute("UPDATE sampling_event SET notes = 'правка' WHERE event_id = 1")
    assert differences(old, db_path) == {"sampling_event": (1, 1)}


def test_edited_field_in_referenced_record(old, db_path):
    """Правка поля образца находится и тогда, когда номера образцов сдвинуты."""
    shift_ids(old, "sample", "sample_id", SAMPLE_REFS)
    with old:
        old.execute(
            "UPDATE sample SET preservative = 'RNAlater' WHERE sample_id = ?",
            (first_shipment(old)[0],),
        )
    assert differences(old, db_path) == {"sample": (1, 1)}


def test_issue_moved_to_other_specimen_is_difference(old, db_path):
    """data_issue.record_id — ссылка без внешнего ключа, тоже сравнивается по содержимому."""
    issue_id, record_id = old.execute(
        "SELECT issue_id, record_id FROM data_issue "
        "WHERE table_name = 'specimen' AND record_id IS NOT NULL LIMIT 1"
    ).fetchone()
    other = count(old, "SELECT min(specimen_id) FROM specimen WHERE specimen_id != ?", record_id)
    with old:
        old.execute("UPDATE data_issue SET record_id = ? WHERE issue_id = ?", (other, issue_id))
    assert differences(old, db_path) == {"data_issue": (1, 1)}


def test_table_order_parents_first(db):
    order = _table_order(db, [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )])  # fmt: skip
    for parent, child in [("region", "city"), ("sample", "sample_shipment"),
                          ("trawling", "specimen"), ("sampling_event", "trawling")]:  # fmt: skip
        assert order.index(parent) < order.index(child)
    assert order[-1] == "data_issue"
