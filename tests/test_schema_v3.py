"""Тесты перехода на схему v3 (этап D3, часть 1).

- откуда запись журнала проблем: data_issue.source = импорт / проверка / пользователь;
- created_by / created_at: у записей из Excel пусто, у введённых программой — время ввода;
- «кто определил» — ссылка на справочник людей (person), а не текст;
- метка образца уникальна в пределах особи, типа и органа — понятная ошибка вместо
  IntegrityError из базы;
- resolved_at — когда закрыта запись журнала.

Каждый тест работает с копией импортированной базы во временной папке (tmp_path).
"""

import re
import shutil

import pytest
from conftest import SOURCE, count

from biobank import browse, entry, records
from biobank.card import specimen_card
from biobank.checks import (
    CHECK_DESCRIPTIONS,
    DESC_SL_GT_TL,
    SOURCE_CHECK,
    SOURCE_IMPORT,
    SOURCE_USER,
    run_checks,
)
from biobank.db import connect
from biobank.edit import EditError, find_or_create_person, identify, resolve_issue

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")

# формат отметок времени в схеме: 'ГГГГ-ММ-ДД ЧЧ:ММ:СС' (UTC)
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")

# таблицы с колонкой created_at (в схеме v3)
CREATED_TABLES = (
    "region", "city", "sampling_site", "gear", "species", "lab", "expedition",
    "sampling_event", "trawling", "water_measurement", "specimen", "species_identification",
    "sample", "sample_shipment", "analysis", "catch_record", "data_issue",
)  # fmt: skip


@pytest.fixture
def conn(db_path, tmp_path):
    """Подключение к своей копии импортированной базы."""
    path = tmp_path / "v3.db"
    shutil.copy(db_path, path)
    c = connect(path)
    yield c
    c.close()


@pytest.fixture
def event(conn):
    """Новый выезд-траление (введён программой, а не из Excel) с тралением 1."""
    expedition = entry.create_expedition(conn, "Кантхо 2027").record_id
    site = count(conn, "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'")
    event_id = entry.create_event(conn, expedition, site, "2026-09-01", "траление").record_id
    entry.add_trawling(conn, event_id, 1, time_start="9:30")
    return event_id


def expect_error(conn, match, func, *args, **kwargs):
    """func(conn, ...) — EditError с текстом match; база не изменилась."""
    before = count(conn, "SELECT total_changes()")
    with pytest.raises(EditError, match=match):
        func(conn, *args, **kwargs)
    assert not conn.in_transaction
    assert count(conn, "SELECT total_changes()") == before


def person_name(conn, identification_id):
    return count(
        conn,
        "SELECT p.full_name FROM species_identification si "
        "LEFT JOIN person p ON p.person_id = si.identified_by WHERE si.identification_id = ?",
        identification_id,
    )


def last_identification(conn, label):
    return count(
        conn,
        "SELECT max(identification_id) FROM species_identification "
        "JOIN specimen USING (specimen_id) WHERE label = ?",
        label,
    )


# ---------------------------------------------------------------------------
# data_issue.source
# ---------------------------------------------------------------------------


def test_import_fills_issue_source(conn):
    """После импорта у каждой записи журнала есть источник: импорт или проверка."""
    by_source = dict(conn.execute("SELECT source, count(*) FROM data_issue GROUP BY source"))
    assert set(by_source) == {SOURCE_IMPORT, SOURCE_CHECK}
    assert by_source[SOURCE_IMPORT] > 0 and by_source[SOURCE_CHECK] > 0
    # записи проверок — с постоянным текстом описания одной из проверок
    for (description,) in conn.execute(
        "SELECT description FROM data_issue WHERE source = ?", (SOURCE_CHECK,)
    ):
        assert description.startswith(CHECK_DESCRIPTIONS)
    # «число стало датой» (SL 155 pc) пишет импорт, а не проверка
    assert (
        count(
            conn,
            "SELECT source FROM data_issue WHERE object_label = '155 pc' AND category = 'формат'",
        )
        == SOURCE_IMPORT
    )


