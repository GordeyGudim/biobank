"""Веб-приложение Flask: маршруты (адреса страниц) и обработка ошибок.

Веб-слой тонкий: каждый маршрут только разбирает запрос, вызывает готовую функцию
(card.specimen_card, reports.report_tables, edit.resolve_issue …) и отдаёт шаблон.
SQL здесь нет и быть не должно — нужна новая выборка, её добавляют в модули логики.

Словарик:
- маршрут (route) — адрес страницы + функция, которая её строит (@app.get("/reports"));
- шаблон — HTML-файл из templates/ с «дырками» {{ … }}, которые Jinja2 заполняет данными.
  Jinja2 сама экранирует значения: «<script>» из базы покажется как текст, а не выполнится;
- HTMX-запрос — запрос, который браузер шлёт сам по атрибутам hx-post/hx-get, без перезагрузки
  страницы; сервер возвращает кусочек HTML (фрагмент, шаблон _*.html), и HTMX вставляет
  его на место старого. Такой запрос приходит с заголовком «HX-Request: true»;
- POST/redirect — запись только методом POST; после успешной записи сервер отвечает
  «перейди на другую страницу» (redirect), и обновление страницы (F5) не повторит запись.

Вход и права (этап D3): все страницы — только после входа (auth.py); запись — от имени
вошедшего (auth.run_edit передаёт user=… в функции entry / records / edit); кнопки и
формы показываются по permissions.can (в шаблонах — can(…), may(…)); нет прав — 403.
Пользователи и роли — admin.py; журнал изменений, история записи, люди — journal.py.
"""

from __future__ import annotations

import sqlite3
import sys
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
    url_for,
)
from werkzeug.exceptions import Forbidden

from biobank import browse, users
from biobank.card import specimen_card
from biobank.db import connect_read_only
from biobank.edit import (
    CONFIDENCES,
    METHODS,
    EditError,
    NotFoundError,
    PermissionDenied,
    identify,
    link_trawl,
    resolve_issue,
)
from biobank.entry import (
    COORD_SOURCES,
    MEASUREMENTS,
    POINT_TYPES,
    SAMPLE_TYPES,
    SEXES,
    SITE_TYPES,
    WATER_NUMBERS,
)
from biobank.parsing import ORGANS
from biobank.permissions import CREATE, DELETE, MANAGE_USERS, MERGE_PEOPLE, RESOLVE, UPDATE
from biobank.reports import REPORTS, report_tables
from biobank.web import auth
from biobank.web.admin import register_admin_routes
from biobank.web.auth import flash_result, need, register_auth, run_edit
from biobank.web.entry_forms import (
    Submitted,
    grouped_sites,
    label_suffixes,
    register_entry_routes,
    site_hints,
)
from biobank.web.journal import register_journal_routes
from biobank.web.record_forms import (
    as_text,
    people_names,
    record_permit,
    register_record_routes,
)
from biobank.web.views import (
    ISSUE_STATUSES,
    KIND_TABLES,
    SPECIMEN_ORDERS,
    TABLE_TITLES,
    field_ids,
    history_url,
    issue_object_url,
    issue_query,
    row_actions,
    ru_date,
    section_views,
    specimen_query,
    utc_parts,
)

# Названия отчётов в меню (ключи — как у команды `python -m biobank report <имя>`)
REPORT_TITLES = {
    "species": "Особи по видам и экспедициям",
    "samples": "Образцы и лаборатории",
    "water": "Параметры воды по выездам",
    "sites": "Места и их посещения",
    "issues": "Открытые проблемы данных",
}


CROSS_SITE = "Запрос пришёл не со страницы этого сайта и отклонён. База не изменена."


class DatabaseMissingError(Exception):
    """Файла базы нет — сайт показывает понятную страницу вместо ошибки 500."""


def cell_text(value, dates: bool = True) -> str | None:
    """Как показать значение в ячейке таблицы; None остаётся None (шаблон пишет «—»).

    Дробные числа — до 10 значащих цифр, как в терминале (координаты не обрезаются).
    Дата ГГГГ-ММ-ДД — как ДД.ММ.ГГГГ (dates=False — как в базе: исходные значения
    в журнале проблем показываются без изменений).

    >>> cell_text(10.103733), cell_text(29.9), cell_text(5), cell_text(None)
    ('10.103733', '29.9', '5', None)
    >>> cell_text("2025-09-25"), cell_text("2025-09-25", dates=False)
    ('25.09.2025', '2025-09-25')
    """
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.10g}"
    return ru_date(str(value)) if dates else str(value)


