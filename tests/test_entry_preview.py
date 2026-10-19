"""Тесты предпросмотра форм (этап C сайта, часть 2, backend): проверки БЕЗ записи.

entry.check_specimen, entry.check_measurements_update, entry.preview_site,
browse.specimen_measurements, browse.list_preservatives, подсказки командной строки.

Главное свойство предпросмотра — он говорит ровно то, что потом сделает запись:
- на подключении только для чтения (connect_read_only) файл базы не меняется
  (сравниваем sha256 — «отпечаток» содержимого файла — до и после);
- номер пробы, название места, предупреждения и тексты ошибок совпадают с тем,
  что выдаёт настоящая запись (create_specimen, update_measurements, create_site).
"""

import hashlib
import shutil

import pytest
from conftest import SOURCE, count

from biobank import browse, entry
from biobank.__main__ import CLI_HINTS, main, with_cli_hints
from biobank.checks import DESC_SL_GT_TL, DESC_SUFFIX
from biobank.db import connect, connect_read_only
from biobank.edit import EditError, NotFoundError

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")

FISHER_LAT, FISHER_LON = 9.5187059, 106.2082146  # точка рыбака 20–22.08.2026


@pytest.fixture
def path(db_path, tmp_path):
    """Копия импортированной базы: в ней можно и проверять, и записывать."""
    copy = tmp_path / "output" / "biobank.db"
    copy.parent.mkdir()
    shutil.copy(db_path, copy)
    return copy


@pytest.fixture
def ro(path):
    conn = connect_read_only(path)
    yield conn
    conn.close()


@pytest.fixture
def rw(path):
    conn = connect(path)
    yield conn
    conn.close()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event_id(conn, sheet):
    return count(conn, "SELECT event_id FROM sampling_event WHERE source_sheet = ?", sheet)


def error_text(func, *args, **kwargs) -> str:
    """Текст EditError, с которым падает настоящая запись."""
    with pytest.raises(EditError) as info:
        func(*args, **kwargs)
    return str(info.value)


# ---------------------------------------------------------------------------
# check_specimen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "species, label, kwargs",
    [
        ("Plotosus canius", None, {"tl_cm": "25,4", "sl_cm": "27"}),  # авто-номер, SL > TL
        ("Plotosus canius", "179 ph", {"tl_cm": 30}),  # суффикс не по виду
        ("Plotosus canius", "200pc", {}),  # пропуск номеров в серии
        ("P. elongatus", None, {"trawl_no": 1, "sex": "самка"}),  # без предупреждений
        ("?", "ХХ2", {}),  # номер не распознан — жёсткая ошибка
    ],
)
def test_check_specimen_matches_create(path, ro, rw, species, label, kwargs):
    """Предпросмотр не пишет в базу и даёт тот же номер и те же предупреждения/ошибки."""
    event = event_id(ro, "Точка 1 2026")
    before = sha256(path)
    final_label, check = entry.check_specimen(ro, event, species, label, **kwargs)
    assert sha256(path) == before

    if check.errors:
        assert check.errors == [error_text(entry.create_specimen, rw, event, species, label,
                                           **kwargs)]  # fmt: skip
        return
    result = entry.create_specimen(rw, event, species, label, **kwargs)
    stored = count(rw, "SELECT label FROM specimen WHERE specimen_id = ?", result.record_id)
    assert final_label == stored
    assert check.warnings == result.warnings


def test_check_specimen_auto_label_and_warnings(ro):
    label, check = entry.check_specimen(
        ro, event_id(ro, "Точка 1 2026"), "Plotosus canius", tl_cm="25,4", sl_cm="27"
    )
    assert label == "179 pc"
    assert check.errors == [] and check.warnings == [DESC_SL_GT_TL]
    _, check = entry.check_specimen(ro, 1, "Plotosus canius", "300 ph")
    assert any(w.startswith(DESC_SUFFIX) for w in check.warnings)


