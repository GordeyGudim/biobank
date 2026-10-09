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
    for text in ("Поиск", "Отчёты", "Журнал проблем", 'name="label"', "<label"):
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
    assert html.count('class="resolve"') == open_issues
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
        f"/issues/{issue_id}/resolve?resolution=x",  # запись по GET — 405
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
