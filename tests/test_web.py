"""Тесты веб-интерфейса через тестовый клиент Flask.

app.test_client() — «браузер понарошку»: отправляет запросы приложению прямо в памяти,
без запуска сервера. Каждый тест работает с копией импортированной базы во временной
папке (фикстура path) — рабочая база output/biobank.db не трогается.
Если Flask не установлен (нет группы [web]), весь файл пропускается.
"""

import hashlib
import shutil
from urllib.parse import quote

import pytest

pytest.importorskip("flask")

from conftest import SOURCE, count  # noqa: E402

from biobank.__main__ import main  # noqa: E402
from biobank.db import connect  # noqa: E402
from biobank.web.app import create_app  # noqa: E402

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")

HTMX = {"HX-Request": "true"}  # так помечает свои запросы HTMX


@pytest.fixture
def path(db_path, tmp_path):
    p = tmp_path / "output" / "biobank.db"
    p.parent.mkdir()
    shutil.copy(db_path, p)
    return p


@pytest.fixture
def client(path):
    return create_app(path).test_client()


def backups(path):
    folder = path.parent / "backups"
    return sorted(folder.iterdir()) if folder.exists() else []


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_open_issue(path):
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT issue_id FROM data_issue WHERE resolved = 0 ORDER BY issue_id LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()


def issue_row(path, issue_id):
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT resolved, resolution FROM data_issue WHERE issue_id = ?", (issue_id,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Страницы просмотра
# ---------------------------------------------------------------------------


