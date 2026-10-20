"""Журнал изменений, история записи, справочник людей, новая проблема в журнале проблем.

- /changes — «кто, когда, что изменил» по всей базе (browse.list_changes) с фильтрами
  по пользователю, таблице и датам; видна всем вошедшим (решение: видимость одинакова);
- /history/<таблица>/<номер> — история одной записи (browse.record_history); на неё ведут
  ссылки «История» из карточек. Работает и для удалённых записей: журнал остаётся;
- /people — справочник людей (browse.list_people); куратор объединяет дубли
  (edit.merge_people) после вопроса «Вы уверены?»;
- /issues/new — исследователь или куратор добавляет проблему (edit.add_issue).
SQL здесь нет — только разбор запроса и шаблоны.
"""

from __future__ import annotations

import datetime as dt

from flask import Flask, abort, redirect, render_template, request, url_for

from biobank import browse
from biobank.edit import EditError, NotFoundError, add_issue, merge_people
from biobank.permissions import CREATE, MERGE_PEOPLE
from biobank.web.auth import allowed, current_user, flash_result, need, run_edit
from biobank.web.views import KIND_TABLES, TABLE_TITLES, read_query

# Как называть таблицы в журнале изменений (в дополнение к TABLE_TITLES журнала проблем)
CHANGE_TABLE_TITLES = {
    **TABLE_TITLES,
    "expedition": "экспедиция",
    "data_issue": "проблема журнала",
    "person": "человек (справочник)",
    "expedition_member": "участник экспедиции",
    "app_user": "пользователь сайта",
    "user_role": "роль пользователя",
    "lab": "лаборатория",
    "species": "вид (справочник)",
    "city": "город",
    "region": "провинция",
    "gear": "сеть",
    "analysis": "анализ",
}
# Таблицы, к которым человек может добавить проблему (форма «Добавить проблему»)
ISSUE_TABLES = {
    table: CHANGE_TABLE_TITLES[table]
    for table in ("specimen", "sample", "sampling_event", "trawling", "sampling_site",
                  "water_measurement", "catch_record", "expedition")
}  # fmt: skip


def table_title(table: str | None) -> str | None:
    return CHANGE_TABLE_TITLES.get(table, table) if table else None


def record_page(table: str, key: str, label: str | None) -> str | None:
    """Адрес карточки записи по таблице и ключу (если у записи есть своя страница)."""
    if not key.isdigit():
        return None
    record_id = int(key)
    if table == "specimen" and label:
        return url_for("specimen", label=label)
    endpoints = {"sampling_event": ("event", "event_id"),
                 "sampling_site": ("site", "site_id"),
                 "trawling": ("trawling", "trawling_id"),
                 "expedition": ("expedition", "expedition_id"),
                 "data_issue": ("issue", "issue_id")}  # fmt: skip
    if table in endpoints:
        endpoint, name = endpoints[table]
        return url_for(endpoint, **{name: record_id})
    return None


def _iso_date(query, name: str, title: str) -> str | None:
    """Дата фильтра ГГГГ-ММ-ДД (так её присылает поле type="date"); иначе — предупреждение."""
    value = query.text(name)
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        query.warnings.append(f"Фильтр «{title}»: «{value}» — не дата, фильтр не учтён.")
        del query.args[name]
        return None


