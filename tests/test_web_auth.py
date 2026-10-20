# Фикстуры импортированы из test_web.py, а тесты принимают их параметрами с тем же именем —
# ruff считает это «переопределением» (F811); для pytest это обычный способ.
# ruff: noqa: F811
"""Тесты входа, ролей, админки и журнала изменений на сайте (этап D3, часть 2).

Пользователи тестовой базы и фикстуры (path, client — вошедший куратор, anon — без входа)
— из test_web.py. Вход в большинстве тестов — через сессию (login_as), без формы: хеш
пароля scrypt считается долго; сама форма входа проверяется отдельно.
"""

import os
import re
import time
from urllib.parse import unquote

import pytest

pytest.importorskip("flask")

from conftest import SOURCE  # noqa: E402

# фикстуры (path, client — вошедший куратор, anon — без входа) — из test_web.py:
# импортированная фикстура работает в этом файле так же, как своя
from test_web import (  # noqa: E402, F401
    GOOD_FISH,
    PASSWORD,
    anon,
    backups,
    clean_web_env,
    client,
    db_value,
    login_as,
    path,
    trawling_event,
    user_id,
    web_db,
)

from biobank import users  # noqa: E402
from biobank.db import connect  # noqa: E402
from biobank.web.app import create_app  # noqa: E402
from biobank.web.auth import IDLE_LIMIT, LOGIN_ATTEMPTS, LOGIN_ATTEMPTS_PER_LOGIN  # noqa: E402

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")

HTMX = {"HX-Request": "true"}


@pytest.fixture
def app(path):
    return create_app(path)


def as_user(app, path, login):
    return login_as(app.test_client(), path, login)


def changes(path):
    return db_value(path, "SELECT count(*) FROM change_log")


