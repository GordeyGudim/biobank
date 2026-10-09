"""Тесты browse.py: списки и карточки для веба (только чтение)."""

import shutil

import pytest
from conftest import SOURCE, count

from biobank import browse
from biobank.card import specimen_card
from biobank.db import connect, connect_read_only
from biobank.edit import NotFoundError, resolve_issue

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture
def ro(db_path):
    """Подключение только на чтение: все функции browse должны работать через него."""
    conn = connect_read_only(db_path)
    yield conn
    conn.close()


def labels(page):
    return [item["label"] for item in page.items]


def event_id_of(conn, sheet):
    return count(conn, "SELECT event_id FROM sampling_event WHERE source_sheet = ?", sheet)


def site_id_of(conn, label):
    """Место поимки особи: своё, если есть, иначе место выезда."""
    return count(
        conn,
        "SELECT coalesce(s.capture_site_id, e.site_id) FROM specimen s "
        "JOIN sampling_event e USING (event_id) WHERE s.label = ?",
        label,
    )


# ---------------------------------------------------------------------------
# Номера проб: нормализация, естественный порядок, автодополнение
# ---------------------------------------------------------------------------


def test_natural_order(ro):
    page = browse.list_specimens(ro, capture_method="рыбак", per_page=500)
    pc = [label for label in labels(page) if label.endswith(" pc")]
    numbers = [int(label.split()[0]) for label in pc]
    assert numbers == sorted(numbers)  # 2 pc раньше 10 pc, хотя по алфавиту наоборот
    assert pc.index("118 pc") < pc.index("138 pc")


def test_label_without_number_is_last(ro):
    page = browse.list_specimens(ro, per_page=500)
    assert page.total == 462
    assert labels(page)[-1].startswith("ХХ")
    assert labels(page)[:2] == ["1", "1 e"]


def test_order_by_series(ro):
    """Сначала вся серия Pangasius, потом pc, Bg, mr; внутри серии — по числу."""
    page = browse.list_specimens(ro, per_page=500)
    rows = [(item["label"], item["specimen_id"]) for item in page.items]
    series = [
        ro.execute("SELECT series FROM specimen WHERE specimen_id = ?", (sid,)).fetchone()[0]
        for _, sid in rows
    ]
    blocks = [s for i, s in enumerate(series) if i == 0 or s != series[i - 1]]
    assert blocks == ["Pangasius", "pc", "Bg", "mr", None]  # каждая серия одним куском
    pc = [label for label, _ in rows if label.endswith(" pc")]
    assert pc[:3] == ["1 pc", "2 pc", "3 pc"]
    assert labels(page)[series.index("pc") - 1].startswith("277")


def test_suggest_labels(ro):
    assert browse.suggest_labels(ro, "2pc") == ["2 pc"]
    assert browse.suggest_labels(ro, "40 рс") == ["40 pc"]  # кириллические «р», «с»
    assert browse.suggest_labels(ro, "15", limit=3) == ["15", "15 pc", "15 sp"]
    assert browse.suggest_labels(ro, "  ") == []
    assert browse.suggest_labels(ro, "%") == []  # «%» — обычный символ, не шаблон LIKE
    assert len(browse.suggest_labels(ro, "1", limit=1000)) == 50  # не больше 50


def test_label_prefix_filter(ro):
    page = browse.list_specimens(ro, label_prefix="155p")
    assert labels(page) == ["155 pc"]
    page = browse.list_specimens(ro, label_prefix="15")
    assert labels(page)[:3] == ["15", "15 sp", "150"]  # сначала вся серия Pangasius
    assert all(label.startswith("15") for label in labels(page))


# ---------------------------------------------------------------------------
# Фильтры
# ---------------------------------------------------------------------------


def test_filter_expedition_and_event(ro):
    assert browse.list_specimens(ro, expedition_id=1).total == 97
    event = event_id_of(ro, "Точка 2")
    page = browse.list_specimens(ro, event_id=event, per_page=100)
    assert page.total == 27
    assert {item["source_sheet"] for item in page.items} == {"Точка 2"}
    assert page.items[0]["event_title"] == "«Точка 2»"


