"""Вход на сайт: сессия, проверка прав на каждый запрос, страницы входа, выхода и смены пароля.

Словарик:
- сессия (session) — небольшой словарь, который Flask хранит в cookie браузера, подписав его
  секретным ключом (SECRET_KEY): прочитать содержимое можно, а подделать без ключа — нельзя.
  Поэтому в сессии только номер пользователя (user_id) и время последнего запроса, никаких
  паролей. Ключ постоянный (файл рядом с базой или переменная окружения): с новым ключом
  при каждом запуске всех выкидывало бы из сайта после перезапуска;
- отзыв сессий: в сессии ещё и версия сессий пользователя (app_user.session_version).
  «Выйти», смена и сброс пароля, закрытие входа, «Завершить все сессии» у админа
  увеличивают версию в базе — все прежние сессии (и скопированная cookie) перестают
  действовать на следующем же запросе;
- Cache-Control: no-store — браузер и прокси не хранят страницы с данными в кэше;
- cookie HttpOnly — скрипты страницы cookie не видят; SameSite=Lax — браузер не пришлёт её
  с формой, отправленной с чужого сайта; Secure — только по HTTPS (включается на сервере);
- «фиксация сессии» — злоумышленник заранее подсовывает жертве свою сессию и ждёт, пока та
  войдёт. Защита — при входе сессия очищается и создаётся заново.

Пользователь перечитывается из базы на КАЖДЫЙ запрос (users.get_user): закрытый вход или
снятая роль действуют сразу, без выхода. Права окончательно проверяют функции записи
(entry, records, edit, users) — сайт по permissions.can только решает, какие кнопки показать.

Флаг веб-запроса (audit.begin_web_request): пока он стоит, запись без пользователя
(системный режим) запрещена — поэтому во все функции записи передаётся user=current_user().
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from biobank import users
from biobank.audit import begin_web_request, end_web_request
from biobank.db import connect
from biobank.edit import EditResult, apply_edit
from biobank.errors import EditError, NotFoundError, PermissionDenied
from biobank.permissions import User, can, denial_message
from biobank.web.views import local_path

log = logging.getLogger(__name__)

IDLE_LIMIT = timedelta(hours=12)  # автовыход после 12 часов бездействия
LOGIN_ATTEMPTS = 10  # неудачных попыток входа с одного адреса для одного логина…
LOGIN_ATTEMPTS_PER_LOGIN = 50  # …или для логина с любых адресов…
LOGIN_WINDOW = 15 * 60  # …за 15 минут (секунды) — дальше «подождите»
SECRET_ENV = "BIOBANK_SECRET_KEY"
SECURE_ENV = "BIOBANK_SECURE_COOKIES"
HOSTS_ENV = "BIOBANK_TRUSTED_HOSTS"
PROXIES_ENV = "BIOBANK_TRUSTED_PROXIES"
SECRET_FILE = "secret_key"
DEFAULT_HOSTS = ["127.0.0.1", "localhost"]

# Страницы, открытые без входа (static — стили и htmx.min.js для страницы входа)
PUBLIC_ENDPOINTS = {"static", "login", "logout"}
# Пока временный пароль не сменён — только эти страницы
PASSWORD_ENDPOINTS = PUBLIC_ENDPOINTS | {"password"}


# ---------------------------------------------------------------------------
# Секретный ключ
# ---------------------------------------------------------------------------


def load_secret_key(db_path: Path) -> str:
    """Постоянный секретный ключ подписи cookie.

    1) переменная окружения BIOBANK_SECRET_KEY (так удобно на сервере);
    2) иначе файл secret_key в папке базы; нет файла — создаётся со случайным ключом
       и правами 600 (читать и писать может только владелец).
    """
    from_env = os.environ.get(SECRET_ENV, "").strip()
    if from_env:
        return from_env
    path = Path(db_path).parent / SECRET_FILE
    try:
        # O_EXCL — создать, только если файла ещё нет (два процесса не перезапишут друг друга)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text(encoding="ascii").strip()
        if (path.stat().st_mode & 0o077) != 0:
            log.warning("Файл ключа %s доступен не только владельцу: chmod 600 %s", path, path)
        if key:
            return key
        raise RuntimeError(f"Файл ключа {path} пуст — удалите его, сайт создаст новый.") from None
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="ascii") as file:
        file.write(key + "\n")
    return key


# ---------------------------------------------------------------------------
# Ограничение попыток входа (защита от подбора пароля)
# ---------------------------------------------------------------------------


@dataclass
class LoginLimiter:
    """Счётчик неудачных входов в памяти процесса — защита от подбора пароля.

    Два счётчика за последние LOGIN_WINDOW секунд (15 минут):
    - пара «адрес + логин»: LOGIN_ATTEMPTS (10) неудач — с этого адреса этот логин
      временно закрыт (подбор пароля с одного компьютера);
    - логин с любых адресов: LOGIN_ATTEMPTS_PER_LOGIN (50) — мягче, чтобы чужой не мог
      десятью неверными паролями закрыть вход конкретному человеку, но и подбор с многих
      адресов был ограничен.
    Адрес отдельно не считается: за прокси (или в одной сети лаборатории) у многих людей
    один адрес, и чужие ошибки не должны закрывать вход остальным.
    Пустые счётчики удаляются — словарь не растёт без конца.
    После перезапуска сайта счётчики обнуляются; при нескольких процессах сервера у
    каждого свой счётчик (лимит — на процесс).
    clock — откуда брать время (тесты подменяют, чтобы «прошло 15 минут»).
    """

    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, deque] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _limits(self, address: str | None, login: str) -> list[tuple[str, int]]:
        login = login.strip().lower()
        return [(f"pair:{address}|{login}", LOGIN_ATTEMPTS),
                (f"login:{login}", LOGIN_ATTEMPTS_PER_LOGIN)]  # fmt: skip

    def _recent(self, key: str, now: float) -> deque:
        """Неудачи по ключу за окно; устаревшие выбрасываются, пустой ключ — удаляется."""
        times = self._failures.get(key, deque())
        while times and now - times[0] > LOGIN_WINDOW:
            times.popleft()
        if not times:
            self._failures.pop(key, None)
        return times

    def _sweep(self, now: float) -> None:
        for key in list(self._failures):
            self._recent(key, now)

    def wait_seconds(self, address: str | None, login: str) -> int:
        """Сколько секунд ждать до следующей попытки (0 — можно пробовать)."""
        with self._lock:
            now = self.clock()
            wait = 0
            for key, limit in self._limits(address, login):
                times = self._recent(key, now)
                if len(times) >= limit:
                    wait = max(wait, int(LOGIN_WINDOW - (now - times[-limit])) + 1)
            return wait

    def failed(self, address: str | None, login: str) -> None:
        with self._lock:
            now = self.clock()
            self._sweep(now)  # заодно убрать устаревшие ключи других логинов
            for key, _limit in self._limits(address, login):
                times = self._recent(key, now)
                times.append(now)
                self._failures[key] = times

    def succeeded(self, address: str | None, login: str) -> None:
        """Удачный вход сбрасывает счётчик пары «адрес + логин»."""
        with self._lock:
            self._failures.pop(self._limits(address, login)[0][0], None)


# ---------------------------------------------------------------------------
# Текущий пользователь и права
# ---------------------------------------------------------------------------


def current_user() -> User | None:
    """Вошедший пользователь этого запроса (свежие данные из базы) или None."""
    return g.get("user")


def allowed(action: str, table: str | None = None, record=None) -> bool:
    """permissions.can для вошедшего пользователя — в шаблонах это can(…)."""
    return can(current_user(), action, table, record)


def need(action: str, table: str | None = None, record=None) -> None:
    """Нет прав на действие — страница 403 с понятным текстом (запрос дальше не идёт)."""
    user = current_user()
    if not can(user, action, table, record):
        message = denial_message(user, action, table, record) if user else "Войдите на сайт."
        abort(403, description=message)


@contextmanager
def write_db() -> Iterator[sqlite3.Connection]:
    """Подключение НА ЗАПИСЬ — для входа и управления пользователями (users.py).

    Данные (особи, выезды…) пишутся через edit.apply_edit (с резервной копией); вход,
    смена пароля и учётные записи — напрямую: users.py сам открывает транзакцию.
    """
    conn = connect(current_app.config["DB_PATH"])
    try:
        yield conn
    finally:
        conn.close()


def start_session(user: User) -> None:
    """Новая сессия вошедшего: номер пользователя и версия сессий (для отзыва)."""
    session.clear()  # новая сессия при входе — защита от фиксации сессии
    session.permanent = True
    session["user_id"] = user.user_id
    session["v"] = user.session_version
    session["seen"] = time.time()


def _login_url() -> str:
    """Адрес входа с ?next=<эта страница> (для GET; после входа вернём сюда)."""
    target = local_path(request.full_path.rstrip("?")) if request.method == "GET" else None
    if target in (None, "/"):
        return url_for("login")
    return url_for("login", next=target)


def _redirect(url: str):
    """Перенаправить и обычный запрос, и HTMX (тот просит заголовком HX-Redirect)."""
    if request.headers.get("HX-Request") == "true":
        response = current_app.response_class(status=204)
        response.headers["HX-Redirect"] = url
        return response
    return redirect(url, code=303 if request.method == "POST" else 302)


# ---------------------------------------------------------------------------
# Подключение к приложению
# ---------------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "yes", "true")


def configure(
    app: Flask,
    db_path: Path,
    secure_cookies: bool | None = None,
    trusted_hosts: list[str] | None = None,
    trusted_proxies: int | None = None,
) -> None:
    """Ключ, cookie сессии, допустимые имена сайта и прокси. None — из переменных окружения.

    trusted_hosts — имена, на которые сайт отвечает (заголовок Host); чужое — 400
    (защита от «DNS rebinding»). По умолчанию 127.0.0.1 и localhost.
    trusted_proxies — сколько прокси-серверов (nginx…) стоит перед сайтом. Только тогда
    сайт верит заголовкам X-Forwarded-For / X-Forwarded-Proto: настоящий адрес клиента
    (для счётчика попыток входа) и https. По умолчанию 0 — не верить: без прокси этот
    заголовок может написать кто угодно.
    """
    app.config["SECRET_KEY"] = load_secret_key(db_path)
    if secure_cookies is None:
        secure_cookies = _env_flag(SECURE_ENV)
    if trusted_hosts is None:
        from_env = [h.strip() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()]
        trusted_hosts = from_env or list(DEFAULT_HOSTS)
    if trusted_proxies is None:
        try:
            trusted_proxies = int(os.environ.get(PROXIES_ENV, "0") or 0)
        except ValueError:
            raise RuntimeError(f"{PROXIES_ENV} — число прокси (0, 1, 2…).") from None
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(secure_cookies),
        PERMANENT_SESSION_LIFETIME=IDLE_LIMIT,  # и браузер забудет cookie через 12 часов
        TRUSTED_HOSTS=list(trusted_hosts),
    )
    if trusted_proxies > 0:
        # ProxyFix берёт адрес клиента и схему (https) из заголовков, которые добавили
        # trusted_proxies последних прокси; остальное (что прислал сам клиент) — игнорирует
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxies, x_proto=trusted_proxies)
    app.extensions["biobank_login_limiter"] = LoginLimiter()


def register_auth(app: Flask, pages) -> None:
    """Проверка входа на каждый запрос и страницы /login, /logout, /password.

    pages — модуль app.py (read_db). Порядок before_request важен: сначала флаг
    веб-запроса, потом CSRF-проверка (в app.py), потом вход.
    """

    @app.before_request
    def mark_web_request():
        g.web_token = begin_web_request()

    @app.teardown_request
    def unmark_web_request(_error):
        token = g.pop("web_token", None)
        if token is not None:
            end_web_request(token)

    @app.after_request
    def no_store(response):
        """Страницы с данными не сохранять в кэше браузера и прокси: после выхода их нельзя
        открыть кнопкой «Назад» или из кэша на чужом компьютере. Стили и скрипты — можно."""
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.context_processor
    def auth_context():
        return {"current_user": current_user(), "can": allowed}

    def load_user():
        """Пользователь из сессии; None — не вошёл (или сессия больше не действует)."""
        user_id = session.get("user_id")
        if user_id is None:
            return None
        seen = session.get("seen", 0)
        if time.time() - seen > IDLE_LIMIT.total_seconds():
            session.clear()
            flash("Вы не заходили больше 12 часов — войдите снова.", "warn")
            return None
        try:
            user = users.get_user(pages.read_db(), int(user_id))
        except (NotFoundError, ValueError, TypeError):
            session.clear()
            return None
        if not user.is_active:
            session.clear()
            flash("Вход для этой учётной записи закрыт. Обратитесь к админу.", "error")
            return None
        if not users.session_is_valid(user, session.get("v")):
            # выход на другом устройстве, смена или сброс пароля — эта сессия отозвана
            session.clear()
            flash("Сессия завершена — войдите снова.", "warn")
            return None
        session["seen"] = time.time()  # бездействие считается от последнего запроса
        return user

    @app.before_request
    def require_login():
        if request.endpoint == "static" or request.routing_exception is not None:
            # нет такой страницы, чужое имя сайта (Host), не тот метод — Flask сам ответит
            # 404 / 400 / 405; страница ошибки данных не показывает
            return None
        pages.read_db()  # нет файла базы — сразу понятная страница 503 (и до входа)
        user = load_user()
        g.user = user
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if user is None:
            return _redirect(_login_url())
        if user.must_change_password and request.endpoint not in PASSWORD_ENDPOINTS:
            flash("Сначала смените временный пароль.", "warn")
            return _redirect(url_for("password"))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        back = local_path(request.values.get("next"))
        if request.method == "GET":
            if current_user() is not None:
                return redirect(back or url_for("index"))
            return render_template("login.html", next_url=back, login_name="")
        login_name = " ".join(request.form.get("login", "").split())
        password = request.form.get("password", "")
        limiter: LoginLimiter = app.extensions["biobank_login_limiter"]

        def refuse(message, status):
            page = render_template("login.html", next_url=back, login_name=login_name,
                                   error=message)  # fmt: skip
            return page, status

        wait = limiter.wait_seconds(request.remote_addr, login_name)
        if wait:
            minutes = max(1, (wait + 59) // 60)
            return refuse(
                f"Слишком много неудачных попыток входа. Подождите {minutes} мин. "
                "и попробуйте снова.",
                429,
            )
        with write_db() as conn:
            user = users.authenticate(conn, login_name, password)
        if user is None:
            limiter.failed(request.remote_addr, login_name)
            return refuse(users.LOGIN_FAILED, 401)
        limiter.succeeded(request.remote_addr, login_name)
        start_session(user)
        if user.must_change_password:
            flash("Вы вошли с временным паролем — придумайте свой.", "warn")
            return redirect(url_for("password"), code=303)
        return redirect(back or url_for("index"), code=303)

    @app.post("/logout")
    def logout():
        """Выход — на всех устройствах (решение пользователя): версия сессий + 1."""
        user = current_user()
        if user is not None:
            with write_db() as conn:
                users.logout(conn, user)
        session.clear()
        flash("Вы вышли из сайта.", "ok")
        return redirect(url_for("login"), code=303)

    @app.route("/password", methods=["GET", "POST"])
    def password():
        user = current_user()
        if request.method == "GET":
            return render_template("password.html")
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        if new != request.form.get("new_password2", ""):
            error = "Новый пароль и повтор не совпадают."
            return render_template("password.html", error=error), 400
        try:
            with write_db() as conn:
                changed = users.change_own_password(conn, user, old, new)
        except EditError as error:  # прежний неверный, новый короткий…
            return render_template("password.html", error=str(error)), 400
        # смена пароля отзывает все сессии; эта остаётся — с новой версией
        session["v"] = changed.session_version
        flash("Пароль изменён.", "ok")
        return redirect(url_for("index"), code=303)


# ---------------------------------------------------------------------------
# Запись от имени вошедшего
# ---------------------------------------------------------------------------


def run_edit(action: Callable[[sqlite3.Connection, User], EditResult]) -> EditResult:
    """edit.apply_edit (резервная копия + правка в транзакции) от имени вошедшего.

    action(conn, user) — вызов функции записи; user ей нужно передать (user=user):
    на сайте запись без пользователя запрещена. Нет прав (PermissionDenied) — сразу
    страница 403 с текстом отказа: это не ошибка ввода, показывать её у формы незачем.
    Прочие ошибки (EditError) — вызывающему, он покажет их у формы.
    """
    user = current_user()
    if user is None:
        abort(403, description="Войдите на сайт.")
    try:
        return apply_edit(current_app.config["DB_PATH"], lambda conn: action(conn, user))
    except PermissionDenied as error:
        abort(403, description=str(error))


def flash_result(result: EditResult) -> None:
    """Сообщения после успешной записи: итог — зелёным; предупреждения о данных и
    служебные (notices: «копии из будущего, проверьте часы») — жёлтым."""
    flash(result.message, "ok")
    for warning in result.warnings:
        flash(f"Внимание: {warning}", "warn")
    for notice in result.notices:
        flash(f"Служебное предупреждение: {notice}", "warn")