@pytest.mark.parametrize(
    "species, label, kwargs, match",
    [
        ("Plotosus canius", "5 pc", {}, "уже занят"),
        ("Plotosus canius", " 155pc ", {}, "уже занят"),
        ("Plotosus canius", None, {"tl_cm": 0}, "больше 0"),
        ("Plotosus canius", None, {"tl_cm": "abc"}, "не число"),
        ("Нет такого", None, {}, "нет в справочнике"),
        ("?", None, {}, "укажите номер явно"),
        ("Plotosus canius", None, {"capture_site_id": 999}, "Места №999"),
        ("Plotosus canius", None, {"trawl_no": 99}, "нет траления 99"),
    ],
)
def test_check_specimen_errors(ro, species, label, kwargs, match):
    final_label, check = entry.check_specimen(ro, 1, species, label, **kwargs)
    assert len(check.errors) == 1 and match in check.errors[0]
    assert check.warnings == []
    # при ошибке — введённый номер в виде, как он хранится (или None, если не введён)
    assert final_label == (None if label is None else entry._typed_label(label))


def test_check_specimen_missing_event(ro):
    with pytest.raises(NotFoundError, match="Выезда №999"):
        entry.check_specimen(ro, 999, "Plotosus canius")


# ---------------------------------------------------------------------------
# check_measurements_update
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, changes",
    [
        ("160", {"tl_cm": "6,1"}),  # исправление опечатки
        ("1 pc", {"sl_cm": 999}),  # SL > TL — предупреждение
        ("1 pc", {"mass_gutted_g": "-"}),  # стереть значение
        ("1 pc", {"tl_cm": 0}),  # жёсткая ошибка
        ("1 pc", {}),  # нечего менять
        ("1 pc", {"weight": 3}),  # неизвестное поле
    ],
)
def test_check_measurements_matches_update(path, ro, rw, label, changes):
    before = sha256(path)
    check = entry.check_measurements_update(ro, label, **changes)
    assert sha256(path) == before
    if check.errors:
        assert check.errors == [error_text(entry.update_measurements, rw, label, **changes)]
        return
    result = entry.update_measurements(rw, label, **changes)
    assert check.warnings == result.warnings


def test_check_measurements_by_id(ro):
    specimen = count(ro, "SELECT specimen_id FROM specimen WHERE label = '1 pc'")
    assert entry.check_measurements_update(ro, specimen, sl_cm=999).warnings == [DESC_SL_GT_TL]
    assert entry.check_measurements_update(ro, "1pc", sl_cm=999).warnings == [DESC_SL_GT_TL]
    with pytest.raises(NotFoundError, match="Особи №999999"):
        entry.check_measurements_update(ro, 999999, tl_cm=5)
    with pytest.raises(NotFoundError, match="не найдена"):
        entry.check_measurements_update(ro, "9999 pc", tl_cm=5)


def test_update_measurements_by_id(rw):
    specimen = count(rw, "SELECT specimen_id FROM specimen WHERE label = '160'")
    result = entry.update_measurements(rw, specimen, tl_cm="6,1")
    assert "Особь 160: TL 3.1 → 6.1" in result.message


# ---------------------------------------------------------------------------
# Промеры числами
# ---------------------------------------------------------------------------


def test_measurements_stored_as_numbers(ro):
    """После импорта все промеры — числа (REAL) или пусто: форма получает float, не текст."""
    for column in entry.MEASUREMENTS:
        # имя колонки — из кода (entry.MEASUREMENTS), не от пользователя
        bad = count(ro, f"SELECT count(*) FROM specimen WHERE typeof({column}) NOT IN "
                        "('real', 'null')")  # fmt: skip
        assert bad == 0, column


def test_specimen_measurements(ro):
    specimen = count(ro, "SELECT specimen_id FROM specimen WHERE label = '160'")
    item = browse.specimen_measurements(ro, "160")
    assert item == browse.specimen_measurements(ro, specimen)
    assert item["specimen_id"] == specimen and item["label"] == "160"
    assert item["tl_cm"] == 3.1 and item["sl_cm"] == 5.9
    for column in entry.MEASUREMENTS:
        assert item[column] is None or isinstance(item[column], float), column
        assert item[column] == browse.get_record(ro, "specimen", specimen)[column]
    assert browse.specimen_measurements(ro, "26ph")["label"] == "26 ph"
    with pytest.raises(NotFoundError):
        browse.specimen_measurements(ro, "9999 pc")
    with pytest.raises(NotFoundError):
        browse.specimen_measurements(ro, 999999)


def test_specimen_measurements_after_integer_entry(rw):
    """Целое число из формы (30) хранится в REAL-колонке и читается как 30.0."""
    result = entry.create_specimen(rw, 1, "Plotosus canius", tl_cm=30, mass_g=200)
    item = browse.specimen_measurements(rw, result.record_id)
    assert (item["tl_cm"], item["mass_g"], item["sl_cm"]) == (30.0, 200.0, None)
    assert isinstance(item["tl_cm"], float)