def create_app(
    db_path: Path | str,
    *,
    secure_cookies: bool | None = None,
    trusted_hosts: list[str] | None = None,
    trusted_proxies: int | None = None,
) -> Flask:
    """Фабрика приложения: собрать сайт для базы db_path.

    Фабрика (функция, которая создаёт приложение), а не один глобальный объект:
    тесты создают своё приложение для копии базы, команда `web` — для рабочей.
    Настройки для сервера (None — из переменных окружения, см. README, раздел «Сайт»):
    secure_cookies — cookie сессии только по HTTPS (BIOBANK_SECURE_COOKIES=1; по умолчанию
    выключено: на 127.0.0.1 HTTPS нет); trusted_hosts — имена сайта (BIOBANK_TRUSTED_HOSTS,
    через запятую; по умолчанию 127.0.0.1, localhost); trusted_proxies — сколько прокси
    перед сайтом (BIOBANK_TRUSTED_PROXIES; по умолчанию 0 — X-Forwarded-For не верить).
    """
    app = Flask(__name__)
    app.config["DB_PATH"] = Path(db_path)
    # постоянный ключ подписи cookie, cookie, имена сайта, прокси — auth.configure.
    # TRUSTED_HOSTS — отвечать только на свои имена. Защита от «DNS rebinding»: чужой сайт
    # может направить своё имя на наш адрес и читать страницы — с чужим именем в
    # заголовке Host Flask ответит ошибкой 400.
    auth.configure(app, Path(db_path), secure_cookies, trusted_hosts, trusted_proxies)
    app.jinja_env.filters["cell"] = cell_text
    app.jinja_env.filters["ru_date"] = ru_date
    app.jinja_env.globals.update(
        utc_parts=utc_parts,
        may=record_permit,  # may('event', 5, UPDATE) — кнопки правки конкретной записи
        author_of=author_of,
        history_url=history_url,
        CREATE=CREATE,
        UPDATE=UPDATE,
        DELETE=DELETE,
        RESOLVE=RESOLVE,
        MANAGE_USERS=MANAGE_USERS,
        MERGE_PEOPLE=MERGE_PEOPLE,
    )

    _register_db(app)  # CSRF-проверка — первой, до входа (вход и выход тоже под ней)
    register_auth(app, sys.modules[__name__])
    _register_errors(app)
    _register_routes(app)
    # формы ввода — в своём модуле entry_forms.py; он получает этот модуль (функции
    # страниц и read_db) параметром, чтобы два модуля не импортировали друг друга по кругу
    register_entry_routes(app, sys.modules[__name__])
    # правка, удаление и пакетный ввод — record_forms.py, устроены так же
    register_record_routes(app, sys.modules[__name__])
    # пользователи (админ); журнал изменений, история записи, люди, новая проблема
    register_admin_routes(app, sys.modules[__name__])
    register_journal_routes(app, sys.modules[__name__])
    return app


# ---------------------------------------------------------------------------
# Подключение к базе: одно на запрос, только на чтение
# ---------------------------------------------------------------------------


def _register_db(app: Flask) -> None:
    @app.teardown_appcontext
    def close_db(_error) -> None:
        # вызывается после каждого запроса, даже если была ошибка
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.after_request
    def vary_on_htmx(response):
        # один адрес отдаёт то всю страницу, то кусок (для HTMX) — пусть кэш браузера
        # их не путает: ответ зависит от заголовка HX-Request
        response.vary.add("HX-Request")
        return response

    @app.before_request
    def reject_cross_site_post():
        """Запись — только со страниц самого сайта.

        Открытая в браузере страница чужого сайта могла бы отправить форму на этот сайт
        от имени вошедшего пользователя (CSRF — подделка запроса). Браузер сообщает,
        откуда пришла форма (заголовки Origin и Sec-Fetch-Site), — чужое отклоняем.
        Вход и выход — тоже POST и тоже под этой проверкой.
        """
        if request.method != "POST":
            return None
        origin = request.headers.get("Origin")
        if origin is not None and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403, description=CROSS_SITE)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403, description=CROSS_SITE)
        return None


