"""Сравнение баз перед импортом (importer.compare_databases): ссылки — по содержимому.

Номера записей (sample_id, specimen_id, trawling_id…) могут сдвинуться, если импорт
создаст новую запись в начале. Такой сдвиг — не расхождение; а другая лаборатория у
отправки, правка поля или удалённая запись — расхождение.
"""

import shutil
import sqlite3

import pytest
from conftest import count

from biobank.db import connect, connect_read_only
from biobank.entry import create_specimen
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


def shift_all(conn):
    """Сдвинуть номера образцов, особей и тралений — как будто импорт добавил записи в начало."""
    shift_ids(conn, "sample", "sample_id", SAMPLE_REFS)
    shift_ids(conn, "specimen", "specimen_id", SPECIMEN_REFS)
    shift_ids(conn, "trawling", "trawling_id", TRAWLING_REFS)


def specimen_id(conn, label):
    return count(conn, "SELECT specimen_id FROM specimen WHERE label = ?", label)


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


# ---- изменения при сдвинутых номерах: находятся и не «расползаются» ----


def test_specimen_moved_to_other_event(old, db_path):
    """Перенос особи на другой выезд — изменена только особь (её образцы — те же)."""
    shift_all(old)
    with old:
        old.execute(
            "UPDATE specimen SET event_id = 2 WHERE specimen_id = ?", (specimen_id(old, "3"),)
        )
    assert differences(old, db_path) == {"specimen": (1, 1)}


def test_link_trawl(old, db_path):
    """link-trawl: NULL → номер траления (составная ссылка) — изменена только особь."""
    shift_all(old)
    event_id = count(old, "SELECT event_id FROM specimen WHERE label = '5 pc'")
    trawling_id = count(old, "SELECT min(trawling_id) FROM trawling WHERE event_id = ?", event_id)
    with old:
        old.execute("UPDATE specimen SET trawling_id = ? WHERE label = '5 pc'", (trawling_id,))
    assert differences(old, db_path) == {"specimen": (1, 1)}


def test_one_of_two_equal_catches_deleted(old, db_path):
    """Две одинаковые записи прилова (выезд 7) — удалена одна: (0, 1), а не «совпадает»."""
    rows = old.execute(
        "SELECT catch_id FROM catch_record WHERE event_id = 7 "
        "AND raw_text = 'речная камбала - 1 шт'"
    ).fetchall()
    assert len(rows) == 2
    shift_all(old)
    with old:
        old.execute("DELETE FROM catch_record WHERE catch_id = ?", (rows[0][0],))
    assert differences(old, db_path) == {"catch_record": (0, 1)}


def test_specimen_added_through_entry(old, db_path):
    """Особь, введённая на сайте (entry.create_specimen), — видна, остальное на месте."""
    shift_all(old)
    create_specimen(old, 1, "Plotosus canius", "999 pc", tl_cm=10)
    diff = differences(old, db_path)
    assert diff["specimen"] == (1, 0)
    assert diff["species_identification"] == (1, 0)
    assert set(diff) <= {"specimen", "species_identification", "data_issue"}


def test_tl_edit_and_sample_moved(old, db_path):
    """Правка TL особи + перенос её образца на другую особь — расхождение и в sample."""
    shift_all(old)
    source, target = specimen_id(old, "3"), specimen_id(old, "5 pc")
    with old:
        old.execute("UPDATE specimen SET tl_cm = 1.5 WHERE specimen_id = ?", (source,))
        old.execute(
            "UPDATE sample SET specimen_id = ? WHERE sample_id = "
            "(SELECT min(sample_id) FROM sample WHERE specimen_id = ?)",
            (target, source),
        )
    assert differences(old, db_path) == {"specimen": (1, 1), "sample": (1, 1)}


def replace_specimen(conn, label, changes: str):
    """Удалить особь label и добавить X1 с теми же образцами и определением.

    changes — фиксированный фрагмент SQL теста: чем X1 отличается кроме номера.
    """
    source = specimen_id(conn, label)
    with conn:
        conn.execute(
            "INSERT INTO specimen (label, label_raw, series, series_no, event_id, species_id, "
            "tl_cm, sl_cm, mass_g, mass_gutted_g, sex, is_dead, notes, source_row) "
            "SELECT 'X1', label_raw, series, series_no, event_id, species_id, tl_cm, sl_cm, "
            "mass_g, mass_gutted_g, sex, is_dead, notes, source_row FROM specimen "
            "WHERE specimen_id = ?",
            (source,),
        )
        new_id = specimen_id(conn, "X1")
        if changes:
            conn.execute(f"UPDATE specimen SET {changes} WHERE specimen_id = ?", (new_id,))
        for table in ("sample", "species_identification"):  # имена — константы теста
            conn.execute(
                f"UPDATE {table} SET specimen_id = ? WHERE specimen_id = ?", (new_id, source)
            )
        conn.execute("DELETE FROM specimen WHERE specimen_id = ?", (source,))


def test_specimen_replaced_by_same_with_other_label(old, db_path):
    """X1 — та же особь «3», отличается только номером: неотличимо от исправленного номера
    (сценарий Б), поэтому изменена одна особь. Ничего не скрыто: образцы и определение
    X1 по содержимому те же, что вернёт импорт у «3»."""
    shift_all(old)
    replace_specimen(old, "3", "")
    assert differences(old, db_path) == {"specimen": (1, 1)}