# ---------------------------------------------------------------------------
# preview_site
# ---------------------------------------------------------------------------


def test_preview_site_nearby(path, ro, rw):
    before = sha256(path)
    name, nearby = entry.preview_site(ro, "точка рыбака", lat=9.5188, lon=106.2083)
    assert sha256(path) == before
    assert name == "Точка рыбака (9.5188, 106.2083)"
    fisher = count(ro, "SELECT site_id FROM sampling_site WHERE abs(lat - ?) < 1e-7",
                   FISHER_LAT)  # fmt: skip
    assert [site.site_id for site in nearby] == [fisher]
    # create_site без подтверждения откажет, перечислив те же места
    text = error_text(entry.create_site, rw, "точка рыбака", lat=9.5188, lon=106.2083)
    assert all(site.describe() in text for site in nearby)


def test_preview_site_same_river_name(ro):
    """Участок реки у того же города — то же место; город нормализуется, как при импорте."""
    name, nearby = entry.preview_site(ro, "участок реки", city="Хонг-на", region="Донг-хап")
    assert name == "Участок у Хонг-нгу"
    assert [site.name for site in nearby] == ["Участок у Хонг-нгу"]
    assert nearby[0].distance_km is None  # совпало по названию


def test_preview_site_new_matches_create(path, ro, rw):
    before = sha256(path)
    name, nearby = entry.preview_site(
        ro, "рынок", city="Шокчанг", region="Шокчанг", new_region=True
    )
    assert sha256(path) == before
    assert (name, nearby) == ("Рынок Шокчанг", [])
    result = entry.create_site(rw, "рынок", city="Шокчанг", region="Шокчанг", new_region=True)
    assert count(rw, "SELECT name FROM sampling_site WHERE site_id = ?", result.record_id) == name


@pytest.mark.parametrize(
    "kwargs",
    [
        {"site_type": "озеро", "name": "X"},
        {"site_type": "точка рыбака", "lat": 9.5},
        {"site_type": "точка рыбака", "lat": 95, "lon": 106},
        {"site_type": "точка рыбака", "lat": "abc", "lon": 106},
        {"site_type": "участок реки"},
        {"site_type": "участок реки", "city": "Тест"},
        {"site_type": "участок реки", "city": "Тест", "region": "Нет такой"},
        {"site_type": "рынок", "name": "Рынок X", "region": "Кантхо"},
    ],
)
def test_preview_site_errors_match_create(path, ro, rw, kwargs):
    kwargs = dict(kwargs)
    site_type = kwargs.pop("site_type")
    before = sha256(path)
    preview = error_text(entry.preview_site, ro, site_type, **kwargs)
    assert sha256(path) == before
    assert preview == error_text(entry.create_site, rw, site_type, **kwargs)


# ---------------------------------------------------------------------------
# Фиксации
# ---------------------------------------------------------------------------


def test_preservatives_cover_import(ro):
    """Все фиксации, записанные импортом, есть в entry.PRESERVATIVES (константа не отстала)."""
    imported = {
        r[0] for r in ro.execute("SELECT DISTINCT preservative FROM sample")
    } - {None}  # fmt: skip
    assert imported and imported <= set(entry.PRESERVATIVES)
    items = browse.list_preservatives(ro)
    assert [i["preservative"] for i in items] == list(entry.PRESERVATIVES)
    for item in items:
        assert item["sample_count"] == count(
            ro, "SELECT count(*) FROM sample WHERE preservative = ?", item["preservative"]
        )


def test_preservatives_include_new_values(rw):
    entry.add_sample(rw, "1 pc", "ДНК", sample_label="1 pc rna", preservative="RNAlater")
    items = browse.list_preservatives(rw)
    assert items[-1] == {"preservative": "RNAlater", "sample_count": 1}
    assert browse.field_choices()["preservative"] == list(entry.PRESERVATIVES)


def test_preview_functions_on_read_only_connection(path):
    """Все функции предпросмотра работают через connect_read_only; файл не меняется."""
    before = sha256(path)
    conn = connect_read_only(path)
    try:
        entry.check_specimen(conn, 1, "Plotosus canius", tl_cm=10, sl_cm=11)
        entry.check_measurements_update(conn, "1 pc", tl_cm=1)
        entry.preview_site(conn, "точка рыбака", lat=FISHER_LAT, lon=FISHER_LON)
        browse.specimen_measurements(conn, "1 pc")
        browse.list_preservatives(conn)
    finally:
        conn.close()
    assert sha256(path) == before