def test_index_has_menu_and_search(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for text in ("Поиск", "Особи", "Экспедиции", "Места", "Отчёты", "Журнал проблем",
                 'name="label"', "<label"):  # fmt: skip
        assert text in html
    assert "/static/htmx.min.js" in html  # HTMX — локальный файл, не CDN
    assert "unpkg" not in html and "cdn" not in html.lower()


def test_static_files_served(client):
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_search_redirects_to_card(client):
    r = client.get("/search?label=155pc")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/specimen/155pc")


def test_specimen_card(client):
    r = client.get("/specimen/155pc")  # любое написание номера
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Особь 155 pc" in html
    assert "Plotosus canius" in html
    assert "29.9 см" in html  # SL, восстановленная из «даты» — как в базе
    assert "неизвестно" in html  # траление неизвестно — так и написано
    for title in ("История определений вида", "Образцы", "Результаты анализов", "Вода"):
        assert title in html
    assert "(пока нет)" in html  # анализов нет — раздел всё равно показан
    assert "Открытых проблем в журнале" in html  # 155 pc: SL стала датой


def test_card_shows_all_columns_even_empty(client):
    """Решённый вопрос 5: все колонки, даже пустые, — у всех особей одинаковый вид."""
    html = client.get("/specimen/155%20pc").get_data(as_text=True)
    for column in ("пол", "примечания", "фиксация", "отправлен", "в таблице записано"):
        assert column in html
    assert 'title="не заполнено"' in html  # пустое значение — прочерк с подсказкой


def test_label_with_spaces_and_cyrillic_in_url(client):
    assert client.get("/specimen/" + quote("26 ph")).status_code == 200
    r = client.get("/specimen/" + quote("ХХ (Точка 1)"))
    assert r.status_code == 200
    assert "Особь ХХ (Точка 1)" in r.get_data(as_text=True)


def test_unknown_specimen_404_with_hint(client):
    r = client.get("/specimen/9999 zz")
    assert r.status_code == 404
    html = r.get_data(as_text=True)
    assert "не найдена" in html
    assert 'name="label"' in html  # форма поиска, чтобы сразу попробовать снова

    r = client.get("/specimen/13ph")  # такого номера нет, есть похожие с числом 13
    assert r.status_code == 404
    assert "Похожие" in r.get_data(as_text=True)


def test_empty_search_shows_error_near_field(client):
    r = client.get("/search?label=+")
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert 'class="field-error"' in html and "Введите номер пробы" in html


@pytest.mark.parametrize("name", ["species", "samples", "water", "sites", "issues"])
def test_reports(client, name):
    assert client.get("/reports").status_code == 200
    r = client.get(f"/reports/{name}")
    assert r.status_code == 200
    assert "<table" in r.get_data(as_text=True)


def test_report_content(client):
    html = client.get("/reports/species").get_data(as_text=True)
    assert "Plotosus canius" in html and "Итого по видам" in html


def test_unknown_report_and_page_404(client):
    assert client.get("/reports/nope").status_code == 404
    r = client.get("/nope")
    assert r.status_code == 404
    assert "Такой страницы нет" in r.get_data(as_text=True)


def test_issues_page_has_form_per_open_issue(client, path):
    r = client.get("/issues")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    conn = connect(path)
    open_issues = count(conn, "SELECT count(*) FROM data_issue WHERE resolved = 0")
    conn.close()
    # журнал постраничный: на первой странице — до 50 записей, у каждой открытой — форма
    assert html.count('class="resolve"') == min(open_issues, 50)
    assert 'hx-post="/issues/' in html
    assert "hx-disabled-elt" in html and "hx-sync" in html  # защита от двойного нажатия


def test_get_pages_do_not_change_database(client, path):
    """Просмотр открывает базу только на чтение: файл не меняется, копий нет."""
    before = digest(path)
    issue_id = first_open_issue(path)
    for url in [
        "/",
        "/search?label=26ph",
        "/specimen/26 ph",
        "/specimen/нет такой",
        "/reports",
        *[f"/reports/{n}" for n in ("species", "samples", "water", "sites", "issues")],
        "/issues",
        "/issues?status=all&page=2",
        "/specimens",
        "/specimens?expedition=1&event=1&species=none&issues=1&order=date&page=2",
        "/suggest?label=15",
        "/expeditions",
        "/expeditions/1",
        "/events/1",
        "/sites",
        "/sites/1",
        f"/issues/{issue_id}/resolve?resolution=x",  # запись по GET — 405
        "/specimen/1/link-trawl?trawl_no=2",  # тоже 405
        "/specimen/1/identify?species=x",
    ]:
        assert client.get(url).status_code < 500, url
    assert client.get(f"/issues/{issue_id}/resolve").status_code == 405
    assert digest(path) == before
    assert backups(path) == []
    assert issue_row(path, issue_id)["resolved"] == 0


# ---------------------------------------------------------------------------
# Кнопка «Решено»
# ---------------------------------------------------------------------------


def test_resolve_via_htmx_changes_db_and_makes_backup(client, path):
    issue_id = first_open_issue(path)
    r = client.post(
        f"/issues/{issue_id}/resolve", data={"resolution": "проверили по журналу"}, headers=HTMX
    )
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Решено" in html and "проверили по журналу" in html
    assert "<html" not in html  # фрагмент, а не целая страница
    row = issue_row(path, issue_id)
    assert row["resolved"] == 1 and row["resolution"] == "проверили по журналу"
    assert len(backups(path)) == 1  # резервная копия до правки


def test_double_resolve_is_message_not_500(client, path):
    issue_id = first_open_issue(path)
    url = f"/issues/{issue_id}/resolve"
    assert client.post(url, data={"resolution": "первый раз"}, headers=HTMX).status_code == 200
    r = client.post(url, data={"resolution": "второй раз"}, headers=HTMX)
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert "уже решена" in html and 'class="field-error"' in html
    assert issue_row(path, issue_id)["resolution"] == "первый раз"  # не перезаписано
    assert len(backups(path)) == 1  # при ошибке копия не остаётся


def test_resolve_empty_text_shows_error(client, path):
    issue_id = first_open_issue(path)
    before = digest(path)
    r = client.post(f"/issues/{issue_id}/resolve", data={"resolution": "  "}, headers=HTMX)
    assert r.status_code == 400
    assert "пустой текст" in r.get_data(as_text=True)
    assert digest(path) == before and backups(path) == []


def test_resolve_unknown_issue_404(client, path):
    r = client.post("/issues/999999/resolve", data={"resolution": "x"}, headers=HTMX)
    assert r.status_code == 404
    assert "нет в журнале" in r.get_data(as_text=True)


def test_resolve_without_htmx_redirects(client, path):
    """Без JavaScript — обычная форма: POST → redirect на журнал → сообщение."""
    issue_id = first_open_issue(path)
    r = client.post(f"/issues/{issue_id}/resolve", data={"resolution": "готово"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/issues")
    page = client.get("/issues").get_data(as_text=True)
    assert f"Проблема №{issue_id} закрыта" in page
    assert f'id="issue-{issue_id}"' not in page  # решённой больше нет в списке открытых

    r = client.post(f"/issues/{issue_id}/resolve", data={"resolution": "ещё раз"})
    assert r.status_code == 302
    assert "уже решена" in client.get("/issues").get_data(as_text=True)


def test_values_are_escaped(client, path):
    """Jinja2 экранирует текст: HTML из ввода показывается как текст, а не выполняется."""
    issue_id = first_open_issue(path)
    r = client.post(
        f"/issues/{issue_id}/resolve", data={"resolution": "<script>alert(1)</script>"},
        headers=HTMX,
    )  # fmt: skip
    html = r.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_cross_site_post_rejected(client, path):
    issue_id = first_open_issue(path)
    r = client.post(
        f"/issues/{issue_id}/resolve",
        data={"resolution": "x"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert issue_row(path, issue_id)["resolved"] == 0 and backups(path) == []


def test_foreign_host_rejected(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_missing_database_page(tmp_path):
    client = create_app(tmp_path / "nope.db").test_client()
    r = client.get("/reports/species")
    assert r.status_code == 503
    assert "Файла базы нет" in r.get_data(as_text=True)
    assert not (tmp_path / "nope.db").exists()  # подключение на чтение файл не создаёт


# ---------------------------------------------------------------------------
# Команда python -m biobank web
# ---------------------------------------------------------------------------


def test_web_command_listens_only_on_localhost(path, monkeypatch):
    calls = {}
    monkeypatch.setattr("flask.Flask.run", lambda self, **kw: calls.update(kw))
    assert main(["--db", str(path), "web", "--port", "5050"]) == 0
    assert calls["host"] == "127.0.0.1" and calls["port"] == 5050


def test_web_command_without_db(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "nope.db"), "web"]) == 1


# ---------------------------------------------------------------------------
# Этап B: списки, навигация, формы карточки
# ---------------------------------------------------------------------------

FRAGMENT = {"HX-Request": "true", "HX-Target": "results"}  # HTMX просит только таблицу


def db_value(path, sql, *params):
    conn = connect(path)
    try:
        return count(conn, sql, *params)
    finally:
        conn.close()


def found(html):
    """Число из строки «Найдено особей: N»."""
    return int(html.split("Найдено особей: <strong>")[1].split("<")[0])


def test_menu_on_new_pages(client):
    for url in ("/specimens", "/expeditions", "/sites", "/issues", "/specimen/1"):
        html = client.get(url).get_data(as_text=True)
        for href in ('href="/specimens"', 'href="/expeditions"', 'href="/sites"',
                     'href="/issues"', 'href="/reports"'):  # fmt: skip
            assert href in html, (url, href)
        assert 'id="top-label"' in html  # поиск в шапке


def test_specimens_list_first_page(client, path):
    r = client.get("/specimens")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    total = db_value(path, "SELECT count(*) FROM specimen")
    assert found(html) == total
    assert html.count('<td><a href="/specimen/') == 50  # 50 номеров на страницу — ссылки
    assert "страница 1 из" in html and 'rel="next"' in html
    assert "<label" in html and 'name="expedition"' in html and 'hx-push-url="true"' in html


@pytest.mark.parametrize(
    ("query", "sql"),
    [
        ("expedition=1", "SELECT count(*) FROM specimen JOIN sampling_event USING (event_id) "
                         "WHERE expedition_id = 1"),
        ("expedition=1&event=1", "SELECT count(*) FROM specimen WHERE event_id = 1"),
        ("species=6", "SELECT count(*) FROM specimen WHERE species_id = 6"),
        ("species=none", "SELECT count(*) FROM specimen WHERE species_id IS NULL"),
        ("label=155pc", "SELECT count(*) FROM specimen WHERE label LIKE '155 pc%'"),
        ("method=рыбак", None),
        ("issues=1", None),
        ("site=1&order=date", "SELECT count(*) FROM specimen s JOIN sampling_event e "
                              "USING (event_id) WHERE coalesce(s.capture_site_id, e.site_id) = 1"),
    ],
)  # fmt: skip
def test_specimens_filters(client, path, query, sql):
    r = client.get(f"/specimens?{query}")
    assert r.status_code == 200
    n = found(r.get_data(as_text=True))
    if sql:
        assert n == db_value(path, sql)
    else:
        assert 0 < n < db_value(path, "SELECT count(*) FROM specimen")


def test_specimens_label_filter_any_spelling(client):
    a = client.get("/specimens?label=155pc").get_data(as_text=True)
    b = client.get("/specimens?label=155+%D1%80%D1%81").get_data(as_text=True)  # «155 рс» кирилл.
    assert found(a) == found(b) == 1
    assert 'href="/specimen/155%20pc"' in a


def test_specimens_pagination(client):
    html = client.get("/specimens?page=2").get_data(as_text=True)
    assert "страница 2 из" in html and 'rel="prev"' in html
    assert "показаны 51–100" in html
    # номер страницы за пределами — последняя страница, не ошибка
    assert client.get("/specimens?page=99999").status_code == 200
    # ссылка «вперёд» сохраняет фильтры
    html = client.get("/specimens?expedition=2").get_data(as_text=True)
    assert "/specimens?page=2&amp;expedition=2" in html


def test_specimens_event_list_only_of_chosen_expedition(client):
    html = client.get("/specimens?expedition=1").get_data(as_text=True)
    select = html.split('id="f-event"')[1].split("</select>")[0]
    assert "Точка 1»" in select and "2026" not in select
    # выезд чужой экспедиции (экспедицию сменили) — фильтр по выезду сбрасывается
    html = client.get("/specimens?expedition=2&event=1").get_data(as_text=True)
    assert found(html) == found(client.get("/specimens?expedition=2").get_data(as_text=True))


def test_specimens_htmx_returns_fragment_with_event_select(client):
    r = client.get("/specimens?expedition=1", headers=FRAGMENT)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "<html" not in html and 'id="results"' in html
    assert 'hx-swap-oob="true"' in html  # список выездов обновляется тем же ответом
    assert "HX-Request" in r.headers.get("Vary", "")
    # «Назад» в браузере (восстановление истории) — вся страница
    r = client.get("/specimens", headers={**FRAGMENT, "HX-History-Restore-Request": "true"})
    assert "<html" in r.get_data(as_text=True)


def test_specimens_bad_filters_are_messages_not_errors(client):
    r = client.get("/specimens?expedition=abc&order=zzz&page=x")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "не номер, фильтр не учтён" in html and "Сортировка «zzz» не подходит" in html
    r = client.get("/specimens?expedition=999")
    assert r.status_code == 200
    assert "Экспедиции №999 нет" in r.get_data(as_text=True)


def test_suggest_labels(client):
    r = client.get("/suggest?label=15")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert html.index('value="15"') < html.index('value="15 pc"') < html.index('value="150"')
    assert "<html" not in html
    assert "<option" not in client.get("/suggest?label=").get_data(as_text=True)
    page = client.get("/").get_data(as_text=True)
    assert 'hx-get="/suggest"' in page and "<datalist" in page and "delay:300ms" in page


def test_expeditions_pages(client):
    r = client.get("/expeditions")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for name in ("Донг-хап", "Виньлонг", "Кантхо"):
        assert name in html
    assert 'href="/expeditions/1"' in html and 'href="/specimens?expedition=1"' in html
    r = client.get("/expeditions/1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert html.count('href="/events/') == 6  # 6 выездов Донг-хап
    assert "Аквахозяйство 1" in html and 'href="/sites/' in html


def test_event_card(client):
    r = client.get("/events/1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for title in ("Траления", "Замеры воды", "Особи", "Прилов", "Проблемы в журнале"):
        assert title in html
    assert 'id="trawling-1"' in html  # строки тралений — цели ссылок «траление»
    assert 'href="/specimen/1"' in html  # особи выезда — ссылки на карточки
    assert 'href="/expeditions/1"' in html and 'href="/sites/1"' in html
    assert 'href="/issues/' in html  # проблемы — ссылки на страницу проблемы


def test_site_card(client, path):
    site_id = db_value(path, "SELECT site_id FROM sampling_site WHERE abs(lat - 9.5187059) < 1e-6")
    r = client.get(f"/sites/{site_id}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Выезды на это место" in html and html.count('href="/events/') >= 2
    assert 'href="/specimen/138%20pc"' in html  # особи рыбака с выезда в другое место
    r = client.get("/sites")
    assert r.status_code == 200 and f'href="/sites/{site_id}"' in r.get_data(as_text=True)


@pytest.mark.parametrize("url", ["/expeditions/999", "/events/999", "/sites/999"])
def test_unknown_ids_404(client, url):
    r = client.get(url)
    assert r.status_code == 404
    assert "нет" in r.get_data(as_text=True)


def test_specimen_card_links(client, path):
    html = client.get("/specimen/118pc").get_data(as_text=True)
    event_id = db_value(path, "SELECT event_id FROM specimen WHERE label = '118 pc'")
    site_id = db_value(path, "SELECT capture_site_id FROM specimen WHERE label = '118 pc'")
    assert f'href="/events/{event_id}"' in html
    assert f'href="/sites/{site_id}"' in html  # своё место поимки (рыбак)
    assert 'href="/expeditions/' in html
    assert 'href="/specimens?species=6"' in html  # вид → список особей вида
    assert 'href="/issues/' in html
    # траление неизвестно — так и написано, без ссылки
    assert "<td>неизвестно</td>" in html


def test_issues_filters_and_pages(client, path):
    html = client.get("/issues?category=связь").get_data(as_text=True)
    n = db_value(path, "SELECT count(*) FROM data_issue WHERE category = 'связь' AND resolved = 0")
    assert f"Записей: <strong>{n}</strong>" in html
    total = db_value(path, "SELECT count(*) FROM data_issue")
    html = client.get("/issues?status=all").get_data(as_text=True)
    assert f"Записей: <strong>{total}</strong>" in html
    if total > 50:
        assert "страница 1 из" in html
        assert client.get("/issues?status=all&page=2").status_code == 200
    html = client.get("/issues?status=resolved").get_data(as_text=True)
    assert "По этим фильтрам записей нет" in html  # после импорта решённых нет
    r = client.get("/issues?status=zzz")
    assert r.status_code == 200 and "не подходит" in r.get_data(as_text=True)
    r = client.get("/issues?category=связь", headers=FRAGMENT)
    assert "<html" not in r.get_data(as_text=True)


def test_resolved_issue_visible_with_resolved_filter(client, path):
    issue_id = first_open_issue(path)
    r = client.post(f"/issues/{issue_id}/resolve", data={"resolution": "сверили с журналом"})
    assert r.status_code == 302
    html = client.get("/issues?status=resolved").get_data(as_text=True)
    assert f'id="issue-{issue_id}"' in html and "сверили с журналом" in html
    assert f'id="issue-{issue_id}"' not in client.get("/issues").get_data(as_text=True)


# ---- форма «Привязать к тралению» ----


def specimen_row(path, label):
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT s.specimen_id, s.trawling_id, t.trawl_no, s.species_id "
            "FROM specimen s LEFT JOIN trawling t USING (trawling_id) WHERE s.label = ?",
            (label,),
        ).fetchone()
    finally:
        conn.close()


def test_card_has_edit_forms(client):
    html = client.get("/specimen/1").get_data(as_text=True)
    assert 'action="/specimen/1/link-trawl"' in html and 'action="/specimen/1/identify"' in html
    assert html.count('method="post"') >= 2
    assert html.count("hx-disabled-elt") >= 2 and html.count('hx-sync="this:drop"') >= 2
    for label in ('for="trawl_no"', 'for="id-species"', 'for="id-method"',
                  'for="id-confidence"', 'for="id-date"', 'for="id-notes"'):  # fmt: skip
        assert label in html
    # особь не из траления (аквахозяйство) — формы «Привязать к тралению» нет
    html = client.get("/specimen/26 ph").get_data(as_text=True)
    assert "/link-trawl" not in html and "Привязать к тралению" not in html


def test_link_trawl_form_changes_db(client, path):
    assert specimen_row(path, "1")["trawling_id"] is None
    r = client.post("/specimen/1/link-trawl", data={"trawl_no": "2"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/specimen/1")
    assert specimen_row(path, "1")["trawl_no"] == 2
    assert len(backups(path)) == 1
    html = client.get("/specimen/1").get_data(as_text=True)
    assert "привязана к тралению 2" in html  # сообщение один раз, на карточке
    trawling_id = specimen_row(path, "1")["trawling_id"]
    assert f'href="/trawlings/{trawling_id}"' in html  # траление — ссылка на его страницу


def test_link_trawl_form_htmx(client, path):
    r = client.post("/specimen/1/link-trawl", data={"trawl_no": "3"}, headers=HTMX,
                    follow_redirects=True)  # fmt: skip
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="card"' in html and "привязана к тралению 3" in html
    assert specimen_row(path, "1")["trawl_no"] == 3


@pytest.mark.parametrize(
    ("label", "trawl_no", "status", "message"),
    [
        ("1", "77", 400, "нет траления 77"),
        ("1", "", 400, "Выберите траление"),
        ("1", "abc", 400, "Выберите траление"),
        ("118pc", "1", 400, "получена не тралением"),
        ("26 ph", "1", 400, "нет тралений"),
        ("9999 zz", "1", 404, "не найдена"),
    ],
)
def test_link_trawl_form_errors(client, path, label, trawl_no, status, message):
    before = digest(path)
    r = client.post(f"/specimen/{label}/link-trawl", data={"trawl_no": trawl_no}, headers=HTMX)
    assert r.status_code == status
    html = r.get_data(as_text=True)
    assert message in html
    if status == 400:
        assert 'class="field-error"' in html and 'id="card"' in html
    assert digest(path) == before and backups(path) == []


def test_link_trawl_cross_site_rejected(client, path):
    r = client.post("/specimen/1/link-trawl", data={"trawl_no": "2"},
                    headers={"Origin": "http://evil.example"})  # fmt: skip
    assert r.status_code == 403
    assert specimen_row(path, "1")["trawling_id"] is None and backups(path) == []


# ---- форма «Новое определение вида» ----


def test_identify_form_changes_db(client, path):
    n_before = db_value(path, "SELECT count(*) FROM species_identification")
    data = {
        "species": "Pangasius elongatus",
        "method": "ДНК",
        "confidence": "точно",
        "identified_on": "2026-10-01",
        "identified_by": "лаборатория",
        "notes": "<script>alert(1)</script>",
    }
    r = client.post("/specimen/1/identify", data=data)
    assert r.status_code == 302
    assert specimen_row(path, "1")["species_id"] == db_value(
        path, "SELECT species_id FROM species WHERE scientific_name = 'Pangasius elongatus'"
    )
    assert db_value(path, "SELECT count(*) FROM species_identification") == n_before + 1
    assert len(backups(path)) == 1
    html = client.get("/specimen/1").get_data(as_text=True)
    assert "Pangasius sp. → Pangasius elongatus" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_identify_unidentified(client, path):
    data = {"species": "", "method": "морфология", "confidence": "не определён"}
    assert client.post("/specimen/1/identify", data=data).status_code == 302
    assert specimen_row(path, "1")["species_id"] is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"species": "", "method": "ДНК", "confidence": "точно"}, "Без вида"),
        ({"species": "Plotosus canius", "method": "ДНК", "confidence": "не определён"},
         "вид не указывают"),
        ({"species": "Plotosus canius", "method": "на глаз", "confidence": "точно"},
         "Метод «на глаз» не подходит"),
        ({"species": "Plotosus canius", "method": "ДНК", "confidence": "точно",
          "identified_on": "01.10.2026"}, "не в формате ГГГГ-ММ-ДД"),
        ({"species": "Rattus rattus", "method": "ДНК", "confidence": "точно"}, "нет в справочнике"),
    ],
)  # fmt: skip
def test_identify_form_errors(client, path, data, message):
    before = digest(path)
    r = client.post("/specimen/1/identify", data=data, headers=HTMX)
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert message in html and 'class="field-error"' in html
    assert '<details class="edit-form" open>' in html  # форма с ошибкой раскрыта
    assert f'value="{data.get("identified_on", "")}"' in html  # введённое не пропало
    assert digest(path) == before and backups(path) == []


# ---------------------------------------------------------------------------
# Этап C: формы ввода новых записей (entry.py через apply_edit)
# ---------------------------------------------------------------------------


def location(r):
    """Адрес перехода после записи: обычная форма — redirect 303, HTMX — заголовок HX-Redirect."""
    return r.headers.get("HX-Redirect") or r.headers["Location"]


def last_id(url):
    return int(url.rstrip("/").rsplit("/", 1)[1])


def trawling_event(path):
    """Выезд-траление с тралениями (для форм карточки выезда)."""
    return db_value(
        path,
        "SELECT min(e.event_id) FROM sampling_event e JOIN capture_method m "
        "USING (capture_method_id) WHERE m.name = 'траление'",
    )


def market_event(path):
    return db_value(
        path,
        "SELECT min(e.event_id) FROM sampling_event e JOIN capture_method m "
        "USING (capture_method_id) WHERE m.name = 'рынок'",
    )


def next_pc(path):
    return db_value(path, "SELECT max(series_no) + 1 FROM specimen WHERE series = 'pc'")


GOOD_FISH = {"species": "Plotosus canius", "tl_cm": "25,4", "sl_cm": "22", "mass_g": "150",
             "mass_gutted_g": "140", "sex": "самка"}  # fmt: skip


def test_entry_forms_on_pages(client, path):
    """Формы ввода есть на своих страницах: POST, подписи, защита от двойного нажатия."""
    pages = {
        "/expeditions": ["/expeditions/new"],
        "/sites": ["/sites/new"],
        "/expeditions/3": ["/expeditions/3/events/new"],
        f"/events/{trawling_event(path)}": ["/specimens/new", "/trawlings/new", "/water/new",
                                           "/catch/new"],
        "/specimen/1": ["/specimen/1/samples/new", "/specimen/1/ship", "/specimen/1/measurements"],
    }  # fmt: skip
    for url, actions in pages.items():
        html = client.get(url).get_data(as_text=True)
        for action in actions:
            assert action in html, (url, action)
        assert 'method="post"' in html and 'hx-sync="this:drop"' in html
        assert 'hx-disabled-elt="find button"' in html
        assert 'title="обязательное поле"' in html  # обязательные поля помечены
    # у рынка нет тралений — и формы «Добавить траление» нет
    html = client.get(f"/events/{market_event(path)}").get_data(as_text=True)
    assert "/trawlings/new" not in html and "/specimens/new" in html
    # место в выезде — по группам типа места
    html = client.get("/expeditions/3").get_data(as_text=True)
    for group in ("Участки реки (траление)", "Рынки", "Точки рыбаков"):
        assert f'<optgroup label="{group}">' in html
    assert 'class="gear-fields' in html


def test_every_entry_field_has_label(client, path):
    """У каждого поля формы ввода есть <label for=…>."""
    import re

    for url in ("/expeditions", "/sites", "/expeditions/3", f"/events/{trawling_event(path)}",
                "/specimen/1"):  # fmt: skip
        html = client.get(url).get_data(as_text=True)
        for field_id in re.findall(r'<(?:input|select|textarea)[^>]*\bid="(f-[\w-]+)"', html):
            assert f'for="{field_id}"' in html, (url, field_id)


def test_full_entry_flow(client, path):
    """Экспедиция → место → выезд → траление → вода → прилов → особь → образец → отправка
    → промеры. Каждая запись — одна резервная копия; после записи — карточка новой записи."""
    r = client.post("/expeditions/new", data={"name": "Кантхо осень 2026", "notes": "тест"})
    assert r.status_code == 303 and "/expeditions/" in location(r)
    expedition_id = last_id(location(r))
    assert "Создана экспедиция" in client.get(location(r)).get_data(as_text=True)
    assert len(backups(path)) == 1

    site = {"site_type": "участок реки", "city": "Новоград", "region": "Кантхо",
            "lat": "10,07", "lon": "105,78"}  # fmt: skip
    r = client.post("/sites/new", data=site)
    assert r.status_code == 303
    site_id = last_id(location(r))
    assert db_value(path, "SELECT name FROM sampling_site WHERE site_id = ?", site_id) == (
        "Участок у Новоград"
    )
    assert len(backups(path)) == 2

    r = client.post(
        f"/expeditions/{expedition_id}/events/new",
        data={"site_id": site_id, "event_date": "15.09.2026", "capture_method": "траление",
              "gear_mesh": "3,5 х 3,5", "gear_size": "6,5x12"},
    )  # fmt: skip
    assert r.status_code == 303
    event_id = last_id(location(r))
    assert db_value(path, "SELECT event_date FROM sampling_event WHERE event_id = ?", event_id) == (
        "2026-09-15"
    )
    assert len(backups(path)) == 3

    trawl = {"trawl_no": "1", "time_start": "9.30", "time_end": "9:50", "duration_min": "20",
             "speed_kmh": "3,5"}  # fmt: skip
    r = client.post(f"/events/{event_id}/trawlings/new", data=trawl)
    assert r.status_code == 303 and location(r).endswith(f"/events/{event_id}")
    assert db_value(path, "SELECT time_start FROM trawling WHERE event_id = ?", event_id) == "09:30"

    water = {"point_type": "старт", "trawl_no": "1", "lat": "10,0731", "lon": "105,7829",
             "coord_source": "Garmin", "ph": "7,2", "o2_pct": "85"}  # fmt: skip
    assert client.post(f"/events/{event_id}/water/new", data=water).status_code == 303
    assert db_value(path, "SELECT ph FROM water_measurement WHERE event_id = ?", event_id) == 7.2

    catch = {"taxon_text": "arius maculatus", "count_n": "39", "trawl_no": "1"}
    assert client.post(f"/events/{event_id}/catch/new", data=catch).status_code == 303
    assert db_value(path, "SELECT raw_text FROM catch_record WHERE event_id = ?", event_id) == (
        "arius maculatus - 39 шт"
    )
    assert len(backups(path)) == 6

    label = f"{next_pc(path)} pc"
    r = client.post(f"/events/{event_id}/specimens/new", data={**GOOD_FISH, "trawl_no": "1"})
    assert r.status_code == 303 and location(r).endswith("/specimen/" + quote(label))
    row = specimen_row(path, label)
    assert row["trawl_no"] == 1
    assert db_value(path, "SELECT tl_cm FROM specimen WHERE label = ?", label) == 25.4
    assert len(backups(path)) == 7
    card = client.get(location(r)).get_data(as_text=True)
    assert f"Добавлена особь {label}" in card

    r = client.post(f"/specimen/{label}/samples/new",
                    data={"sample_type": "ДНК", "labs": "Россия\nТропцентр"})  # fmt: skip
    assert r.status_code == 303
    sample_id = db_value(path, "SELECT max(sample_id) FROM sample")
    assert (
        db_value(path, "SELECT count(*) FROM sample_shipment WHERE sample_id = ?", sample_id) == 2
    )

    r = client.post(f"/specimen/{label}/ship",
                    data={"sample_id": sample_id, "lab": "Кантхо (секвенирование)"})  # fmt: skip
    assert r.status_code == 303
    assert (
        db_value(path, "SELECT count(*) FROM sample_shipment WHERE sample_id = ?", sample_id) == 3
    )

    old = {"old_tl_cm": "25.4", "old_sl_cm": "22", "old_mass_g": "150", "old_mass_gutted_g": "140"}
    data = {**old, "tl_cm": "26", "sl_cm": "22", "mass_g": "150", "mass_gutted_g": "140"}
    r = client.post(f"/specimen/{label}/measurements", data=data)
    assert r.status_code == 303
    assert db_value(path, "SELECT tl_cm FROM specimen WHERE label = ?", label) == 26
    assert len(backups(path)) == 10
    assert "TL 25.4 → 26" in client.get(location(r)).get_data(as_text=True)


def test_htmx_success_uses_hx_redirect(client, path):
    r = client.post("/expeditions/new", data={"name": "Через HTMX"}, headers=HTMX)
    assert r.status_code == 204
    assert r.headers["HX-Redirect"].startswith("/expeditions/")
    assert len(backups(path)) == 1


def test_htmx_error_returns_form_with_entered_values(client, path):
    before = digest(path)
    r = client.post("/expeditions/new", data={"name": "Ошибка", "date_start": "32.13.2026"},
                    headers=HTMX)  # fmt: skip
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert 'id="f-expedition" open' in html and "не распознана" in html
    assert 'value="32.13.2026"' in html and 'value="Ошибка"' in html  # введённое не пропало
    assert digest(path) == before and backups(path) == []


SELECT_FIELDS = {"species", "sample_type", "site_type", "point_type", "capture_method",
                 "site_id", "sample_id", "sex", "trawl_no"}  # fmt: skip


def hard_error_cases(path):
    event_id = trawling_event(path)
    return [
        ("/expeditions/new", {"name": ""}, "Укажите название"),
        ("/expeditions/new", {"name": "Х", "date_start": "01.01.2099"}, "ещё не наступила"),
        ("/expeditions/new", {"name": "Донг-хап 2025"}, "уже есть"),
        ("/sites/new", {"site_type": "участок реки"}, "Укажите название места"),
        ("/sites/new", {"site_type": "рынок", "city": "Новоград", "region": "Атлантида"},
         "Провинции «Атлантида» нет"),
        ("/sites/new", {"site_type": "точка рыбака", "lat": "10,1"}, "Координаты нужны обе"),
        ("/sites/new", {"site_type": "точка рыбака", "lat": "100", "lon": "1"}, "Широта"),
        ("/expeditions/3/events/new", {"event_date": "2026-09-01", "capture_method": "рыбак"},
         "Выберите место"),
        ("/expeditions/3/events/new", {"site_id": "1", "event_date": "2026-09-01",
                                       "capture_method": "рыбак"}, "не подходит к месту"),
        ("/expeditions/3/events/new", {"site_id": "1", "event_date": "",
                                       "capture_method": "траление"}, "Укажите дату"),
        (f"/events/{event_id}/trawlings/new", {"trawl_no": "абв"}, "целое число"),
        (f"/events/{event_id}/trawlings/new", {"trawl_no": "1"}, "уже есть"),
        (f"/events/{event_id}/trawlings/new", {"trawl_no": "9", "time_start": "25:00"},
         "не время"),
        (f"/events/{event_id}/water/new", {"point_type": "старт"}, "укажите номер траления"),
        (f"/events/{event_id}/water/new", {"point_type": "точка", "ph": "15"},
         "pH не может быть больше 14"),
        (f"/events/{event_id}/catch/new", {}, "Укажите, что поймали"),
        (f"/events/{market_event(path)}/trawlings/new", {"trawl_no": "1"}, "а не траление"),
        (f"/events/{event_id}/specimens/new", {**GOOD_FISH, "tl_cm": "0"},
         "TL должна быть больше 0"),
        (f"/events/{event_id}/specimens/new", {**GOOD_FISH, "label": "1 pc"}, "уже занят"),
        (f"/events/{event_id}/specimens/new", {**GOOD_FISH, "species": ""}, "Выберите вид"),
        (f"/events/{event_id}/specimens/new", {"species": "?"}, "укажите номер явно"),
        (f"/events/{event_id}/specimens/new", {**GOOD_FISH, "species": "Rattus rattus"},
         "нет в справочнике"),
        (f"/events/{event_id}/specimens/new", {**GOOD_FISH, "trawl_no": "77"}, "нет траления 77"),
        ("/specimen/1/samples/new", {"sample_type": "кровь"}, "не подходит"),
        ("/specimen/1/samples/new", {"sample_type": "ДНК", "labs": "Марс"},
         "нет в справочнике"),
        ("/specimen/1/ship", {"lab": "Россия"}, "Выберите образец"),
        ("/specimen/1/measurements", {"tl_cm": "-1", "old_tl_cm": "93"}, "TL должна быть больше 0"),
        ("/specimen/1/measurements", {"tl_cm": "abc", "old_tl_cm": "93"}, "не число"),
        ("/specimen/1/measurements", {"tl_cm": "93", "old_tl_cm": "93"}, "не изменены"),
    ]  # fmt: skip


def test_hard_errors_400_nothing_written(client, path):
    """Жёсткая ошибка: 400, текст у формы, введённое на месте, база и копии не меняются."""
    before = digest(path)
    for url, data, message in hard_error_cases(path):
        r = client.post(url, data=data, headers=HTMX)
        html = r.get_data(as_text=True)
        assert r.status_code == 400, (url, data, r.status_code)
        assert message in html, (url, data, message)
        assert 'class="field-error' in html and " open>" in html, url
        for name, value in data.items():
            # введённое в текстовые поля — снова в полях; в списках выбора своего значения
            # не впишешь (Rattus rattus в списке видов нет) — их не проверяем
            is_select = name in SELECT_FIELDS and not url.endswith("/trawlings/new")
            if value and not is_select:
                assert f'value="{value}"' in html or f">{value}<" in html, (url, value)
        assert digest(path) == before and backups(path) == [], url


def test_ship_sample_new_lab_only_with_checkbox(client, path):
    sample_id = db_value(path, "SELECT min(sample_id) FROM sample")
    label = db_value(path, "SELECT sp.label FROM sample sa JOIN specimen sp USING (specimen_id) "
                           "WHERE sample_id = ?", sample_id)  # fmt: skip
    url = f"/specimen/{label}/ship"
    r = client.post(url, data={"sample_id": sample_id, "lab": "Лаборатория Ханоя"})
    assert r.status_code == 400 and "нет в справочнике" in r.get_data(as_text=True)
    assert db_value(path, "SELECT count(*) FROM lab WHERE name = 'Лаборатория Ханоя'") == 0
    assert backups(path) == []
    r = client.post(url, data={"sample_id": sample_id, "lab": "Лаборатория Ханоя", "new_lab": "1",
                               "sent_on": "01.10.2026"})  # fmt: skip
    assert r.status_code == 303
    assert db_value(path, "SELECT count(*) FROM lab WHERE name = 'Лаборатория Ханоя'") == 1
    assert "(новая лаборатория)" in client.get(location(r)).get_data(as_text=True)


def test_new_region_only_with_checkbox(client, path):
    data = {"site_type": "рынок", "city": "Новоград", "region": "Новая провинция"}
    r = client.post("/sites/new", data=data)
    assert r.status_code == 400 and backups(path) == []
    r = client.post("/sites/new", data={**data, "new_region": "1"})
    assert r.status_code == 303
    assert db_value(path, "SELECT count(*) FROM region WHERE name = 'Новая провинция'") == 1


# ---- мягкие предупреждения: сохранить только после «Всё равно сохранить» ----


def shown_warnings(html):
    """Тексты предупреждений из скрытых полей confirmed (их отправит «Всё равно сохранить»)."""
    import html as html_lib
    import re

    return [html_lib.unescape(v) for v in re.findall(r'name="confirmed" value="([^"]*)"', html)]


def test_specimen_soft_warning_needs_confirmation(client, path):
    event_id = trawling_event(path)
    n = db_value(path, "SELECT count(*) FROM specimen")
    label = f"{next_pc(path)} pc"
    data = {**GOOD_FISH, "tl_cm": "20", "sl_cm": "22"}  # SL > TL
    r = client.post(f"/events/{event_id}/specimens/new", data=data, headers=HTMX)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "SL больше TL" in html and "Всё равно сохранить" in html
    assert 'name="confirmed" value="SL больше TL"' in html
    assert f'value="{label}"' in html  # номер, под которым запишется, виден заранее
    assert db_value(path, "SELECT count(*) FROM specimen") == n and backups(path) == []

    # подтверждено другое предупреждение — снова спросить, не сохранять
    r = client.post(f"/events/{event_id}/specimens/new", data={**data, "confirmed": "что-то"})
    assert r.status_code == 200 and db_value(path, "SELECT count(*) FROM specimen") == n

    r = client.post(f"/events/{event_id}/specimens/new",
                    data={**data, "label": label, "confirmed": "SL больше TL"})  # fmt: skip
    assert r.status_code == 303
    assert db_value(path, "SELECT count(*) FROM specimen") == n + 1
    assert len(backups(path)) == 1
    specimen_id = specimen_row(path, label)["specimen_id"]
    assert (
        db_value(
            path,
            "SELECT count(*) FROM data_issue WHERE resolved = 0 AND table_name = 'specimen' "
            "AND record_id = ? AND description LIKE '%SL больше TL%'",
            specimen_id,
        )
        == 1
    )
    page = client.get(location(r)).get_data(as_text=True)
    assert "Внимание: SL больше TL" in page  # предупреждение после записи — жёлтым


def test_measurements_soft_warning_needs_confirmation(client, path):
    # особь 1: TL 93, SL 79, m 141, m без внутр. 111 — форма присылает показанное (old_…)
    old = {"old_tl_cm": "93", "old_sl_cm": "79", "old_mass_g": "141", "old_mass_gutted_g": "111"}
    tl_before = db_value(path, "SELECT tl_cm FROM specimen WHERE label = '1'")
    data = {**old, "tl_cm": "93", "sl_cm": "99", "mass_g": "141", "mass_gutted_g": "111"}
    r = client.post("/specimen/1/measurements", data=data, headers=HTMX)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "SL больше TL" in html and "Всё равно сохранить" in html
    assert "SL больше TL" in shown_warnings(html)
    assert db_value(path, "SELECT sl_cm FROM specimen WHERE label = '1'") == 79  # не записано
    assert backups(path) == []
    r = client.post("/specimen/1/measurements", data={**data, "confirmed": shown_warnings(html)})
    assert r.status_code == 303
    assert db_value(path, "SELECT sl_cm FROM specimen WHERE label = '1'") == 99
    assert db_value(path, "SELECT tl_cm FROM specimen WHERE label = '1'") == tl_before  # не тронут


def test_measurements_form_prefilled_from_card(client):
    html = client.get("/specimen/155pc").get_data(as_text=True)
    form = html.split('id="f-measure"')[1].split("</details>")[0]
    assert 'name="sl_cm" value="29.9"' in form and 'name="old_sl_cm" value="29.9"' in form


# ---- похожее место: предложение вместо дубля ----


def test_nearby_site_suggested_instead_of_duplicate(client, path):
    site_id = db_value(path, "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'")
    n = db_value(path, "SELECT count(*) FROM sampling_site")
    data = {"site_type": "участок реки", "city": "Кантхо", "region": "Кантхо"}
    r = client.post("/sites/new", data=data, headers=HTMX)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Такое место, похоже, уже есть" in html
    assert f'href="/sites/{site_id}"' in html and "Использовать это место" in html
    assert f'name="allow_nearby" value="{site_id}"' in html and "Всё равно создать новое" in html
    assert db_value(path, "SELECT count(*) FROM sampling_site") == n and backups(path) == []

    # подтверждение для других мест не годится — снова предложение
    r = client.post("/sites/new", data={**data, "allow_nearby": "99999"})
    assert r.status_code == 200 and db_value(path, "SELECT count(*) FROM sampling_site") == n

    r = client.post("/sites/new", data={**data, "allow_nearby": str(site_id)})
    assert r.status_code == 303
    assert db_value(path, "SELECT count(*) FROM sampling_site") == n + 1
    assert "Рядом есть место" in client.get(location(r)).get_data(as_text=True)


def test_nearby_fisher_point_by_distance(client, path):
    """Точка рыбака в ~10 м от существующей (9.5187059, 106.2082146) — то же место."""
    data = {"site_type": "точка рыбака", "lat": "9,51875", "lon": "106,20821"}
    r = client.post("/sites/new", data=data)
    assert r.status_code == 200 and " м)" in r.get_data(as_text=True)
    assert backups(path) == []


def test_use_existing_site_opens_event_form(client, path):
    html = client.get("/sites/13").get_data(as_text=True)
    assert "Добавить выезд на это место" in html
    assert 'href="/expeditions/3?site=13#f-event"' in html
    html = client.get("/expeditions/3?site=13").get_data(as_text=True)
    assert 'id="f-event" open' in html and '<option value="13" selected>' in html


# ---- кнопка «Следующий свободный», ссылки на формы ----


def test_next_label_fragment(client, path):
    r = client.get("/next-label?suffix=pc")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "<html" not in html and f'value="{next_pc(path)} pc"' in html
    r = client.get("/next-label?suffix=zz")
    assert r.status_code == 200 and "не известен" in r.get_data(as_text=True)
    page = client.get(f"/events/{trawling_event(path)}").get_data(as_text=True)
    assert 'hx-get="/next-label"' in page and 'hx-include="#f-specimen-suffix"' in page


def test_more_specimens_link_opens_form(client, path):
    html = client.get("/specimen/1").get_data(as_text=True)
    assert "?form=specimen#f-specimen" in html
    html = client.get("/events/1?form=specimen").get_data(as_text=True)
    assert 'id="f-specimen" open' in html


# ---- безопасность: только POST со своего сайта, GET ничего не пишет ----


def entry_urls(path):
    event_id = trawling_event(path)
    return [
        "/expeditions/new",
        "/sites/new",
        "/expeditions/3/events/new",
        f"/events/{event_id}/trawlings/new",
        f"/events/{event_id}/water/new",
        f"/events/{event_id}/catch/new",
        f"/events/{event_id}/specimens/new",
        "/specimen/1/samples/new",
        "/specimen/1/ship",
        "/specimen/1/measurements",
    ]


def test_entry_cross_site_post_rejected(client, path):
    before = digest(path)
    for url in entry_urls(path):
        r = client.post(url, data={"name": "x"}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403, url
        r = client.post(url, data={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403, url
    assert digest(path) == before and backups(path) == []


def test_entry_get_writes_nothing(client, path):
    before = digest(path)
    for url in entry_urls(path):
        # 405 — «так нельзя»; у адресов особи 404: /specimen/<номер> читает «1/ship» как номер
        assert client.get(url + "?name=x&trawl_no=1").status_code in (404, 405), url
    for url in ("/next-label?suffix=pc", "/expeditions/3?site=13", "/events/1?form=specimen",
                "/events/1?form=zzz", "/sites/13"):  # fmt: skip
        assert client.get(url).status_code == 200, url
    assert digest(path) == before and backups(path) == []


def test_entry_unknown_parent_404(client, path):
    before = digest(path)
    assert client.post("/expeditions/999/events/new", data={"site_id": "1"}).status_code == 404
    r = client.post("/events/99999/catch/new", data={"taxon_text": "x"})
    assert r.status_code == 404
    r = client.post("/specimen/9999 zz/samples/new", data={"sample_type": "ДНК"})
    assert r.status_code == 404
    assert digest(path) == before and backups(path) == []


def test_entry_values_escaped(client, path):
    r = client.post(
        "/expeditions/new", data={"name": "<b>жирно</b>", "notes": "<script>x</script>"}
    )
    assert r.status_code == 303
    html = client.get(location(r)).get_data(as_text=True)
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "<b>жирно</b>" not in html


# ---------------------------------------------------------------------------
# Этап C, часть 2: правка, удаление, пакетный ввод, страницы траления и проблемы
# ---------------------------------------------------------------------------

# вид записи на сайте → (таблица, ключ) — чтобы сравнить строку до и после правки
EDIT_TABLES = {
    "expedition": ("expedition", "expedition_id"),
    "site": ("sampling_site", "site_id"),
    "event": ("sampling_event", "event_id"),
    "trawling": ("trawling", "trawling_id"),
    "water": ("water_measurement", "measurement_id"),
    "specimen": ("specimen", "specimen_id"),
    "sample": ("sample", "sample_id"),
    "catch": ("catch_record", "catch_id"),
}


def db_row(path, kind, record_id):
    table, key = EDIT_TABLES[kind]
    conn = connect(path)
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (record_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def first_id(path, kind):
    table, key = EDIT_TABLES[kind]
    return db_value(path, f"SELECT min({key}) FROM {table}")


def edit_form_data(path, kind, record_id, **changes):
    """Что отправит форма правки: показанные значения (и old_…) + исправленные поля."""
    from biobank import browse
    from biobank.web.record_forms import KINDS, record_values

    conn = connect(path)
    try:
        rec = browse.get_record(conn, KINDS[kind].record, record_id)
    finally:
        conn.close()
    values = record_values(KINDS[kind], rec)
    return {**values, **{f"old_{k}": v for k, v in values.items()}, **changes}


def test_edit_fields_are_known_to_records():
    """Каждое поле формы правки — поле функции records.update_… (имена совпадают)."""
    from biobank import records
    from biobank.web.record_forms import KINDS

    allowed = {
        "expedition": records.EXPEDITION_FIELDS, "site": records.SITE_FIELDS,
        "event": records.EVENT_FIELDS, "trawling": records.TRAWLING_FIELDS,
        "water": records.WATER_FIELDS, "specimen": records.SPECIMEN_FIELDS,
        "sample": records.SAMPLE_FIELDS, "catch": records.CATCH_FIELDS,
    }  # fmt: skip
    for kind, spec in KINDS.items():
        names = {f.name for f in spec.fields}
        assert names <= set(allowed.get(kind, {})), kind


@pytest.mark.parametrize("kind", list(EDIT_TABLES))
def test_edit_form_prefilled(client, path, kind):
    record_id = first_id(path, kind)
    r = client.get(f"/edit/{kind}/{record_id}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'method="post"' in html and 'hx-sync="this:drop"' in html
    assert 'name="old_' in html and "Сохранить изменения" in html
    import re

    for field_id in re.findall(r'<(?:input|select|textarea)[^>]*\bid="(f-[\w-]+)"', html):
        assert f'for="{field_id}"' in html, (kind, field_id)


EDIT_CASES = [
    ("expedition", {"notes": "правка с сайта"}, "notes", "правка с сайта"),
    ("site", {"description": "новое описание"}, "description", "новое описание"),
    ("event", {"notes": "заметка"}, "notes", "заметка"),
    ("trawling", {"catch_note": "трал пустой"}, "catch_note", "трал пустой"),
    ("water", {"ph": "7,1"}, "ph", 7.1),
    ("specimen", {"sex": "самец"}, "sex", "самец"),
    ("sample", {"preservative": "формалин 10%"}, "preservative", "формалин 10%"),
    ("catch", {"mass_g": "12,5"}, "mass_g", 12.5),
]


@pytest.mark.parametrize(("kind", "change", "column", "expected"), EDIT_CASES)
def test_edit_changes_only_given_field(client, path, kind, change, column, expected):
    record_id = first_id(path, kind)
    before = db_row(path, kind, record_id)
    r = client.post(
        f"/edit/{kind}/{record_id}", data=edit_form_data(path, kind, record_id, **change)
    )
    assert r.status_code == 303, r.get_data(as_text=True)[-1500:]
    after = db_row(path, kind, record_id)
    assert after[column] == expected
    assert {k: v for k, v in after.items() if k != column} == {
        k: v for k, v in before.items() if k != column
    }
    assert len(backups(path)) == 1
    assert "→" in client.get(location(r)).get_data(as_text=True)  # сообщение «было → стало»


def test_edit_nothing_changed_is_error(client, path):
    before = digest(path)
    r = client.post("/edit/expedition/1", data=edit_form_data(path, "expedition", 1))
    assert r.status_code == 400 and "Ничего не изменено" in r.get_data(as_text=True)
    assert digest(path) == before and backups(path) == []


def test_edit_error_keeps_entered_values(client, path):
    before = digest(path)
    specimen_id = specimen_row(path, "2 pc")["specimen_id"]
    data = edit_form_data(path, "specimen", specimen_id, label="1 pc", notes="моя заметка")
    r = client.post(f"/edit/specimen/{specimen_id}", data=data, headers=HTMX)
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert "уже занят" in html and 'value="1 pc"' in html and "моя заметка" in html
    assert 'class="field-error' in html
    assert digest(path) == before and backups(path) == []


def test_edit_htmx_success_redirects(client, path):
    r = client.post("/edit/expedition/1", headers=HTMX,
                    data=edit_form_data(path, "expedition", 1, notes="через HTMX"))  # fmt: skip
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/expeditions/1"


def test_edit_specimen_label_warns_about_sample_labels(client, path):
    specimen_id = specimen_row(path, "155 pc")["specimen_id"]
    new = f"{next_pc(path)} pc"
    labels = db_value(path, "SELECT group_concat(label) FROM sample WHERE specimen_id = ?",
                      specimen_id)  # fmt: skip
    r = client.post(f"/edit/specimen/{specimen_id}",
                    data=edit_form_data(path, "specimen", specimen_id, label=new))  # fmt: skip
    assert r.status_code == 303 and location(r).endswith("/specimen/" + quote(new))
    page = client.get(location(r)).get_data(as_text=True)
    assert "Внимание:" in page and "метки образцов" in page
    assert labels == db_value(
        path, "SELECT group_concat(label) FROM sample WHERE specimen_id = ?", specimen_id
    )  # метки образцов не изменились


def test_edit_site_duplicate_needs_checkbox(client, path):
    other = db_value(path, "SELECT name FROM sampling_site WHERE site_id = 2")
    data = edit_form_data(path, "site", 1, name=other)
    r = client.post("/edit/site/1", data=data)
    assert r.status_code == 400 and "похоже, уже есть" in r.get_data(as_text=True)
    assert backups(path) == []
    r = client.post("/edit/site/1", data={**data, "allow_nearby": "1"})
    assert r.status_code == 303
    assert db_value(path, "SELECT name FROM sampling_site WHERE site_id = 1") == other


def test_edit_unknown_kind_or_record_404(client):
    assert client.get("/edit/zzz/1").status_code == 404
    assert client.get("/edit/specimen/99999").status_code == 404
    assert client.get("/edit/identification/1").status_code == 404  # только удаление
    assert client.get("/delete/sample/99999").status_code == 404


# ---- удаление ----


DELETE_KINDS = [*EDIT_TABLES, "identification"]


def test_delete_confirm_step_writes_nothing(client, path):
    before = digest(path)
    for kind in DELETE_KINDS:
        record_id = 1 if kind == "identification" else first_id(path, kind)
        url = f"/delete/{kind}/{record_id}"
        r = client.get(url)
        assert r.status_code == 200, url
        html = r.get_data(as_text=True)
        assert "Вы уверены? Будет удалено" in html and "Да, удалить" in html
        assert 'name="confirm" value="1"' in html and 'hx-sync="this:drop"' in html
        # POST без кнопки подтверждения — снова вопрос, ничего не удалено
        assert client.post(url).status_code == 200
    assert digest(path) == before and backups(path) == []


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("/delete/event/1", "особ"),
        ("/delete/expedition/1", "выезд"),
        ("/delete/site/1", "выезд"),
        ("/delete/specimen/1", "образ"),
        ("/delete/identification/1", "единственное определение"),
    ],
)
def test_delete_refused_with_dependents(client, path, url, message):
    before = digest(path)
    r = client.post(url, data={"confirm": "1"}, headers=HTMX)
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert "удалить нельзя" in html and message in html and 'class="flash flash-error' in html
    assert digest(path) == before and backups(path) == []


def test_delete_specimen_closes_its_issues(client, path):
    event_id = trawling_event(path)
    label = f"{next_pc(path)} pc"
    data = {**GOOD_FISH, "tl_cm": "20", "sl_cm": "22", "confirmed": "SL больше TL"}
    assert client.post(f"/events/{event_id}/specimens/new", data=data).status_code == 303
    specimen_id = specimen_row(path, label)["specimen_id"]
    issue_id = db_value(path, "SELECT issue_id FROM data_issue WHERE table_name = 'specimen' "
                              "AND record_id = ? AND resolved = 0", specimen_id)  # fmt: skip
    assert issue_id is not None

    r = client.post(f"/delete/specimen/{specimen_id}", data={"confirm": "1"})
    assert r.status_code == 303 and location(r).endswith(f"/events/{event_id}")
    assert specimen_row(path, label) is None
    assert db_value(path, "SELECT count(*) FROM species_identification WHERE specimen_id = ?",
                    specimen_id) == 0  # fmt: skip
    row = issue_row(path, issue_id)
    assert row["resolved"] == 1 and row["resolution"].startswith("Запись удалена")
    assert len(backups(path)) == 2  # ввод + удаление
    assert f"Удалена особь {label}" in client.get(location(r)).get_data(as_text=True)


def test_delete_trawling_with_its_water(client, path):
    trawling_id, event_id = 1, db_value(path, "SELECT event_id FROM trawling WHERE trawling_id = 1")
    assert db_value(path, "SELECT count(*) FROM water_measurement WHERE trawling_id = 1") == 2
    html = client.get("/delete/trawling/1").get_data(as_text=True)
    assert "замеры воды" in html  # что удалится вместе с ним
    r = client.post(f"/delete/trawling/{trawling_id}", data={"confirm": "1"}, headers=HTMX)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/events/{event_id}"
    assert db_row(path, "trawling", 1) is None
    assert db_value(path, "SELECT count(*) FROM water_measurement WHERE trawling_id = 1") == 0


def test_delete_new_expedition_goes_to_list(client, path):
    r = client.post("/expeditions/new", data={"name": "Удалить меня"})
    expedition_id = last_id(location(r))
    r = client.post(f"/delete/expedition/{expedition_id}", data={"confirm": "1"})
    assert r.status_code == 303 and location(r).endswith("/expeditions")
    assert db_row(path, "expedition", expedition_id) is None


def test_delete_identification_restores_species(client, path):
    species_before = specimen_row(path, "1")["species_id"]
    r = client.post(
        "/specimen/1/identify",
        data={"species": "Plotosus canius", "method": "ДНК", "confidence": "точно"},
    )
    assert r.status_code == 302
    ident = db_value(path, "SELECT max(identification_id) FROM species_identification")
    assert f"/delete/identification/{ident}" in client.get("/specimen/1").get_data(as_text=True)
    r = client.post(f"/delete/identification/{ident}", data={"confirm": "1"})
    assert r.status_code == 303 and location(r).endswith("/specimen/1")
    assert specimen_row(path, "1")["species_id"] == species_before


def test_delete_shipment(client, path):
    sample_id, lab_id = (
        700,
        db_value(path, "SELECT lab_id FROM sample_shipment WHERE sample_id = 700"),
    )
    label = db_value(path, "SELECT sp.label FROM sample sa JOIN specimen sp USING (specimen_id) "
                           "WHERE sa.sample_id = 700")  # fmt: skip
    card = client.get("/specimen/" + quote(label)).get_data(as_text=True)
    url = f"/delete/shipment/{sample_id}/{lab_id}"
    assert url in card
    assert "Вы уверены" in client.get(url).get_data(as_text=True)
    r = client.post(url, data={"confirm": "1"})
    assert r.status_code == 303 and location(r).endswith("/specimen/" + quote(label))
    assert db_value(path, "SELECT count(*) FROM sample_shipment WHERE sample_id = ? AND lab_id = ?",
                    sample_id, lab_id) == 0  # fmt: skip
    assert db_row(path, "sample", sample_id) is not None  # сам образец остался
    assert client.get(url).status_code == 404


def test_cards_have_edit_and_delete_buttons(client, path):
    html = client.get("/events/1").get_data(as_text=True)
    for part in ("/edit/event/1", "/delete/event/1", "/edit/specimen/", "/delete/trawling/",
                 "/edit/water/", "/specimens/batch"):  # fmt: skip
        assert part in html, part
    html = client.get("/specimen/155pc").get_data(as_text=True)
    for part in (
        "/edit/specimen/",
        "/delete/specimen/",
        "/edit/sample/",
        "/delete/identification/",
    ):
        assert part in html, part
    assert "/edit/identification/" not in html
    assert "/edit/expedition/1" in client.get("/expeditions/1").get_data(as_text=True)
    assert "/delete/site/1" in client.get("/sites/1").get_data(as_text=True)
    assert "/edit/" not in client.get("/reports/samples").get_data(as_text=True)


# ---- пакетный ввод особей ----


def batch_data(rows, action="check", **extra):
    data = {"action": action, **extra}
    for i, row in enumerate(rows, start=1):
        for name, value in row.items():
            data[f"r{i}-{name}"] = value
    return data


def test_batch_page_and_row_fragment(client, path):
    event_id = trawling_event(path)
    html = client.get(f"/events/{event_id}/specimens/batch").get_data(as_text=True)
    assert 'name="r10-label"' in html and 'name="r11-label"' not in html
    assert 'name="r1-trawl_no"' in html and 'name="r1-dna"' in html
    assert 'hx-get="/events/' in html and 'hx-swap="beforeend"' in html
    import re

    for field_id in re.findall(r'<(?:input|select|textarea)[^>]*\bid="(r\d+-\w+)"', html):
        assert f'for="{field_id}"' in html, field_id
    r = client.get(f"/events/{event_id}/specimens/batch/row?next=11")
    frag = r.get_data(as_text=True)
    assert r.status_code == 200 and "<html" not in frag
    assert 'name="r11-label"' in frag and 'value="12" hx-swap-oob="true"' in frag
    assert client.get(f"/events/{event_id}/specimens/batch/row?next=abc").status_code == 400
    assert client.get("/events/99999/specimens/batch").status_code == 404


def test_batch_check_writes_nothing(client, path):
    event_id = trawling_event(path)
    before = digest(path)
    rows = [{"species": "Plotosus canius", "tl_cm": "25,4"},
            {"species": "Plotosus canius", "tl_cm": "20", "sl_cm": "22"}]  # fmt: skip
    r = client.post(f"/events/{event_id}/specimens/batch", data=batch_data(rows))
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    n = next_pc(path)
    assert f"будет: <strong>{n} pc</strong>" in html and f"<strong>{n + 1} pc</strong>" in html
    assert "SL больше TL" in html and 'class="row-warn"' in html
    assert digest(path) == before and backups(path) == []


def test_batch_all_or_nothing(client, path):
    event_id = trawling_event(path)
    n = db_value(path, "SELECT count(*) FROM specimen")
    rows = [{"species": "Plotosus canius", "tl_cm": "25"},
            {"species": "Plotosus canius", "tl_cm": "0"}]  # fmt: skip
    r = client.post(f"/events/{event_id}/specimens/batch", data=batch_data(rows, "save"))
    assert r.status_code == 400
    html = r.get_data(as_text=True)
    assert "TL должна быть больше 0" in html and 'class="row-error"' in html
    assert "ничего не сохранено" in html
    assert 'name="r2-tl_cm" value="0"' in html  # введённое на месте
    assert db_value(path, "SELECT count(*) FROM specimen") == n and backups(path) == []


def test_batch_soft_warnings_need_confirmation(client, path):
    event_id = trawling_event(path)
    n = db_value(path, "SELECT count(*) FROM specimen")
    first = next_pc(path)
    rows = [
        {"species": "Plotosus canius", "tl_cm": "25,4", "dna": "1", "trawl_no": "1"},
        {},
        {"species": "Plotosus canius", "tl_cm": "20", "sl_cm": "22", "is_dead": "1"},
    ]
    url = f"/events/{event_id}/specimens/batch"
    r = client.post(url, data=batch_data(rows, "save"), headers=HTMX)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Всё равно сохранить" in html
    warnings = shown_warnings(html)
    assert warnings and all(w.startswith("строка 3") for w in warnings)  # пустая строка 2 — пропуск
    assert db_value(path, "SELECT count(*) FROM specimen") == n and backups(path) == []

    r = client.post(url, data=batch_data(rows, "save", confirmed=warnings))
    assert r.status_code == 303 and location(r).endswith(f"/events/{event_id}")
    assert db_value(path, "SELECT count(*) FROM specimen") == n + 2
    assert len(backups(path)) == 1
    assert specimen_row(path, f"{first} pc")["trawl_no"] == 1
    assert db_value(path, "SELECT is_dead FROM specimen WHERE label = ?", f"{first + 1} pc") == 1
    assert (
        db_value(
            path,
            "SELECT count(*) FROM sample sa JOIN specimen s USING (specimen_id) "
            "WHERE s.label = ? AND sa.sample_type = 'ДНК'",
            f"{first} pc",
        )
        == 1
    )
    page = client.get(location(r)).get_data(as_text=True)
    assert f"{first} pc, {first + 1} pc" in page  # список добавленных номеров


def test_batch_errors_without_database(client, path):
    event_id = trawling_event(path)
    url = f"/events/{event_id}/specimens/batch"
    r = client.post(url, data=batch_data([{"notes": "без вида"}], "save"))
    assert r.status_code == 400 and "Выберите вид" in r.get_data(as_text=True)
    r = client.post(url, data=batch_data([{}, {}], "save"))
    assert r.status_code == 400 and "Нет ни одной заполненной строки" in r.get_data(as_text=True)
    r = client.post(url, data=batch_data([{"label": "5 pc", "species": "Plotosus canius"}], "more"))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'name="r11-label"' in html and 'value="5 pc"' in html
    assert backups(path) == []


# ---- страница траления, страница проблемы, форма траления ----


def test_trawling_page(client, path):
    r = client.get("/trawlings/1")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for title in ("Замеры воды", "Особи из этого траления", "Прилов", "Проблемы в журнале"):
        assert title in html
    event_id = db_value(path, "SELECT event_id FROM trawling WHERE trawling_id = 1")
    assert f'href="/events/{event_id}"' in html and "/edit/trawling/1" in html
    assert 'href="/trawlings/1"' in client.get(f"/events/{event_id}").get_data(as_text=True)
    assert client.get("/trawlings/99999").status_code == 404


def test_issue_page(client, path):
    issue_id = first_open_issue(path)
    r = client.get(f"/issues/{issue_id}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert f"Проблема №{issue_id}" in html and f'action="/issues/{issue_id}/resolve"' in html
    assert f'href="/issues/{issue_id}"' in client.get("/issues").get_data(as_text=True)
    assert client.get("/issues/999999").status_code == 404


def test_link_trawl_form_only_for_trawled_specimens(client):
    assert "/specimen/1/link-trawl" in client.get("/specimen/1").get_data(as_text=True)
    html = client.get("/specimen/118pc").get_data(as_text=True)  # от рыбака на выезде-тралении
    assert "/link-trawl" not in html


# ---- безопасность новых адресов ----


def record_post_urls(path):
    event_id = trawling_event(path)
    return [
        "/edit/expedition/1", "/edit/specimen/1", "/edit/water/1",
        "/delete/event/1", "/delete/specimen/1", "/delete/identification/1",
        "/delete/shipment/700/1", f"/events/{event_id}/specimens/batch",
    ]  # fmt: skip


def test_record_forms_cross_site_post_rejected(client, path):
    before = digest(path)
    for url in record_post_urls(path):
        data = {"confirm": "1", "action": "save", "r1-species": "Plotosus canius", "notes": "x"}
        assert (
            client.post(url, data=data, headers={"Origin": "http://evil.example"}).status_code
            == 403
        )
        r = client.post(url, data=data, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403, url
    assert digest(path) == before and backups(path) == []


def test_record_pages_get_writes_nothing(client, path):
    before = digest(path)
    for url in [
        *record_post_urls(path),
        "/trawlings/1",
        f"/issues/{first_open_issue(path)}",
        "/events/1/specimens/batch/row?next=3",
        "/delete/specimen/1?confirm=1",
    ]:
        assert client.get(url).status_code == 200, url
    assert digest(path) == before and backups(path) == []


def test_entry_messages_without_cli_flags(client, path):
    """Тексты ошибок логики на сайте — без флагов командной строки (--label, allow_nearby)."""
    import re

    event_id = trawling_event(path)
    r = client.post(f"/events/{event_id}/specimens/new", data={"species": "?"})
    assert r.status_code == 400 and "укажите номер явно" in r.get_data(as_text=True)
    r2 = client.post(
        "/edit/site/1",
        data=edit_form_data(
            path, "site", 1, name=db_value(path, "SELECT name FROM sampling_site WHERE site_id = 2")
        ),
    )
    for html in (r.get_data(as_text=True), r2.get_data(as_text=True)):
        errors = " ".join(re.findall(r'class="[^"]*field-error[^"]*"[^>]*>([^<]*)<', html))
        assert errors and "--" not in errors and "allow_nearby" not in errors
