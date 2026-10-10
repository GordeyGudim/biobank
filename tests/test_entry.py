"""Тесты ввода новых данных (entry.py, этап 4 ROADMAP).

Каждый тест работает с КОПИЕЙ импортированной базы (фикстура entry_db).
Для каждой функции проверяем:
- успешное создание (и id созданной записи в EditResult.record_id);
- жёсткие ошибки: EditError, в базе ничего не изменилось (сравниваем «снимок» базы);
- мягкие предупреждения: запись сохранена, а проблема появилась в журнале data_issue.
"""

import shutil
import sqlite3

import pytest
from conftest import SOURCE, count

from biobank import entry
from biobank.__main__ import main
from biobank.checks import (
    DESC_CONDITION,
    DESC_COPIED,
    DESC_GUTTED_GT_MASS,
    DESC_NO_TRAWL,
    DESC_SL_GT_TL,
    DESC_SL_TOO_SMALL,
    DESC_SUFFIX,
    measurement_problems,
)
from biobank.db import connect
from biobank.edit import EditError, NotFoundError

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")

# точка рыбака 20–22.08.2026 (критерии приёмки): одно место на три выезда
FISHER_LAT, FISHER_LON = 9.5187059, 106.2082146


@pytest.fixture
def entry_path(db_path, tmp_path):
    path = tmp_path / "output" / "biobank.db"
    path.parent.mkdir()
    shutil.copy(db_path, path)
    return path


@pytest.fixture
def entry_db(entry_path):
    conn = connect(entry_path)
    yield conn
    conn.close()