def read_db() -> sqlite3.Connection:
    """Подключение только на чтение для текущего запроса (создаётся при первом вызове).

    Страницы просмотра пользуются только им: даже ошибка в коде не сможет изменить
    базу. Запись — только через edit.apply_edit (резервная копия + транзакция).
    """
    if "conn" not in g:
        path: Path = current_app.config["DB_PATH"]
        if not path.exists():
            raise DatabaseMissingError(str(path))
        g.conn = connect_read_only(path)
    return g.conn


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _wants_fragment(target: str) -> bool:
    """HTMX просит только кусок страницы с id=target (таблицу списка), а не всю страницу.

    Заголовок HX-Target — id элемента, куда HTMX вставит ответ. Кнопка «Назад» в браузере
    иногда просит у сервера страницу заново (HX-History-Restore-Request) — тогда целиком.
    """
    return (
        _is_htmx()
        and request.headers.get("HX-Target") == target
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


# ---------------------------------------------------------------------------
# Ошибки → понятные страницы
# ---------------------------------------------------------------------------


def _register_errors(app: Flask) -> None:
    @app.errorhandler(NotFoundError)
    def not_found_record(error: NotFoundError):
        return render_template("error.html", title="Не найдено", message=str(error)), 404

    @app.errorhandler(PermissionDenied)
    def permission_denied(error: PermissionDenied):
        return render_template("error.html", title="Нет прав", message=str(error)), 403

    @app.errorhandler(EditError)
    def edit_error(error: EditError):
        return render_template("error.html", title="Ошибка", message=str(error)), 400

    @app.errorhandler(DatabaseMissingError)
    def database_missing(error: DatabaseMissingError):
        message = (
            f"Файла базы нет: {error}. Создайте её командой "
            "python -m biobank import data/source.xlsx и перезапустите сайт."
        )
        return render_template("error.html", title="Нет базы", message=message), 503

    @app.errorhandler(404)
    def page_not_found(_error):
        message = "Такой страницы нет. Проверьте адрес или воспользуйтесь меню."
        return render_template("error.html", title="Страница не найдена", message=message), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        message = "Эту страницу нельзя открыть так: изменения вносятся только кнопкой в форме."
        return render_template("error.html", title="Недопустимый запрос", message=message), 405

    @app.errorhandler(403)
    def forbidden(error):
        # abort(403, description=…) — текст отказа (нет прав, чужой сайт); иначе общий
        message = error.description if error.description != Forbidden.description else None
        message = message or "Нет прав на это действие."
        return render_template("error.html", title="Нет прав", message=message), 403


# ---------------------------------------------------------------------------
# Страницы
# ---------------------------------------------------------------------------


def _register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/search")
    def search():
        """Форма поиска → карточка особи. Номер нормализует specimen_card (find_specimen)."""
        label = " ".join(request.args.get("label", "").split())
        if not label:
            error = "Введите номер пробы, например 26 ph или 155pc."
            return render_template("index.html", error=error), 400
        return redirect(url_for("specimen", label=label))

    @app.get("/suggest")
    def suggest():
        """Подсказки номеров для поля поиска: HTMX вставляет <option> в <datalist>."""
        labels = browse.suggest_labels(read_db(), request.args.get("label", ""))
        return render_template("_suggest.html", labels=labels)

    # ---- особи ----

    @app.get("/specimens")
    def specimens():
        """Список особей с фильтрами из адреса; HTMX получает только таблицу (#results)."""
        conn = read_db()
        query = specimen_query(request.args)
        expedition_id = query.kwargs["expedition_id"]
        try:
            events = browse.list_events(conn, expedition_id)
        except NotFoundError as error:
            query.warnings.append(f"{error} Выберите экспедицию из списка.")
            events = []
        # выезд другой экспедиции (экспедицию сменили) — фильтр по выезду сбрасываем
        event_id = query.kwargs["event_id"]
        if event_id is not None and event_id not in {e["event_id"] for e in events}:
            if expedition_id is None:
                query.warnings.append(f"Выезда №{event_id} нет.")
            query.kwargs["event_id"] = None
            query.args.pop("event", None)
        page = browse.list_specimens(conn, **query.kwargs)
        context = {
            "query": query,
            "page": page,
            "events": events,
            "event_id": query.kwargs["event_id"],
        }
        if _wants_fragment("results"):
            return render_template("_specimens_results.html", oob=True, **context)
        return render_template(
            "specimens.html",
            expeditions=browse.list_expeditions(conn),
            sites=browse.list_sites(conn),
            species=browse.list_species(conn),
            methods=browse.list_capture_methods(conn),
            orders=SPECIMEN_ORDERS,
            **context,
        )

    # <path:label> — номер может содержать любые символы; пробелы и кириллица в адресе
    # кодируются (url_for: «26 ph» → /specimen/26%20ph), Flask раскодирует их обратно
    @app.get("/specimen/<path:label>")
    def specimen(label: str):
        return _specimen_page(label)

    @app.post("/specimen/<path:label>/link-trawl")
    def specimen_link_trawl(label: str):
        """Форма «Привязать к тралению» → edit.link_trawl через apply_edit."""
        need(UPDATE, "specimen", specimen_record(label))
        form = {"trawl_no": request.form.get("trawl_no", "")}
        try:
            trawl_no = int(form["trawl_no"])
        except ValueError:
            return _specimen_page(label, 400, form, link_error="Выберите траление из списка.")

        def action(conn, user):
            return link_trawl(conn, label, trawl_no, user=user)

        return _specimen_edit(label, form, "link_error", action)

    @app.post("/specimen/<path:label>/identify")
    def specimen_identify(label: str):
        """Форма «Новое определение вида» → edit.identify через apply_edit."""
        need(CREATE, "species_identification")
        form = {
            name: " ".join(request.form.get(name, "").split())
            for name in ("species", "method", "confidence", "identified_on", "identified_by")
        }
        form["notes"] = request.form.get("notes", "").strip()

        def action(conn, user):
            return identify(
                conn,
                label,
                form["species"] or None,  # пустой выбор — «вид не определён»
                form["method"],
                form["confidence"],
                identified_by=form["identified_by"] or None,
                identified_on=form["identified_on"] or None,
                notes=form["notes"] or None,
                user=user,
            )

        return _specimen_edit(label, form, "identify_error", action)

    # ---- экспедиции, выезды, места ----

    @app.get("/expeditions")
    def expeditions():
        return expeditions_page()

    @app.get("/expeditions/<int:expedition_id>")
    def expedition(expedition_id: int):
        # ?site=N — пришли со страницы места («Добавить выезд на это место»):
        # форма нового выезда сразу раскрыта, место выбрано
        site = request.args.get("site", "")
        submitted = Submitted("f-event", {"site_id": site}) if site.isdigit() else None
        return expedition_page(expedition_id, submitted)

    @app.get("/events/<int:event_id>")
    def event(event_id: int):
        # ?form=specimen — ссылка «Ещё особь на этом выезде»: форма особи сразу раскрыта
        opened = request.args.get("form", "")
        submitted = Submitted(f"f-{opened}") if opened in EVENT_FORMS else None
        return event_page(event_id, submitted)

    @app.get("/sites")
    def sites():
        return sites_page()

    @app.get("/sites/<int:site_id>")
    def site(site_id: int):
        conn = read_db()
        sections = browse.site_card(conn, site_id)  # нет места — 404
        views = section_views(
            sections,
            own_key="site_id",
            actions=True,
            here=url_for("site", site_id=site_id),
            permit=record_permit,
        )
        return render_template(
            "site.html",
            views=views,
            title=sections[0].rows[0][1],
            site_id=site_id,
            expeditions=browse.list_expeditions(conn),
            **record_info("site", site_id),
        )

    @app.get("/trawlings/<int:trawling_id>")
    def trawling(trawling_id: int):
        """Карточка траления: замеры воды, особи, прилов, проблемы (browse.trawling_card)."""
        sections = browse.trawling_card(read_db(), trawling_id)  # нет траления — 404
        ids = field_ids(sections[0])
        fields = dict(sections[0].rows)
        return render_template(
            "trawling.html",
            views=section_views(
                sections,
                own_key="trawling_id",
                actions=True,
                here=url_for("trawling", trawling_id=trawling_id),
                permit=record_permit,
            ),
            trawling_id=trawling_id,
            trawl_no=fields["траление"],
            event_title=fields["выезд"],
            ids=ids,
            **record_info("trawling", trawling_id),
        )

    # ---- отчёты ----

    @app.get("/reports")
    def reports():
        return render_template("reports.html", titles=REPORT_TITLES)

    @app.get("/reports/<name>")
    def report(name: str):
        if name not in REPORTS:
            abort(404)
        sections = report_tables(read_db(), name)
        id_column = {"issues": "issue_id", "sites": "site_id"}.get(name)
        return render_template(
            "report.html",
            name=name,
            title=REPORT_TITLES.get(name, name),
            views=section_views(sections, id_column=id_column),
        )

    # ---- журнал проблем ----

    @app.get("/issues")
    def issues():
        conn = read_db()
        query = issue_query(request.args)
        page = browse.list_issues(conn, **query.kwargs)
        for item in page.items:
            item["object_url"] = issue_object_url(item)
            item["table_title"] = TABLE_TITLES.get(item["table_name"], item["table_name"])
            item["can_resolve"] = auth.allowed(RESOLVE, "data_issue", item)
        context = {"query": query, "page": page}
        if _wants_fragment("results"):
            return render_template("_issues_results.html", **context)
        return render_template(
            "issues.html",
            categories=browse.issue_categories(conn),
            statuses=ISSUE_STATUSES,
            **context,
        )

    @app.get("/issues/<int:issue_id>")
    def issue(issue_id: int):
        """Одна проблема журнала (на неё ведут ссылки из карточек) + форма «Решено»."""
        item = browse.get_issue(read_db(), issue_id)  # нет такой — 404
        item["can_resolve"] = auth.allowed(RESOLVE, "data_issue", item)
        return render_template(
            "issue.html",
            issue=item,
            object_url=issue_object_url(item),
            table_title=TABLE_TITLES.get(item["table_name"], item["table_name"]),
        )

    @app.post("/issues/<int:issue_id>/resolve")
    def resolve(issue_id: int):
        """Отметить проблему решённой: apply_edit = резервная копия + правка в транзакции.

        HTMX-запрос получает фрагмент вместо формы (без перезагрузки страницы);
        обычная отправка формы (если JavaScript выключен) — redirect на журнал.
        """
        need(RESOLVE, "data_issue", browse.get_issue(read_db(), issue_id))  # нет такой — 404
        resolution = request.form.get("resolution", "")
        try:
            result = run_edit(
                lambda conn, user: resolve_issue(conn, issue_id, resolution, user=user)
            )
        except EditError as error:
            # NotFoundError — нет такой проблемы; иначе — пустой текст, уже решена и т.п.
            status = 404 if isinstance(error, NotFoundError) else 400
            if _is_htmx():
                return render_template(
                    "_resolve_form.html",
                    issue_id=issue_id,
                    resolution=resolution,
                    error=str(error),
                ), status
            flash(f"Проблема №{issue_id}: {error}", "error")
            return redirect(url_for("issues", _anchor=f"issue-{issue_id}"))
        if _is_htmx():
            return render_template(
                "_resolve_done.html",
                message=result.message,
                resolution=resolution,
                notices=[*result.warnings, *result.notices],
            )
        flash_result(result)
        return redirect(url_for("issues"))


# ---------------------------------------------------------------------------
# Страницы с формами ввода (их вызывают и маршруты просмотра, и entry_forms.py:
# после ошибки ввода — та же страница с раскрытой формой и введёнными значениями)
# ---------------------------------------------------------------------------

# Формы карточки выезда, которые можно раскрыть ссылкой /events/N?form=…
EVENT_FORMS = ("trawling", "water", "catch", "specimen")


def expeditions_page(submitted: Submitted | None = None, status: int = 200):
    """Список экспедиций + форма «Новая экспедиция»."""
    expeditions = browse.list_expeditions(read_db())
    return render_template("expeditions.html", expeditions=expeditions, submitted=submitted), status


def expedition_page(expedition_id: int, submitted: Submitted | None = None, status: int = 200):
    """Экспедиция, её выезды + форма «Новый выезд» (место — по группам типов мест)."""
    conn = read_db()
    events = browse.list_events(conn, expedition_id)  # нет экспедиции — NotFoundError → 404
    info = next(x for x in browse.list_expeditions(conn) if x["expedition_id"] == expedition_id)
    return render_template(
        "expedition.html",
        **record_info("expedition", expedition_id),
        expedition=info,
        events=events,
        site_groups=grouped_sites(browse.list_sites(conn)),
        methods=browse.list_capture_methods(conn),
        submitted=submitted,
    ), status


def event_page(event_id: int, submitted: Submitted | None = None, status: int = 200):
    """Карточка выезда + формы: траление, замер воды, прилов, новая особь."""
    conn = read_db()
    sections = browse.event_card(conn, event_id)  # нет выезда — 404
    ids = field_ids(sections[0])
    fields = dict(sections[0].rows)
    trawls = browse.event_trawls(conn, event_id)
    return render_template(
        "event.html",
        views=section_views(
            sections,
            own_key="event_id",
            event_id=event_id,
            actions=True,
            here=url_for("event", event_id=event_id),
            permit=record_permit,
        ),
        **record_info("event", event_id),
        title=fields["выезд"],
        event_id=event_id,
        ids=ids,
        is_trawling=fields["способ получения"] == "траление",
        trawls=trawls,
        next_trawl_no=max((t["trawl_no"] for t in trawls), default=0) + 1,
        species=browse.list_species(conn),
        methods=browse.list_capture_methods(conn),
        site_groups=grouped_sites(browse.list_sites(conn)),
        people=people_names(conn),
        suffixes=label_suffixes(),
        confidences=CONFIDENCES,
        sexes=SEXES,
        point_types=POINT_TYPES,
        coord_sources=COORD_SOURCES.values(),
        water_numbers=WATER_NUMBERS,
        measurements=MEASUREMENTS,
        submitted=submitted,
    ), status


def sites_page(submitted: Submitted | None = None, status: int = 200):
    """Список мест + форма «Новое место» (с проверкой похожих мест)."""
    sites = browse.list_sites(read_db())
    return render_template(
        "sites.html",
        sites=sites,
        site_types=SITE_TYPES,
        hints=site_hints(sites),
        submitted=submitted,
    ), status


# ---------------------------------------------------------------------------
# Карточка особи и её формы
# ---------------------------------------------------------------------------


def _specimen_page(
    label: str,
    status: int = 200,
    form: dict | None = None,
    submitted: Submitted | None = None,
    **errors,
):
    """Карточка особи с формами правки.

    form — что пользователь ввёл в формы этапа B (траление, определение вида);
    submitted — состояние форм ввода (образец, отправка, промеры);
    errors — link_error / identify_error: текст ошибки рядом со своей формой.
    """
    conn = read_db()
    try:
        sections = specimen_card(conn, label)
    except NotFoundError as error:
        # своя 404 с формой поиска: подсказка «Похожие: …» — из find_specimen
        return render_template("not_found.html", message=str(error), label=label), 404
    ids = field_ids(sections[0])
    fields = dict(sections[0].rows)
    samples = next(sec for sec in sections if sec.title == "Образцы")  # sample_id — в row_ids
    sample_ids = [row_ids.get("sample_id") for row_ids in samples.row_ids]
    # адрес карточки — с номером как в базе: сюда вернут кнопки «Изменить»/«Удалить»
    # (страница могла прийти и от POST-формы — тогда request.path не адрес карточки)
    here = url_for("specimen", label=fields["номер пробы"])
    views = section_views(
        sections,
        own_key="specimen_id",
        event_id=ids["event_id"],
        actions=True,
        here=here,
        permit=record_permit,
    )
    return render_template(
        "specimen.html",
        views=views,
        label=fields["номер пробы"],  # номер как в базе: «155pc» → «155 pc»
        ids=ids,
        # форма «Привязать к тралению» — только у особей, полученных тралением
        # (способ особи, а если своего нет — выезда; из row_ids раздела «Особь»)
        is_trawled=ids.get("capture_method") == "траление",
        own_actions=row_actions(
            {"specimen_id": ids["specimen_id"]}, here, record_permit, history=False
        ),
        **record_info("specimen", ids["specimen_id"]),
        # куда отправлены образцы — для кнопок «удалить отправку» (только те, что можно)
        shipments=[
            (dict(zip(samples.columns, row, strict=True)), shipment)
            for row, sample_id in zip(samples.rows, sample_ids, strict=True)
            if sample_id is not None
            for shipment in browse.list_shipments(conn, sample_id)
            if auth.allowed(DELETE, "sample_shipment", shipment)
        ],
        trawls=browse.event_trawls(conn, ids["event_id"]),
        species=browse.list_species(conn),
        people=people_names(conn),
        methods=METHODS,
        confidences=CONFIDENCES,
        form=form or {},
        submitted=submitted,
        sample_types=SAMPLE_TYPES,
        samples=[
            (row_ids.get("sample_id"), dict(zip(samples.columns, row, strict=True)))
            for row, row_ids in zip(samples.rows, samples.row_ids, strict=True)
        ],
        labs=[lab["name"] for lab in browse.list_labs(conn)],
        organs=list(dict.fromkeys(name for _, name in ORGANS)),
        preservatives=[p["preservative"] for p in browse.list_preservatives(conn)],
        current=current_measurements(conn, ids["specimen_id"]),
        measurements=MEASUREMENTS,
        **errors,
    ), status


def specimen_entry_page(label: str, submitted: Submitted, status: int = 400):
    """Карточка особи после отправки формы ввода (ошибка — 400, ждёт подтверждения — 200)."""
    return _specimen_page(label, status, submitted=submitted)


def current_measurements(conn: sqlite3.Connection, specimen_id: int) -> dict[str, str]:
    """Промеры особи для формы «Исправить промеры»: 29.9 → '29.9', не измерено → ''.

    Числа берутся из базы (browse.specimen_measurements), а не из текста карточки; форма
    отправляет назад и показанное значение (скрытые поля old_…) — меняются только
    исправленные поля.
    """
    numbers = browse.specimen_measurements(conn, specimen_id)
    return {column: as_text(numbers[column]) for column in MEASUREMENTS}


def _specimen_edit(label: str, form: dict, error_name: str, action):
    """Общая часть форм карточки: run_edit (резервная копия + транзакция, от имени вошедшего).

    Успех — сообщение и redirect на карточку (F5 не повторит запись).
    Ошибка — та же карточка с текстом ошибки у формы (код 400; нет особи — 404).
    HTMX-форма берёт из ответа только блок #card — карточка обновляется без перезагрузки.
    """
    try:
        result = run_edit(action)
    except NotFoundError as error:
        return render_template("not_found.html", message=str(error), label=label), 404
    except EditError as error:
        return _specimen_page(label, 400, form, **{error_name: str(error)})
    flash_result(result)
    return redirect(url_for("specimen", label=label))


# ---------------------------------------------------------------------------
# Кто внёс запись, история, права на запись по номеру пробы
# ---------------------------------------------------------------------------


def user_name(user_id: int | None) -> str | None:
    """Подпись пользователя по номеру (один запрос к базе на номер за запрос страницы)."""
    if user_id is None:
        return None
    names = g.setdefault("user_names", {})
    if user_id not in names:
        try:
            names[user_id] = users.get_user(read_db(), user_id).display_name
        except NotFoundError:
            names[user_id] = f"пользователь №{user_id}"
    return names[user_id]


def author_of(record) -> dict | None:
    """Кто и когда внёс запись (created_by / created_at): {'name', 'at'};
    None — запись перенесена из Excel (created_by пустой)."""
    if record is None or record.get("created_by") is None:
        return None
    return {"name": user_name(record["created_by"]), "at": record.get("created_at")}


def record_info(kind: str, record_id: int) -> dict:
    """Для шаблона карточки: сама запись (record — кто внёс) и её история изменений.

    kind — expedition, site, event, trawling, specimen (так же зовутся и в get_record)."""
    conn = read_db()
    record = browse.get_record(conn, kind, record_id)
    table = KIND_TABLES[kind]
    return {
        "record": record,
        "history": browse.record_history(conn, table, record_id, viewer=auth.current_user()),
        "history_link": history_url(table, record_id),
    }


def specimen_record(label: str) -> dict:
    """Запись особи по номеру в любом написании (для проверки прав); нет — 404."""
    conn = read_db()
    specimen_id = browse.specimen_measurements(conn, label)["specimen_id"]
    return browse.get_record(conn, "specimen", specimen_id)
