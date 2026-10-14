"""Тесты этапа C сайта (backend): правка и удаление записей (records.py), пакетный ввод
особей (entry.create_specimens / check_specimens), новые функции browse и карточки.

Каждый тест работает с КОПИЕЙ импортированной базы (фикстура rec_db). Для жёстких ошибок
проверяем «снимок» всех таблиц: после EditError база не изменилась ни на строку.
"""

import shutil

import pytest
from conftest import SOURCE, count

from biobank import browse, entry, records
from biobank.card import specimen_card
from biobank.checks import DESC_SL_GT_TL, DESC_SUFFIX
from biobank.db import connect, connect_read_only
from biobank.edit import EditError, NotFoundError, apply_edit, identify, resolve_issue

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture
def rec_path(db_path, tmp_path):
    path = tmp_path / "output" / "biobank.db"
    path.parent.mkdir()
    shutil.copy(db_path, path)
    return path


@pytest.fixture
def rec_db(rec_path):
    conn = connect(rec_path)
    yield conn
    conn.close()


@pytest.fixture
def ro(db_path):
    conn = connect_read_only(db_path)
    yield conn
    conn.close()


def snapshot(conn):
    """Все строки всех таблиц — чтобы убедиться, что после ошибки база не изменилась."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    # имя таблицы — из sqlite_master самой базы, не от пользователя
    return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}"))) for t in tables}


def expect_error(conn, match, func, *args, error=EditError, **kwargs):
    """func(...) падает с EditError, и в базе ничего не изменилось."""
    before = snapshot(conn)
    with pytest.raises(error, match=match) as info:
        func(conn, *args, **kwargs)
    assert not conn.in_transaction
    assert snapshot(conn) == before
    return info.value


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


def event_id(conn, sheet):
    return count(conn, "SELECT event_id FROM sampling_event WHERE source_sheet = ?", sheet)


def site_id(conn, name):
    return count(conn, "SELECT site_id FROM sampling_site WHERE name = ?", name)


def specimen_id(conn, label):
    return count(conn, "SELECT specimen_id FROM specimen WHERE label = ?", label)


def trawling_id(conn, sheet, trawl_no):
    return count(
        conn,
        "SELECT trawling_id FROM trawling JOIN sampling_event USING (event_id) "
        "WHERE source_sheet = ? AND trawl_no = ?",
        sheet,
        trawl_no,
    )


@pytest.fixture
def trawl_event(rec_db):
    """Новая экспедиция → выезд-траление на «Участок у Кантхо» → траления 1 и 2."""
    expedition = entry.create_expedition(rec_db, "Кантхо 2027").record_id
    event = entry.create_event(
        rec_db, expedition, site_id(rec_db, "Участок у Кантхо"), "2026-09-01", "траление",
        gear_mesh="3,5х3,5", gear_size="6,5x12",
    ).record_id  # fmt: skip
    for n in (1, 2):
        entry.add_trawling(rec_db, event, n, time_start="9.30", time_end="9:50")
    return event


@pytest.fixture
def fisher_site(rec_db):
    """Новая точка рыбака далеко от остальных мест."""
    return entry.create_site(rec_db, "точка рыбака", lat=10.5, lon=105.5).record_id


# ---------------------------------------------------------------------------
# Общие правила правки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func",
    [
        records.update_expedition, records.update_site, records.update_event,
        records.update_trawling, records.update_water_measurement, records.update_specimen,
        records.update_sample, records.update_catch_record,
    ],
)  # fmt: skip
def test_update_needs_known_fields(rec_db, func):
    expect_error(rec_db, "Неизвестные поля: colour", func, 1, colour="red")
    expect_error(rec_db, "Укажите, что изменить", func, 1)


@pytest.mark.parametrize(
    "func, field",
    [
        (records.update_expedition, "notes"), (records.update_site, "description"),
        (records.update_event, "notes"), (records.update_trawling, "notes"),
        (records.update_water_measurement, "notes"), (records.update_specimen, "notes"),
        (records.update_sample, "notes"), (records.update_catch_record, "taxon_text"),
    ],
)  # fmt: skip
def test_update_missing_record(rec_db, func, field):
    expect_error(rec_db, "999999 нет", func, 999999, error=NotFoundError, **{field: "x"})


def test_update_inside_open_transaction_refused(rec_db):
    rec_db.execute("BEGIN")
    with pytest.raises(EditError, match="уже в транзакции"):
        records.update_expedition(rec_db, 1, notes="x")
    rec_db.rollback()


def test_update_via_apply_edit_makes_backup(rec_path):
    result = apply_edit(rec_path, lambda conn: records.update_expedition(conn, 1, notes="проверка"))
    assert result.backup.exists()
    assert result.record_id == 1


# ---------------------------------------------------------------------------
# Экспедиция
# ---------------------------------------------------------------------------


def test_update_expedition(rec_db):
    result = records.update_expedition(rec_db, 1, name=" Донг-хап   2025 (сентябрь) ", notes="x")
    row = one(rec_db, "SELECT * FROM expedition WHERE expedition_id = 1")
    assert (row["name"], row["notes"]) == ("Донг-хап 2025 (сентябрь)", "x")
    assert "название" in result.message and "→ «x»" in result.message
    again = records.update_expedition(rec_db, 1, notes="x")
    assert "ничего не изменилось" in again.message


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"name": "  "}, "обязательное поле"),
        ({"date_start": "2025-10-30", "date_end": "2025-10-01"}, "позже"),
        # выезды Донг-хап — 25.09–12.10.2025: даты экспедиции должны их включать
        ({"date_start": "2025-09-27"}, "должны их включать"),
        ({"date_end": "2025-10-01"}, "должны их включать"),
        ({"date_start": "2099-01-01"}, "ещё не наступила"),
    ],
)
def test_update_expedition_errors(rec_db, changes, match):
    expect_error(rec_db, match, records.update_expedition, 1, **changes)


def test_update_expedition_duplicate_name(rec_db):
    other = one(rec_db, "SELECT name FROM expedition WHERE expedition_id = 2")["name"]
    expect_error(rec_db, "уже есть", records.update_expedition, 1, name=other)


# ---------------------------------------------------------------------------
# Место
# ---------------------------------------------------------------------------


def test_update_site_fields(rec_db, fisher_site):
    result = records.update_site(
        rec_db, fisher_site, description="у моста", lat="10,6", lon=105.6,
        city="Кантхо", region="Кантхо",
    )  # fmt: skip
    row = browse.get_record(rec_db, "site", fisher_site)
    assert (row["description"], row["lat"], row["lon"], row["city"]) == (
        "у моста", 10.6, 105.6, "Кантхо"
    )  # fmt: skip
    assert "город — → «Кантхо»" in result.message
    # только город — провинция прежняя; city=None — город убрать
    records.update_site(rec_db, fisher_site, city="Тран-де")
    assert browse.get_record(rec_db, "site", fisher_site)["region"] == "Кантхо"
    records.update_site(rec_db, fisher_site, city=None)
    assert browse.get_record(rec_db, "site", fisher_site)["city_id"] is None


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"lat": None}, "обе"),
        ({"lat": 95}, "не может быть больше 90"),
        ({"site_type": "озеро"}, "Тип места"),
        ({"name": ""}, "обязательное поле"),
        ({"city": "Новый город"}, "Укажите провинцию"),
        ({"city": "Новый город", "region": "Нигде"}, "Провинции «Нигде» нет"),
    ],
)
def test_update_site_errors(rec_db, fisher_site, changes, match):
    expect_error(rec_db, match, records.update_site, fisher_site, **changes)


def test_update_site_type_must_fit_events(rec_db):
    """Участок реки с выездами-тралениями нельзя сделать рынком."""
    site = site_id(rec_db, "Участок у Хонг-нгу")
    expect_error(rec_db, "Точка 1.*траление", records.update_site, site, site_type="рынок")


def test_update_site_type_must_fit_capture_specimens(rec_db):
    """Место поимки 138–142 pc — точка рыбака; тип «рынок» не подходит к способу «рыбак»."""
    site = count(rec_db, "SELECT capture_site_id FROM specimen WHERE label = '138 pc'")
    expect_error(rec_db, "рыбак", records.update_site, site, site_type="рынок")


def test_update_site_nearby_duplicate(rec_db, fisher_site):
    """Перенести точку к уже известной точке рыбака (≤ 200 м) — только с подтверждением."""
    expect_error(
        rec_db, "похоже, уже есть", records.update_site, fisher_site,
        lat=9.5187059, lon=106.2082146,
    )  # fmt: skip
    result = records.update_site(
        rec_db, fisher_site, lat=9.5187059, lon=106.2082146, allow_nearby=True
    )
    assert any("Рядом есть место" in w for w in result.warnings)


def test_update_site_same_name_is_duplicate(rec_db):
    site = entry.create_site(rec_db, "участок реки", name="Участок у моста").record_id
    expect_error(rec_db, "похоже, уже есть", records.update_site, site, name="Участок у Кантхо")


# ---------------------------------------------------------------------------
# Выезд
# ---------------------------------------------------------------------------


def test_update_event_date_extends_expedition(rec_db, trawl_event):
    result = records.update_event(rec_db, trawl_event, event_date="2026-09-05", notes="правка")
    row = browse.get_record(rec_db, "event", trawl_event)
    assert (row["event_date"], row["notes"]) == ("2026-09-05", "правка")
    exp = one(rec_db, "SELECT date_start, date_end FROM expedition WHERE name = 'Кантхо 2027'")
    assert tuple(exp) == ("2026-09-01", "2026-09-05")
    assert "дата «2026-09-01» → «2026-09-05»" in result.message


def test_update_event_market_date_warns_about_identifications(rec_db):
    """Рынок без даты (дата — предыдущего выезда): исправили дату — определения не трогаем."""
    market = event_id(rec_db, "Рынок.Бенче")
    old = browse.get_record(rec_db, "event", market)["event_date"]
    result = records.update_event(rec_db, market, event_date="2025-10-23")
    assert any("определений вида" in w and old in w for w in result.warnings)
    assert count(
        rec_db,
        "SELECT count(*) FROM species_identification JOIN specimen USING (specimen_id) "
        "WHERE event_id = ? AND identified_on = ?",
        market,
        old,
    ) == 14  # fmt: skip


def test_update_event_gear(rec_db, trawl_event):
    records.update_event(rec_db, trawl_event, gear_mesh="5x5")
    row = browse.get_record(rec_db, "event", trawl_event)
    assert (row["gear_mesh"], row["gear_size"]) == ("5x5", "6,5x12")


def test_update_event_method_and_site(rec_db, fisher_site):
    """Выезд рыбака на точку рыбака: способ и место меняются вместе."""
    expedition = entry.create_expedition(rec_db, "Тест 2027").record_id
    event = entry.create_event(rec_db, expedition, fisher_site, "2026-09-01", "рыбак").record_id
    market = site_id(rec_db, "Рынок Бенче")
    expect_error(rec_db, "нужно место типа «точка рыбака»", records.update_event, event,
                 site_id=market)  # fmt: skip
    expect_error(rec_db, "только у тралений", records.update_event, event, gear_mesh="5x5")
    result = records.update_event(rec_db, event, site_id=str(market), capture_method="рынок")
    row = browse.get_record(rec_db, "event", event)
    assert (row["site_id"], row["capture_method"]) == (market, "рынок")
    assert "«рыбак» → «рынок»" in result.message


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"capture_method": "рыбак"}, "нужно место типа"),
        ({"event_date": ""}, "обязательное поле"),
        ({"expedition_id": 999}, "Экспедиции №999"),
        ({"site_id": 999}, "Места №999"),
        ({"capture_method": "сачок"}, "Способа получения «сачок» нет"),
    ],
)
def test_update_event_errors(rec_db, trawl_event, changes, match):
    expect_error(rec_db, match, records.update_event, trawl_event, **changes)


def test_update_event_trawl_method_locked_by_trawlings(rec_db, trawl_event):
    """Траления есть — способ «траление» не сменить (даже на подходящее место)."""
    fisher = site_id(rec_db, "Точка рыбака (9.5187, 106.2082)")
    expect_error(rec_db, "есть траления", records.update_event, trawl_event, site_id=fisher,
                 capture_method="рыбак", gear_mesh=None, gear_size=None)  # fmt: skip


def test_update_event_duplicate(rec_db, trawl_event):
    other = entry.create_event(
        rec_db, browse.get_record(rec_db, "event", trawl_event)["expedition_id"],
        site_id(rec_db, "Участок у Кантхо"), "2026-09-02", "траление",
    ).record_id  # fmt: skip
    expect_error(rec_db, "Такой выезд уже есть", records.update_event, other,
                 event_date="2026-09-01")  # fmt: skip


# ---------------------------------------------------------------------------
# Траление и вода
# ---------------------------------------------------------------------------


def test_update_trawling(rec_db, trawl_event):
    trawl = count(rec_db, "SELECT trawling_id FROM trawling WHERE event_id = ? AND trawl_no = 2",
                  trawl_event)  # fmt: skip
    result = records.update_trawling(
        rec_db, trawl, trawl_no="3", duration_min="25", catch_note="2 plotosus", time_end="9.55"
    )
    row = browse.get_record(rec_db, "trawling", trawl)
    assert (row["trawl_no"], row["duration_min"], row["time_end"]) == (3, 25.0, "09:55")
    assert result.message.startswith("Траление 3 выезда")
    expect_error(rec_db, "уже есть", records.update_trawling, trawl, trawl_no=1)
    expect_error(rec_db, "обязательное", records.update_trawling, trawl, trawl_no="")
    expect_error(rec_db, "больше 0", records.update_trawling, trawl, duration_min=0)
    expect_error(rec_db, "не время", records.update_trawling, trawl, time_start="25:00")


def test_update_imported_trawling_time(rec_db):
    """Правка записи из Excel разрешена: время 10.48 → 10:50 по полевому журналу."""
    trawl = trawling_id(rec_db, "Точка 1.", 4)
    records.update_trawling(rec_db, trawl, time_start="10:50")
    assert browse.get_record(rec_db, "trawling", trawl)["time_start"] == "10:50"


def test_update_water_measurement(rec_db, trawl_event):
    m = entry.add_water_measurement(rec_db, trawl_event, "старт", 1, ph=7.1, lat=10, lon=105)
    mid = m.record_id
    result = records.update_water_measurement(rec_db, mid, ph="7,4", coord_source="google")
    row = browse.get_record(rec_db, "water_measurement", mid)
    assert (row["ph"], row["coord_source"]) == (7.4, "Google Maps")
    assert "pH 7.1 → 7.4" in result.message
    # перенос на другое траление того же выезда
    records.update_water_measurement(rec_db, mid, trawl_no=2)
    assert browse.get_record(rec_db, "water_measurement", mid)["trawl_no"] == 2
    # «точка» — только вместе с удалением траления
    expect_error(rec_db, "уберите номер траления", records.update_water_measurement, mid,
                 point_type="точка")  # fmt: skip
    records.update_water_measurement(rec_db, mid, point_type="точка", trawl_no=None)
    assert browse.get_record(rec_db, "water_measurement", mid)["trawling_id"] is None


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"lat": None}, "обе"),
        ({"ph": 15}, "не может быть больше 14"),
        ({"trawl_no": None}, "укажите номер траления"),
        ({"trawl_no": 9}, "нет траления 9"),
        ({"point_type": "финиш", "trawl_no": 2}, "уже есть"),
        ({"point_type": "середина"}, "Тип точки"),
        ({"coord_source": "компас"}, "Источник координат"),
    ],
)
def test_update_water_errors(rec_db, trawl_event, changes, match):
    entry.add_water_measurement(rec_db, trawl_event, "финиш", 2)
    mid = entry.add_water_measurement(rec_db, trawl_event, "старт", 1, lat=10, lon=105).record_id
    expect_error(rec_db, match, records.update_water_measurement, mid, **changes)


# ---------------------------------------------------------------------------
# Особь
# ---------------------------------------------------------------------------


def test_update_specimen_label(rec_db, trawl_event):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius").record_id  # 179 pc
    result = records.update_specimen(rec_db, sid, label="181рс")
    row = browse.get_record(rec_db, "specimen", sid)
    assert (row["label"], row["series"], row["series_no"]) == ("181 pc", "pc", 181)
    assert "«179 pc» → «181 pc»" in result.message
    assert any("пропущены" in w for w in result.warnings)  # 179, 180 свободны
    assert entry.next_label(rec_db, "pc") == "182 pc"


def test_update_specimen_label_fix_closes_suffix_issue(rec_db, trawl_event):
    """Исправили суффикс по явному запросу — проблема в журнале закрылась сама."""
    sid = entry.create_specimen(rec_db, trawl_event, "P. elongatus", "300 ph").record_id
    assert count(rec_db, "SELECT count(*) FROM data_issue WHERE resolved = 0 AND record_id = ? "
                 "AND description = ?", sid, DESC_SUFFIX) == 1  # fmt: skip
    result = records.update_specimen(rec_db, sid, label="300 e")
    assert result.checks.closed >= 1
    assert count(rec_db, "SELECT count(*) FROM data_issue WHERE resolved = 0 AND record_id = ? "
                 "AND description = ?", sid, DESC_SUFFIX) == 0  # fmt: skip


def test_update_specimen_label_warnings(rec_db):
    """155 pc: 5 образцов с меткой «155 pc…» — метки не меняются, только предупреждение."""
    sid = specimen_id(rec_db, "155 pc")
    result = records.update_specimen(rec_db, sid, label="400 pc")
    assert any("образцов" in w for w in result.warnings)
    assert count(rec_db, "SELECT count(*) FROM sample WHERE specimen_id = ? AND label LIKE "
                 "'155 pc%'", sid) == 5  # fmt: skip
    # своя проблема (число стало датой) привязана по id — карточка её по-прежнему показывает
    issues = {s.title: s for s in specimen_card(rec_db, "400 pc")}
    rows = issues["Проблемы в журнале (особь и её выезд)"].rows
    assert any("дату" in str(r) for r in rows)


def test_update_specimen_same_label_with_other_suffix(rec_db):
    """26 ph → 26 sp: тот же номер серии; «уже есть с другим суффиксом» — не про саму себя."""
    result = records.update_specimen(rec_db, specimen_id(rec_db, "26 ph"), label="26 sp")
    assert not any("другим суффиксом: 26 ph" in w for w in result.warnings)


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"label": "5 pc"}, "уже занят"),
        ({"label": "ХХ 2"}, "не распознан"),
        ({"tl_cm": 30}, "update_measurements"),
        ({"species": "Plotosus canius"}, "identify"),
        ({"sex": "м"}, "Пол"),
        ({"is_dead": "может быть"}, "да.*нет"),
        ({"trawl_no": 7}, "нет траления 7"),
        ({"capture_method": "рыбак", "trawl_no": 1}, "не тралением"),
        ({"capture_site_id": 999}, "Места №999"),
        ({"event_id": 999}, "Выезда №999"),
        ({"capture_date": "2099-01-01"}, "ещё не наступила"),
    ],
)
def test_update_specimen_errors(rec_db, trawl_event, changes, match):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", trawl_no=1).record_id
    expect_error(rec_db, match, records.update_specimen, sid, **changes)


def test_update_specimen_fields(rec_db, trawl_event, fisher_site):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", trawl_no=1).record_id
    result = records.update_specimen(rec_db, sid, sex="самка", is_dead="да", notes="икра")
    row = browse.get_record(rec_db, "specimen", sid)
    assert (row["sex"], row["is_dead"], row["notes"]) == ("самка", 1, "икра")
    assert "мёртвая нет → да" in result.message
    # траление изменить и убрать
    records.update_specimen(rec_db, sid, trawl_no="2")
    assert browse.get_record(rec_db, "specimen", sid)["trawl_no"] == 2
    records.update_specimen(rec_db, sid, trawl_no=None)
    assert browse.get_record(rec_db, "specimen", sid)["trawling_id"] is None
    # от рыбака внутри выезда-траления; способ как у выезда — не записывается
    records.update_specimen(rec_db, sid, capture_method="рыбак", capture_site_id=fisher_site)
    row = browse.get_record(rec_db, "specimen", sid)
    assert (row["capture_method"], row["capture_site_id"]) == ("рыбак", fisher_site)
    records.update_specimen(rec_db, sid, capture_method="траление", capture_date="2026-09-01")
    row = browse.get_record(rec_db, "specimen", sid)
    assert (row["capture_method_id"], row["capture_date"]) == (None, None)


def test_update_specimen_move_to_other_event(rec_db, trawl_event):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", trawl_no=1).record_id
    other = event_id(rec_db, "Точка 6 2026")
    expect_error(rec_db, "при переносе", records.update_specimen, sid, event_id=other)
    result = records.update_specimen(rec_db, sid, event_id=other, trawl_no=3)
    row = browse.get_record(rec_db, "specimen", sid)
    assert (row["event_id"], row["trawl_no"]) == (other, 3)
    assert any("Дата определения" in w for w in result.warnings)  # 2026-09-01 ≠ дата выезда


# ---------------------------------------------------------------------------
# Образец и прилов
# ---------------------------------------------------------------------------


def test_update_sample(rec_db):
    sample = entry.add_sample(rec_db, "1 pc", "ДНК", sample_label="1 pc dna").record_id
    records.update_sample(rec_db, sample, sample_type="гистология", organ="печень", label="1 pc a")
    row = browse.get_record(rec_db, "sample", sample)
    assert (row["sample_type"], row["organ"], row["label"]) == ("гистология", "печень", "1 pc a")
    records.update_sample(rec_db, sample, label="")  # пусто — номер особи
    assert browse.get_record(rec_db, "sample", sample)["label"] == "1 pc"
    expect_error(rec_db, "уже есть у особи 26 ph", records.update_sample, sample, label="26 ph")
    expect_error(rec_db, "Тип образца", records.update_sample, sample, sample_type="кровь")


def test_update_catch_record(rec_db, trawl_event):
    catch = entry.create_catch_record(rec_db, trawl_event, raw_text="краб", trawl_no=1).record_id
    result = records.update_catch_record(rec_db, catch, trawl_no=2, count_n="3", taxon_text="краб")
    row = browse.get_record(rec_db, "catch_record", catch)
    assert (row["trawl_no"], row["count_n"], row["taxon_text"]) == (2, 3, "краб")
    assert "траление 1 → 2" in result.message
    records.update_catch_record(rec_db, catch, trawl_no=None)  # улов всего выезда
    assert browse.get_record(rec_db, "catch_record", catch)["trawling_id"] is None
    expect_error(rec_db, "обязательное", records.update_catch_record, catch, raw_text=" ")
    expect_error(rec_db, "целое", records.update_catch_record, catch, count_n="2,5")
    expect_error(rec_db, "нет траления 9", records.update_catch_record, catch, trawl_no=9)


# ---------------------------------------------------------------------------
# Удаление: запрет при зависимых записях
# ---------------------------------------------------------------------------


def test_delete_event_refused_with_dependents(rec_db):
    error = expect_error(
        rec_db, "удалить нельзя", records.delete_event, event_id(rec_db, "Точка 1")
    )
    assert "16 особей, 4 траления" in str(error)


def test_delete_refused_lists_dependents(rec_db):
    expect_error(rec_db, "выездов", records.delete_expedition, 1)
    expect_error(rec_db, "выезд", records.delete_site, site_id(rec_db, "Участок у Хонг-нгу"))
    expect_error(rec_db, "5 образцов", records.delete_specimen, specimen_id(rec_db, "155 pc"))
    fisher = count(rec_db, "SELECT capture_site_id FROM specimen WHERE label = '163 pc'")
    expect_error(rec_db, "особ", records.delete_site, fisher)


def test_delete_trawling_refused_with_specimen_and_catch(rec_db, trawl_event):
    trawl = count(rec_db, "SELECT trawling_id FROM trawling WHERE event_id = ? AND trawl_no = 1",
                  trawl_event)  # fmt: skip
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", trawl_no=1).record_id
    entry.create_catch_record(rec_db, trawl_event, raw_text="краб", trawl_no=1)
    error = expect_error(rec_db, "удалить нельзя", records.delete_trawling, trawl)
    assert "1 особь, 1 запись прилова" in str(error)
    records.update_specimen(rec_db, sid, trawl_no=None)
    expect_error(rec_db, "1 запись прилова", records.delete_trawling, trawl)


def test_delete_sample_refused_with_analysis(rec_db):
    sample = entry.add_sample(rec_db, "1 pc", "ДНК", sample_label="1 pc dna").record_id
    entry.add_analysis(rec_db, sample, "секвенирование COI")
    expect_error(rec_db, "1 результат анализа", records.delete_sample, sample)


def test_delete_missing_records(rec_db):
    for func in (
        records.delete_expedition, records.delete_site, records.delete_event,
        records.delete_trawling, records.delete_water_measurement, records.delete_specimen,
        records.delete_sample, records.delete_catch_record, records.delete_identification,
    ):  # fmt: skip
        expect_error(rec_db, "999999", func, 999999, error=NotFoundError)
    expect_error(rec_db, "Отправки", records.delete_shipment, 1, 999999, error=NotFoundError)


# ---------------------------------------------------------------------------
# Удаление: успех и разрешённые каскады
# ---------------------------------------------------------------------------


def test_delete_whole_manual_expedition_bottom_up(rec_db, trawl_event):
    """Введённую по ошибке экспедицию можно удалить целиком — снизу вверх."""
    expedition = browse.get_record(rec_db, "event", trawl_event)["expedition_id"]
    trawls = [r[0] for r in rec_db.execute(
        "SELECT trawling_id FROM trawling WHERE event_id = ?", (trawl_event,))]  # fmt: skip
    entry.add_water_measurement(rec_db, trawl_event, "старт", 1)
    entry.add_water_measurement(rec_db, trawl_event, "финиш", 1)
    point = entry.add_water_measurement(rec_db, trawl_event, "точка").record_id
    first = records.delete_trawling(rec_db, trawls[0])
    assert "2 замера воды" in first.message  # каскад: замеры траления
    records.delete_trawling(rec_db, trawls[1])
    expect_error(rec_db, "1 замер воды", records.delete_event, trawl_event)
    records.delete_water_measurement(rec_db, point)
    records.delete_event(rec_db, trawl_event)
    records.delete_expedition(rec_db, expedition)
    for sql in (
        "SELECT count(*) FROM expedition WHERE expedition_id = ?",
        "SELECT count(*) FROM sampling_event WHERE expedition_id = ?",
    ):
        assert count(rec_db, sql, expedition) == 0
    assert count(rec_db, "SELECT count(*) FROM water_measurement WHERE event_id = ?",
                 trawl_event) == 0  # fmt: skip
    assert rec_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_specimen_cascades_identifications(rec_db, trawl_event):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius").record_id
    identify(rec_db, "179 pc", "Plotosus canius", "ДНК", "точно")
    result = records.delete_specimen(rec_db, sid)
    assert "2 определения вида" in result.message
    assert count(rec_db, "SELECT count(*) FROM species_identification WHERE specimen_id = ?",
                 sid) == 0  # fmt: skip
    assert entry.next_label(rec_db, "pc") == "179 pc"  # номер освободился


def test_delete_sample_cascades_shipments(rec_db):
    sample = entry.add_sample(rec_db, "1 pc", "ДНК", sample_label="1 pc dna",
                              labs=["Россия", "Тропцентр"]).record_id  # fmt: skip
    result = records.delete_sample(rec_db, sample)
    assert "2 отправки в лаборатории" in result.message
    assert count(rec_db, "SELECT count(*) FROM sample_shipment WHERE sample_id = ?", sample) == 0


def test_delete_shipment(rec_db):
    sample = entry.add_sample(rec_db, "1 pc", "ДНК", sample_label="1 pc dna",
                              labs=["Россия", "Тропцентр"]).record_id  # fmt: skip
    shipments = browse.list_shipments(rec_db, sample)
    assert [s["lab"] for s in shipments] == ["Россия", "Тропцентр"]
    records.delete_shipment(rec_db, sample, shipments[0]["lab_id"])
    assert [s["lab"] for s in browse.list_shipments(rec_db, sample)] == ["Тропцентр"]


def test_delete_catch_record(rec_db, trawl_event):
    catch = entry.create_catch_record(rec_db, trawl_event, raw_text="краб").record_id
    assert "краб" in records.delete_catch_record(rec_db, catch).message
    assert count(rec_db, "SELECT count(*) FROM catch_record WHERE catch_id = ?", catch) == 0


def test_delete_unused_site(rec_db, fisher_site):
    records.delete_site(rec_db, fisher_site)
    assert count(rec_db, "SELECT count(*) FROM sampling_site WHERE site_id = ?", fisher_site) == 0


# ---------------------------------------------------------------------------
# Определения вида: текущий вид согласован с историей
# ---------------------------------------------------------------------------


def current_species(conn, label):
    return count(
        conn,
        "SELECT coalesce(sp.scientific_name, '?') FROM specimen s "
        "LEFT JOIN species sp USING (species_id) WHERE s.label = ?",
        label,
    )


def idents(conn, label):
    return [r[0] for r in conn.execute(
        "SELECT identification_id FROM species_identification JOIN specimen USING (specimen_id) "
        "WHERE label = ? ORDER BY identification_id", (label,))]  # fmt: skip


def test_delete_only_identification_refused(rec_db):
    (only,) = idents(rec_db, "26 ph")
    expect_error(rec_db, "единственное", records.delete_identification, only)


def test_delete_latest_identification_restores_previous(rec_db):
    assert current_species(rec_db, "26 ph") == "Pangasianodon hypophthalmus"
    identify(rec_db, "26 ph", "Pangasius sp.", "ДНК", "до рода")
    assert current_species(rec_db, "26 ph") == "Pangasius sp."
    result = records.delete_identification(rec_db, idents(rec_db, "26 ph")[-1])
    assert current_species(rec_db, "26 ph") == "Pangasianodon hypophthalmus"
    assert "Текущий вид особи теперь: Pangasianodon hypophthalmus" in result.message


def test_delete_older_identification_keeps_current(rec_db):
    identify(rec_db, "26 ph", "Pangasius sp.", "ДНК", "до рода")
    result = records.delete_identification(rec_db, idents(rec_db, "26 ph")[0])
    assert current_species(rec_db, "26 ph") == "Pangasius sp."
    assert "Текущий вид" not in result.message


def test_delete_identification_to_undetermined(rec_db):
    """Самое новое оставшееся — «не определён»: текущий вид становится NULL."""
    identify(rec_db, "26 ph", None, "морфология", "не определён")
    identify(rec_db, "26 ph", "Pangasius sp.", "ДНК", "до рода")
    records.delete_identification(rec_db, idents(rec_db, "26 ph")[-1])
    assert current_species(rec_db, "26 ph") == "?"


# ---------------------------------------------------------------------------
# Журнал проблем при удалении
# ---------------------------------------------------------------------------


def test_delete_closes_issue_of_deleted_record(rec_db, trawl_event):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", tl_cm=10.2,
                                sl_cm=10.3).record_id  # fmt: skip
    issue = count(rec_db, "SELECT issue_id FROM data_issue WHERE record_id = ? AND description = ?",
                  sid, DESC_SL_GT_TL)  # fmt: skip
    result = records.delete_specimen(rec_db, sid)
    assert "Записей журнала проблем об удалённом закрыто: 1" in result.message
    row = browse.get_issue(rec_db, issue)
    assert row["resolved"] == 1 and row["record_id"] is None
    assert row["resolution"].startswith("Запись удалена") and "особь 179 pc" in row["resolution"]
    assert row["object_label"] == "179 pc"  # что было — видно


def test_delete_keeps_resolution_of_resolved_issue(rec_db, trawl_event):
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", tl_cm=10.2,
                                sl_cm=10.3).record_id  # fmt: skip
    issue = count(rec_db, "SELECT issue_id FROM data_issue WHERE record_id = ?", sid)
    resolve_issue(rec_db, issue, "проверили по журналу")
    records.delete_specimen(rec_db, sid)
    resolution = browse.get_issue(rec_db, issue)["resolution"]
    assert resolution.startswith("проверили по журналу; запись удалена")


def test_new_record_with_reused_id_gets_no_old_issues(rec_db, trawl_event):
    """SQLite может выдать id удалённой особи новой — чужая история к ней не прилипает."""
    sid = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", tl_cm=10.2,
                                sl_cm=10.3).record_id  # fmt: skip
    records.delete_specimen(rec_db, sid)
    new = entry.create_specimen(rec_db, trawl_event, "Plotosus canius", "190 pc").record_id
    assert new == sid  # id тот же
    sections = {s.title: s for s in specimen_card(rec_db, "190 pc")}
    rows = sections["Проблемы в журнале (особь и её выезд)"].rows
    assert not any(r[5] == DESC_SL_GT_TL for r in rows)


def test_delete_trawling_closes_water_issue(rec_db):
    """Траление из Excel без особей и прилова удалить можно; проблемы его замеров закрываются."""
    trawl = trawling_id(rec_db, "Точка 1", 2)
    water = count(rec_db, "SELECT measurement_id FROM water_measurement WHERE trawling_id = ? "
                  "AND point_type = 'старт'", trawl)  # fmt: skip
    rec_db.execute(  # проблема с record_id замера (у импорта их нет — добавим для проверки)
        "INSERT INTO data_issue (category, table_name, record_id, description) "
        "VALUES ('координаты', 'water_measurement', ?, 'проверка')",
        (water,),
    )
    rec_db.commit()
    records.delete_trawling(rec_db, trawl)
    row = one(rec_db, "SELECT * FROM data_issue WHERE description = 'проверка'")
    assert row["resolved"] == 1 and row["record_id"] is None
    assert "замер «старт», траление 2" in row["resolution"]


# ---------------------------------------------------------------------------
# Пакетный ввод особей
# ---------------------------------------------------------------------------


def labels_of(conn, ids):
    return [count(conn, "SELECT label FROM specimen WHERE specimen_id = ?", i) for i in ids]


def test_create_specimens_auto_labels_in_a_row(rec_db, trawl_event):
    rows = [
        {"label": "авто", "species": "Plotosus canius", "tl_cm": "25,4", "trawl_no": "1"},
        {"species": "Plotosus canius", "sex": "самка", "is_dead": "on"},
        {"label": "", "species": "P. elongatus",
         "samples": [{"sample_type": "ДНК", "labs": ["Россия"]},
                     {"sample_type": "гистология", "organ": "печень", "sample_label": "278 e a"}]},
    ]  # fmt: skip
    result = entry.create_specimens(rec_db, trawl_event, rows)
    assert labels_of(rec_db, result.record_ids) == ["179 pc", "180 pc", "278 e"]
    second = browse.get_record(rec_db, "specimen", result.record_ids[1])
    assert (second["sex"], second["is_dead"]) == ("самка", 1)
    assert count(rec_db, "SELECT count(*) FROM species_identification WHERE specimen_id IN "
                 "(?, ?, ?)", *result.record_ids) == 3  # fmt: skip
    samples = rec_db.execute(
        "SELECT label, sample_type FROM sample WHERE specimen_id = ? ORDER BY sample_id",
        (result.record_ids[2],),
    ).fetchall()
    assert [tuple(s) for s in samples] == [("278 e", "ДНК"), ("278 e a", "гистология")]
    assert "образцов: 2" in result.message


def test_create_specimens_all_or_nothing(rec_db, trawl_event):
    rows = [
        {"species": "Plotosus canius"},
        {"species": "Plotosus canius", "tl_cm": 0},  # жёсткая ошибка
        {"species": "Esox lucius"},
    ]
    error = expect_error(rec_db, "Ошибки в строках 2, 3", entry.create_specimens, trawl_event, rows)
    check = error.check
    assert [r.row for r in check.rows] == [1, 2, 3]
    assert check.rows[0].errors == [] and check.rows[0].label == "179 pc"
    assert "TL должна быть больше 0" in check.rows[1].errors[0]
    assert "нет в справочнике" in check.rows[2].errors[0]
    assert check.error_rows == [2, 3] and not check.ok


def test_create_specimens_duplicate_in_batch(rec_db, trawl_event):
    rows = [{"label": "200 pc", "species": "Plotosus canius"},
            {"label": "200pc", "species": "Plotosus canius"}]  # fmt: skip
    error = expect_error(rec_db, "строке 2", entry.create_specimens, trawl_event, rows)
    assert "уже есть в этом вводе (строка 1)" in error.check.rows[1].errors[0]


def test_create_specimens_taken_label(rec_db, trawl_event):
    error = expect_error(rec_db, "уже занят", entry.create_specimens, trawl_event,
                         [{"label": "5 pc", "species": "Plotosus canius"}])  # fmt: skip
    assert error.check.rows[0].row == 1


def test_auto_label_skips_explicit_label_below(rec_db, trawl_event):
    """«авто» не выдаёт номер, который явно записан ниже в том же пакете."""
    rows = [{"species": "Plotosus canius"}, {"label": "179 pc", "species": "Plotosus canius"}]
    result = entry.create_specimens(rec_db, trawl_event, rows)
    assert labels_of(rec_db, result.record_ids) == ["180 pc", "179 pc"]
    assert not any("пропущены" in w for w in result.warnings)


def test_batch_gap_warning_counts_batch_numbers(rec_db, trawl_event):
    rows = [{"label": "180 pc", "species": "Plotosus canius"},
            {"label": "179 pc", "species": "Plotosus canius"},
            {"label": "185 pc", "species": "Plotosus canius"}]  # fmt: skip
    check = entry.check_specimens(rec_db, trawl_event, rows)
    assert [any("пропущены" in w for w in r.warnings) for r in check.rows] == [False, False, True]


def test_batch_blank_rows_skipped_and_numbered(rec_db, trawl_event):
    rows = [{}, {"label": "", "species": " ", "samples": []}, {"species": "Plotosus canius"}]
    check = entry.check_specimens(rec_db, trawl_event, rows)
    assert [r.row for r in check.rows] == [3]
    expect_error(rec_db, "Нет ни одной", entry.create_specimens, trawl_event, [{}, {}])
    expect_error(rec_db, "список", entry.create_specimens, trawl_event, {"species": "x"})


def test_batch_warnings_flattened_and_logged(rec_db, trawl_event):
    rows = [{"species": "Plotosus canius"},
            {"species": "Plotosus canius", "tl_cm": 10.2, "sl_cm": 10.3}]  # fmt: skip
    result = entry.create_specimens(rec_db, trawl_event, rows)
    assert f"строка 2 (180 pc): {DESC_SL_GT_TL}" in result.warnings
    assert count(rec_db, "SELECT count(*) FROM data_issue WHERE resolved = 0 AND record_id = ? "
                 "AND description = ?", result.record_ids[1], DESC_SL_GT_TL) == 1  # fmt: skip


@pytest.mark.parametrize(
    "row, match",
    [
        ({"species": "Plotosus canius", "colour": "x"}, "Неизвестные поля: colour"),
        ({"species": "Plotosus canius", "trawl_no": "первое"}, "не целое"),
        ({"species": "Plotosus canius", "trawl_no": "9"}, "нет траления 9"),
        ({"species": "Plotosus canius", "is_dead": "наверное"}, "да.*нет"),
        ({"species": "Plotosus canius", "capture_site_id": "999"}, "Места №999"),
        ({"species": "Plotosus canius", "samples": [{"sample_type": "кровь"}]}, "Тип образца"),
        ({"species": "Plotosus canius", "samples": [{"sample_type": "ДНК", "labs": ["Марс"]}]},
         "Марс"),
        ({"species": "Plotosus canius", "samples": [{"sample_type": "ДНК",
                                                     "sample_label": "26 ph"}]}, "26 ph"),
        ({"species": "Plotosus canius", "samples": ["ДНК"]}, "набор полей"),
        ("строка", "набор полей"),
    ],
)  # fmt: skip
def test_batch_row_errors(rec_db, trawl_event, row, match):
    error = expect_error(rec_db, match, entry.create_specimens, trawl_event, [row])
    assert error.check.error_rows == [1]


def test_batch_sample_label_of_other_row(rec_db, trawl_event):
    rows = [
        {"species": "Plotosus canius", "samples": [{"sample_type": "ДНК", "sample_label": "X1"}]},
        {"species": "Plotosus canius", "samples": [{"sample_type": "ДНК", "sample_label": "X1"}]},
    ]
    error = expect_error(rec_db, "уже есть у особи 179 pc", entry.create_specimens,
                         trawl_event, rows)  # fmt: skip
    assert error.check.error_rows == [2]


def test_check_specimens_read_only(db_path):
    """Предпросмотр работает на подключении только для чтения и ничего не пишет."""
    before = db_path.read_bytes()
    conn = connect_read_only(db_path)
    try:
        event = event_id(conn, "Точка 6 2026")
        check = entry.check_specimens(conn, event, [{"species": "Plotosus canius"}] * 2)
    finally:
        conn.close()
    assert [r.label for r in check.rows] == ["179 pc", "180 pc"] and check.ok
    assert db_path.read_bytes() == before


def test_create_specimens_missing_event(rec_db):
    expect_error(rec_db, "Выезда №999", entry.create_specimens, 999,
                 [{"species": "Plotosus canius"}], error=NotFoundError)  # fmt: skip


def test_create_specimens_one_backup(rec_path):
    """Одно сохранение пакета — одна резервная копия и одна транзакция."""
    conn = connect(rec_path)
    event = event_id(conn, "Точка 6 2026")
    conn.close()
    rows = [{"species": "Plotosus canius"} for _ in range(5)]
    result = apply_edit(rec_path, lambda c: entry.create_specimens(c, event, rows))
    assert len(result.record_ids) == 5
    assert len(list((rec_path.parent / "backups").iterdir())) == 1


def test_create_specimen_single_unchanged(rec_db, trawl_event):
    """Одиночный ввод после рефакторинга: те же номер, предупреждения и образцы."""
    result = entry.create_specimen(rec_db, trawl_event, "P. elongatus", "300 ph")
    assert any(w.startswith(DESC_SUFFIX) for w in result.warnings)
    assert any("пропущены" in w for w in result.warnings)
    sample = entry.add_sample(rec_db, "300 ph", "ДНК", labs=["Россия"])
    assert "отправлен: Россия" in sample.message


# ---------------------------------------------------------------------------
# browse и карточка
# ---------------------------------------------------------------------------


def test_get_issue(ro):
    issue = browse.get_issue(ro, 25)
    assert (issue["object_label"], issue["resolved"]) == ("155 pc", 0)
    assert set(issue) == {
        "issue_id", "category", "table_name", "record_id", "sheet_name", "cell",
        "object_label", "raw_value", "description", "resolved", "resolution",
    }  # fmt: skip
    with pytest.raises(NotFoundError, match="999999"):
        browse.get_issue(ro, 999999)


def test_trawling_card(ro):
    sections = browse.trawling_card(ro, trawling_id(ro, "Точка 1.", 4))
    titles = [s.title for s in sections]
    assert titles == [
        "Траление", "Замеры воды", "Особи из этого траления", "Прилов",
        "Проблемы в журнале (траление и его замеры)",
    ]  # fmt: skip
    main = dict(sections[0].rows)
    assert (main["траление"], main["выезд"], main["старт"]) == (4, "«Точка 1.»", "10:48")
    ids = {k: v for d in sections[0].row_ids for k, v in d.items()}
    assert ids["event_id"] == event_id(ro, "Точка 1.")
    assert len(sections[1].rows) == 2  # старт и финиш
    issues = sections[4]
    assert {r[4] for r in issues.rows} == {"траление 4 старт", "траление 4 финиш"}
    assert [d["issue_id"] for d in issues.row_ids] == [r[0] for r in issues.rows]
    with pytest.raises(NotFoundError):
        browse.trawling_card(ro, 999999)


def test_trawling_card_issue_label_not_prefix_of_other_number(ro):
    """«траление 1» не подхватывает проблемы «траление 10 …»."""
    sections = browse.trawling_card(ro, trawling_id(ro, "Точка 2", 1))
    labels = {r[4] for r in sections[4].rows}
    assert labels == {"траление 1 старт"}  # точка в 50 км (координаты)


def test_trawling_card_specimens_and_catch(rec_db, trawl_event):
    entry.create_specimen(rec_db, trawl_event, "Plotosus canius", trawl_no=1)
    entry.create_catch_record(rec_db, trawl_event, raw_text="краб", trawl_no=1)
    trawl = count(rec_db, "SELECT trawling_id FROM trawling WHERE event_id = ? AND trawl_no = 1",
                  trawl_event)  # fmt: skip
    sections = {s.title: s for s in browse.trawling_card(rec_db, trawl)}
    assert sections["Особи из этого траления"].rows[0][0] == "179 pc"
    assert "specimen_id" in sections["Особи из этого траления"].row_ids[0]
    assert sections["Прилов"].rows[0][3] == "краб"


def test_reference_lists(ro):
    labs = browse.list_labs(ro)
    assert {lab["name"] for lab in labs} >= {"Россия", "Тропцентр"}
    assert sum(lab["sample_count"] for lab in labs) == count(
        ro, "SELECT count(*) FROM sample_shipment"
    )
    gear = browse.list_gear(ro)
    assert len(gear) == 2 and all(g["event_count"] > 0 for g in gear)
    regions = browse.list_regions(ro)
    assert [r["name"] for r in regions] == ["Донг-хап", "Виньлонг", "Кантхо"]
    cities = browse.list_cities(ro)
    assert len(cities) == count(ro, "SELECT count(*) FROM city")
    cantho = next(r["region_id"] for r in regions if r["name"] == "Кантхо")
    assert {c["region"] for c in browse.list_cities(ro, cantho)} == {"Кантхо"}
    with pytest.raises(NotFoundError):
        browse.list_cities(ro, 999)
    with pytest.raises(NotFoundError):
        browse.list_shipments(ro, 999999)


def test_field_choices():
    choices = browse.field_choices()
    assert choices["point_type"] == ["старт", "финиш", "точка"]
    assert "целая особь" in choices["sample_type"]
    assert choices["confidence"][-1] == "не определён"
    assert choices["site_type"] == ["участок реки", "рынок", "аквахозяйство", "точка рыбака"]
    assert set(choices) == {
        "site_type", "point_type", "sample_type", "sex", "coord_source", "method",
        "confidence", "capture_method", "preservative",
    }  # fmt: skip
    assert choices["preservative"] == ["формалин 10%", "заморозка", "этанол"]
    assert not any("96" in p for p in choices["preservative"])


@pytest.mark.parametrize("kind", list(browse.RECORD_SQL))
def test_get_record_kinds(ro, kind):
    with pytest.raises(NotFoundError):
        browse.get_record(ro, kind, 999999)
    record = browse.get_record(ro, kind, 1)
    assert isinstance(record, dict) and record


def test_get_record_fields_match_update_functions(ro):
    """Поля для формы называются так же, как поля функций правки."""
    forms = {
        "expedition": records.EXPEDITION_FIELDS, "site": records.SITE_FIELDS,
        "event": records.EVENT_FIELDS, "trawling": records.TRAWLING_FIELDS,
        "water_measurement": records.WATER_FIELDS, "specimen": records.SPECIMEN_FIELDS,
        "sample": records.SAMPLE_FIELDS, "catch_record": records.CATCH_FIELDS,
    }  # fmt: skip
    for kind, fields in forms.items():
        assert set(fields) <= set(browse.get_record(ro, kind, 1)), kind
    with pytest.raises(ValueError):
        browse.get_record(ro, "fish", 1)


@pytest.mark.parametrize(
    "label, method", [("138 pc", "рыбак"), ("26 ph", "аквахозяйство"), ("1", "траление")]
)
def test_card_capture_method_ids(ro, label, method):
    main = specimen_card(ro, label)[0]
    ids = dict(zip([row[0] for row in main.rows], main.row_ids, strict=True))
    assert ids["способ получения"]["capture_method"] == method
    assert ids["способ получения"]["capture_method_id"] == count(
        ro, "SELECT capture_method_id FROM capture_method WHERE name = ?", method
    )