def snapshot(conn):
    """Все строки всех таблиц — чтобы убедиться, что после ошибки база не изменилась."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    # имя таблицы — из sqlite_master самой базы, не от пользователя
    return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}"))) for t in tables}


def site_id(conn, name):
    return count(conn, "SELECT site_id FROM sampling_site WHERE name = ?", name)


def event_id(conn, sheet):
    return count(conn, "SELECT event_id FROM sampling_event WHERE source_sheet = ?", sheet)


def open_issues(conn, description, record_id=None):
    """Открытые проблемы журнала с этим описанием (и, если указан, record_id)."""
    rows = conn.execute(
        "SELECT * FROM data_issue WHERE resolved = 0 AND description = ?", (description,)
    ).fetchall()
    return [r for r in rows if record_id is None or r["record_id"] == record_id]


@pytest.fixture
def trawl_event(entry_db):
    """Новая экспедиция → выезд-траление на «Участок у Кантхо» → траления 1 и 2."""
    expedition = entry.create_expedition(entry_db, "Кантхо 2027").record_id
    event = entry.create_event(
        entry_db, expedition, site_id(entry_db, "Участок у Кантхо"), "2026-09-01", "траление",
        gear_mesh="3,5х3,5", gear_size="6,5x12",
    ).record_id  # fmt: skip
    for n in (1, 2):
        entry.add_trawling(entry_db, event, n, time_start="9.30", time_end="9:50")
    return event


def expect_error(conn, match, func, *args, error=EditError, **kwargs):
    """func(...) падает с EditError, и в базе ничего не изменилось."""
    before = snapshot(conn)
    with pytest.raises(error, match=match):
        func(conn, *args, **kwargs)
    assert not conn.in_transaction
    assert snapshot(conn) == before


# ---------------------------------------------------------------------------
# Экспедиция
# ---------------------------------------------------------------------------


def test_create_expedition(entry_db):
    result = entry.create_expedition(entry_db, "  Кантхо   2027 ", notes="пробная")
    row = entry_db.execute(
        "SELECT * FROM expedition WHERE expedition_id = ?", (result.record_id,)
    ).fetchone()
    assert row["name"] == "Кантхо 2027"
    assert (row["date_start"], row["date_end"], row["notes"]) == (None, None, "пробная")
    assert "Кантхо 2027" in result.message


@pytest.mark.parametrize(
    "name, start, end, match",
    [
        ("Кантхо 2026", None, None, "уже есть"),  # имя из импорта
        ("   ", None, None, "Укажите название"),
        ("X", "2026-09-10", "2026-09-01", "позже"),
        ("X", "2099-01-01", None, "ещё не наступила"),
        ("X", "10 сентября", None, "не распознана"),
    ],
)
def test_create_expedition_errors(entry_db, name, start, end, match):
    expect_error(entry_db, match, entry.create_expedition, name, start, end)


# ---------------------------------------------------------------------------
# Места и дедупликация
# ---------------------------------------------------------------------------


def test_create_site_new_city(entry_db):
    result = entry.create_site(
        entry_db, "участок реки", city="Кай-Ранг", region="Кантхо", lat="10,1", lon=105.7
    )
    row = entry_db.execute(
        "SELECT s.*, c.name AS city FROM sampling_site s JOIN city c USING (city_id) "
        "WHERE site_id = ?",
        (result.record_id,),
    ).fetchone()
    assert (row["name"], row["city"], row["lat"]) == ("Участок у Кай-Ранг", "Кай-Ранг", 10.1)
    assert "город «Кай-Ранг»" in result.message


def test_create_site_city_normalized(entry_db):
    """«Хонг-на» — то же, что «Хонг-нгу» (как при импорте): такой участок уже есть."""
    expect_error(entry_db, "«Участок у Хонг-нгу»", entry.create_site, "участок реки",
                 city="Хонг-на", region="Донг-хап")  # fmt: skip


def test_create_site_existing_city_not_duplicated(entry_db):
    cities = count(entry_db, "SELECT count(*) FROM city")
    entry.create_site(entry_db, "рынок", city="Бенче", region="Виньлонг", allow_nearby=True)
    assert count(entry_db, "SELECT count(*) FROM city") == cities


def test_river_site_same_name_is_duplicate(entry_db):
    """Участки реки с одним названием — одно место, как бы далеко ни были координаты."""
    expect_error(
        entry_db, "Участок у Кантхо", entry.create_site, "участок реки",
        city="Кантхо", region="Кантхо", lat=11.0, lon=106.5,
    )  # fmt: skip


def test_fisher_point_nearby_is_duplicate(entry_db):
    """Точка в ~15 м от известной точки рыбака — не создаём дубль, а предлагаем её."""
    known = count(
        entry_db,
        "SELECT site_id FROM sampling_site WHERE site_type = 'точка рыбака' "
        "AND abs(lat - ?) < 1e-6 AND abs(lon - ?) < 1e-6",
        FISHER_LAT, FISHER_LON,
    )  # fmt: skip
    nearby = entry.find_nearby_sites(entry_db, "точка рыбака", 9.5188, 106.2083)
    assert [s.site_id for s in nearby] == [known]
    assert nearby[0].distance_km < 0.05
    expect_error(
        entry_db, f"№{known} .*м\\)", entry.create_site, "точка рыбака",
        name="Новая точка", lat=9.5188, lon=106.2083,
    )  # fmt: skip


def test_nearby_site_allowed_with_flag(entry_db):
    result = entry.create_site(
        entry_db, "точка рыбака", name="Другой рыбак", lat=9.5188, lon=106.2083, allow_nearby=True
    )
    assert result.record_id is not None
    assert result.warnings and "Рядом есть место" in result.warnings[0]


def test_far_site_is_not_nearby(entry_db):
    assert entry.find_nearby_sites(entry_db, "точка рыбака", 9.53, 106.2082) == []  # ~1,2 км
    assert entry.find_nearby_sites(entry_db, "аквахозяйство", FISHER_LAT, FISHER_LON) == []


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"site_type": "озеро", "name": "X"}, "Тип места"),
        ({"site_type": "точка рыбака", "lat": 9.5}, "обе"),
        ({"site_type": "точка рыбака", "lat": 95, "lon": 106}, "Широта не может быть больше 90"),
        ({"site_type": "участок реки"}, "Укажите название"),
        ({"site_type": "участок реки", "city": "Тест"}, "провинцию"),
        (
            {"site_type": "участок реки", "city": "Тест", "region": "Нет такой"},
            "это новая провинция",
        ),
        ({"site_type": "рынок", "name": "Рынок X", "region": "Кантхо"}, "вместе с городом"),
    ],
)
def test_create_site_errors(entry_db, kwargs, match):
    kwargs = dict(kwargs)
    expect_error(entry_db, match, entry.create_site, kwargs.pop("site_type"), **kwargs)


def test_create_site_new_region(entry_db):
    result = entry.create_site(entry_db, "рынок", city="Шокчанг", region="Шокчанг", new_region=True)
    assert "провинция «Шокчанг»" in result.message
    assert count(entry_db, "SELECT count(*) FROM region") == 4


# ---------------------------------------------------------------------------
# Выезды
# ---------------------------------------------------------------------------


def test_create_event(entry_db):
    expedition = entry.create_expedition(entry_db, "Кантхо 2027").record_id
    gears = count(entry_db, "SELECT count(*) FROM gear")
    result = entry.create_event(
        entry_db, expedition, site_id(entry_db, "Участок у Кантхо"), "01.09.2026", "траление",
        gear_mesh="3,5 х 3,5", gear_size="6,5x12",
    )  # fmt: skip
    event = entry_db.execute(
        "SELECT e.*, g.mesh_cm FROM sampling_event e JOIN gear g USING (gear_id) "
        "WHERE event_id = ?",
        (result.record_id,),
    ).fetchone()
    assert (event["event_date"], event["source_sheet"], event["mesh_cm"]) == (
        "2026-09-01", None, "3,5x3,5"
    )  # fmt: skip
    assert count(entry_db, "SELECT count(*) FROM gear") == gears  # та же сеть, что в 2026
    dates = entry_db.execute(
        "SELECT date_start, date_end FROM expedition WHERE expedition_id = ?", (expedition,)
    ).fetchone()
    assert tuple(dates) == ("2026-09-01", "2026-09-01")  # даты экспедиции — по выезду


def test_event_outside_expedition_dates_extends_with_warning(entry_db):
    expedition = entry.create_expedition(entry_db, "X", "2026-09-01", "2026-09-05").record_id
    result = entry.create_event(
        entry_db, expedition, site_id(entry_db, "Участок у Кантхо"), "2026-09-07", "траление"
    )
    assert any("вне дат экспедиции" in w for w in result.warnings)
    assert (
        count(entry_db, "SELECT date_end FROM expedition WHERE expedition_id = ?", expedition)
        == "2026-09-07"
    )


@pytest.mark.parametrize(
    "site, method, kwargs, match",
    [
        ("Участок у Кантхо", "рыбак", {}, "нужно место типа «точка рыбака»"),
        ("Рынок Бенче", "рынок", {"gear_mesh": "5x5"}, "только у тралений"),
        ("Участок у Кантхо", "сеть", {}, "Способа получения «сеть» нет"),
    ],
)
def test_create_event_errors(entry_db, site, method, kwargs, match):
    expedition = entry.create_expedition(entry_db, "X").record_id
    expect_error(
        entry_db, match, entry.create_event, expedition, site_id(entry_db, site),
        "2026-09-01", method, **kwargs,
    )  # fmt: skip


def test_create_event_missing_records(entry_db):
    site = site_id(entry_db, "Участок у Кантхо")
    expect_error(entry_db, "Экспедиции №999", entry.create_event, 999, site, "2026-09-01",
                 "траление", error=NotFoundError)  # fmt: skip
    expect_error(entry_db, "Места №999", entry.create_event, 1, 999, "2026-09-01", "траление",
                 error=NotFoundError)  # fmt: skip


def test_duplicate_event_refused(trawl_event, entry_db):
    expedition = count(
        entry_db, "SELECT expedition_id FROM sampling_event WHERE event_id = ?", trawl_event
    )
    expect_error(
        entry_db, f"уже есть: №{trawl_event}", entry.create_event, expedition,
        site_id(entry_db, "Участок у Кантхо"), "2026-09-01", "траление",
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Траления и вода
# ---------------------------------------------------------------------------


def test_add_trawling(trawl_event, entry_db):
    result = entry.add_trawling(
        entry_db, trawl_event, 3, time_start="10.05", duration_min="20 min", speed_kmh="3,5",
        catch_note="2 plotosus",
    )  # fmt: skip
    row = entry_db.execute(
        "SELECT * FROM trawling WHERE trawling_id = ?", (result.record_id,)
    ).fetchone()
    assert (row["time_start"], row["duration_min"], row["speed_kmh"]) == ("10:05", 20.0, 3.5)


@pytest.mark.parametrize(
    "trawl_no, kwargs, match",
    [
        (1, {}, "уже есть"),
        (0, {}, "больше 0"),
        (3, {"time_start": "25:00"}, "не время"),
        (3, {"speed_kmh": "-1"}, "меньше 0"),
    ],
)
def test_add_trawling_errors(trawl_event, entry_db, trawl_no, kwargs, match):
    expect_error(entry_db, match, entry.add_trawling, trawl_event, trawl_no, **kwargs)


def test_trawling_only_for_trawl_events(entry_db):
    expect_error(entry_db, "не траление", entry.add_trawling, event_id(entry_db, "Рынок.Бенче"), 1)


def test_add_water_measurement(trawl_event, entry_db):
    result = entry.add_water_measurement(
        entry_db, trawl_event, "старт", 1, lat="10,05", lon=105.78, coord_source="garmin",
        ph="7,2", o2_pct=85, notes="hanna – 4,45",
    )  # fmt: skip
    row = entry_db.execute(
        "SELECT w.*, t.trawl_no FROM water_measurement w JOIN trawling t USING (trawling_id) "
        "WHERE measurement_id = ?",
        (result.record_id,),
    ).fetchone()
    assert (row["trawl_no"], row["point_type"], row["coord_source"], row["ph"]) == (
        1, "старт", "Garmin", 7.2
    )  # fmt: skip
    point = entry.add_water_measurement(entry_db, trawl_event, "точка", lat=10.0, lon=105.0)
    assert count(
        entry_db, "SELECT trawling_id FROM water_measurement WHERE measurement_id = ?",
        point.record_id,
    ) is None  # fmt: skip


@pytest.mark.parametrize(
    "point, trawl_no, kwargs, match",
    [
        ("старт", None, {}, "укажите номер траления"),
        ("точка", 1, {}, "не указывают"),
        ("середина", 1, {}, "Тип точки"),
        ("финиш", 7, {}, "нет траления 7. Есть: 1, 2"),
        ("финиш", 1, {"ph": 15}, "pH не может быть больше 14"),
        ("финиш", 1, {"lat": 10}, "обе"),
        ("финиш", 1, {"salinity": 3}, "Неизвестные поля"),
        ("финиш", 1, {"coord_source": "GPS"}, "Источник координат"),
    ],
)
def test_add_water_errors(trawl_event, entry_db, point, trawl_no, kwargs, match):
    expect_error(entry_db, match, entry.add_water_measurement, trawl_event, point, trawl_no,
                 **kwargs)  # fmt: skip


def test_water_duplicate_point_refused(trawl_event, entry_db):
    entry.add_water_measurement(entry_db, trawl_event, "финиш", 2)
    expect_error(entry_db, "уже есть", entry.add_water_measurement, trawl_event, "финиш", 2)


def test_water_trawl_of_other_event_not_found(trawl_event, entry_db):
    """Траление ищется по номеру внутри выезда: у «Точка 4» трал 3 есть, у нового — нет."""
    expect_error(entry_db, "нет траления 3", entry.add_water_measurement, trawl_event, "старт", 3)


# ---------------------------------------------------------------------------
# Номера проб
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix, expected",
    [("pc", "179 pc"), ("рс", "179 pc"), ("ph", "278 ph"), ("e", "278 e"), ("", "278"),
     ("Bg", "6 Bg"), ("mr", "6 mr")],
)  # fmt: skip
def test_next_label(entry_db, suffix, expected):
    assert entry.next_label(entry_db, suffix) == expected


def test_next_label_unknown_suffix(entry_db):
    with pytest.raises(EditError, match="не известен"):
        entry.next_label(entry_db, "xx")


# ---------------------------------------------------------------------------
# Особи
# ---------------------------------------------------------------------------


def specimen(conn, label):
    return conn.execute("SELECT * FROM specimen WHERE label = ?", (label,)).fetchone()


def test_create_specimen(trawl_event, entry_db):
    result = entry.create_specimen(
        entry_db, trawl_event, "Plotosus canius", tl_cm="25,4", sl_cm=22, mass_g=150,
        mass_gutted_g=140, trawl_no=1, sex="самка", identified_by="Иванов",
    )  # fmt: skip
    row = specimen(entry_db, "179 pc")  # номер — следующий свободный в серии pc
    assert row["specimen_id"] == result.record_id
    assert (row["series"], row["series_no"], row["tl_cm"], row["label_raw"]) == (
        "pc", 179, 25.4, None
    )  # fmt: skip
    assert count(entry_db, "SELECT trawl_no FROM trawling WHERE trawling_id = ?",
                 row["trawling_id"]) == 1  # fmt: skip
    ident = entry_db.execute(
        "SELECT * FROM species_identification WHERE specimen_id = ?", (row["specimen_id"],)
    ).fetchall()
    assert len(ident) == 1
    assert (ident[0]["method"], ident[0]["confidence"], ident[0]["identified_on"]) == (
        "морфология", "точно", "2026-09-01"
    )  # fmt: skip
    assert ident[0]["species_id"] == row["species_id"]
    assert result.warnings == []
    assert open_issues(entry_db, DESC_NO_TRAWL, trawl_event) == []  # траление указано


def test_create_specimen_defaults(trawl_event, entry_db):
    entry.create_specimen(entry_db, trawl_event, "Pangasius sp.")
    row = specimen(entry_db, "278 sp")
    assert count(
        entry_db, "SELECT confidence FROM species_identification WHERE specimen_id = ?",
        row["specimen_id"],
    ) == "до рода"  # fmt: skip
    entry.create_specimen(entry_db, trawl_event, "?", "279")
    undetermined = specimen(entry_db, "279")
    assert undetermined["species_id"] is None


def test_specimen_without_trawl_gets_link_issue(trawl_event, entry_db):
    entry.create_specimen(entry_db, trawl_event, "Plotosus canius")
    assert len(open_issues(entry_db, DESC_NO_TRAWL, trawl_event)) == 1


def test_specimen_same_method_and_site_as_event_not_stored(trawl_event, entry_db):
    entry.create_specimen(
        entry_db, trawl_event, "Plotosus canius", capture_method="траление",
        capture_site_id=site_id(entry_db, "Участок у Кантхо"), capture_date="2026-09-01",
    )  # fmt: skip
    row = specimen(entry_db, "179 pc")
    assert (row["capture_method_id"], row["capture_site_id"], row["capture_date"]) == (
        None, None, None
    )  # fmt: skip


def test_specimen_from_fisher_inside_trawl_event(trawl_event, entry_db):
    fisher = site_id(entry_db, "Точка рыбака (9.5187, 106.2082)")
    entry.create_specimen(entry_db, trawl_event, "Plotosus canius", capture_method="рыбак",
                          capture_site_id=fisher)  # fmt: skip
    row = specimen(entry_db, "179 pc")
    assert row["capture_site_id"] == fisher and row["capture_method_id"] is not None


@pytest.mark.parametrize(
    "species, kwargs, match",
    [
        ("Plotosus canius", {"label": "5 pc"}, "уже занят. Следующий свободный: 179 pc"),
        ("Plotosus canius", {"label": "5pc"}, "уже занят"),  # нормализуется
        ("Plotosus canius", {"tl_cm": 0}, "TL должна быть больше 0"),
        ("Plotosus canius", {"mass_g": "-3"}, "m не может быть меньше 0"),
        ("Plotosus canius", {"tl_cm": "двадцать"}, "не число"),
        ("Plotosus canius", {"trawl_no": 5}, "нет траления 5"),
        ("Plotosus canius", {"label": "ХХ"}, "не распознан"),
        ("Plotosus canius", {"sex": "м"}, "Пол"),
        ("Plotosus canius", {"capture_method": "рыбак", "trawl_no": 1}, "не тралением"),
        ("Plotosus canius", {"confidence": "не определён"}, "вместо вида"),
        ("?", {"confidence": "точно"}, "только уверенность"),
        ("?", {}, "нет кода"),  # без вида номер сам не подобрать
        ("Pangasius conchophilus", {}, "нет кода"),
        ("Esox lucius", {}, "нет в справочнике"),
        ("Plotosus canius", {"capture_site_id": 999}, "Места №999"),
        ("Plotosus canius", {"capture_date": "2099-01-01"}, "ещё не наступила"),
    ],
)
def test_create_specimen_errors(trawl_event, entry_db, species, kwargs, match):
    expect_error(entry_db, match, entry.create_specimen, trawl_event, species, **kwargs)


def test_create_specimen_missing_event(entry_db):
    expect_error(entry_db, "Выезда №999", entry.create_specimen, 999, "Plotosus canius",
                 error=NotFoundError)  # fmt: skip


def test_trawling_of_other_event_rejected_by_schema(trawl_event, entry_db):
    """Составной внешний ключ: даже в обход entry.py особь не привязать к чужому тралению."""
    entry.create_specimen(entry_db, trawl_event, "Plotosus canius")
    other = count(
        entry_db, "SELECT trawling_id FROM trawling WHERE event_id = ?",
        event_id(entry_db, "Точка 4"),
    )  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with entry_db:
            entry_db.execute("UPDATE specimen SET trawling_id = ? WHERE label = '179 pc'", (other,))


@pytest.mark.parametrize(
    "values, description",
    [
        ({"tl_cm": 10.2, "sl_cm": 10.3}, DESC_SL_GT_TL),
        ({"mass_g": 28, "mass_gutted_g": 34}, DESC_GUTTED_GT_MASS),
        ({"tl_cm": 93, "mass_g": 141}, DESC_CONDITION),
    ],
)
def test_soft_warning_saved_and_logged(trawl_event, entry_db, values, description):
    result = entry.create_specimen(entry_db, trawl_event, "Plotosus canius", **values)
    assert description in result.warnings
    assert specimen(entry_db, "179 pc") is not None  # запись сохранена
    assert len(open_issues(entry_db, description, result.record_id)) == 1
    assert result.checks.added >= 1


def test_suffix_mismatch_logged(trawl_event, entry_db):
    result = entry.create_specimen(entry_db, trawl_event, "P. elongatus", "300 ph")
    assert any(w.startswith(DESC_SUFFIX) for w in result.warnings)
    assert any("пропущены" in w for w in result.warnings)  # после 277 сразу 300
    assert len(open_issues(entry_db, DESC_SUFFIX, result.record_id)) == 1


def test_number_reused_with_other_suffix_warns(trawl_event, entry_db):
    result = entry.create_specimen(entry_db, trawl_event, "Pangasius sp.", "26 sp")  # есть 26 ph
    assert any("другим суффиксом: 26 ph" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# validate_specimen и check_measurements — одни пороги
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tl, sl, m, mg, expected",
    [
        (10.2, 10.3, 11.0, 8.6, DESC_SL_GT_TL),
        (21.3, 1.8, 46.0, 42.0, DESC_SL_TOO_SMALL),
        (22.0, 20.4, 28.0, 34.0, DESC_GUTTED_GT_MASS),
        (43.0, 37.5, 43.0, 37.5, DESC_COPIED),
        (93.0, 79.0, 141.0, 111.0, DESC_CONDITION),
    ],
)
def test_validate_matches_checks(tl, sl, m, mg, expected):
    check = entry.validate_specimen(tl, sl, m, mg)
    assert check.errors == []
    assert expected in check.warnings
    assert check.warnings == measurement_problems(tl, sl, m, mg)


def test_validate_limits_are_parameters():
    """Пороги можно передать свои (для не-рыб): тогда та же особь — без предупреждения."""
    from biobank.checks import MeasurementLimits

    loose = MeasurementLimits(condition_min=0.01, condition_max=100, sl_min_share=0.01)
    assert entry.validate_specimen(93.0, 79.0, 141.0, 111.0, limits=loose).warnings == []


# ---------------------------------------------------------------------------
# Исправление промеров
# ---------------------------------------------------------------------------


def test_update_measurements_closes_issue(entry_db):
    """160: TL 3,1 < SL 5,9 — исправили TL, проблема в журнале закрылась сама."""
    assert len(open_issues(entry_db, DESC_SL_GT_TL)) >= 1
    before = len(open_issues(entry_db, DESC_SL_GT_TL))
    result = entry.update_measurements(entry_db, "160", tl_cm="6,1")
    assert "TL 3.1 → 6.1" in result.message
    assert specimen(entry_db, "160")["tl_cm"] == 6.1
    assert len(open_issues(entry_db, DESC_SL_GT_TL)) == before - 1
    assert result.checks.closed >= 1


def test_update_measurements_clear_value(entry_db):
    entry.update_measurements(entry_db, "1 pc", mass_gutted_g="-")
    assert specimen(entry_db, "1 pc")["mass_gutted_g"] is None


def test_update_measurements_soft_warning(entry_db):
    result = entry.update_measurements(entry_db, "1 pc", sl_cm=999)
    assert DESC_SL_GT_TL in result.warnings
    assert len(open_issues(entry_db, DESC_SL_GT_TL, result.record_id)) == 1


@pytest.mark.parametrize(
    "label, changes, match",
    [
        ("1 pc", {"tl_cm": 0}, "больше 0"),
        ("1 pc", {}, "Укажите"),
        ("1 pc", {"weight": 3}, "Неизвестные"),
        ("9999 pc", {"tl_cm": 5}, "не найдена"),
    ],
)
def test_update_measurements_errors(entry_db, label, changes, match):
    expect_error(entry_db, match, entry.update_measurements, label, **changes)


# ---------------------------------------------------------------------------
# Образцы, отправки, анализы, прилов
# ---------------------------------------------------------------------------


def test_add_sample_with_labs(entry_db):
    result = entry.add_sample(entry_db, "1 pc", "ДНК", sample_label="1 pc dna",
                              labs=["Россия", "Тропцентр", "Россия"])  # fmt: skip
    labs = [
        r[0] for r in entry_db.execute(
            "SELECT l.name FROM sample_shipment JOIN lab l USING (lab_id) WHERE sample_id = ? "
            "ORDER BY l.name", (result.record_id,),
        )
    ]  # fmt: skip
    assert labs == ["Россия", "Тропцентр"]


def test_add_sample_default_label(entry_db):
    result = entry.add_sample(entry_db, "1pc", "гистология", organ="печень")
    assert (
        count(entry_db, "SELECT label FROM sample WHERE sample_id = ?", result.record_id) == "1 pc"
    )


@pytest.mark.parametrize(
    "label, sample_type, kwargs, match",
    [
        ("1 pc", "кровь", {}, "Тип образца"),
        ("1 pc", "ДНК", {"sample_label": "26 ph"}, "уже есть у особи 26 ph"),
        ("1 pc", "ДНК", {"labs": ["Марс"]}, "Лаборатории «Марс» нет"),
        ("9999 pc", "ДНК", {}, "не найдена"),
    ],
)
def test_add_sample_errors(entry_db, label, sample_type, kwargs, match):
    expect_error(entry_db, match, entry.add_sample, label, sample_type, **kwargs)


def test_ship_sample(entry_db):
    sample = entry.add_sample(entry_db, "1 pc", "ДНК", sample_label="1 pc dna").record_id
    entry.ship_sample(entry_db, sample, "Кантхо (секвенирование)", "2026-09-20")
    assert count(entry_db, "SELECT sent_on FROM sample_shipment WHERE sample_id = ?",
                 sample) == "2026-09-20"  # fmt: skip
    expect_error(entry_db, "уже отмечен", entry.ship_sample, sample, "Кантхо (секвенирование)")
    expect_error(entry_db, "это новая лаборатория", entry.ship_sample, sample, "Новая лаборатория")
    result = entry.ship_sample(entry_db, sample, "Новая лаборатория", new_lab=True)
    assert "новая лаборатория" in result.message
    expect_error(entry_db, "Образца №999999", entry.ship_sample, 999999, "Россия",
                 error=NotFoundError)  # fmt: skip


def test_add_analysis(entry_db):
    sample = entry.add_sample(entry_db, "1 pc", "ДНК", sample_label="1 pc dna").record_id
    result = entry.add_analysis(entry_db, sample, "секвенирование COI",
                                lab="Кантхо (секвенирование)", accession="PQ123456")  # fmt: skip
    row = entry_db.execute(
        "SELECT * FROM analysis WHERE analysis_id = ?", (result.record_id,)
    ).fetchone()
    assert (row["sample_id"], row["accession"]) == (sample, "PQ123456")
    expect_error(entry_db, "вид анализа", entry.add_analysis, sample, " ")


def test_create_catch_record(trawl_event, entry_db):
    result = entry.create_catch_record(
        entry_db, trawl_event, taxon_text="arius maculatus", count_n="39", trawl_no=1
    )
    row = entry_db.execute(
        "SELECT * FROM catch_record WHERE catch_id = ?", (result.record_id,)
    ).fetchone()
    assert (row["raw_text"], row["count_n"], row["trawling_id"] is not None) == (
        "arius maculatus - 39 шт", 39, True
    )  # fmt: skip
    expect_error(entry_db, "Укажите, что поймали", entry.create_catch_record, trawl_event)
    expect_error(entry_db, "целое", entry.create_catch_record, trawl_event, count_n="2,5")
    expect_error(entry_db, "нет траления 9", entry.create_catch_record, trawl_event,
                 raw_text="краб", trawl_no=9)  # fmt: skip


# ---------------------------------------------------------------------------
# Транзакции, CLI, защита введённых данных от import
# ---------------------------------------------------------------------------


def test_entry_inside_open_transaction_refused(entry_db):
    entry_db.execute("BEGIN")
    with pytest.raises(EditError, match="уже в транзакции"):
        entry.create_expedition(entry_db, "X")
    entry_db.rollback()


def run(path, *args):
    return main(["--db", str(path), *args])


def test_cli_full_entry_sequence(entry_path, capsys):
    """Новая экспедиция от начала до образца — как в примере README."""
    assert run(entry_path, "add-expedition", "Кантхо 2027") == 0
    site = str(site_id(connect(entry_path), "Участок у Кантхо"))
    assert run(entry_path, "add-event", "4", site, "2026-09-01", "траление",
               "--mesh", "3,5x3,5", "--size", "6,5x12") == 0  # fmt: skip
    assert run(entry_path, "add-trawling", "25", "1", "--start", "9:30", "--end", "9:50") == 0
    assert run(entry_path, "add-water", "25", "старт", "--trawl", "1", "--lat", "10,05",
               "--lon", "105,78", "--ph", "7,2") == 0  # fmt: skip
    assert run(entry_path, "next-label", "pc") == 0
    assert capsys.readouterr().out.strip().endswith("179 pc")
    assert run(entry_path, "add-specimen", "25", "Plotosus canius", "--tl", "25,4",
               "--sl", "27", "--trawl", "1") == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "Добавлена особь 179 pc" in out
    assert f"Внимание: {DESC_SL_GT_TL}" in out
    assert "Журнал проблем: новых 1" in out
    assert run(entry_path, "update-measurements", "179 pc", "--sl", "22") == 0
    assert run(entry_path, "add-sample", "179 pc", "ДНК", "--lab", "Россия") == 0
    assert "отправлен: Россия" in capsys.readouterr().out
    assert run(entry_path, "add-catch", "25", "--text", "arius maculatus - 39 шт") == 0
    backups = list((entry_path.parent / "backups").iterdir())
    assert len(backups) == 8  # копия перед каждым вводом


def test_cli_entry_error_leaves_no_backup(entry_path, capsys):
    assert run(entry_path, "add-specimen", "1", "Plotosus canius", "--label", "5 pc") == 1
    err = capsys.readouterr().err
    assert "уже занят" in err and "База не изменена" in err
    assert list((entry_path.parent / "backups").iterdir()) == []


def test_import_refuses_after_manual_entry(entry_path, capsys):
    """Введённое вручную import без --force не сотрёт: база отличается от Excel."""
    assert run(entry_path, "add-expedition", "Кантхо 2027") == 0
    conn = connect(entry_path)
    before = snapshot(conn)
    conn.close()
    assert run(entry_path, "import", str(SOURCE), "--yes") == 1
    out = capsys.readouterr()
    assert "экспедиции" in out.out or "expedition" in out.out
    assert "--force" in out.err
    conn = connect(entry_path)
    assert snapshot(conn) == before
    conn.close()