def excel_specimen(path):
    """Особь из Excel (created_by пустой): (id, номер)."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT specimen_id, label FROM specimen WHERE created_by IS NULL "
            "ORDER BY specimen_id LIMIT 1"
        ).fetchone()
        return row["specimen_id"], row["label"]
    finally:
        conn.close()


def html_of(response):
    return response.get_data(as_text=True)


def add_fish(client, path):
    """Новая особь через форму карточки выезда (номер — следующий свободный):
    вернуть (specimen_id, номер)."""
    event_id = trawling_event(path)
    r = client.post(f"/events/{event_id}/specimens/new", data=GOOD_FISH)
    assert r.status_code == 303, html_of(r)
    label = unquote(r.headers["Location"].rsplit("/specimen/", 1)[1])
    return db_value(path, "SELECT specimen_id FROM specimen WHERE label = ?", label), label


# ---------------------------------------------------------------------------
# Вход и выход
# ---------------------------------------------------------------------------


def test_pages_require_login(anon):
    r = anon.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    r = anon.get("/specimen/26 ph")
    assert r.status_code == 302
    assert "/login?next=/specimen/26" in r.headers["Location"]
    # HTMX-запрос не умеет переходить по обычному redirect — просим заголовком
    r = anon.get("/specimens", headers=HTMX)
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/login")
    # стили и htmx.min.js нужны и странице входа
    assert anon.get("/static/style.css").status_code == 200
    assert anon.get("/static/localtime.js").status_code == 200


def test_login_page_has_labels(anon):
    r = anon.get("/login")
    assert r.status_code == 200
    html = html_of(r)
    assert '<label for="login-name">' in html and '<label for="login-password">' in html
    assert 'type="password"' in html and 'method="post"' in html
    assert "Выйти" not in html and "Журнал изменений" not in html  # меню — только после входа


def test_login_and_logout(anon, path):
    r = anon.post(
        "/login", data={"login": "curator", "password": PASSWORD, "next": "/specimen/26 ph"}
    )
    assert r.status_code == 303 and r.headers["Location"] == "/specimen/26%20ph"
    assert db_value(path, "SELECT last_login_at IS NOT NULL FROM app_user WHERE login='curator'")
    html = html_of(anon.get("/"))
    assert "Имя curator" in html and "куратор" in html
    assert "Сменить пароль" in html and "Выйти" in html
    assert anon.get("/logout").status_code == 405  # выход — только POST (кнопкой)
    r = anon.post("/logout")
    assert r.status_code == 303 and r.headers["Location"].endswith("/login")
    assert anon.get("/").status_code == 302  # сессия больше не действует


def test_login_next_only_local(anon):
    r = anon.post(
        "/login", data={"login": "curator", "password": PASSWORD, "next": "//evil.example/x"}
    )
    assert r.status_code == 303 and r.headers["Location"] == "/"


def test_wrong_password_same_message(anon):
    for login, password in (("curator", "неверный-пароль"), ("nobody", PASSWORD)):
        r = anon.post("/login", data={"login": login, "password": password})
        assert r.status_code == 401
        assert users.LOGIN_FAILED in html_of(r)
        assert f'value="{login}"' in html_of(r)  # логин остаётся в поле, пароль — нет
    assert anon.get("/").status_code == 302


def test_login_rate_limit(app, path):
    client = app.test_client()
    limiter = app.extensions["biobank_login_limiter"]
    now = [1000.0]
    limiter.clock = lambda: now[0]
    for _ in range(LOGIN_ATTEMPTS):
        assert (
            client.post("/login", data={"login": "curator", "password": "x" * 12}).status_code
            == 401
        )
    r = client.post("/login", data={"login": "curator", "password": PASSWORD})
    assert r.status_code == 429  # даже верный пароль — пока ждём
    assert "Слишком много неудачных попыток" in html_of(r)
    # тот же адрес, другой логин — открыт: счётчик по паре «адрес + логин»
    assert client.post("/login", data={"login": "reader", "password": PASSWORD}).status_code == 303
    now[0] += 15 * 60 + 1
    r = client.post("/login", data={"login": "curator", "password": PASSWORD})
    assert r.status_code == 303


def test_login_cross_site_rejected(anon):
    r = anon.post(
        "/login",
        data={"login": "curator", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert anon.get("/").status_code == 302  # не вошли


def test_session_cookie_flags(path):
    client = create_app(path).test_client()
    r = client.post("/login", data={"login": "curator", "password": PASSWORD})
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
    secure = create_app(path, secure_cookies=True).test_client()
    r = secure.post("/login", data={"login": "curator", "password": PASSWORD})
    assert "Secure" in r.headers["Set-Cookie"]


def test_secret_key_persistent_file(path, monkeypatch):
    monkeypatch.delenv("BIOBANK_SECRET_KEY", raising=False)
    key_file = path.parent / "secret_key"
    first = create_app(path).config["SECRET_KEY"]
    assert key_file.exists() and (key_file.stat().st_mode & 0o777) == 0o600
    assert create_app(path).config["SECRET_KEY"] == first  # тот же ключ после перезапуска
    monkeypatch.setenv("BIOBANK_SECRET_KEY", "ключ-из-окружения")
    assert create_app(path).config["SECRET_KEY"] == "ключ-из-окружения"


def test_session_survives_restart(path, monkeypatch):
    """Ключ постоянный — после перезапуска сайта вход не слетает."""
    monkeypatch.delenv("BIOBANK_SECRET_KEY", raising=False)
    client = create_app(path).test_client()
    client.post("/login", data={"login": "curator", "password": PASSWORD})
    cookie = client.get_cookie("session")
    other = create_app(path).test_client()
    other.set_cookie("session", cookie.value)
    assert other.get("/").status_code == 200


def test_session_fixation_new_session_on_login(app, path):
    client = app.test_client()
    with client.session_transaction() as s:
        s["planted"] = "чужое"
    client.post("/login", data={"login": "curator", "password": PASSWORD})
    with client.session_transaction() as s:
        assert "planted" not in s and s["user_id"] == user_id(path, "curator")


def test_idle_logout(app, path):
    client = as_user(app, path, "curator")
    with client.session_transaction() as s:
        s["seen"] = time.time() - IDLE_LIMIT.total_seconds() - 60
    r = client.get("/specimens")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert "12 часов" in html_of(client.get("/login"))


def test_deactivated_user_loses_session_at_once(app, path):
    client = as_user(app, path, "researcher")
    assert client.get("/").status_code == 200
    conn = connect(path)
    try:
        users.deactivate(conn, "researcher")  # системный режим (как команда CLI)
    finally:
        conn.close()
    assert client.get("/").status_code == 302
    r = app.test_client().post("/login", data={"login": "researcher", "password": PASSWORD})
    assert r.status_code == 401


def test_removed_role_acts_at_once(app, path):
    client = as_user(app, path, "researcher")
    event_id = trawling_event(path)
    assert "Новая особь" in html_of(client.get(f"/events/{event_id}"))
    conn = connect(path)
    try:
        users.set_roles(conn, "researcher", ["читатель"])
    finally:
        conn.close()
    assert "Новая особь" not in html_of(client.get(f"/events/{event_id}"))


def test_must_change_password(app, path):
    conn = connect(path)
    try:
        temporary = users.reset_password(conn, "reader")
    finally:
        conn.close()
    client = app.test_client()
    r = client.post("/login", data={"login": "reader", "password": temporary})
    assert r.status_code == 303 and r.headers["Location"].endswith("/password")
    r = client.get("/specimens")  # никуда больше не пускаем
    assert r.status_code == 302 and r.headers["Location"].endswith("/password")
    html = html_of(client.get("/password"))
    assert "временным паролем" in html and '<label for="new-password">' in html
    r = client.post(
        "/password",
        data={
            "old_password": temporary,
            "new_password": "новый-пароль-42",
            "new_password2": "другой-пароль-42",
        },
    )
    assert r.status_code == 400 and "не совпадают" in html_of(r)
    r = client.post(
        "/password",
        data={"old_password": temporary, "new_password": "короткий", "new_password2": "короткий"},
    )
    assert r.status_code == 400 and "слишком короткий" in html_of(r)
    r = client.post(
        "/password",
        data={
            "old_password": temporary,
            "new_password": "новый-пароль-42",
            "new_password2": "новый-пароль-42",
        },
    )
    assert r.status_code == 303
    assert client.get("/specimens").status_code == 200
    assert not db_value(path, "SELECT must_change_password FROM app_user WHERE login='reader'")
    # выход разрешён и с временным паролем
    assert client.post("/logout").status_code == 303


def test_change_own_password_wrong_old(app, path):
    client = as_user(app, path, "reader")
    r = client.post(
        "/password",
        data={
            "old_password": "не-тот-пароль",
            "new_password": "новый-пароль-42",
            "new_password2": "новый-пароль-42",
        },
    )
    assert r.status_code == 400 and "Прежний пароль неверный" in html_of(r)


# ---------------------------------------------------------------------------
# Все POST-маршруты: без входа — на вход, у читателя — 403; база не меняется
# ---------------------------------------------------------------------------

# Значения параметров адреса для обхода всех маршрутов
SAMPLE_ARGS = {"label": "26 ph", "kind_name": "specimen", "table": "specimen", "key": "1"}
OPEN_POSTS = {"/login", "/logout", "/password"}


def post_urls(app):
    urls = []
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods:
            continue
        values = {arg: SAMPLE_ARGS.get(arg, 1) for arg in rule.arguments}
        urls.append(rule.build(values)[1])
    return sorted(urls)


def test_post_routes_found(app):
    urls = post_urls(app)
    assert len(urls) >= 25
    for part in (
        "/admin/users/new",
        "/people/merge",
        "/issues/new",
        "/specimen/26%20ph/identify",
        "/delete/specimen/1",
        "/events/1/specimens/batch",
    ):
        assert part in urls


def test_all_posts_without_login_redirect(app, path):
    client = app.test_client()
    before = changes(path)
    for url in post_urls(app):
        if url in OPEN_POSTS:
            continue
        r = client.post(
            url,
            data={
                "confirm": "1",
                "action": "save",
                "species": "?",
                "resolution": "x",
                "login": "x",
            },
        )
        assert r.status_code == 303, url
        assert "/login" in r.headers["Location"], url
    assert changes(path) == before
    assert backups(path) == []


def test_all_posts_as_reader_forbidden(app, path):
    client = as_user(app, path, "reader")
    before = changes(path)
    for url in post_urls(app):
        if url in OPEN_POSTS:
            continue
        r = client.post(
            url, data={"confirm": "1", "action": "save", "species": "?", "resolution": "x"}
        )
        assert r.status_code == 403, url
    assert changes(path) == before
    assert backups(path) == []


def test_admin_cannot_post_data(app, path):
    client = as_user(app, path, "admin")
    before = changes(path)
    for url in post_urls(app):
        if url in OPEN_POSTS or url.startswith("/admin/"):
            continue
        r = client.post(url, data={"confirm": "1", "action": "save", "resolution": "x"})
        assert r.status_code == 403, url
    assert changes(path) == before


# ---------------------------------------------------------------------------
# Кнопки по ролям
# ---------------------------------------------------------------------------

DATA_BUTTONS = (
    "Изменить",
    "Удалить",
    "Новое определение вида",
    "Новая особь",
    "Добавить образец",
    "Ввести несколько особей",
    "Решено",
    "Добавить проблему",
    "Новая экспедиция",
    "Новое место",
)


def card_pages(path):
    event_id = trawling_event(path)
    trawling_id = db_value(
        path, "SELECT min(trawling_id) FROM trawling WHERE event_id = ?", event_id
    )
    expedition_id = db_value(
        path, "SELECT expedition_id FROM sampling_event WHERE event_id = ?", event_id
    )
    return [
        "/specimen/26 ph",
        f"/events/{event_id}",
        f"/trawlings/{trawling_id}",
        "/sites/1",
        f"/expeditions/{expedition_id}",
        "/expeditions",
        "/sites",
        "/issues",
        f"/issues/{db_value(path, 'SELECT min(issue_id) FROM data_issue')}",
    ]


@pytest.mark.parametrize("login", ["reader", "admin"])
def test_no_data_buttons_for_reader_and_admin(app, path, login):
    client = as_user(app, path, login)
    for url in card_pages(path):
        r = client.get(url)
        assert r.status_code == 200, url
        html = html_of(r)
        for text in DATA_BUTTONS:
            assert f">{text}" not in html, (url, text)
        assert 'method="post"' not in html.replace(
            '<form method="post" action="/logout" class="logout">', ""
        ), url
    html = html_of(client.get("/specimen/26 ph"))
    assert ">История<" in html  # история видна всем


def test_admin_menu_only_for_admin(app, path):
    assert ">Пользователи<" in html_of(as_user(app, path, "admin").get("/"))
    assert ">Пользователи<" not in html_of(as_user(app, path, "curator").get("/"))
    assert as_user(app, path, "curator").get("/admin/users").status_code == 403


def test_curator_sees_buttons_on_excel_records(client, path):
    html = html_of(client.get("/specimen/26 ph"))
    assert "Изменить сведения об особи" in html and "Удалить особь" in html
    assert "Новое определение вида" in html


def test_researcher_buttons_only_on_own(app, path):
    client = as_user(app, path, "researcher")
    _excel_id, excel_label = excel_specimen(path)
    html = html_of(client.get(f"/specimen/{excel_label}"))
    assert "Изменить сведения об особи" not in html and "Удалить особь" not in html
    assert "Исправить промеры" not in html
    assert "Новое определение вида" in html  # определение — к любой особи (решение 2026-10-20)
    assert "Добавить образец" in html
    own, label = add_fish(client, path)
    html = html_of(client.get(f"/specimen/{label}"))
    assert "Изменить сведения об особи" in html and "Удалить особь" in html
    assert client.get(f"/edit/specimen/{own}").status_code == 200
    # другой исследователь — чужая запись
    other = as_user(app, path, "researcher2")
    assert "Изменить сведения об особи" not in html_of(other.get(f"/specimen/{label}"))
    r = other.get(f"/edit/specimen/{own}")
    assert r.status_code == 403 and "Чужие записи" in html_of(r)
    assert other.get(f"/delete/specimen/{own}").status_code == 403


def test_researcher_forbidden_on_excel_record(app, path):
    client = as_user(app, path, "researcher")
    excel_id, excel_label = excel_specimen(path)
    before = changes(path)
    r = client.get(f"/edit/specimen/{excel_id}")
    assert r.status_code == 403 and "перенесённые из Excel" in html_of(r)
    assert client.get(f"/delete/specimen/{excel_id}").status_code == 403
    r = client.post(f"/delete/specimen/{excel_id}", data={"confirm": "1"})
    assert r.status_code == 403
    r = client.post(f"/specimen/{excel_label}/measurements", data={"tl_cm": "30", "old_tl_cm": ""})
    assert r.status_code == 403
    assert changes(path) == before and backups(path) == []


def test_researcher_can_identify_excel_specimen(app, path):
    client = as_user(app, path, "researcher")
    _excel_id, label = excel_specimen(path)
    data = {"species": "Pangasius elongatus", "method": "ДНК", "confidence": "точно"}
    r = client.post(f"/specimen/{label}/identify", data=data)
    assert r.status_code == 302
    assert (
        db_value(
            path, "SELECT count(*) FROM change_log WHERE user_id = ?", user_id(path, "researcher")
        )
        >= 1
    )


def test_reader_batch_page_forbidden(app, path):
    client = as_user(app, path, "reader")
    assert client.get(f"/events/{trawling_event(path)}/specimens/batch").status_code == 403


# ---------------------------------------------------------------------------
# Кто внёс, история, журнал изменений
# ---------------------------------------------------------------------------


def test_author_and_history_on_card(client, path):
    html = html_of(client.get("/specimen/26 ph"))
    assert "импорт из Excel" in html
    assert "История изменений (0)" in html
    _id, label = add_fish(client, path)
    html = html_of(client.get(f"/specimen/{label}"))
    assert "Внёс(ла): Имя curator" in html
    match = re.search(
        r'<time class="utc" datetime="(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)">'
        r"(\d\d\.\d\d\.\d{4} \d\d:\d\d UTC)</time>",
        html,
    )
    assert match, "время — в элементе <time> с UTC"
    assert "История изменений (1)" in html and "создание" in html


def test_changes_page_and_filters(client, path):
    _id, label = add_fish(client, path)
    r = client.get("/changes")
    assert r.status_code == 200
    html = html_of(r)
    assert "Журнал изменений" in html and "Имя curator" in html and label in html
    assert 'type="date"' in html
    uid = user_id(path, "reader")
    assert label not in html_of(client.get(f"/changes?user={uid}"))
    assert label in html_of(client.get("/changes?table=specimen"))
    assert label not in html_of(client.get("/changes?date_to=2000-01-01"))
    html = html_of(client.get("/changes?date_from=вчера"))
    assert "не дата" in html
    # HTMX — только таблица
    html = html_of(client.get("/changes?table=specimen", headers={**HTMX, "HX-Target": "results"}))
    assert html.lstrip().startswith('<div id="results"')


def test_changes_visible_to_reader(app, path):
    assert as_user(app, path, "reader").get("/changes").status_code == 200


def test_history_page(client, path):
    specimen_id, label = add_fish(client, path)
    r = client.get(f"/history/specimen/{specimen_id}")
    assert r.status_code == 200 and label in html_of(r) and "создание" in html_of(r)
    assert client.get("/history/sqlite_master/1").status_code == 404
    # удалённая запись: история остаётся
    client.post(f"/delete/specimen/{specimen_id}", data={"confirm": "1"})
    html = html_of(client.get(f"/history/specimen/{specimen_id}"))
    assert "удаление" in html


def test_history_links_on_sample_rows(client, path):
    html = html_of(client.get("/specimen/26 ph"))
    assert "/history/sample/" in html


# ---------------------------------------------------------------------------
# Даты
# ---------------------------------------------------------------------------


def test_dates_shown_russian(client, path):
    event_id = trawling_event(path)
    iso = db_value(path, "SELECT event_date FROM sampling_event WHERE event_id = ?", event_id)
    y, m, d = iso.split("-")
    html = html_of(client.get(f"/events/{event_id}"))
    assert f"{d}.{m}.{y}" in html
    assert f"<td>{iso}</td>" not in html
    # поле ввода даты в форме определения вида — как было (type="date", ISO)
    assert 'type="date"' in html_of(client.get("/specimen/26 ph"))


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------


def test_admin_create_user_password_shown_once(app, path):
    client = as_user(app, path, "admin")
    r = client.get("/admin/users")
    assert r.status_code == 200
    html = html_of(r)
    assert (
        "curator" in html and "Новый пользователь" in html and '<label for="f-user-login">' in html
    )
    r = client.post(
        "/admin/users/new",
        data={
            "login": "ivanova",
            "display_name": "Иванова А.",
            "roles": ["исследователь"],
            "password_mode": "generate",
        },
    )
    # пароль — прямо в ответе на POST (без перенаправления), страница не кэшируется
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    password = re.search(r"<code>([a-z0-9-]{14})</code>", html_of(r)).group(1)
    assert "один раз" in html_of(r)
    assert password not in r.headers.get("Set-Cookie", "")
    for url in ("/admin/users", f"/admin/users/{user_id(path, 'ivanova')}", "/changes"):
        assert password not in html_of(client.get(url))  # второй раз — нигде
    # новый пользователь входит с временным паролем и должен его сменить
    new = app.test_client()
    r = new.post("/login", data={"login": "ivanova", "password": password})
    assert r.headers["Location"].endswith("/password")


def test_admin_create_user_errors_at_form(app, path):
    client = as_user(app, path, "admin")
    r = client.post(
        "/admin/users/new", data={"login": "curator", "display_name": "x", "roles": ["читатель"]}
    )
    assert r.status_code == 400 and "уже занят" in html_of(r)
    r = client.post(
        "/admin/users/new",
        data={
            "login": "petrov",
            "display_name": "Петров",
            "roles": ["читатель"],
            "password_mode": "manual",
            "password": "короткий",
        },
    )
    assert r.status_code == 400 and "слишком короткий" in html_of(r)
    assert 'value="petrov"' in html_of(r)


def test_admin_roles_reset_deactivate(app, path):
    client = as_user(app, path, "admin")
    uid = user_id(path, "reader")
    assert client.get(f"/admin/users/{uid}").status_code == 200
    r = client.post(f"/admin/users/{uid}/roles", data={"roles": ["исследователь", "читатель"]})
    assert r.status_code == 303
    roles = db_value(path, "SELECT group_concat(role) FROM user_role WHERE user_id = ?", uid)
    assert "исследователь" in roles
    reader = as_user(app, path, "reader")
    r = client.post(f"/admin/users/{uid}/reset-password")
    assert r.status_code == 200 and re.search(r"<code>[a-z0-9-]{14}</code>", html_of(r))
    assert reader.get("/").status_code == 302  # сброс пароля отзывает сессии
    reader = as_user(app, path, "reader")
    r = client.post(f"/admin/users/{uid}/active", data={"active": "0"})
    assert r.status_code == 303
    assert reader.get("/").status_code == 302  # закрытый вход — сразу
    client.post(f"/admin/users/{uid}/active", data={"active": "1"})
    assert db_value(path, "SELECT is_active FROM app_user WHERE user_id = ?", uid) == 1


def test_admin_last_admin_error_at_form(app, path):
    client = as_user(app, path, "admin")
    uid = user_id(path, "admin")
    r = client.post(f"/admin/users/{uid}/roles", data={"roles": ["читатель"]})
    assert r.status_code == 400 and "последний активный админ" in html_of(r)
    r = client.post(f"/admin/users/{uid}/active", data={"active": "0"})
    assert r.status_code == 400 and "последний активный админ" in html_of(r)


def test_admin_unknown_user_404(app, path):
    assert as_user(app, path, "admin").get("/admin/users/9999").status_code == 404


# ---------------------------------------------------------------------------
# Люди
# ---------------------------------------------------------------------------


def make_people(client, labels_names):
    for label, name in labels_names:
        r = client.post(
            f"/specimen/{label}/identify",
            data={
                "species": "Pangasius elongatus",
                "method": "ДНК",
                "confidence": "точно",
                "identified_by": name,
            },
        )
        assert r.status_code == 302


def test_people_merge(client, path):
    make_people(client, [("1", "Иванов А."), ("2", "Иванов  А.А.")])
    keep = db_value(path, "SELECT person_id FROM person WHERE full_name = 'Иванов А.'")
    remove = db_value(path, "SELECT person_id FROM person WHERE full_name = 'Иванов А.А.'")
    html = html_of(client.get("/people"))
    assert "Иванов А.А." in html and "Объединить двух людей" in html
    r = client.post("/people/merge", data={"keep_id": keep, "remove_id": remove})
    assert r.status_code == 200 and "Вы уверены?" in html_of(r)
    assert db_value(path, "SELECT count(*) FROM person WHERE person_id = ?", remove) == 1
    r = client.post("/people/merge", data={"keep_id": keep, "remove_id": remove, "confirm": "1"})
    assert r.status_code == 303
    assert db_value(path, "SELECT count(*) FROM person WHERE person_id = ?", remove) == 0
    r = client.post("/people/merge", data={"keep_id": keep, "remove_id": keep})
    assert r.status_code == 400


def test_people_merge_only_curator(app, path):
    client = as_user(app, path, "researcher")
    html = html_of(client.get("/people"))
    assert "Объединить двух людей" not in html
    assert client.post("/people/merge", data={"keep_id": 1, "remove_id": 2}).status_code == 403


def test_people_datalist_in_specimen_and_batch_forms(client, path):
    make_people(client, [("1", "Петрова Б.")])
    event_id = trawling_event(path)
    assert '<option value="Петрова Б.">' in html_of(client.get(f"/events/{event_id}"))
    html = html_of(client.get(f"/events/{event_id}/specimens/batch"))
    assert 'list="batch-people"' in html and '<option value="Петрова Б.">' in html


def test_batch_identified_by(client, path):
    event_id = trawling_event(path)
    data = {
        "r1-label": "",
        "r1-species": "Plotosus canius",
        "r1-tl_cm": "25",
        "r1-identified_by": "Сидоров В.",
        "action": "save",
    }
    r = client.post(f"/events/{event_id}/specimens/batch", data=data)
    assert r.status_code == 303, html_of(r)
    assert db_value(path, "SELECT count(*) FROM person WHERE full_name = 'Сидоров В.'") == 1


# ---------------------------------------------------------------------------
# Журнал проблем
# ---------------------------------------------------------------------------


def test_issue_add_and_resolve_by_researcher(app, path):
    client = as_user(app, path, "researcher")
    excel_id, label = excel_specimen(path)
    r = client.get(f"/issues/new?table_name=specimen&record_id={excel_id}&object_label={label}")
    assert r.status_code == 200 and f'value="{excel_id}"' in html_of(r)
    r = client.post(
        "/issues/new",
        data={
            "category": "промеры",
            "description": "сверить TL",
            "table_name": "specimen",
            "record_id": str(excel_id),
            "object_label": label,
        },
    )
    assert r.status_code == 303
    issue_id = db_value(path, "SELECT max(issue_id) FROM data_issue")
    assert (
        db_value(path, "SELECT source FROM data_issue WHERE issue_id = ?", issue_id)
        == "пользователь"
    )
    html = html_of(client.get(f"/issues/{issue_id}"))
    assert "Имя researcher" in html and "пользователь" in html and ">Решено<" in html
    r = client.post(f"/issues/{issue_id}/resolve", data={"resolution": "сверено"})
    assert r.status_code == 302
    html = html_of(client.get(f"/issues/{issue_id}"))
    assert "закрыл(а)" in html and "Имя researcher" in html


def test_issue_add_errors(app, path):
    client = as_user(app, path, "researcher")
    r = client.post("/issues/new", data={"category": "", "description": "x"})
    assert r.status_code == 400 and "категорию" in html_of(r)
    r = client.post(
        "/issues/new",
        data={
            "category": "промеры",
            "description": "x",
            "table_name": "specimen",
            "record_id": "999999",
        },
    )
    assert r.status_code == 400 and "нет" in html_of(r)
    assert as_user(app, path, "reader").get("/issues/new").status_code == 403


def test_researcher_cannot_resolve_import_issue(app, path):
    client = as_user(app, path, "researcher")
    issue_id = db_value(
        path, "SELECT min(issue_id) FROM data_issue WHERE resolved = 0 AND source <> 'пользователь'"
    )
    html = html_of(client.get(f"/issues/{issue_id}"))
    assert ">Решено<" not in html and "может куратор" in html
    assert "Добавить проблему" in html_of(client.get("/issues"))
    r = client.post(f"/issues/{issue_id}/resolve", data={"resolution": "x"})
    assert r.status_code == 403
    assert db_value(path, "SELECT resolved FROM data_issue WHERE issue_id = ?", issue_id) == 0


def test_issue_list_shows_source(client, path):
    html = html_of(client.get("/issues?status=all"))
    assert '<th scope="col">откуда</th>' in html and "импорт" in html


# ---------------------------------------------------------------------------
# Служебные предупреждения (EditResult.notices)
# ---------------------------------------------------------------------------


def test_notices_shown_yellow(client, path):
    folder = path.parent / "backups" / "pre-op"
    folder.mkdir(parents=True)
    future = folder / "biobank.db_pre-op_20990101_000000.db"
    future.write_bytes(b"")
    os.utime(future)
    data = {"species": "Pangasius elongatus", "method": "ДНК", "confidence": "точно"}
    r = client.post("/specimen/1/identify", data=data)
    assert r.status_code == 302
    html = html_of(client.get("/specimen/1"))
    assert "flash flash-warn" in html and "проверьте" in html and "часы" in html


# ---------------------------------------------------------------------------
# Отзыв сессий (session_version)
# ---------------------------------------------------------------------------


def login_form(client, login="curator", password=PASSWORD, **environ):
    return client.post("/login", data={"login": login, "password": password}, environ_base=environ)


def test_copied_cookie_dead_after_logout(app, path):
    client = app.test_client()
    login_form(client)
    stolen = client.get_cookie("session").value  # копия cookie (другое устройство, кража)
    other = app.test_client()
    other.set_cookie("session", stolen)
    assert other.get("/").status_code == 200
    client.post("/logout")
    assert other.get("/").status_code == 302  # выход — на всех устройствах
    assert "Сессия завершена" in html_of(other.get("/login"))


def test_password_change_revokes_other_devices(app, path):
    phone, laptop = app.test_client(), app.test_client()
    login_form(phone)
    login_form(laptop)
    r = laptop.post(
        "/password",
        data={
            "old_password": PASSWORD,
            "new_password": "новый-пароль-42",
            "new_password2": "новый-пароль-42",
        },
    )
    assert r.status_code == 303
    assert laptop.get("/").status_code == 200  # своё устройство — работает дальше
    assert phone.get("/").status_code == 302  # другое — выйдет


def test_admin_ends_all_sessions(app, path):
    reader = as_user(app, path, "reader")
    admin = as_user(app, path, "admin")
    uid = user_id(path, "reader")
    assert "Завершить все сессии" in html_of(admin.get(f"/admin/users/{uid}"))
    r = admin.post(f"/admin/users/{uid}/sessions")
    assert r.status_code == 303
    assert reader.get("/").status_code == 302
    # админ завершает свои сессии — его текущая остаётся, прочие выходят
    other = as_user(app, path, "admin")
    admin.post(f"/admin/users/{user_id(path, 'admin')}/sessions")
    assert admin.get("/admin/users").status_code == 200
    assert other.get("/").status_code == 302
    assert as_user(app, path, "curator").post(f"/admin/users/{uid}/sessions").status_code == 403


# ---------------------------------------------------------------------------
# Кэш, прокси, имена сайта
# ---------------------------------------------------------------------------


def test_no_store_on_pages_not_static(client, anon):
    for c, url in ((client, "/specimen/26 ph"), (client, "/changes"), (anon, "/login"),
                   (client, "/password")):  # fmt: skip
        r = c.get(url)
        assert r.headers["Cache-Control"] == "no-store", url
        assert r.headers["Pragma"] == "no-cache"
    assert anon.get("/").headers["Cache-Control"] == "no-store"  # и редирект на вход
    assert "no-store" not in anon.get("/static/style.css").headers.get("Cache-Control", "")


def test_stranger_cannot_lock_out_user(app, path):
    """Десять чужих неудач с разных адресов не закрывают вход куратору с его адреса."""
    for i in range(LOGIN_ATTEMPTS):
        login_form(app.test_client(), password="x" * 12, REMOTE_ADDR=f"10.0.0.{i}")
    r = login_form(app.test_client(), REMOTE_ADDR="10.9.9.9")
    assert r.status_code == 303
    # с одного адреса тот же логин — 10 неудач → 429
    for _ in range(LOGIN_ATTEMPTS):
        login_form(app.test_client(), login="reader", password="x" * 12, REMOTE_ADDR="10.1.1.1")
    assert login_form(app.test_client(), login="reader", REMOTE_ADDR="10.1.1.1").status_code == 429
    assert login_form(app.test_client(), login="reader", REMOTE_ADDR="10.2.2.2").status_code == 303


def test_login_limit_per_login_softer(app, path):
    limiter = app.extensions["biobank_login_limiter"]
    for i in range(LOGIN_ATTEMPTS_PER_LOGIN):
        limiter.failed(f"10.0.{i // 200}.{i % 200}", "curator")
    assert login_form(app.test_client(), REMOTE_ADDR="10.9.9.9").status_code == 429


def test_limiter_drops_old_keys(app):
    limiter = app.extensions["biobank_login_limiter"]
    now = [0.0]
    limiter.clock = lambda: now[0]
    for i in range(100):
        limiter.failed(f"10.0.0.{i}", f"user{i}")
    assert len(limiter._failures) == 200
    now[0] += 15 * 60 + 1
    limiter.failed("10.9.9.9", "x")
    assert len(limiter._failures) == 2  # устаревшие ключи удалены


def test_forwarded_for_ignored_without_setting(app, path):
    client = app.test_client()
    for i in range(LOGIN_ATTEMPTS):
        client.post("/login", data={"login": "curator", "password": "x" * 12},
                    headers={"X-Forwarded-For": f"1.2.3.{i}"})  # fmt: skip
    # без настройки адрес — REMOTE_ADDR (127.0.0.1): поддельный заголовок не помог
    assert login_form(app.test_client()).status_code == 429


def test_proxyfix_separates_clients(path):
    app = create_app(path, trusted_proxies=1)
    for _ in range(LOGIN_ATTEMPTS):
        app.test_client().post("/login", data={"login": "curator", "password": "x" * 12},
                               headers={"X-Forwarded-For": "1.2.3.4"})  # fmt: skip
    r = app.test_client().post("/login", data={"login": "curator", "password": PASSWORD},
                               headers={"X-Forwarded-For": "1.2.3.4"})  # fmt: skip
    assert r.status_code == 429
    r = app.test_client().post("/login", data={"login": "curator", "password": PASSWORD},
                               headers={"X-Forwarded-For": "5.6.7.8"})  # fmt: skip
    assert r.status_code == 303


def test_proxyfix_https_origin(path):
    """За прокси с HTTPS форма приходит с Origin https://… — CSRF-проверка её пропускает."""
    app = create_app(path, trusted_proxies=1, trusted_hosts=["biobank.example.org"])
    r = app.test_client().post(
        "/login",
        data={"login": "curator", "password": PASSWORD},
        base_url="http://biobank.example.org",
        headers={"X-Forwarded-Proto": "https", "Origin": "https://biobank.example.org"},
    )
    assert r.status_code == 303