def test_checks_ignore_user_issue_with_check_text(conn):
    """Запись человека с тем же текстом, что у проверки, проверки не трогают:
    источник определяется колонкой source, а не текстом описания."""
    issue = conn.execute(
        "INSERT INTO data_issue (category, table_name, object_label, description) "
        "VALUES ('промеры', 'specimen', '1 pc', ?)",
        (DESC_SL_GT_TL,),
    ).lastrowid
    conn.commit()
    assert count(conn, "SELECT source FROM data_issue WHERE issue_id = ?", issue) == SOURCE_USER
    with conn:
        run_checks(conn)  # проверка эту «проблему» не находит — но и не закрывает чужую запись
    assert count(conn, "SELECT resolved FROM data_issue WHERE issue_id = ?", issue) == 0


def test_check_closes_own_issue_with_resolved_at(conn):
    """Исправили промер — запись проверки закрылась сама, с временем закрытия."""
    issue = count(
        conn,
        "SELECT issue_id FROM data_issue WHERE source = ? AND description = ? AND resolved = 0 "
        "AND object_label = '262'",
        SOURCE_CHECK,
        DESC_SL_GT_TL,
    )
    with conn:
        conn.execute("UPDATE specimen SET sl_cm = tl_cm - 1 WHERE label = '262'")
        run_checks(conn)
    row = conn.execute("SELECT resolved, resolved_at FROM data_issue WHERE issue_id = ?", (issue,))
    resolved, resolved_at = row.fetchone()
    assert resolved == 1 and TIMESTAMP.fullmatch(resolved_at)


def test_resolve_issue_sets_resolved_at(conn):
    issue = count(conn, "SELECT min(issue_id) FROM data_issue WHERE resolved = 0")
    resolve_issue(conn, issue, "сверено с полевым журналом")
    resolved_at = count(conn, "SELECT resolved_at FROM data_issue WHERE issue_id = ?", issue)
    assert TIMESTAMP.fullmatch(resolved_at)


# ---------------------------------------------------------------------------
# created_by / created_at
# ---------------------------------------------------------------------------


def test_imported_records_have_no_author_and_time(conn):
    """NULL в created_by / created_at = «перенесено из Excel» (решение по схеме v3)."""
    for table in CREATED_TABLES:
        # имя таблицы — из списка в этом файле, не от пользователя
        filled = count(
            conn,
            f"SELECT count(*) FROM {table} WHERE created_by IS NOT NULL OR created_at IS NOT NULL",
        )
        assert filled == 0, table
    assert count(conn, "SELECT count(*) FROM species_identification "
                       "WHERE identified_by IS NOT NULL") == 0  # fmt: skip
    for table in ("person", "app_user", "user_role", "expedition_member", "change_log"):
        assert count(conn, f"SELECT count(*) FROM {table}") == 0, table


def test_entered_records_get_created_at(conn, event):
    """Записи, введённые программой, получают время ввода (UTC); автор — пока пусто."""
    specimen = entry.create_specimen(conn, event, "Plotosus canius", trawl_no=1).record_id
    entry.add_sample(conn, "179 pc", "ДНК", labs=["Россия"])
    for table, where in (
        ("expedition", "name = 'Кантхо 2027'"),
        ("sampling_event", f"event_id = {event}"),
        ("trawling", f"event_id = {event}"),
        ("specimen", f"specimen_id = {specimen}"),
        ("species_identification", f"specimen_id = {specimen}"),
        ("sample", f"specimen_id = {specimen}"),
    ):
        row = conn.execute(f"SELECT created_at, created_by FROM {table} WHERE {where}").fetchone()
        assert TIMESTAMP.fullmatch(row["created_at"]), table
        assert row["created_by"] is None, table  # кто внёс — после входа на сайт (часть 2)


# ---------------------------------------------------------------------------
# «Кто определил» — справочник людей
# ---------------------------------------------------------------------------


def test_find_or_create_person(conn):
    assert find_or_create_person(conn, None) == (None, False)
    assert find_or_create_person(conn, "   ") == (None, False)
    person, created = find_or_create_person(conn, "  Слынько   Е.Е. ")
    assert created
    assert count(conn, "SELECT full_name FROM person WHERE person_id = ?", person) == "Слынько Е.Е."
    assert find_or_create_person(conn, "Слынько Е.Е.") == (person, False)  # тот же человек
    conn.rollback()


def test_find_person_ambiguous_name(conn):
    """Одно имя в двух организациях — по имени не понять, кто это: ошибка, а не угадывание."""
    conn.executemany(
        "INSERT INTO person (full_name, organization) VALUES ('Нгуен Т.', ?)",
        [("Тропцентр",), ("Университет Кантхо",)],
    )
    with pytest.raises(EditError, match="несколько «Нгуен Т.»"):
        find_or_create_person(conn, "Нгуен Т.")
    conn.rollback()


