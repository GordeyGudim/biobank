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
"""

from __future__ import annotations

import os
import sqlite3
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

from biobank import browse
from biobank.card import specimen_card
from biobank.db import connect_read_only
from biobank.edit import (
    CONFIDENCES,
    METHODS,
    EditError,
    NotFoundError,
    apply_edit,
    identify,
    link_trawl,
    resolve_issue,
)
from biobank.reports import REPORTS, report_tables
from biobank.web.views import (
    ISSUE_STATUSES,
    SPECIMEN_ORDERS,
    TABLE_TITLES,
    field_ids,
    issue_object_url,
    issue_query,
    section_views,
    specimen_query,
)

# Названия отчётов в меню (ключи — как у команды `python -m biobank report <имя>`)
REPORT_TITLES = {
    "species": "Особи по видам и экспедициям",
    "samples": "Образцы и лаборатории",
    "water": "Параметры воды по выездам",
    "sites": "Места и их посещения",
    "issues": "Открытые проблемы данных",
}


class DatabaseMissingError(Exception):
    """Файла базы нет — сайт показывает понятную страницу вместо ошибки 500."""


def cell_text(value) -> str | None:
    """Как показать значение в ячейке таблицы; None остаётся None (шаблон пишет «—»).

    Дробные числа — до 10 значащих цифр, как в терминале (координаты не обрезаются).

    >>> cell_text(10.103733), cell_text(29.9), cell_text(5), cell_text(None)
    ('10.103733', '29.9', '5', None)
    """
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def create_app(db_path: Path | str) -> Flask:
    """Фабрика приложения: собрать сайт для базы db_path.

    Фабрика (функция, которая создаёт приложение), а не один глобальный объект:
    тесты создают своё приложение для копии базы, команда `web` — для рабочей.
    """
    app = Flask(__name__)
    app.config["DB_PATH"] = Path(db_path)
    # ключ для подписи cookie с сообщениями flash(); новый при каждом запуске —
    # входа по паролю пока нет, хранить в cookie больше нечего
    app.config["SECRET_KEY"] = os.urandom(32)
    # Отвечать только на адреса этого компьютера. Защита от «DNS rebinding»: чужой сайт
    # может направить своё имя на 127.0.0.1 и читать страницы — с чужим именем в
    # заголовке Host Flask ответит ошибкой 400.
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]
    app.jinja_env.filters["cell"] = cell_text

    _register_db(app)
    _register_errors(app)
    _register_routes(app)
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

        Пароля пока нет, поэтому любая открытая в браузере страница чужого сайта могла бы
        отправить форму на http://127.0.0.1:5000/… (CSRF — подделка запроса). Браузер
        сообщает, откуда пришла форма (заголовки Origin и Sec-Fetch-Site), — чужое отклоняем.
        """
        if request.method != "POST":
            return None
        origin = request.headers.get("Origin")
        if origin is not None and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
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
    def forbidden(_error):
        message = "Запрос пришёл не со страницы этого сайта и отклонён. База не изменена."
        return render_template("error.html", title="Запрещено", message=message), 403


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
        form = {"trawl_no": request.form.get("trawl_no", "")}
        try:
            trawl_no = int(form["trawl_no"])
        except ValueError:
            return _specimen_page(label, 400, form, link_error="Выберите траление из списка.")
        return _specimen_edit(label, form, "link_error", lambda c: link_trawl(c, label, trawl_no))

    @app.post("/specimen/<path:label>/identify")
    def specimen_identify(label: str):
        """Форма «Новое определение вида» → edit.identify через apply_edit."""
        form = {
            name: " ".join(request.form.get(name, "").split())
            for name in ("species", "method", "confidence", "identified_on", "identified_by")
        }
        form["notes"] = request.form.get("notes", "").strip()

        def action(conn):
            return identify(
                conn,
                label,
                form["species"] or None,  # пустой выбор — «вид не определён»
                form["method"],
                form["confidence"],
                identified_by=form["identified_by"] or None,
                identified_on=form["identified_on"] or None,
                notes=form["notes"] or None,
            )

        return _specimen_edit(label, form, "identify_error", action)

    # ---- экспедиции, выезды, места ----

    @app.get("/expeditions")
    def expeditions():
        return render_template("expeditions.html", expeditions=browse.list_expeditions(read_db()))

    @app.get("/expeditions/<int:expedition_id>")
    def expedition(expedition_id: int):
        conn = read_db()
        events = browse.list_events(conn, expedition_id)  # нет экспедиции — NotFoundError → 404
        info = next(x for x in browse.list_expeditions(conn) if x["expedition_id"] == expedition_id)
        return render_template("expedition.html", expedition=info, events=events)

    @app.get("/events/<int:event_id>")
    def event(event_id: int):
        sections = browse.event_card(read_db(), event_id)  # нет выезда — 404
        ids = field_ids(sections[0])
        views = section_views(sections, own_key="event_id", event_id=event_id)
        title = sections[0].rows[0][1]
        return render_template("event.html", views=views, title=title, event_id=event_id, ids=ids)

    @app.get("/sites")
    def sites():
        return render_template("sites.html", sites=browse.list_sites(read_db()))

    @app.get("/sites/<int:site_id>")
    def site(site_id: int):
        sections = browse.site_card(read_db(), site_id)  # нет места — 404
        views = section_views(sections, own_key="site_id")
        return render_template(
            "site.html", views=views, title=sections[0].rows[0][1], site_id=site_id
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
        context = {"query": query, "page": page}
        if _wants_fragment("results"):
            return render_template("_issues_results.html", **context)
        return render_template(
            "issues.html",
            categories=browse.issue_categories(conn),
            statuses=ISSUE_STATUSES,
            **context,
        )

    @app.post("/issues/<int:issue_id>/resolve")
    def resolve(issue_id: int):
        """Отметить проблему решённой: apply_edit = резервная копия + правка в транзакции.

        HTMX-запрос получает фрагмент вместо формы (без перезагрузки страницы);
        обычная отправка формы (если JavaScript выключен) — redirect на журнал.
        """
        resolution = request.form.get("resolution", "")
        try:
            result = apply_edit(
                app.config["DB_PATH"], lambda conn: resolve_issue(conn, issue_id, resolution)
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
                "_resolve_done.html", message=result.message, resolution=resolution
            )
        flash(result.message, "ok")
        return redirect(url_for("issues"))


# ---------------------------------------------------------------------------
# Карточка особи и её формы
# ---------------------------------------------------------------------------


def _specimen_page(label: str, status: int = 200, form: dict | None = None, **errors):
    """Карточка особи с формами правки.

    form — что пользователь ввёл (после ошибки поля заполнены снова, вводить заново
    не нужно); errors — link_error / identify_error: текст ошибки рядом со своей формой.
    """
    conn = read_db()
    try:
        sections = specimen_card(conn, label)
    except NotFoundError as error:
        # своя 404 с формой поиска: подсказка «Похожие: …» — из find_specimen
        return render_template("not_found.html", message=str(error), label=label), 404
    ids = field_ids(sections[0])
    return render_template(
        "specimen.html",
        views=section_views(sections, own_key="specimen_id", event_id=ids["event_id"]),
        label=sections[0].rows[0][1],  # номер как в базе: «155pc» → «155 pc»
        ids=ids,
        trawls=browse.event_trawls(conn, ids["event_id"]),
        species=browse.list_species(conn),
        methods=METHODS,
        confidences=CONFIDENCES,
        form=form or {},
        **errors,
    ), status


def _specimen_edit(label: str, form: dict, error_name: str, action):
    """Общая часть форм карточки: apply_edit (резервная копия + транзакция).

    Успех — сообщение и redirect на карточку (F5 не повторит запись).
    Ошибка — та же карточка с текстом ошибки у формы (код 400; нет особи — 404).
    HTMX-форма берёт из ответа только блок #card — карточка обновляется без перезагрузки.
    """
    try:
        result = apply_edit(current_app.config["DB_PATH"], action)
    except NotFoundError as error:
        return render_template("not_found.html", message=str(error), label=label), 404
    except EditError as error:
        return _specimen_page(label, 400, form, **{error_name: str(error)})
    flash(result.message, "ok")
    for warning in result.warnings:
        flash(warning, "warn")
    return redirect(url_for("specimen", label=label))
