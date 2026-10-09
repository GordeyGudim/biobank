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
    assert 'href="/issues?status=open' in html  # проблемы — ссылки в журнал


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
    assert 'href="/issues?status=open' in html
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
    # у выезда без тралений — объяснение вместо формы
    html = client.get("/specimen/26 ph").get_data(as_text=True)
    assert "нет тралений" in html


def test_link_trawl_form_changes_db(client, path):
    assert specimen_row(path, "1")["trawling_id"] is None
    r = client.post("/specimen/1/link-trawl", data={"trawl_no": "2"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/specimen/1")
    assert specimen_row(path, "1")["trawl_no"] == 2
    assert len(backups(path)) == 1
    html = client.get("/specimen/1").get_data(as_text=True)
    assert "привязана к тралению 2" in html  # сообщение один раз, на карточке
    assert "#trawling-" in html  # траление в карточке — теперь ссылка


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