def test_filter_site_uses_capture_site(ro):
    """138–142 pc пойманы рыбаком на другом месте — фильтр по месту находит их там."""
    site = site_id_of(ro, "140 pc")
    page = browse.list_specimens(ro, site_id=site, per_page=100)
    assert {"138 pc", "142 pc"} <= set(labels(page))
    assert page.total == 25
    # а на месте самого выезда «Точка 6 2026» их нет
    event_site = count(ro, "SELECT site_id FROM sampling_event WHERE source_sheet = 'Точка 6 2026'")
    assert "140 pc" not in labels(browse.list_specimens(ro, site_id=event_site, per_page=100))


def test_filter_capture_method_coalesce(ro):
    """Способ: свой у особи (рыбак внутри выезда-траления), иначе — выезда."""
    fisher = labels(browse.list_specimens(ro, capture_method="рыбак", per_page=500))
    trawl = labels(browse.list_specimens(ro, capture_method="траление", per_page=500))
    assert "140 pc" in fisher and "140 pc" not in trawl
    assert "10 pc" in trawl
    assert "26 ph" in labels(browse.list_specimens(ro, capture_method="аквахозяйство", per_page=50))


def test_filter_species_and_unidentified(ro):
    plotosus = count(ro, "SELECT species_id FROM species WHERE scientific_name = 'Plotosus canius'")
    page = browse.list_specimens(ro, species_id=plotosus)
    assert page.total == count(ro, "SELECT count(*) FROM specimen WHERE species_id = ?", plotosus)
    assert {item["species"] for item in page.items} == {"Plotosus canius"}
    unknown = browse.list_specimens(ro, unidentified=True)
    assert unknown.total == count(ro, "SELECT count(*) FROM specimen WHERE species_id IS NULL")
    assert all(item["species"] is None for item in unknown.items)
    assert all(item["confidence"] == "не определён" for item in unknown.items)


def test_filter_open_issues(ro):
    page = browse.list_specimens(ro, has_open_issues=True, per_page=500)
    found = dict((item["label"], item["open_issue_count"]) for item in page.items)
    assert found["155 pc"] >= 1  # SL стала датой
    assert found["6 e"] >= 1  # в свёрнутой записи «5 e, 6 e, 7 e, 8 e»
    assert "9 e" not in found
    assert all(n > 0 for n in found.values())


def test_filters_combine_and_unknown_id(ro):
    page = browse.list_specimens(ro, expedition_id=1, label_prefix="26")
    assert labels(page) == ["26 ph"]
    assert browse.list_specimens(ro, expedition_id=999).total == 0


def test_specimen_item_fields(ro):
    item = browse.list_specimens(ro, label_prefix="155 pc").items[0]
    assert item["sl_cm"] == pytest.approx(29.9)
    assert item["species"] == "Plotosus canius"
    assert item["sample_count"] == 5
    assert item["capture_method"] == "рыбак"
    assert item["expedition"] == "Кантхо 2026"


def test_order_by_date(ro):
    page = browse.list_specimens(ro, order="date", per_page=500)
    dates = [item["event_date"] for item in page.items]
    assert dates == sorted(dates)
    with pytest.raises(ValueError, match="Сортировка"):
        browse.list_specimens(ro, order="label; DROP TABLE specimen")


# ---------------------------------------------------------------------------
# Постраничность
# ---------------------------------------------------------------------------


def test_pagination(ro):
    first = browse.list_specimens(ro, per_page=50)
    second = browse.list_specimens(ro, per_page=50, page=2)
    assert (first.total, first.pages, first.page) == (462, 10, 1)
    assert len(first.items) == 50
    assert not set(labels(first)) & set(labels(second))  # страницы не пересекаются
    last = browse.list_specimens(ro, per_page=50, page=10)
    assert len(last.items) == 12


def test_pagination_out_of_range(ro):
    assert browse.list_specimens(ro, per_page=50, page=1000).page == 10
    assert browse.list_specimens(ro, per_page=50, page=0).page == 1
    assert browse.list_specimens(ro, per_page=0).per_page == 1
    assert browse.list_specimens(ro, per_page=10**6).per_page == browse.MAX_PER_PAGE
    empty = browse.list_specimens(ro, expedition_id=999)
    assert (empty.items, empty.pages, empty.page) == ([], 1, 1)