def test_identify_links_person_and_card_shows_name(conn):
    result = identify(conn, "1 pc", "Plotosus canius", "ДНК", "точно", identified_by="Иванова А.")
    assert "добавлен(а) «Иванова А.»" in result.message
    first = last_identification(conn, "1 pc")
    assert person_name(conn, first) == "Иванова А."

    # второе определение тем же человеком — без нового человека в справочнике
    result = identify(conn, "2 pc", "Plotosus canius", "ДНК", "точно", identified_by="Иванова А.")
    assert "справочник" not in result.message
    assert count(conn, "SELECT count(*) FROM person") == 1
    assert browse.list_people(conn)[0]["identification_count"] == 2

    # карточка показывает имя, а не номер человека
    section = next(s for s in specimen_card(conn, "1 pc") if s.title == "История определений вида")
    who = section.columns.index("кто")
    assert section.rows[-1][who] == "Иванова А."
    assert browse.get_record(conn, "identification", first)["identified_by_name"] == "Иванова А."


def test_identify_without_person(conn):
    identify(conn, "1 pc", "Plotosus canius", "ДНК", "точно")
    assert person_name(conn, last_identification(conn, "1 pc")) is None
    assert count(conn, "SELECT count(*) FROM person") == 0


def test_create_specimen_links_person(conn, event):
    result = entry.create_specimen(conn, event, "Plotosus canius", identified_by="Петров")
    assert "В справочник людей добавлены: Петров" in result.message
    assert person_name(conn, last_identification(conn, "179 pc")) == "Петров"


def test_batch_creates_person_once(conn, event):
    rows = [
        {"species": "Plotosus canius", "identified_by": "Петров"},
        {"species": "Plotosus canius", "identified_by": "Петров"},
    ]
    result = entry.create_specimens(conn, event, rows)
    assert result.message.count("Петров") == 1
    assert count(conn, "SELECT count(*) FROM person WHERE full_name = 'Петров'") == 1
    assert person_name(conn, last_identification(conn, "180 pc")) == "Петров"


def test_check_specimen_does_not_create_person(conn, event):
    """Предпросмотр только читает базу: нового человека не создаёт."""
    label, check = entry.check_specimen(conn, event, "Plotosus canius", identified_by="Петров")
    assert (label, check.errors) == ("179 pc", [])
    assert count(conn, "SELECT count(*) FROM person") == 0


# ---------------------------------------------------------------------------
# Уникальная метка образца (ux_sample_label)
# ---------------------------------------------------------------------------


def test_add_sample_duplicate_label(conn):
    """У 1 pc из Excel уже есть гистология печени с меткой «1 pc»."""
    expect_error(
        conn, r"У особи 1 pc уже есть образец «1 pc» \(гистология, печень\)",
        entry.add_sample, "1 pc", "гистология", organ="печень",
    )  # fmt: skip
    # ДНК без органа — тоже уже есть (пустой орган равен пустому)
    expect_error(conn, r"«1 pc» \(ДНК\)", entry.add_sample, "1 pc", "ДНК")
    # тот же тип и метка, но другой орган — можно
    entry.add_sample(conn, "1 pc", "гистология", organ="почки")


def test_update_sample_to_duplicate(conn):
    sample = entry.add_sample(conn, "1 pc", "гистология", organ="почки").record_id
    expect_error(
        conn, r"уже есть образец «1 pc» \(гистология, печень\)",
        records.update_sample, sample, organ="печень",
    )  # fmt: skip
    # без изменений «в себя» — не дубль
    records.update_sample(conn, sample, organ="почки", label="1 pc")


def test_batch_duplicate_samples_in_one_row(conn, event):
    rows = [
        {
            "species": "Plotosus canius",
            "samples": [
                {"sample_type": "гистология", "organ": "печень"},
                {"sample_type": "гистология", "organ": "печень"},
            ],
        }
    ]
    with pytest.raises(EditError, match="уже есть образец .* в этом вводе"):
        entry.create_specimens(conn, event, rows)
    assert count(conn, "SELECT count(*) FROM specimen WHERE label = '179 pc'") == 0