# ---------------------------------------------------------------------------
# Сообщения: нейтральные в логике, флаги — только в командной строке
# ---------------------------------------------------------------------------

CLI_FLAGS = ("--new-lab", "--new-region", "--region", "--new", "--mesh", "--label", "--tl")


def test_logic_messages_have_no_cli_flags(rw):
    """Тексты ошибок и предупреждений логики годятся и для сайта: без флагов командной строки."""
    texts = [
        error_text(entry.ship_sample, rw, 1, "Новая лаборатория"),
        error_text(entry.create_site, rw, "рынок", city="Тест", region="Нет такой"),
        error_text(entry.create_site, rw, "рынок", city="Тест"),
        error_text(entry.create_site, rw, "участок реки"),
        error_text(entry.create_site, rw, "точка рыбака", lat=FISHER_LAT, lon=FISHER_LON),
        error_text(entry.create_specimen, rw, 1, "?"),
        error_text(entry.update_measurements, rw, "1 pc"),
    ]
    expedition = entry.create_expedition(rw, "Кантхо 2027").record_id
    site = count(rw, "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'")
    texts += entry.create_event(rw, expedition, site, "2026-09-01", "траление").warnings
    for text in texts:
        assert not any(flag in text for flag in CLI_FLAGS), text
        assert "python -m biobank" not in text, text
        # у каждого такого текста в командной строке есть подсказка
        assert with_cli_hints(text) != text, text


def test_every_cli_hint_is_used():
    """Каждый кусок текста из CLI_HINTS действительно встречается в коде логики.

    db.py — сообщение о рабочей базе (go-live) для import и init."""
    from pathlib import Path

    import biobank

    package = Path(biobank.__file__).parent
    code = "".join(
        (package / name).read_text() for name in ("entry.py", "records.py", "edit.py", "db.py")
    )
    for marker, _ in CLI_HINTS:
        assert marker in code, marker


def run(path, *args):
    return main(["--db", str(path), *args])


@pytest.mark.parametrize(
    "args, hint",
    [
        (("add-site", "рынок", "--city", "Тест", "--region", "Нет такой"), "--new-region"),
        (("add-site", "рынок", "--city", "Тест"), "--region"),
        (("add-site", "участок реки"), "--name"),
        (("add-site", "точка рыбака", "--lat", "9.5188", "--lon", "106.2083"), "флаг --new"),
        (("ship-sample", "1", "Новая лаборатория"), "--new-lab"),
        (("add-specimen", "1", "?"), "--label"),
        (("update-measurements", "1 pc"), "--tl"),
        (("add-specimen", "999", "Plotosus canius"), "python -m biobank sql"),
    ],
)
def test_cli_prints_flag_hints(path, capsys, args, hint):
    assert run(path, *args) == 1
    err = capsys.readouterr().err
    assert "в командной строке:" in err and hint in err
    assert "База не изменена" in err


def test_cli_warning_hint(path, capsys):
    assert run(path, "add-expedition", "Кантхо 2027") == 0
    conn = connect(path)
    site = str(count(conn, "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'"))
    conn.close()
    assert run(path, "add-event", "4", site, "2026-09-01", "траление") == 0
    out = capsys.readouterr().out
    assert "Внимание: Сеть не указана" in out and "в командной строке: --mesh, --size" in out


def test_edit_messages_neutral_with_cli_hints(path, tmp_path, capsys):
    """edit.py: тексты без команд CLI; подсказку дописывает командная строка."""
    from biobank.edit import apply_edit, check_database, resolve_issue

    missing = tmp_path / "нет.db"
    texts = [
        error_text(apply_edit, missing, lambda conn: None),
        error_text(check_database, missing),
    ]
    conn = connect(path)
    texts.append(error_text(resolve_issue, conn, 999999, "решено"))
    conn.close()
    for text in texts:
        assert "python -m biobank" not in text, text
        assert "в командной строке: " in with_cli_hints(text), text
    assert "Сначала создайте её импортом из Excel" in texts[0]

    assert run(path, "resolve-issue", "999999", "решено") == 1
    err = capsys.readouterr().err
    assert (
        "нет в журнале" in err
        and "в командной строке: список: python -m biobank report issues" in err
    )