def register_journal_routes(app: Flask, pages) -> None:
    """Маршруты журнала изменений, истории, людей и новой проблемы. pages — модуль app.py."""

    app.jinja_env.globals["table_title"] = table_title

    # ---- журнал изменений ----

    @app.get("/changes")
    def changes():
        conn = pages.read_db()
        query = read_query(request.args, ("user", "table", "date_from", "date_to"))
        kwargs = {
            "user_id": query.number("user", "кто"),
            "table": query.text("table") or None,
            "date_from": _iso_date(query, "date_from", "с"),
            "date_to": _iso_date(query, "date_to", "по"),
            "page": query.page,
        }
        # viewer: изменения пользователей и ролей, логины — только админу
        page = browse.list_changes(conn, **kwargs, viewer=current_user())
        for item in page.items:
            item["url"] = record_page(item["table_name"], item["record_key"],
                                      item["record_label"])  # fmt: skip
        context = {"query": query, "page": page}
        if pages._wants_fragment("results"):
            return render_template("_changes_results.html", **context)
        return render_template(
            "changes.html",
            users=browse.change_authors(conn, viewer=current_user()),
            tables=browse.change_tables(conn, viewer=current_user()),
            **context,
        )

    @app.get("/history/<table>/<path:key>")
    def history(table: str, key: str):
        conn = pages.read_db()
        known = set(KIND_TABLES.values()) | set(CHANGE_TABLE_TITLES)
        if table not in known:
            abort(404)
        items = browse.record_history(conn, table, key, viewer=current_user())
        label = next((i["record_label"] for i in reversed(items) if i["record_label"]), None)
        return render_template(
            "history.html",
            table=table,
            key=key,
            label=label,
            items=items,
            url=record_page(table, key, label),
        )

    # ---- справочник людей ----

    def people_page(error=None, status=200, confirm=None, form=None):
        return render_template(
            "people.html",
            people=browse.list_people(pages.read_db()),
            can_merge=allowed(MERGE_PEOPLE, "person"),
            error=error,
            confirm=confirm,
            form=form or {},
        ), status

    @app.get("/people")
    def people():
        return people_page()

    @app.post("/people/merge")
    def people_merge():
        need(MERGE_PEOPLE, "person")
        form = {name: request.form.get(name, "") for name in ("keep_id", "remove_id")}
        try:
            keep_id, remove_id = int(form["keep_id"]), int(form["remove_id"])
        except ValueError:
            return people_page("Выберите обоих людей из списков.", 400, form=form)
        if keep_id == remove_id:
            return people_page("Это один и тот же человек — объединять нечего.", 400, form=form)
        everyone = {p["person_id"]: p for p in browse.list_people(pages.read_db())}
        if keep_id not in everyone or remove_id not in everyone:
            raise NotFoundError("Такого человека нет в справочнике — обновите страницу.")
        if request.form.get("confirm") != "1":  # сначала — «Вы уверены?»
            confirm = {"keep": everyone[keep_id], "remove": everyone[remove_id]}
            return people_page(confirm=confirm, form=form)
        try:
            result = run_edit(lambda conn, user: merge_people(conn, keep_id, remove_id, user=user))
        except NotFoundError:
            raise
        except EditError as error:  # у обоих учётные записи сайта и т.п.
            return people_page(str(error), 400, form=form)
        flash_result(result)
        return redirect(url_for("people"), code=303)

    # ---- новая проблема в журнале проблем ----

    def issue_form(values, error=None, status=200):
        return render_template(
            "issue_new.html",
            values=values,
            error=error,
            tables=ISSUE_TABLES,
            categories=browse.issue_categories(pages.read_db()),
        ), status

    @app.route("/issues/new", methods=["GET", "POST"])
    def issue_new():
        need(CREATE, "data_issue")
        names = ("category", "description", "table_name", "record_id", "object_label",
                 "raw_value")  # fmt: skip
        source = request.form if request.method == "POST" else request.args
        values = {name: " ".join(source.get(name, "").split()) for name in names}
        if request.method == "GET":
            return issue_form(values)
        table = values["table_name"] or None
        if table is not None and table not in ISSUE_TABLES:
            return issue_form(values, "Выберите, к чему относится проблема, из списка.", 400)
        record_id = None
        if values["record_id"]:
            if not values["record_id"].isdigit():
                return issue_form(values, "Номер записи — целое число.", 400)
            record_id = int(values["record_id"])

        def action(conn, user):
            return add_issue(
                conn, category=values["category"], description=values["description"],
                table_name=table, record_id=record_id,
                object_label=values["object_label"] or None,
                raw_value=values["raw_value"] or None, user=user,
            )  # fmt: skip

        try:
            result = run_edit(action)
        except EditError as error:  # пусто, нет такой записи…
            return issue_form(values, str(error), 400)
        flash_result(result)
        return redirect(url_for("issue", issue_id=result.record_id), code=303)