# ---------------------------------------------------------------------------
# Экспедиции, выезды, места, справочники
# ---------------------------------------------------------------------------


def test_expeditions(ro):
    exps = browse.list_expeditions(ro)
    assert [x["name"] for x in exps] == ["Донг-хап 2025", "Виньлонг 2025", "Кантхо 2026"]
    assert sum(x["specimen_count"] for x in exps) == 462
    assert sum(x["event_count"] for x in exps) == 24


def test_events(ro):
    assert len(browse.list_events(ro)) == 24
    events = browse.list_events(ro, expedition_id=1)
    assert len(events) == 6
    first = events[0]
    assert (first["title"], first["trawl_count"], first["specimen_count"]) == ("«Точка 1»", 4, 16)
    with pytest.raises(NotFoundError):
        browse.list_events(ro, expedition_id=999)


def test_event_trawls(ro):
    trawls = browse.event_trawls(ro, event_id_of(ro, "Точка 3"))
    assert [t["trawl_no"] for t in trawls] == [1, 2, 3, 4, 5, 6]
    assert browse.event_trawls(ro, event_id_of(ro, "Аквахозяйство 1")) == []
    with pytest.raises(NotFoundError):
        browse.event_trawls(ro, 999)


def test_event_card(ro):
    sections = {sec.title: sec for sec in browse.event_card(ro, event_id_of(ro, "Точка 6."))}
    main = sections["Выезд"]
    fields = dict(main.rows)
    assert fields["выезд"] == "«Точка 6.»"
    assert len(main.row_ids) == len(main.rows)
    assert main.row_ids[[r[0] for r in main.rows].index("место")]["site_id"] is not None
    assert len(sections["Траления"].rows) == 5
    assert all("trawling_id" in ids for ids in sections["Траления"].row_ids)
    water = sections["Замеры воды"]
    assert len(water.rows) == 11  # 5 тралений × старт/финиш + точка лова рыбака
    specimens = sections["Особи"]
    assert len(specimens.rows) == 45
    assert all(ids["specimen_id"] for ids in specimens.row_ids)
    issues = sections["Проблемы в журнале (выезд, его особи и траления)"]
    assert any("не привязаны" in row[6] for row in issues.rows)


def test_event_card_catch(ro):
    event = count(ro, "SELECT event_id FROM catch_record LIMIT 1")
    catch = {sec.title: sec for sec in browse.event_card(ro, event)}["Прилов и общий улов"]
    assert catch.rows
    assert all("catch_id" in ids for ids in catch.row_ids)


def test_event_card_not_found(ro):
    with pytest.raises(NotFoundError, match="999"):
        browse.event_card(ro, 999)


def test_sites(ro):
    sites = browse.list_sites(ro)
    assert len(sites) == count(ro, "SELECT count(*) FROM sampling_site")
    without_city = [s for s in sites if s["city"] is None]
    assert without_city and all(s["region"] is None for s in without_city)
    fisher = next(s for s in sites if s["lat"] == pytest.approx(9.5187059))
    assert fisher["event_count"] == 2


def test_site_card(ro):
    site = site_id_of(ro, "140 pc")
    sections = {sec.title: sec for sec in browse.site_card(ro, site)}
    assert len(sections["Выезды на это место"].rows) == 2
    assert all("event_id" in ids for ids in sections["Выезды на это место"].row_ids)
    other = sections["Особи, пойманные здесь на выездах в другие места"]
    assert [row[0] for row in other.rows] == ["138 pc", "139 pc", "140 pc", "141 pc", "142 pc"]
    with pytest.raises(NotFoundError):
        browse.site_card(ro, 999)


def test_species_and_methods(ro):
    species = browse.list_species(ro)
    assert len(species) == 8
    assert sum(s["specimen_count"] for s in species) == count(
        ro, "SELECT count(*) FROM specimen WHERE species_id IS NOT NULL"
    )
    assert browse.list_capture_methods(ro) == ["траление", "рыбак", "рынок", "аквахозяйство"]


