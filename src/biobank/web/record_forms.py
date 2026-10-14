"""Правка и удаление записей, пакетный ввод особей (этап C сайта, часть 2).

Правка: кнопка «Изменить» у записи → страница /edit/<вид>/<id> с формой, заполненной
из browse.get_record. Форма помнит, что было показано (скрытые поля old_…), и отправляет
в records.update_… только изменённые поля: если кто-то другой тем временем поправил
другое поле этой записи, его правка не затрётся старым значением из формы.

Удаление: кнопка «Удалить» → страница /delete/<вид>/<id> «Вы уверены? Будет удалено: …»
(только чтение) → кнопка «Да, удалить» (POST) → records.delete_…. Если удалить нельзя
(есть зависимые записи) — текст ошибки на той же странице, база не меняется.
После удаления — переход на «родителя»: выезд → экспедиция, особь → выезд и т.д.

Куда вернуться: кнопки «Изменить»/«Удалить» в карточках передают адрес своей страницы
параметром next (/edit/specimen/5?next=/events/3). Форма хранит его в скрытом поле, и после
сохранения (и по «Отмене») пользователь возвращается туда, откуда пришёл. Принимается только
путь на этом сайте (views.local_path). После удаления на страницу самой удалённой записи
вернуться нельзя — её больше нет: тогда переход на «родителя».

Пакетный ввод: таблица-форма на странице /events/<id>/specimens/batch, строка = особь.
«Проверить» — entry.check_specimens (только чтение), «Сохранить» — entry.create_specimens
(всё или ничего). Мягкие предупреждения — как у одиночной формы: «Всё равно сохранить».

Все записи — только POST через edit.apply_edit (резервная копия + транзакция), после
успеха — redirect (POST/redirect), ошибки — у формы, введённое не теряется.
Здесь нет SQL и правил предметной области: только «какие поля показать и как их назвать».
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from flask import Flask, abort, current_app, flash, render_template, request, url_for

from biobank import browse, entry, records
from biobank.edit import EditError, NotFoundError, apply_edit, event_title
from biobank.parsing import ORGANS
from biobank.web.entry_forms import confirmed, go
from biobank.web.views import local_path

# ---------------------------------------------------------------------------
# Поля форм правки
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormField:
    """Одно поле формы правки.

    name — имя поля функции records.update_… (и колонки из browse.get_record);
    widget — как показать: text, number, date, time, area (многострочный), select, check;
    choices — какой список вариантов взять для select (см. build_choices);
    empty — подпись пустого варианта select (None — пустого варианта нет);
    datalist — подсказки для текстового поля (можно выбрать или написать своё).
    """

    name: str
    label: str
    widget: str = "text"
    required: bool = False
    hint: str | None = None
    choices: str | None = None
    empty: str | None = None
    placeholder: str | None = None
    datalist: str | None = None


def _water_fields() -> tuple[FormField, ...]:
    return tuple(
        FormField(column, what, "number") for column, (what, _, _) in entry.WATER_NUMBERS.items()
    )


@dataclass(frozen=True)
class Kind:
    """Вид записи на сайте: как её найти, назвать, править и удалять.

    record — вид для browse.get_record; title — как называть («траление»);
    update / delete — функции records.py (update=None — правки нет, только удаление);
    together — что удаляется вместе с записью (правило records.py, для вопроса «Вы уверены?»).
    """

    record: str
    title: str
    fields: tuple[FormField, ...]
    update: Callable | None
    delete: Callable
    together: str | None = None


KINDS: dict[str, Kind] = {
    "expedition": Kind(
        "expedition", "экспедиция",
        (
            FormField("name", "Название", required=True),
            FormField("date_start", "Дата начала", "date"),
            FormField("date_end", "Дата окончания", "date",
                      hint="даты должны включать даты всех выездов"),
            FormField("notes", "Примечания", "area"),
        ),
        records.update_expedition, records.delete_expedition,
    ),
    "site": Kind(
        "site", "место",
        (
            FormField("site_type", "Тип места", "select", required=True, choices="site_type",
                      hint="должен подходить к способу получения всех выездов места"),
            FormField("name", "Название", required=True),
            FormField("city", "Город", datalist="cities",
                      hint="пусто — города нет; провинция прежняя, если её не менять"),
            FormField("region", "Провинция", datalist="regions"),
            FormField("lat", "Широта", "number"),
            FormField("lon", "Долгота", "number", hint="координаты — обе или ни одной"),
            FormField("description", "Описание", "area"),
        ),
        records.update_site, records.delete_site,
    ),
    "event": Kind(
        "event", "выезд",
        (
            FormField("expedition_id", "Экспедиция", "select", required=True,
                      choices="expedition_id"),
            FormField("site_id", "Место", "select", required=True, choices="site_id",
                      hint="тип места должен подходить к способу получения"),
            FormField("event_date", "Дата выезда", "date", required=True),
            FormField("capture_method", "Способ получения", "select", required=True,
                      choices="capture_method",
                      hint="«траление» нельзя сменить, пока у выезда есть траления"),
            FormField("gear_mesh", "Сеть: размер ячеи, см", placeholder="3,5x3,5",
                      hint="только у траления; при смене способа — очистите оба поля сети"),
            FormField("gear_size", "Сеть: размер, м", placeholder="6,5x12"),
            FormField("notes", "Примечания", "area"),
        ),
        records.update_event, records.delete_event,
    ),
    "trawling": Kind(
        "trawling", "траление",
        (
            FormField("trawl_no", "Номер траления", "number", required=True),
            FormField("time_start", "Время старта", "time"),
            FormField("time_end", "Время финиша", "time"),
            FormField("duration_min", "Длительность, мин", "number"),
            FormField("speed_kmh", "Скорость, км/ч", "number"),
            FormField("intermediate_depth_m", "Промежуточная глубина, м", "number"),
            FormField("catch_note", "Улов (как в журнале)"),
            FormField("notes", "Примечания", "area"),
        ),
        records.update_trawling, records.delete_trawling,
        together="его замеры воды (старт и финиш)",
    ),
    "water": Kind(
        "water_measurement", "замер воды",
        (
            FormField("point_type", "Точка", "select", required=True, choices="point_type",
                      hint="«точка» — замер не при тралении, без номера траления"),
            FormField("trawl_no", "Траление", "select", choices="trawl_no",
                      empty="— без траления (точка) —"),
            FormField("measured_time", "Время замера", "time"),
            FormField("coord_source", "Координаты по", "select", choices="coord_source",
                      empty="— не указано —"),
            FormField("garmin_wp", "№ точки в Garmin"),
            *_water_fields(),
            FormField("notes", "Примечания", "area"),
        ),
        records.update_water_measurement, records.delete_water_measurement,
    ),
    "specimen": Kind(
        "specimen", "особь",
        (
            FormField("label", "Номер пробы", required=True,
                      hint="метки образцов (надписи на пробирках) при смене номера не меняются"),
            FormField("event_id", "Выезд", "select", required=True, choices="event_id",
                      hint="при переносе в другой выезд траление уберите (или выберите позже)"),
            FormField("trawl_no", "Траление", "select", choices="trawl_no", empty="неизвестно",
                      hint="только если точно известно"),
            FormField("sex", "Пол", "select", choices="sex", empty="— не указан —"),
            FormField("is_dead", "Особь мёртвая", "check"),
            FormField("capture_method", "Свой способ получения", "select",
                      choices="capture_method", empty="как у выезда"),
            FormField("capture_site_id", "Своё место поимки", "select", choices="site_id",
                      empty="как у выезда"),
            FormField("capture_date", "Своя дата поимки", "date", hint="пусто — дата выезда"),
            FormField("notes", "Примечания", "area"),
        ),
        records.update_specimen, records.delete_specimen,
        together="вся история определений вида этой особи",
    ),
    "sample": Kind(
        "sample", "образец",
        (
            FormField("sample_type", "Тип образца", "select", required=True,
                      choices="sample_type"),
            FormField("organ", "Орган", datalist="organs"),
            FormField("label", "Метка образца", hint="пусто — номер особи"),
            FormField("preservative", "Фиксация", datalist="preservatives"),
            FormField("notes", "Примечания", "area"),
        ),
        records.update_sample, records.delete_sample,
        together="отметки об отправке образца в лаборатории",
    ),
    "catch": Kind(
        "catch_record", "запись прилова",
        (
            FormField("raw_text", "Как записано в журнале", "area", required=True),
            FormField("taxon_text", "Кто"),
            FormField("count_n", "Штук", "number"),
            FormField("mass_g", "Масса, г", "number"),
            FormField("trawl_no", "Траление", "select", choices="trawl_no",
                      empty="— весь выезд —"),
        ),
        records.update_catch_record, records.delete_catch_record,
    ),
    # определение вида не правят: вид меняют новым определением (история сохраняется);
    # ошибочно добавленное — удаляют
    "identification": Kind(
        "identification", "определение вида", (), None, records.delete_identification
    ),
}  # fmt: skip


def as_text(value) -> str:
    """Значение из базы → текст поля формы: None — пусто, 29.9 → '29.9', 3 → '3'.

    >>> as_text(None), as_text(10.103733), as_text(7), as_text("печень")
    ('', '10.103733', '7', 'печень')
    """
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def record_values(kind: Kind, record: dict) -> dict[str, str]:
    """Значения полей формы из записи (browse.get_record)."""
    values = {}
    for f in kind.fields:
        if f.widget == "check":
            values[f.name] = "1" if record.get(f.name) else ""
        else:
            values[f.name] = as_text(record.get(f.name))
    return values


def clean(text: str, widget: str) -> str:
    """Текст поля как в entry_forms.form_values: лишние пробелы убраны; в многострочных —
    только по краям (и переводы строк в одном виде: браузер присылает их как \\r\\n)."""
    if widget == "area":
        return text.replace("\r\n", "\n").strip()
    return " ".join(text.split())


def read_form(kind: Kind) -> tuple[dict[str, str], dict[str, str]]:
    """(введённые значения, показанные значения old_…) из POST-запроса."""
    values, old = {}, {}
    for f in kind.fields:
        if f.widget == "check":
            values[f.name] = "1" if request.form.get(f.name) == "1" else ""
            old[f.name] = "1" if request.form.get(f"old_{f.name}") == "1" else ""
        else:
            values[f.name] = clean(request.form.get(f.name, ""), f.widget)
            old[f.name] = clean(request.form.get(f"old_{f.name}", ""), f.widget)
    return values, old


def build_choices(conn, kind: Kind, record: dict) -> dict[str, list[tuple[str, str]]]:
    """Варианты для полей выбора и подсказки (datalist) — только для полей этого вида."""
    keys = {f.choices for f in kind.fields if f.choices} | {
        f.datalist for f in kind.fields if f.datalist
    }
    fixed = browse.field_choices()
    out: dict[str, list[tuple[str, str]]] = {}
    for key in keys:
        if key in ("site_type", "point_type", "sample_type", "sex", "coord_source"):
            out[key] = [(v, v) for v in fixed[key]]
        elif key == "capture_method":
            out[key] = [(m, m) for m in browse.list_capture_methods(conn)]
        elif key == "expedition_id":
            out[key] = [(str(x["expedition_id"]), x["name"]) for x in browse.list_expeditions(conn)]
        elif key == "site_id":
            out[key] = [
                (str(s["site_id"]), f"№{s['site_id']} {s['name']} ({s['site_type']})")
                for s in browse.list_sites(conn)
            ]
        elif key == "event_id":
            # у выездов из Excel title — лист, у введённых вручную — «№25 от <даты>»
            out[key] = [
                (str(e["event_id"]), e["title"] + (f", {e['event_date']}" if e["source_sheet"]
                                                   else "") + f" — {e['site']}")
                for e in browse.list_events(conn)
            ]  # fmt: skip
        elif key == "trawl_no":
            out[key] = [
                (str(t["trawl_no"]), f"№{t['trawl_no']}" + (f", {t['time_start']}"
                                                            if t["time_start"] else ""))
                for t in browse.event_trawls(conn, record["event_id"])
            ]  # fmt: skip
        elif key == "cities":
            names = dict.fromkeys(c["name"] for c in browse.list_cities(conn))
            out[key] = [(n, n) for n in names]
        elif key == "regions":
            out[key] = [(r["name"], r["name"]) for r in browse.list_regions(conn)]
        elif key == "organs":
            out[key] = [(n, n) for n in dict.fromkeys(name for _, name in ORGANS)]
        elif key == "preservatives":
            names = [p["preservative"] for p in browse.list_preservatives(conn)]
            out[key] = [(n, n) for n in names]
    return out


def display_rows(conn, kind_name: str, kind: Kind, record: dict) -> list[tuple[str, str | None]]:
    """Что показать в вопросе «Вы уверены?»: (подпись, значение) по полям записи."""
    if kind_name == "identification":
        names = {s["species_id"]: s["scientific_name"] for s in browse.list_species(conn)}
        return [
            ("вид", names.get(record["species_id"], "(не определён)")),
            ("метод", record["method"]),
            ("уверенность", record["confidence"]),
            ("дата", record["identified_on"]),
            ("кто определил", record["identified_by"]),
            ("в таблице записано", record["raw_text"]),
            ("примечания", record["notes"]),
        ]
    choices = build_choices(conn, kind, record)
    rows = []
    for f in kind.fields:
        value = record.get(f.name)
        if f.widget == "check":
            text = "да" if value else "нет"
        elif f.choices and value is not None:
            text = dict(choices.get(f.choices, [])).get(as_text(value), as_text(value))
        else:
            text = as_text(value) or None
        rows.append((f.label, text))
    return rows


# ---------------------------------------------------------------------------
# Как назвать запись, куда вернуться
# ---------------------------------------------------------------------------


def _specimen_label(conn, specimen_id: int) -> str:
    return browse.get_record(conn, "specimen", specimen_id)["label"]


def record_title(conn, kind_name: str, rec: dict) -> str:
    """«траление 3 выезда «Точка 1»» — для заголовков и вопроса «Вы уверены?»."""
    if kind_name == "expedition":
        return f"экспедиция «{rec['name']}»"
    if kind_name == "site":
        return f"место №{rec['site_id']} «{rec['name']}»"
    if kind_name == "event":
        return "выезд " + event_title(rec["source_sheet"], rec["event_id"], rec["event_date"])
    if kind_name == "trawling":
        event = browse.get_record(conn, "event", rec["event_id"])
        title = event_title(event["source_sheet"], event["event_id"], event["event_date"])
        return f"траление {rec['trawl_no']} выезда {title}"
    if kind_name == "water":
        where = f", траление {rec['trawl_no']}" if rec.get("trawl_no") is not None else ""
        return f"замер воды №{rec['measurement_id']} ({rec['point_type']}{where})"
    if kind_name == "specimen":
        return f"особь {rec['label']}"
    if kind_name == "sample":
        owner = _specimen_label(conn, rec["specimen_id"])
        return f"образец «{rec['label']}» ({rec['sample_type']}) особи {owner}"
    if kind_name == "catch":
        return f"запись прилова «{rec['raw_text']}»"
    if kind_name == "identification":
        return f"определение вида особи {_specimen_label(conn, rec['specimen_id'])}"
    return kind_name


def view_url(conn, kind_name: str, rec: dict) -> str:
    """Страница, где запись видна (туда ведёт «Отмена» и переход после правки)."""
    if kind_name == "expedition":
        return url_for("expedition", expedition_id=rec["expedition_id"])
    if kind_name == "site":
        return url_for("site", site_id=rec["site_id"])
    if kind_name == "trawling":
        return url_for("trawling", trawling_id=rec["trawling_id"])
    if kind_name == "water" and rec.get("trawling_id") is not None:
        return url_for("trawling", trawling_id=rec["trawling_id"])
    if kind_name == "specimen":
        return url_for("specimen", label=rec["label"])
    if kind_name in ("sample", "identification"):
        return url_for("specimen", label=_specimen_label(conn, rec["specimen_id"]))
    return url_for("event", event_id=rec["event_id"])  # выезд, замер без траления, прилов


def parent_url(conn, kind_name: str, rec: dict) -> str:
    """Куда перейти после удаления: на «родителя» записи."""
    if kind_name == "expedition":
        return url_for("expeditions")
    if kind_name == "site":
        return url_for("sites")
    if kind_name == "event":
        return url_for("expedition", expedition_id=rec["expedition_id"])
    if kind_name in ("trawling", "specimen", "catch"):
        return url_for("event", event_id=rec["event_id"])
    return view_url(conn, kind_name, rec)  # замер, образец, определение вида


def requested_next() -> str | None:
    """Куда вернуться: next из адреса (GET) или из скрытого поля формы (POST).

    Только путь на этом сайте (views.local_path); чужой адрес — None (родная страница записи).
    """
    raw = request.form.get("next") if request.method == "POST" else request.args.get("next")
    return local_path(raw)


def _path(url: str) -> str:
    """Путь адреса без параметров и в раскодированном виде: «/specimen/26%20ph?x=1» →
    «/specimen/26 ph» — чтобы сравнивать адреса, записанные по-разному."""
    return unquote(urlsplit(url).path)


def _is_form_page(url: str) -> bool:
    """Страница правки или удаления — после записи на неё не возвращаемся."""
    return _path(url).startswith(("/edit/", "/delete/"))


def own_pages(kind_name: str, rec: dict) -> set[str]:
    """Пути страниц самой записи (у образца, замера, прилова своей страницы нет)."""
    if kind_name == "expedition":
        url = url_for("expedition", expedition_id=rec["expedition_id"])
    elif kind_name == "site":
        url = url_for("site", site_id=rec["site_id"])
    elif kind_name == "event":
        url = url_for("event", event_id=rec["event_id"])
    elif kind_name == "trawling":
        url = url_for("trawling", trawling_id=rec["trawling_id"])
    elif kind_name == "specimen":
        url = url_for("specimen", label=rec["label"])
    else:
        return set()
    return {_path(url)}


def after_edit_url(conn, kind_name: str, before: dict, after: dict, back: str | None) -> str:
    """Куда перейти после правки: туда, откуда пришли (back), иначе — страница записи.

    Номер особи изменился, а пришли с карточки по старому номеру — на карточку с новым
    номером (по старому её уже не найти: была бы 404).
    """
    if back is None or _is_form_page(back):
        return view_url(conn, kind_name, after)
    # карточка особи по старому номеру (в любом написании: «26ph», «26 ph») — уже 404
    if kind_name == "specimen" and before["label"] != after["label"]:
        if _path(back).startswith("/specimen/"):
            return view_url(conn, kind_name, after)
    return back


def after_delete_url(conn, kind_name: str, rec: dict, back: str | None) -> str:
    """Куда перейти после удаления: туда, откуда пришли, — если та страница ещё есть.

    Страница самой удалённой записи (и её формы /edit/…, /delete/…) — уже 404: тогда
    на «родителя». Для особи — любая карточка /specimen/…: номер в адресе можно написать
    по-разному («26ph», «26 ph»), при сомнении — «родитель».
    """
    if back is None or _is_form_page(back) or _path(back) in own_pages(kind_name, rec):
        return parent_url(conn, kind_name, rec)
    if kind_name == "specimen" and _path(back).startswith("/specimen/"):
        return parent_url(conn, kind_name, rec)
    return back


# ---------------------------------------------------------------------------
# Пакетный ввод особей
# ---------------------------------------------------------------------------

# Колонки таблицы-формы: имя → (подпись, ширина поля в символах)
BATCH_TEXT = {
    "label": ("№ пробы", 7),
    "tl_cm": ("TL, см", 5),
    "sl_cm": ("SL, см", 5),
    "mass_g": ("m, г", 6),
    "mass_gutted_g": ("m без внутр., г", 6),
}
BATCH_CHECKS = {"is_dead": "мёртвая", "dna": "ДНК", "smear": "мазок крови"}
BATCH_FIELDS = ("label", "species", "tl_cm", "sl_cm", "mass_g", "mass_gutted_g", "trawl_no",
                "is_dead", "notes", "dna", "smear")  # fmt: skip
# Поля строки, которые передаются в entry.py как есть (галочки образцов — отдельно)
BATCH_PASSED = ("label", "species", "tl_cm", "sl_cm", "mass_g", "mass_gutted_g", "trawl_no",
                "is_dead", "notes")  # fmt: skip
# Образцы галочками → тип образца (метка — номер особи, фиксация не указана: не угадываем)
BATCH_SAMPLES = {"dna": "ДНК", "smear": "мазок крови"}
BATCH_START_ROWS = 10  # пустых строк в новой таблице и на кнопке «Ещё строки»
_ROW_NAME = re.compile(r"^r(\d+)-(\w+)$")


@dataclass
class RowStatus:
    """Что показать в строке таблицы после проверки."""

    label: str | None = None  # номер, под которым особь будет записана
    auto: bool = False  # номер подобран («авто»)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def empty_row() -> dict[str, str]:
    return dict.fromkeys(BATCH_FIELDS, "")


def read_batch_rows() -> list[dict[str, str]]:
    """Строки таблицы из POST: поля r<номер>-<имя> → список словарей по номерам строк.

    Номер строки — в имени поля, а не по порядку: невыбранная галочка в форму не
    попадает, и без номера значения строк «съехали» бы. Пропуск номера — пустая строка.
    """
    found: dict[int, dict[str, str]] = {}
    for name, value in request.form.items(multi=True):
        match = _ROW_NAME.match(name)
        if not match or match.group(2) not in BATCH_FIELDS:
            continue
        i = int(match.group(1))
        if not 1 <= i <= entry.MAX_BATCH_ROWS:
            continue
        row = found.setdefault(i, empty_row())
        if match.group(2) in BATCH_CHECKS:
            row[match.group(2)] = "1" if value == "1" else ""
        else:
            row[match.group(2)] = clean(value, "area" if match.group(2) == "notes" else "text")
    count = max(found, default=0)
    return [found.get(i, empty_row()) for i in range(1, count + 1)]


def is_filled(row: dict[str, str]) -> bool:
    return any(row.values())


def backend_row(row: dict[str, str]) -> dict:
    """Строка формы → строка для entry.check_specimens / create_specimens.

    Пустой номер — «авто» (следующий свободный в серии вида); пустая строка
    целиком (без вида и галочек) пропускается функциями entry.py.
    """
    data = {name: row[name] for name in BATCH_PASSED}
    data["samples"] = [{"sample_type": kind} for name, kind in BATCH_SAMPLES.items() if row[name]]
    return data


def web_errors(rows: list[dict[str, str]]) -> dict[int, str]:
    """Ошибки, которые видны без базы: строка заполнена, а вид не выбран.

    Как в одиночной форме: пустой вид — скорее забыли, чем «не определён» (для этого — «?»).
    """
    return {
        i: "Выберите вид. Если вид неизвестен — «? — вид не определён»."
        for i, row in enumerate(rows, start=1)
        if is_filled(row) and not row["species"]
    }


def row_statuses(rows, check, extra: dict[int, str]) -> dict[int, RowStatus]:
    """Итоги проверки (entry.BatchCheck) + свои ошибки web_errors → статус по номеру строки."""
    statuses: dict[int, RowStatus] = {}
    if check is not None:
        for r in check.rows:
            label_text = rows[r.row - 1]["label"]
            auto = not label_text or label_text.lower() in entry.AUTO_LABELS
            statuses[r.row] = RowStatus(r.label, auto, list(r.errors), list(r.warnings))
    for i, error in extra.items():
        statuses[i] = RowStatus(errors=[error])
    return statuses


def batch_warnings(statuses: dict[int, RowStatus]) -> list[str]:
    """Мягкие предупреждения всего пакета одним списком «строка N (номер): …»."""
    return [f"строка {i} ({s.label}): {w}" for i, s in sorted(statuses.items()) for w in s.warnings]


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------


def register_record_routes(app: Flask, pages) -> None:
    """Маршруты правки, удаления и пакетного ввода. pages — модуль app.py (read_db…)."""

    def kind_or_404(kind_name: str) -> Kind:
        if kind_name not in KINDS:
            abort(404)
        return KINDS[kind_name]

    # ---- правка ----

    def edit_page(kind_name, kind, rec, values, old, error=None, status=200):
        conn = pages.read_db()
        back = requested_next()
        f = {"id": "f-edit", "data": values, "old": old, "error": error, "warnings": [],
             "nearby": [], "open": True}  # fmt: skip
        return render_template(
            "record_edit.html",
            kind_name=kind_name,
            kind=kind,
            f=f,
            title=record_title(conn, kind_name, rec),
            back=back or view_url(conn, kind_name, rec),
            next_url=back,
            choices=build_choices(conn, kind, rec),
            extra={"new_region": request.form.get("new_region") == "1",
                   "allow_nearby": request.form.get("allow_nearby") == "1"},
        ), status  # fmt: skip

    @app.route("/edit/<kind_name>/<int:record_id>", methods=["GET", "POST"], endpoint="record_edit")
    def record_edit(kind_name: str, record_id: int):
        """GET — форма с текущими значениями; POST — сохранить изменённые поля."""
        kind = kind_or_404(kind_name)
        if kind.update is None:
            abort(404)
        conn = pages.read_db()
        rec = browse.get_record(conn, kind.record, record_id)  # нет записи — 404
        if request.method == "GET":
            values = record_values(kind, rec)
            return edit_page(kind_name, kind, rec, values, dict(values))

        values, old = read_form(kind)
        # только то, что пользователь исправил (поле отличается от показанного)
        changes = {name: values[name] for name in values if values[name] != old[name]}
        if not changes:
            error = "Ничего не изменено — исправьте нужное поле и сохраните снова."
            return edit_page(kind_name, kind, rec, values, old, error, 400)
        options = {}
        if kind_name == "site":
            options = {"new_region": request.form.get("new_region") == "1",
                       "allow_nearby": request.form.get("allow_nearby") == "1"}  # fmt: skip

        def action(c):
            return kind.update(c, record_id, **changes, **options)

        try:
            result = apply_edit(current_app.config["DB_PATH"], action)
        except NotFoundError:
            raise
        except EditError as error:
            return edit_page(kind_name, kind, rec, values, old, str(error), 400)
        flash(result.message, "ok")
        for warning in result.warnings:
            flash(f"Внимание: {warning}", "warn")
        # туда, откуда пришли; номер особи мог измениться — запись берём заново
        after = browse.get_record(conn, kind.record, record_id)
        return go(after_edit_url(conn, kind_name, rec, after, requested_next()))

    # ---- удаление ----

    def delete_page(
        title, rows, back, action, together=None, error=None, status=200, came_from=None
    ):
        return render_template(
            "record_delete.html", title=title, rows=rows, back=back, action=action,
            together=together, error=error, next_url=came_from,
        ), status  # fmt: skip

    def delete(action, success_url, page):
        """«Да, удалить» (POST): без кнопки подтверждения — снова вопрос, ничего не удалено."""
        if request.form.get("confirm") != "1":
            return page(None, 200)
        try:
            result = apply_edit(current_app.config["DB_PATH"], action)
        except NotFoundError:
            raise
        except EditError as error:  # есть зависимые записи и т.п. — база не изменена
            return page(str(error), 400)
        flash(result.message, "ok")
        for warning in result.warnings:
            flash(f"Внимание: {warning}", "warn")
        return go(success_url)

    @app.route("/delete/<kind_name>/<int:record_id>", methods=["GET", "POST"],
               endpoint="record_delete")  # fmt: skip
    def record_delete(kind_name: str, record_id: int):
        """GET — «Вы уверены? Будет удалено: …» (только чтение); POST — удалить."""
        kind = kind_or_404(kind_name)
        conn = pages.read_db()
        rec = browse.get_record(conn, kind.record, record_id)  # нет записи — 404
        # всё, что нужно показать и куда вернуться, — до удаления: потом записи уже нет
        title = record_title(conn, kind_name, rec)
        rows = display_rows(conn, kind_name, kind, rec)
        came_from = requested_next()
        back = came_from or view_url(conn, kind_name, rec)  # «Отмена»: запись на месте
        success = after_delete_url(conn, kind_name, rec, came_from)

        def page(error, status):
            action = url_for("record_delete", kind_name=kind_name, record_id=record_id)
            return delete_page(title, rows, back, action, kind.together, error, status, came_from)

        if request.method == "GET":
            return page(None, 200)
        return delete(lambda c: kind.delete(c, record_id), success, page)

    @app.route("/delete/shipment/<int:sample_id>/<int:lab_id>", methods=["GET", "POST"])
    def shipment_delete(sample_id: int, lab_id: int):
        """Удалить отметку «образец отправлен в лабораторию» (у отправки нет своего id)."""
        conn = pages.read_db()
        shipments = browse.list_shipments(conn, sample_id)  # нет образца — 404
        shipment = next((s for s in shipments if s["lab_id"] == lab_id), None)
        if shipment is None:
            raise NotFoundError(f"Отправки образца №{sample_id} в лабораторию №{lab_id} нет.")
        sample = browse.get_record(conn, "sample", sample_id)
        title = f"отметка об отправке: {record_title(conn, 'sample', sample)} → {shipment['lab']}"
        rows = [
            ("образец", sample["label"]),
            ("тип", sample["sample_type"]),
            ("лаборатория", shipment["lab"]),
            ("дата отправки", shipment["sent_on"]),
        ]
        back = view_url(conn, "sample", sample)

        def page(error, status):
            action = url_for("shipment_delete", sample_id=sample_id, lab_id=lab_id)
            return delete_page(title, rows, back, action, None, error, status)

        if request.method == "GET":
            return page(None, 200)
        return delete(lambda c: records.delete_shipment(c, sample_id, lab_id), back, page)

    # ---- пакетный ввод особей ----

    def batch_page(event_id, rows, statuses=None, error=None, warnings=(), status=200):
        conn = pages.read_db()
        event = browse.get_record(conn, "event", event_id)  # нет выезда — 404
        return render_template(
            "batch.html",
            event_id=event_id,
            event_title=event_title(event["source_sheet"], event_id, event["event_date"]),
            expedition_id=event["expedition_id"],
            rows=rows,
            statuses=statuses or {},
            error=error,
            warnings=list(warnings),
            species=browse.list_species(conn),
            trawls=browse.event_trawls(conn, event_id),
            text_fields=BATCH_TEXT,
            checks=BATCH_CHECKS,
        ), status

    @app.route("/events/<int:event_id>/specimens/batch", methods=["GET", "POST"])
    def specimen_batch(event_id: int):
        """Таблица-форма для нескольких особей: проверить (только чтение) и сохранить."""
        if request.method == "GET":
            return batch_page(event_id, [empty_row() for _ in range(BATCH_START_ROWS)])
        rows = read_batch_rows() or [empty_row()]
        what = request.form.get("action", "check")
        if what == "more":  # «Ещё строки» без JavaScript: ничего не проверяем и не пишем
            more = min(len(rows) + BATCH_START_ROWS, entry.MAX_BATCH_ROWS)
            return batch_page(event_id, rows + [empty_row() for _ in range(more - len(rows))])

        mine = web_errors(rows)
        conn = pages.read_db()
        try:
            check = entry.check_specimens(conn, event_id, [backend_row(r) for r in rows])
        except NotFoundError:
            raise
        except EditError as error:  # нет ни одной заполненной строки и т.п.
            return batch_page(event_id, rows, error=str(error), status=400)
        statuses = row_statuses(rows, check, mine)
        warnings = batch_warnings(statuses)
        has_errors = any(s.errors for s in statuses.values())
        if what != "save":  # «Проверить»: только показать итог
            return batch_page(event_id, rows, statuses, warnings=warnings)
        if has_errors:
            error = "Есть ошибки в строках (выделены красным) — ничего не сохранено."
            return batch_page(event_id, rows, statuses, error, warnings, 400)
        if warnings and not confirmed(warnings):
            return batch_page(event_id, rows, statuses, warnings=warnings)

        def action(c):
            return entry.create_specimens(c, event_id, [backend_row(r) for r in rows])

        try:
            result = apply_edit(current_app.config["DB_PATH"], action)
        except NotFoundError:
            raise
        except entry.BatchError as error:  # кто-то успел занять номер и т.п.
            statuses = row_statuses(rows, error.check, mine)
            message = "Есть ошибки в строках (выделены красным) — ничего не сохранено."
            return batch_page(event_id, rows, statuses, message, batch_warnings(statuses), 400)
        except EditError as error:
            return batch_page(event_id, rows, statuses, str(error), warnings, 400)
        flash(result.message, "ok")
        for warning in result.warnings:
            flash(f"Внимание: {warning}", "warn")
        return go(url_for("event", event_id=event_id))

    @app.get("/events/<int:event_id>/specimens/batch/row")
    def specimen_batch_row(event_id: int):
        """«Добавить строку» (HTMX): одна пустая строка таблицы с номером next."""
        try:
            i = int(request.args.get("next", ""))
        except ValueError:
            abort(400)
        if not 1 <= i <= entry.MAX_BATCH_ROWS:
            abort(400)
        conn = pages.read_db()
        return render_template(
            "_batch_row.html",
            i=i,
            row=empty_row(),
            status=None,
            species=browse.list_species(conn),
            trawls=browse.event_trawls(conn, event_id),  # нет выезда — 404
            text_fields=BATCH_TEXT,
            checks=BATCH_CHECKS,
            oob=True,
        )