def test_specimen_replaced_by_other_with_same_samples(old, db_path):
    """X1 отличается от «3» не только номером (другие промеры, пол, выезд) — это другая
    особь: и она, и её образцы, и определение видны в сводке."""
    shift_all(old)
    replace_specimen(old, "3", "tl_cm = 50, sl_cm = 40, mass_g = 900, sex = 'самка', event_id = 2")
    diff = differences(old, db_path)
    assert diff["specimen"] == (1, 1)
    assert diff["sample"] == (3, 3)
    assert diff["species_identification"] == (1, 1)


def test_event_site_changed(old, db_path):
    """Сценарий А: у выезда 1 сменили место — изменён только выезд (особи, траления,
    замеры воды и записи журнала о нём — те же)."""
    shift_all(old)
    with old:
        old.execute("UPDATE sampling_event SET site_id = 2 WHERE event_id = 1")
    assert differences(old, db_path) == {"sampling_event": (1, 1)}


def test_event_site_changed_and_specimen_edited(old, db_path):
    shift_all(old)
    with old:
        old.execute("UPDATE sampling_event SET site_id = 2 WHERE event_id = 1")
        old.execute("UPDATE specimen SET tl_cm = 1.5 WHERE label = '3'")  # особь выезда 1
    assert differences(old, db_path) == {"sampling_event": (1, 1), "specimen": (1, 1)}


def test_specimen_label_changed(old, db_path):
    """Сценарий Б: исправили номер особи — изменена только особь, не её образцы."""
    shift_all(old)
    with old:
        old.execute("UPDATE specimen SET label = '1 испр' WHERE label = '1'")
    assert differences(old, db_path) == {"specimen": (1, 1)}


def test_two_events_of_one_site_not_mixed(old, db_path):
    """Два выезда одного места; у одного сменили место — их особи не перепутаны."""
    site_id, first, second = old.execute(
        "SELECT site_id, min(event_id), max(event_id) FROM sampling_event "
        "GROUP BY site_id HAVING count(*) > 1 ORDER BY site_id LIMIT 1"
    ).fetchone()
    other_site = count(old, "SELECT min(site_id) FROM sampling_site WHERE site_id != ?", site_id)
    shift_all(old)
    with old:
        old.execute("UPDATE sampling_event SET site_id = ? WHERE event_id = ?", (other_site, first))
    assert differences(old, db_path) == {"sampling_event": (1, 1)}
    assert first != second


def test_lab_renamed(old, db_path):
    """Справочник: переименовали лабораторию — узнаётся по тем же отправкам, без каскада."""
    shift_all(old)
    with old:
        old.execute("UPDATE lab SET name = 'Россия (ЗИН)' WHERE name = 'Россия'")
    assert differences(old, db_path) == {"lab": (1, 1)}


def swap_lab_names(conn, first, second):
    with conn:
        conn.execute("UPDATE lab SET name = 'временно' WHERE name = ?", (first,))
        conn.execute("UPDATE lab SET name = ? WHERE name = ?", (first, second))
        conn.execute("UPDATE lab SET name = ? WHERE name = 'временно'", (second,))


def test_two_labs_swapped_names(old, db_path):
    """«Россия» и «Кантхо» обменялись названиями: отправки по смыслу ушли в другие
    лаборатории — это видно (ничего не «склеено» по названию)."""
    swap_lab_names(old, "Россия", "Кантхо (секвенирование)")
    assert differences(old, db_path)["sample_shipment"] != (0, 0)


def test_labs_with_equal_shipments_swapped(old, db_path):
    """«Россия» и «Тропцентр» получили одни и те же образцы (по 236): обмен названиями
    даёт базу, совпадающую с исходной с точностью до номеров, — расхождений нет."""
    swap_lab_names(old, "Россия", "Тропцентр")
    assert differences(old, db_path) == {}


def test_all_smears_changed_shipments_unchanged(old, db_path):
    """Изменён фиксатор у всех мазков и сдвинуты номера: отправки мазков — те же."""
    smears = count(old, "SELECT count(*) FROM sample WHERE sample_type = 'мазок крови'")
    assert count(
        old,
        "SELECT count(*) FROM sample_shipment JOIN sample USING (sample_id) "
        "WHERE sample_type = 'мазок крови'",
    )  # у мазков есть отправки — есть что «потерять»
    with old:
        old.execute("UPDATE sample SET preservative = 'другой' WHERE sample_type = 'мазок крови'")
        # удалить первый образец без отправок: номера расходятся не «ровно», как при новом
        # образце «ХХ» в начале импорта (на этом прежнее сопоставление «по порядку» ошибалось)
        old.execute(
            "DELETE FROM sample WHERE sample_id = (SELECT min(sample_id) FROM sample "
            "WHERE sample_id NOT IN (SELECT sample_id FROM sample_shipment))"
        )
    shift_all(old)
    assert differences(old, db_path) == {"sample": (smears, smears + 1)}


def test_self_reference_is_error(old, db_path):
    """Запись журнала о записи журнала — сравнить нельзя: ошибка, а не KeyError."""
    with old:
        old.execute(
            "UPDATE data_issue SET table_name = 'data_issue', record_id = issue_id "
            "WHERE issue_id = (SELECT min(issue_id) FROM data_issue)"
        )
    with pytest.raises(sqlite3.DatabaseError, match="той же таблицы"):
        differences(old, db_path)