# ---------------------------------------------------------------------------
# Журнал проблем
# ---------------------------------------------------------------------------


def test_issues(ro):
    everything = browse.list_issues(ro, per_page=500)
    assert everything.total == count(ro, "SELECT count(*) FROM data_issue")
    measurements = browse.list_issues(ro, category="промеры")
    assert measurements.total > 0
    assert {i["category"] for i in measurements.items} == {"промеры"}
    page = browse.list_issues(ro, table_name="sampling_event", per_page=5, page=2)
    assert page.page == 2 and len(page.items) == 5
    assert {i["table_name"] for i in page.items} == {"sampling_event"}


def test_issue_categories(ro):
    cats = {c["category"]: c for c in browse.issue_categories(ro)}
    assert cats["промеры"]["total"] == cats["промеры"]["open"] + cats["промеры"]["resolved"]


def test_issues_resolved_filter(db_path, tmp_path):
    path = tmp_path / "copy.db"
    shutil.copy(db_path, path)
    conn = connect(path)
    try:
        issue = browse.list_issues(conn, category="промеры").items[0]["issue_id"]
        resolve_issue(conn, issue, "сверено с журналом")
        done = browse.list_issues(conn, resolved=True)
        opened = browse.list_issues(conn, resolved=False, per_page=500)
        all_issues = browse.list_issues(conn, per_page=500)
    finally:
        conn.close()
    assert [i["issue_id"] for i in done.items] == [issue]
    assert issue not in [i["issue_id"] for i in opened.items]
    assert done.total + opened.total == all_issues.total
    assert all_issues.items[-1]["issue_id"] == issue  # решённые — в конце


# ---------------------------------------------------------------------------
# Только чтение и id в карточке особи
# ---------------------------------------------------------------------------


def test_browse_does_not_write(db_path, tmp_path):
    """Все функции работают через connect_read_only, и файл базы не меняется."""
    path = tmp_path / "copy.db"
    shutil.copy(db_path, path)
    before = path.read_bytes()
    conn = connect_read_only(path)
    try:
        browse.list_specimens(conn, has_open_issues=True, label_prefix="1")
        browse.list_expeditions(conn)
        browse.list_events(conn, 1)
        browse.event_card(conn, 1)
        browse.event_trawls(conn, 1)
        browse.list_sites(conn)
        browse.site_card(conn, 1)
        browse.list_issues(conn, resolved=False, category="промеры")
        browse.issue_categories(conn)
        browse.list_species(conn)
        browse.suggest_labels(conn, "1")
    finally:
        conn.close()
    assert path.read_bytes() == before


def test_specimen_card_ids(ro):
    sections = {sec.title.split(":")[0]: sec for sec in specimen_card(ro, "155 pc")}
    main = sections["Особь"]
    ids = dict(zip([row[0] for row in main.rows], main.row_ids, strict=True))
    specimen = count(ro, "SELECT specimen_id FROM specimen WHERE label = '155 pc'")
    assert ids["номер пробы"] == {"specimen_id": specimen}
    assert ids["выезд (лист)"]["event_id"] == count(
        ro, "SELECT event_id FROM specimen WHERE label = '155 pc'"
    )
    assert ids["место"]["site_id"] == site_id_of(ro, "155 pc")
    assert ids["траление"] == {"trawling_id": None}  # неизвестно
    assert ids["TL"] == {}
    samples = sections["Образцы"]
    assert len(samples.row_ids) == len(samples.rows) == 5
    sample_ids = {r["sample_id"] for r in samples.row_ids}
    assert sample_ids == {
        r[0] for r in ro.execute("SELECT sample_id FROM sample WHERE specimen_id = ?", (specimen,))
    }
    assert "_sample_id" not in samples.columns  # служебные id не показываются
    issues = sections["Проблемы в журнале (особь и её выезд)"]
    assert [ids["issue_id"] for ids in issues.row_ids] == [row[0] for row in issues.rows]
    water = sections["Вода"]
    assert len(water.row_ids) == len(water.rows)
