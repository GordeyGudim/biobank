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

from biobank.card import specimen_card
from biobank.db import connect_read_only
from biobank.edit import EditError, NotFoundError, apply_edit, resolve_issue
from biobank.reports import REPORTS, report_tables

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

    # <path:label> — номер может содержать любые символы; пробелы и кириллица в адресе
    # кодируются (url_for: «26 ph» → /specimen/26%20ph), Flask раскодирует их обратно
    @app.get("/specimen/<path:label>")
    def specimen(label: str):
        try:
            sections = specimen_card(read_db(), label)
        except NotFoundError as error:
            # своя 404 с формой поиска: подсказка «Похожие: …» — из find_specimen
            return render_template("not_found.html", message=str(error), label=label), 404
        return render_template("specimen.html", sections=sections, label=label)

    @app.get("/reports")
    def reports():
        return render_template("reports.html", titles=REPORT_TITLES)

    @app.get("/reports/<name>")
    def report(name: str):
        if name not in REPORTS:
            abort(404)
        sections = report_tables(read_db(), name)
        return render_template(
            "report.html", name=name, title=REPORT_TITLES.get(name, name), sections=sections
        )

    @app.get("/issues")
    def issues():
        # TODO(этап B): заменить на browse.list_issues (фильтры по категории, листу,
        # открытые/решённые) и issue_categories — когда бэкенд добавит browse.py.
        section = report_tables(read_db(), "issues")[0]
        return render_template("issues.html", section=section, id_index=section.columns.index("id"))

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
