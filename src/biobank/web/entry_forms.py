"""Формы ввода новых записей (этап C сайта): экспедиция, место, выезд, траление, замер воды,
прилов, особь, образец, отправка образца, исправление промеров.

Каждая форма устроена одинаково:
- страница с формой — обычный GET (только чтение); форма — <details> с id (f-expedition…);
- кнопка отправляет POST; запись — только через функции entry.py внутри edit.apply_edit
  (резервная копия базы + проверка и запись в одной транзакции) от имени вошедшего
  (auth.run_edit: user=…); права проверяются до всего остального (auth.need) и ещё раз —
  самой функцией записи;
- ошибка (EditError) — та же страница с кодом 400: форма раскрыта, введённое на месте,
  текст ошибки — над полями; HTMX берёт из ответа только блок формы (hx-select) и меняет его;
- успех — сообщение (flash) и переход на карточку созданной записи. Обычная форма получает
  redirect (код 303 — «теперь открой этот адрес методом GET»), HTMX-запрос — заголовок
  HX-Redirect (HTMX сам откроет адрес). Обновление страницы (F5) запись не повторит.

Числа («25,4»), даты («15.09.2026»), время («9.30») здесь не разбираются — текст из формы
передаётся в функции entry.py как есть, они его проверяют и разбирают.

Предварительные проверки (только чтение, до записи) делает логика, а не сайт:
entry.check_specimen и entry.check_measurements_update (мягкие предупреждения ждут кнопки
«Всё равно сохранить»), entry.preview_site (похожее место уже есть). Проверки те же,
что при записи, — тексты ошибок и предупреждений совпадают.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import Flask, current_app, redirect, render_template, request, url_for

from biobank import entry
from biobank.edit import EditError, NotFoundError
from biobank.parsing import SUFFIXES
from biobank.permissions import CREATE, UPDATE
from biobank.web.auth import flash_result, need, run_edit

# ---------------------------------------------------------------------------
# Состояние отправленной формы
# ---------------------------------------------------------------------------


@dataclass
class Submitted:
    """Что пользователь отправил и что ответить у формы.

    form — id формы на странице; values — введённое (после ошибки поля заполняются снова);
    error — жёсткая ошибка; warnings — мягкие предупреждения, ждут подтверждения;
    nearby — похожие места (entry.NearbySite), ждут выбора.
    """

    form: str
    values: dict = field(default_factory=dict)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    nearby: list = field(default_factory=list)


def form_view(submitted: Submitted | None, form_id: str, defaults: dict | None = None) -> dict:
    """Данные для шаблона одной формы (функция доступна в шаблонах как form_view).

    Если отправлена именно эта форма — её значения, ошибка и предупреждения, и она раскрыта;
    иначе — значения по умолчанию, форма свёрнута.
    """
    mine = submitted is not None and submitted.form == form_id
    return {
        "id": form_id,
        "data": submitted.values if mine else dict(defaults or {}),
        "error": submitted.error if mine else None,
        "warnings": submitted.warnings if mine else [],
        "nearby": submitted.nearby if mine else [],
        "open": mine,
    }


def form_values(*names: str, text: tuple[str, ...] = ()) -> dict[str, str]:
    """Значения полей из POST-запроса: лишние пробелы убраны; text — многострочные поля
    (примечания), в них пробелы внутри не трогаем, только по краям."""
    values = {name: " ".join(request.form.get(name, "").split()) for name in names}
    values.update({name: request.form.get(name, "").strip() for name in text})
    return values


def checked(name: str) -> bool:
    """Отмечена ли галочка (checkbox): браузер присылает её, только если она отмечена."""
    return request.form.get(name) == "1"


def to_int(text: str) -> int | None:
    """Номер из списка выбора (<select>): '' — не выбрано (None); не число — EditError."""
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise EditError(f"«{text}» — не номер. Выберите значение из списка.") from None


def go(url: str):
    """Перейти на страницу результата после успешной записи (POST → redirect)."""
    if request.headers.get("HX-Request") == "true":
        # HTMX не переходит по обычному redirect сам — его просят заголовком HX-Redirect;
        # 204 — «ответ без содержимого»: вставлять в страницу нечего
        response = current_app.response_class(status=204)
        response.headers["HX-Redirect"] = url
        return response
    return redirect(url, code=303)


def save(action, on_error, success_url):
    """run_edit(action) → сообщение и переход на success_url(result) или on_error(текст).

    action(conn, user) — запись от имени вошедшего. NotFoundError (нет экспедиции, выезда,
    особи…) не ловим — её покажет общий обработчик ошибок страницей 404; нет прав — 403
    (run_edit). Предупреждения записи (EditResult.warnings, notices) — жёлтым: запись
    сохранена; проблемы данных (SL > TL и т.п.) при этом уже записаны в журнал.
    """
    try:
        result = run_edit(action)
    except NotFoundError:
        raise
    except EditError as error:
        return on_error(str(error))
    flash_result(result)
    return go(success_url(result))


# ---------------------------------------------------------------------------
# Подсказки и справочники для форм (только чтение)
# ---------------------------------------------------------------------------

# Порядок групп в выборе места (<optgroup>) и как их назвать
SITE_GROUPS = (
    ("участок реки", "Участки реки (траление)"),
    ("рынок", "Рынки"),
    ("точка рыбака", "Точки рыбаков"),
    ("аквахозяйство", "Аквахозяйства"),
)


def grouped_sites(sites: list[dict]) -> list[tuple[str, list[dict]]]:
    """Места, разложенные по типу: [(заголовок группы, [места])…]; пустые группы пропущены."""
    groups = []
    for site_type, title in SITE_GROUPS:
        items = [s for s in sites if s["site_type"] == site_type]
        if items:
            groups.append((title, items))
    other = [s for s in sites if s["site_type"] not in dict(SITE_GROUPS)]
    if other:
        groups.append(("Другие", other))
    return groups


# Как назвать серию номеров в выборе суффикса (серии — из parsing.SUFFIXES)
SERIES_TITLES = {
    "Pangasius": "Pangasius, общая серия",
    "pc": "Plotosus",
    "Bg": "Bagarius",
    "mr": "Macrobrachium",
}


def label_suffixes() -> list[tuple[str, str]]:
    """Суффиксы номеров проб для выбора: [(суффикс, название серии)…] — из parsing.SUFFIXES."""
    pairs = dict.fromkeys(SUFFIXES.values())
    return [(suffix, SERIES_TITLES.get(series, series)) for suffix, series in pairs]


def site_hints(sites: list[dict]) -> dict[str, list[str]]:
    """Города и провинции, которые уже есть у мест, — подсказки для полей формы места."""
    return {
        "cities": sorted({s["city"] for s in sites if s["city"]}),
        "regions": sorted({s["region"] for s in sites if s["region"]}),
    }


# ---------------------------------------------------------------------------
# Предварительные проверки (только чтение, до записи)
# ---------------------------------------------------------------------------


def confirmed(warnings: list[str]) -> bool:
    """Все ли эти предупреждения пользователь уже видел и подтвердил кнопкой.

    Тексты подтверждённых предупреждений приходят из скрытых полей confirmed. Если после
    подтверждения значения изменили и появилось новое предупреждение — спросим снова.
    """
    return set(warnings) <= set(request.form.getlist("confirmed"))


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------


def register_entry_routes(app: Flask, pages) -> None:
    """Маршруты форм ввода. pages — модуль app.py: функции страниц (expeditions_page…)
    и read_db (подключение только на чтение). Передаётся параметром, чтобы модули
    не импортировали друг друга по кругу."""
    app.jinja_env.globals["form_view"] = form_view

    @app.get("/next-label")
    def next_label():
        """Кнопка «Следующий свободный»: HTMX вставляет поле номера с готовым значением."""
        suffix = request.args.get("suffix", "")
        try:
            label, error = entry.next_label(pages.read_db(), suffix), None
        except EditError as exc:
            label, error = request.args.get("label", ""), str(exc)
        return render_template("_next_label.html", label=label, error=error)

    # ---- экспедиция ----

    @app.post("/expeditions/new")
    def expedition_new():
        need(CREATE, "expedition")
        values = form_values("name", "date_start", "date_end", text=("notes",))

        def action(conn, user):
            return entry.create_expedition(
                conn, values["name"], values["date_start"], values["date_end"], values["notes"],
                user=user,
            )  # fmt: skip

        return save(
            action,
            lambda error: pages.expeditions_page(Submitted("f-expedition", values, error), 400),
            lambda result: url_for("expedition", expedition_id=result.record_id),
        )

    # ---- место ----

    @app.post("/sites/new")
    def site_new():
        need(CREATE, "sampling_site")
        values = form_values("site_type", "name", "city", "region", "lat", "lon",
                             text=("description",))  # fmt: skip
        values["new_region"] = "1" if checked("new_region") else ""

        def page(status=400, **state):
            return pages.sites_page(Submitted("f-site", values, **state), status)

        try:  # что сделает create_site — без записи: название, похожие места
            _name, nearby = entry.preview_site(
                pages.read_db(), values["site_type"], name=values["name"], lat=values["lat"],
                lon=values["lon"], city=values["city"], region=values["region"],
                new_region=checked("new_region"),
            )  # fmt: skip
        except EditError as error:
            return page(error=str(error))
        # «Всё равно создать новое» — кнопка с номерами показанных похожих мест;
        # подтверждение действует, только если похожие места те же самые
        shown = set(request.form.get("allow_nearby", "").split(","))
        allow = bool(nearby) and {str(site.site_id) for site in nearby} <= shown
        if nearby and not allow:
            return page(200, nearby=nearby)

        def action(conn, user):
            return entry.create_site(
                conn, values["site_type"], name=values["name"], lat=values["lat"],
                lon=values["lon"], city=values["city"], region=values["region"],
                description=values["description"], new_region=checked("new_region"),
                allow_nearby=allow, user=user,
            )  # fmt: skip

        return save(
            action,
            lambda error: page(error=error),
            lambda result: url_for("site", site_id=result.record_id),
        )

    # ---- выезд ----

    @app.post("/expeditions/<int:expedition_id>/events/new")
    def event_new(expedition_id: int):
        need(CREATE, "sampling_event")
        values = form_values("site_id", "event_date", "capture_method", "gear_mesh",
                             "gear_size", text=("notes",))  # fmt: skip

        def page(error):
            sub = Submitted("f-event", values, error)
            return pages.expedition_page(expedition_id, sub, 400)

        try:
            site_id = to_int(values["site_id"])
        except EditError as error:
            return page(str(error))
        if site_id is None:
            return page("Выберите место из списка. Нового места нет — сначала создайте его "
                        "на странице «Места».")  # fmt: skip
        if not values["capture_method"]:
            return page("Выберите способ получения.")

        def action(conn, user):
            return entry.create_event(
                conn, expedition_id, site_id, values["event_date"], values["capture_method"],
                gear_mesh=values["gear_mesh"], gear_size=values["gear_size"],
                notes=values["notes"], user=user,
            )  # fmt: skip

        return save(action, page, lambda result: url_for("event", event_id=result.record_id))

    # ---- траление, замер воды, прилов (карточка выезда) ----

    def event_form_error(event_id: int, form_id: str, values: dict):
        def page(error):
            return pages.event_page(event_id, Submitted(form_id, values, error), 400)

        return page

    @app.post("/events/<int:event_id>/trawlings/new")
    def trawling_new(event_id: int):
        need(CREATE, "trawling")
        values = form_values("trawl_no", "time_start", "time_end", "duration_min",
                             "speed_kmh", "intermediate_depth_m", "catch_note",
                             text=("notes",))  # fmt: skip
        page = event_form_error(event_id, "f-trawling", values)
        try:
            trawl_no = int(values["trawl_no"])
        except ValueError:
            return page(f"Номер траления — целое число (введено «{values['trawl_no']}»).")

        def action(conn, user):
            return entry.add_trawling(
                conn, event_id, trawl_no, time_start=values["time_start"],
                time_end=values["time_end"], duration_min=values["duration_min"],
                speed_kmh=values["speed_kmh"],
                intermediate_depth_m=values["intermediate_depth_m"],
                catch_note=values["catch_note"], notes=values["notes"], user=user,
            )  # fmt: skip

        return save(action, page, lambda _result: url_for("event", event_id=event_id))

    @app.post("/events/<int:event_id>/water/new")
    def water_new(event_id: int):
        need(CREATE, "water_measurement")
        values = form_values("point_type", "trawl_no", "measured_time", "coord_source",
                             "garmin_wp", *entry.WATER_NUMBERS, text=("notes",))  # fmt: skip
        page = event_form_error(event_id, "f-water", values)
        try:
            trawl_no = to_int(values["trawl_no"])
        except EditError as error:
            return page(str(error))

        def action(conn, user):
            return entry.add_water_measurement(
                conn, event_id, values["point_type"], trawl_no,
                measured_time=values["measured_time"], coord_source=values["coord_source"],
                garmin_wp=values["garmin_wp"], notes=values["notes"], user=user,
                **{column: values[column] for column in entry.WATER_NUMBERS},
            )  # fmt: skip

        return save(action, page, lambda _result: url_for("event", event_id=event_id))

    @app.post("/events/<int:event_id>/catch/new")
    def catch_new(event_id: int):
        need(CREATE, "catch_record")
        values = form_values("trawl_no", "taxon_text", "count_n", "mass_g", text=("raw_text",))
        page = event_form_error(event_id, "f-catch", values)
        try:
            trawl_no = to_int(values["trawl_no"])
        except EditError as error:
            return page(str(error))

        def action(conn, user):
            return entry.create_catch_record(
                conn, event_id, raw_text=values["raw_text"], taxon_text=values["taxon_text"],
                count_n=values["count_n"], mass_g=values["mass_g"], trawl_no=trawl_no,
                user=user,
            )  # fmt: skip

        return save(action, page, lambda _result: url_for("event", event_id=event_id))

    # ---- особь ----

    @app.post("/events/<int:event_id>/specimens/new")
    def specimen_new(event_id: int):
        need(CREATE, "specimen")
        values = form_values("label", "suffix", "species", "confidence", *entry.MEASUREMENTS,
                             "sex", "trawl_no", "capture_method", "capture_site_id",
                             "capture_date", "identified_by", text=("notes",))  # fmt: skip
        values["is_dead"] = "1" if checked("is_dead") else ""

        def page(status=400, **state):
            return pages.event_page(event_id, Submitted("f-specimen", values, **state), status)

        if not values["species"]:  # пустой выбор — скорее забыли, чем «не определён»
            return page(error="Выберите вид. Если вид неизвестен — «? — вид не определён».")
        try:
            trawl_no = to_int(values["trawl_no"])
            site_id = to_int(values["capture_site_id"])
        except EditError as error:
            return page(error=str(error))
        # одни и те же аргументы — для проверки без записи и для самой записи
        arguments = dict(
            confidence=values["confidence"] or None,
            **{column: values[column] for column in entry.MEASUREMENTS},
            sex=values["sex"] or None, is_dead=checked("is_dead"), trawl_no=trawl_no,
            capture_method=values["capture_method"] or None, capture_site_id=site_id,
            capture_date=values["capture_date"], identified_by=values["identified_by"],
            notes=values["notes"],
        )  # fmt: skip
        try:  # не число, вида нет в справочнике… (нет выезда — NotFoundError → 404)
            label, check = entry.check_specimen(
                pages.read_db(), event_id, values["species"], values["label"] or None,
                **arguments,
            )  # fmt: skip
        except NotFoundError:
            raise
        except EditError as error:
            return page(error=str(error))
        if check.errors:  # TL ≤ 0, номер занят, траления нет…
            return page(error=" ".join(check.errors))
        if check.warnings and not confirmed(check.warnings):
            values["label"] = label or ""  # номер, который будет записан, — виден сразу
            return page(200, warnings=check.warnings)

        def action(conn, user):
            return entry.create_specimen(
                conn, event_id, values["species"], label, **arguments, user=user
            )

        # номер известен заранее (введён или подобран выше) — по нему откроем карточку;
        # без номера (вид без кода) create_specimen откажет и объяснит, что указать
        return save(
            action, lambda error: page(error=error), lambda _r: url_for("specimen", label=label)
        )

    # ---- образцы и промеры (карточка особи) ----

    @app.post("/specimen/<path:label>/samples/new")
    def sample_new(label: str):
        need(CREATE, "sample")
        values = form_values("sample_type", "organ", "sample_label", "preservative",
                             text=("notes", "labs"))  # fmt: skip
        labs = [line.strip() for line in values["labs"].splitlines() if line.strip()]

        def action(conn, user):
            return entry.add_sample(
                conn, label, values["sample_type"], organ=values["organ"],
                sample_label=values["sample_label"], preservative=values["preservative"],
                notes=values["notes"], labs=labs, user=user,
            )  # fmt: skip

        return save(
            action,
            lambda error: pages.specimen_entry_page(label, Submitted("f-sample", values, error)),
            lambda _result: url_for("specimen", label=label),
        )

    @app.post("/specimen/<path:label>/ship")
    def sample_ship(label: str):
        need(CREATE, "sample_shipment")
        values = form_values("sample_id", "lab", "sent_on")
        values["new_lab"] = "1" if checked("new_lab") else ""

        def page(error):
            return pages.specimen_entry_page(label, Submitted("f-ship", values, error))

        try:
            sample_id = to_int(values["sample_id"])
        except EditError as error:
            return page(str(error))
        if sample_id is None:
            return page("Выберите образец из списка.")

        def action(conn, user):
            return entry.ship_sample(
                conn,
                sample_id,
                values["lab"],
                values["sent_on"],
                new_lab=checked("new_lab"),
                user=user,
            )

        return save(action, page, lambda _result: url_for("specimen", label=label))

    @app.post("/specimen/<path:label>/measurements")
    def measurements(label: str):
        need(UPDATE, "specimen", pages.specimen_record(label))
        names = tuple(entry.MEASUREMENTS)
        values = form_values(*names, *(f"old_{name}" for name in names))

        def page(status=400, **state):
            sub = Submitted("f-measure", values, **state)
            return pages.specimen_entry_page(label, sub, status)

        # меняем только те промеры, которые пользователь исправил (поле отличается от
        # значения, показанного в форме); пустое поле — стереть значение
        changes = {name: values[name] for name in names if values[name] != values[f"old_{name}"]}
        if not changes:
            return page(error="Промеры не изменены — исправьте нужное поле и сохраните снова.")
        try:  # все четыре промера — какими станут после правки (без записи)
            check = entry.check_measurements_update(pages.read_db(), label, **changes)
        except NotFoundError:
            raise
        except EditError as error:  # «abc» — не число
            return page(error=str(error))
        if check.errors:
            return page(error=" ".join(check.errors))
        if check.warnings and not confirmed(check.warnings):
            return page(200, warnings=check.warnings)

        def action(conn, user):
            return entry.update_measurements(conn, label, **changes, user=user)

        return save(
            action, lambda error: page(error=error), lambda _r: url_for("specimen", label=label)
        )