def test_trusted_hosts_setting(path, monkeypatch):
    app = create_app(path, trusted_hosts=["biobank.example.org"])
    assert app.test_client().get("/login", base_url="http://biobank.example.org").status_code == 200
    assert app.test_client().get("/login").status_code == 400  # localhost — уже чужое имя
    monkeypatch.setenv("BIOBANK_TRUSTED_HOSTS", "a.example, b.example")
    assert create_app(path).config["TRUSTED_HOSTS"] == ["a.example", "b.example"]
    monkeypatch.delenv("BIOBANK_TRUSTED_HOSTS")
    assert create_app(path).config["TRUSTED_HOSTS"] == ["127.0.0.1", "localhost"]


# ---------------------------------------------------------------------------
# Журнал изменений: учётные записи и логины — только админу
# ---------------------------------------------------------------------------


def test_user_changes_hidden_from_non_admin(app, path):
    admin = as_user(app, path, "admin")
    admin.post(f"/admin/users/{user_id(path, 'reader')}/roles",
               data={"roles": ["читатель", "исследователь"]})  # fmt: skip
    uid = user_id(path, "reader")
    for login in ("reader", "curator"):
        client = as_user(app, path, login)
        html = html_of(client.get("/changes?table=app_user"))
        assert "Изменений: <strong>0</strong>" in html
        html = html_of(client.get("/changes?table=user_role"))
        assert "Изменений: <strong>0</strong>" in html
        html = html_of(client.get(f"/history/app_user/{uid}"))
        assert "session_version" not in html and "(reader)" not in html
        assert "пользователь сайта" not in html_of(client.get("/changes"))
    html = html_of(admin.get("/changes?table=user_role"))
    assert "Изменений: <strong>0</strong>" not in html and "reader: исследователь" in html
    assert "(admin)" in html  # логин автора — админу виден


def test_reader_sees_no_logins_in_changes(client, app, path):
    _id, _label = add_fish(client, path)
    html = html_of(as_user(app, path, "reader").get("/changes"))
    assert "Имя curator" in html and "(curator)" not in html


def test_researcher_deletes_own_shipment_only(app, path):
    client = as_user(app, path, "researcher")
    _id, label = add_fish(client, path)
    r = client.post(f"/specimen/{label}/samples/new", data={"sample_type": "ДНК"})
    assert r.status_code == 303, html_of(r)
    sample_id = db_value(path, "SELECT max(sample_id) FROM sample WHERE specimen_id = ?", _id)
    lab = db_value(path, "SELECT name FROM lab ORDER BY lab_id LIMIT 1")
    r = client.post(f"/specimen/{label}/ship", data={"sample_id": sample_id, "lab": lab})
    assert r.status_code == 303, html_of(r)
    lab_id = db_value(path, "SELECT lab_id FROM lab WHERE name = ?", lab)
    url = f"/delete/shipment/{sample_id}/{lab_id}"
    assert url in html_of(client.get(f"/specimen/{label}"))
    # отметки из Excel исследователь не удаляет: кнопки нет, страница — 403
    where = "FROM sample_shipment WHERE created_by IS NULL ORDER BY sample_id, lab_id LIMIT 1"
    excel = (db_value(path, "SELECT sample_id " + where), db_value(path, "SELECT lab_id " + where))
    assert client.get(f"/delete/shipment/{excel[0]}/{excel[1]}").status_code == 403
    assert client.post(url, data={"confirm": "1"}).status_code == 303
    assert (
        db_value(path, "SELECT count(*) FROM sample_shipment WHERE sample_id = ?", sample_id) == 0
    )
