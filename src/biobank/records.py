"""Правка и удаление уже введённых записей: экспедиции, места, выезды, траления, вода,
особи, образцы, отправки, прилов, определения вида.

База — источник правды (решённый вопрос 8), поэтому править и удалять можно и записи,
пришедшие из Excel. Спорные значения сами по себе не исправляются: функции вызываются
только по явному запросу пользователя (форма правки на сайте). Исправленная опечатка
закрывает свою проблему в журнале автоматически — run_checks в той же транзакции.

Правила те же, что у ввода (entry.py):
- проверка и запись — в одной транзакции BEGIN IMMEDIATE (edit.write_transaction);
  резервная копия — apply_edit(db_path, lambda conn: update_…(conn, …));
- жёсткие ошибки — EditError (в базу ничего не записано), нет записи — NotFoundError;
- функции ничего не печатают и возвращают EditResult (record_id — id изменённой записи).

ПРАВКА: update_<запись>(conn, id, поле=значение, …) — меняются только переданные поля;
None или пустая строка — стереть значение (у обязательных полей — EditError);
имена полей — колонки таблицы (кроме ссылок: trawl_no вместо trawling_id и т.п.),
список — в <ЗАПИСЬ>_FIELDS. Проверки — те же, что при создании.

УДАЛЕНИЕ: delete_<запись>(conn, id). Если от записи зависят другие (у выезда — особи,
траления…) — EditError со списком «что мешает: 12 особей, 3 траления», ничего не удаляется.
Вместе с записью удаляется только то, что без неё бессмысленно и вводится вместе с ней:
- у траления — его замеры воды «старт»/«финиш»;
- у особи — история определений вида (первая запись создаётся вместе с особью);
- у образца — отметки об отправке в лаборатории (add_sample(labs=…) создаёт их вместе).
Результаты анализов (analysis) так не удаляются никогда: образец с анализом удалить нельзя.

ЖУРНАЛ при удалении: записи data_issue об удалённой записи (table_name + record_id, у особи —
и по номеру пробы в object_label, если record_id не записан) не теряются и не удаляются:
открытые закрываются с пометкой «Запись удалена <дата>: <что>», у решённых пометка
дописывается к resolution. record_id стирается (NULL): SQLite может выдать тот же id новой
записи, и та получила бы чужую историю. Что именно было — в описании, object_label и пометке.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable

from biobank.checks import suffix_mismatch
from biobank.edit import (
    EditError,
    EditResult,
    NotFoundError,
    event_title,
    write_transaction,
)
from biobank.entry import (
    MEASUREMENTS,
    POINT_TYPES,
    SAMPLE_TYPES,
    SEXES,
    SITE_TYPES,
    WATER_NUMBERS,
    _city_id,
    _done,
    _event,
    _expedition,
    _extend_expedition_dates,
    _gear_id,
    _method_id,
    _numbering_warnings,
    _row_flag,
    _row_int,
    _site,
    _title,
    _trawling,
    clean_text,
    find_nearby_sites,
    normalize_gear_size,
    parse_coord_source,
    parse_entry_date,
    parse_entry_number,
    parse_entry_time,
)
from biobank.importer import SITE_TYPE
from biobank.parsing import SUFFIXES, parse_label

# Поля, которые можно менять: имя → как называть в сообщениях
EXPEDITION_FIELDS = {
    "name": "название", "date_start": "дата начала", "date_end": "дата окончания",
    "notes": "примечания",
}  # fmt: skip
SITE_FIELDS = {
    "name": "название", "site_type": "тип", "lat": "широта", "lon": "долгота",
    "city": "город", "region": "провинция", "description": "описание",
}  # fmt: skip
EVENT_FIELDS = {
    "expedition_id": "экспедиция", "site_id": "место", "event_date": "дата",
    "capture_method": "способ получения", "gear_mesh": "ячея сети", "gear_size": "размер сети",
    "notes": "примечания",
}  # fmt: skip
TRAWLING_FIELDS = {
    "trawl_no": "номер", "time_start": "старт", "time_end": "финиш",
    "duration_min": "длительность", "speed_kmh": "скорость",
    "intermediate_depth_m": "промежуточная глубина", "catch_note": "улов", "notes": "примечания",
}  # fmt: skip
WATER_FIELDS = {
    "point_type": "точка", "trawl_no": "траление", "measured_time": "время",
    "coord_source": "источник координат", "garmin_wp": "точка Garmin", "notes": "примечания",
    **{column: what for column, (what, _, _) in WATER_NUMBERS.items()},
}  # fmt: skip
SPECIMEN_FIELDS = {
    "label": "номер пробы", "event_id": "выезд", "trawl_no": "траление", "sex": "пол",
    "is_dead": "мёртвая", "notes": "примечания", "capture_method": "свой способ получения",
    "capture_site_id": "своё место поимки", "capture_date": "своя дата поимки",
}  # fmt: skip
SAMPLE_FIELDS = {
    "sample_type": "тип", "organ": "орган", "label": "метка", "preservative": "фиксация",
    "notes": "примечания",
}  # fmt: skip
CATCH_FIELDS = {
    "raw_text": "запись", "taxon_text": "таксон", "count_n": "число", "mass_g": "масса",
    "trawl_no": "траление",
}  # fmt: skip


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------


def _check_fields(changes: dict, allowed: dict[str, str]) -> None:
    """Только известные поля и хотя бы одно изменение."""
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise EditError(
            f"Неизвестные поля: {', '.join(unknown)}. Можно менять: {', '.join(allowed)}."
        )
    if not changes:
        raise EditError("Укажите, что изменить.")


def _required(value, what: str) -> str:
    text = clean_text(value)
    if text is None:
        raise EditError(f"{what} — обязательное поле, пустым быть не может.")
    return text


def _show(value) -> str:
    """Значение для сообщения «было → стало».

    >>> _show(None), _show(29.0), _show("печень"), _show(3)
    ('—', '29', '«печень»', '3')
    """
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        return f"«{value}»"
    return str(value)


def _get(conn: sqlite3.Connection, sql: str, record_id, message: str) -> sqlite3.Row:
    """Запись по id или NotFoundError с текстом message."""
    row = conn.execute(sql, (record_id,)).fetchone()
    if row is None:
        raise NotFoundError(message)
    return row


def _apply(conn: sqlite3.Connection, table: str, key: str, record_id: int, old, new: dict) -> dict:
    """UPDATE только тех колонок, значение которых правда меняется; вернуть их.

    Имена таблицы и колонок — из кода этого модуля (не от пользователя), значения — через «?».
    """
    changed = {column: value for column, value in new.items() if old[column] != value}
    if changed:
        assignments = ", ".join(f"{column} = ?" for column in changed)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {key} = ?", (*changed.values(), record_id)
        )
    return changed


def _describe(names: dict[str, str], old, changed: dict, shown: dict | None = None) -> list[str]:
    """«поле: было → стало» по изменённым колонкам.

    shown — как показывать колонку-ссылку: колонка → (было, стало) текстом (город, а не city_id).
    """
    shown = shown or {}
    lines = []
    for column, value in changed.items():
        was, now = shown.get(column, (_show(old[column]), _show(value)))
        lines.append(f"{names.get(column, column)} {was} → {now}")
    return lines


def _finish(
    conn: sqlite3.Connection, title: str, lines: list[str], record_id: int,
    warnings: list[str] | None = None,
) -> EditResult:  # fmt: skip
    what = ", ".join(lines) if lines else "ничего не изменилось"
    return _done(conn, f"{title}: {what}.", record_id, warnings)


def _id(value, what: str) -> int:
    """id записи из формы (число или строка цифр); пусто — EditError (ссылка обязательна)."""
    record_id = _row_int(value, what)
    if record_id is None:
        raise EditError(f"{what} — обязательное поле, пустым быть не может.")
    return record_id


# ---------------------------------------------------------------------------
# Правка
# ---------------------------------------------------------------------------


def update_expedition(conn: sqlite3.Connection, expedition_id: int, **changes) -> EditResult:
    """Изменить экспедицию: name, date_start, date_end, notes (EXPEDITION_FIELDS).

    Название уникально. Даты должны включать даты всех выездов экспедиции
    (при импорте и вводе они так и считаются: min/max дат выездов).
    """
    _check_fields(changes, EXPEDITION_FIELDS)
    new = {}
    if "name" in changes:
        new["name"] = _required(changes["name"], "Название экспедиции")
    if "date_start" in changes:
        new["date_start"] = parse_entry_date(changes["date_start"], "Дата начала")
    if "date_end" in changes:
        new["date_end"] = parse_entry_date(changes["date_end"], "Дата окончания")
    if "notes" in changes:
        new["notes"] = clean_text(changes["notes"])

    with write_transaction(conn):
        old = _get(
            conn, "SELECT * FROM expedition WHERE expedition_id = ?", expedition_id,
            f"Экспедиции №{expedition_id} нет.",
        )  # fmt: skip
        merged = {**dict(old), **new}
        if conn.execute(
            "SELECT 1 FROM expedition WHERE name = ? AND expedition_id != ?",
            (merged["name"], expedition_id),
        ).fetchone():
            raise EditError(f"Экспедиция «{merged['name']}» уже есть — выберите другое название.")
        start, end = merged["date_start"], merged["date_end"]
        if start and end and start > end:
            raise EditError(f"Дата начала {start} позже даты окончания {end}.")
        first, last = conn.execute(
            "SELECT min(event_date), max(event_date) FROM sampling_event WHERE expedition_id = ?",
            (expedition_id,),
        ).fetchone()
        if (start and first and first < start) or (end and last and last > end):
            raise EditError(
                f"Выезды экспедиции — с {first} по {last}: даты экспедиции должны их включать."
            )
        changed = _apply(conn, "expedition", "expedition_id", expedition_id, old, new)
        title = f"Экспедиция №{expedition_id} «{merged['name']}»"
        return _finish(conn, title, _describe(EXPEDITION_FIELDS, old, changed), expedition_id)


_SITE_SQL = """
    SELECT st.*, c.name AS city, r.name AS region
    FROM sampling_site st
    LEFT JOIN city c   ON c.city_id = st.city_id
    LEFT JOIN region r ON r.region_id = c.region_id
    WHERE st.site_id = ?
"""


def _site_type_conflicts(conn: sqlite3.Connection, site_id: int, site_type: str) -> list[str]:
    """Выезды и особи места, способ получения которых не подходит к типу site_type."""
    conflicts = []
    events = conn.execute(
        "SELECT e.event_id, e.event_date, e.source_sheet, m.name FROM sampling_event e "
        "JOIN capture_method m USING (capture_method_id) WHERE e.site_id = ? ORDER BY e.event_id",
        (site_id,),
    ).fetchall()
    for e in events:
        if SITE_TYPE[e["name"]] != site_type:
            title = event_title(e["source_sheet"], e["event_id"], e["event_date"])
            conflicts.append(f"выезд {title} ({e['name']})")
    # особи, пойманные здесь на выезде в другое место: свой способ, иначе способ выезда
    specimens = conn.execute(
        "SELECT s.label, coalesce(cm.name, em.name) AS method FROM specimen s "
        "JOIN sampling_event e USING (event_id) "
        "JOIN capture_method em ON em.capture_method_id = e.capture_method_id "
        "LEFT JOIN capture_method cm ON cm.capture_method_id = s.capture_method_id "
        "WHERE s.capture_site_id = ? ORDER BY s.specimen_id",
        (site_id,),
    ).fetchall()
    for s in specimens:
        if SITE_TYPE[s["method"]] != site_type:
            conflicts.append(f"особь {s['label']} ({s['method']})")
    return conflicts


def update_site(
    conn: sqlite3.Connection,
    site_id: int,
    *,
    allow_nearby: bool = False,
    new_region: bool = False,
    **changes,
) -> EditResult:
    """Изменить место: name, site_type, lat, lon, city, region, description (SITE_FIELDS).

    Город указывается с провинцией (как при создании); если меняется только город —
    провинция прежняя. city=None — убрать город. new_region=True — разрешить новую провинцию.
    Тип места должен подходить ко всем выездам и особям места (траление → участок реки…).
    Если после правки место совпадёт с другим (тот же тип и ближе 200 м или то же
    название) — EditError; allow_nearby=True — это действительно другое место.
    """
    _check_fields(changes, SITE_FIELDS)
    new = {}
    if "name" in changes:
        new["name"] = _required(changes["name"], "Название места")
    if "site_type" in changes:
        if changes["site_type"] not in SITE_TYPES:
            raise EditError(
                f"Тип места «{changes['site_type']}» не подходит. "
                f"Возможные: {', '.join(SITE_TYPES)}."
            )
        new["site_type"] = changes["site_type"]
    if "lat" in changes:
        new["lat"] = parse_entry_number(changes["lat"], "Широта", minimum=-90, maximum=90)
    if "lon" in changes:
        new["lon"] = parse_entry_number(changes["lon"], "Долгота", minimum=-180, maximum=180)
    if "description" in changes:
        new["description"] = clean_text(changes["description"])

    with write_transaction(conn):
        old = _get(conn, _SITE_SQL, site_id, f"Места №{site_id} нет.")
        merged = {**dict(old), **new}
        if (merged["lat"] is None) != (merged["lon"] is None):
            raise EditError("Координаты нужны обе — широта и долгота — или ни одной.")
        shown = {}
        created: list[str] = []
        if "city" in changes or "region" in changes:
            city = changes["city"] if "city" in changes else old["city"]
            if "region" in changes:
                region = changes["region"]
            else:  # прежняя провинция — только если город остаётся указанным
                region = old["region"] if clean_text(city) else None
            new["city_id"], created = _city_id(conn, city, region, new_region)
            city_name = conn.execute(
                "SELECT name FROM city WHERE city_id = ?", (new["city_id"],)
            ).fetchone()
            shown["city_id"] = (_show(old["city"]), _show(city_name[0] if city_name else None))
        if merged["site_type"] != old["site_type"]:
            conflicts = _site_type_conflicts(conn, site_id, merged["site_type"])
            if conflicts:
                raise EditError(
                    f"Тип «{merged['site_type']}» не подходит к способу получения: "
                    f"{'; '.join(conflicts)}."
                )
        warnings = []
        if {"name", "site_type", "lat", "lon"} & set(new):
            nearby = [
                site
                for site in find_nearby_sites(
                    conn, merged["site_type"], merged["lat"], merged["lon"], merged["name"]
                )
                if site.site_id != site_id
            ]
            if nearby and not allow_nearby:
                listed = "; ".join(site.describe() for site in nearby)
                raise EditError(
                    f"Такое место, похоже, уже есть: {listed}. Если это действительно другое "
                    "место — подтвердите правку."
                )
            warnings = [f"Рядом есть место {site.describe()}" for site in nearby]
        changed = _apply(conn, "sampling_site", "site_id", site_id, old, new)
        names = {**SITE_FIELDS, "city_id": "город"}
        lines = _describe(names, old, changed, shown)
        if created:
            lines.append(f"добавлены: {', '.join(created)}")
        return _finish(conn, f"Место №{site_id} «{merged['name']}»", lines, site_id, warnings)


_EVENT_FULL_SQL = """
    SELECT e.*, m.name AS method, g.mesh_cm, g.size_m, x.name AS expedition, st.name AS site
    FROM sampling_event e
    JOIN capture_method m ON m.capture_method_id = e.capture_method_id
    JOIN expedition x     ON x.expedition_id = e.expedition_id
    JOIN sampling_site st ON st.site_id = e.site_id
    LEFT JOIN gear g      ON g.gear_id = e.gear_id
    WHERE e.event_id = ?
"""


def _gear_text(mesh: str | None, size: str | None) -> str:
    return "—" if not (mesh or size) else f"ячея {mesh or '?'} см, сеть {size or '?'} м"


def update_event(conn: sqlite3.Connection, event_id: int, **changes) -> EditResult:
    """Изменить выезд: expedition_id, site_id, event_date, capture_method, gear_mesh,
    gear_size, notes (EVENT_FIELDS). Лист Excel (source_sheet) не меняется — это происхождение.

    Проверки как у create_event: тип места подходит к способу получения, такого же выезда
    ещё нет, сеть — только у тралений. Способ «траление» нельзя сменить, пока у выезда
    есть траления; чтобы сменить на другой способ, сеть убирают в той же правке
    (gear_mesh=None, gear_size=None). Даты экспедиции расширяются до даты выезда;
    даты прежней экспедиции (при переносе) не сужаются — их правят update_expedition.
    """
    _check_fields(changes, EVENT_FIELDS)
    new_date = None
    if "event_date" in changes:
        new_date = parse_entry_date(changes["event_date"], "Дата выезда")
        if new_date is None:
            raise EditError("Дата выезда — обязательное поле, пустым быть не может.")
    if "capture_method" in changes:
        _required(changes["capture_method"], "Способ получения")

    with write_transaction(conn):
        old = _get(conn, _EVENT_FULL_SQL, event_id, f"Выезда №{event_id} нет.")
        title = event_title(old["source_sheet"], event_id, old["event_date"])
        expedition_id = old["expedition_id"]
        if "expedition_id" in changes:
            expedition_id = _id(changes["expedition_id"], "Экспедиция")
        expedition = _expedition(conn, expedition_id)
        site_id = old["site_id"]
        if "site_id" in changes:
            site_id = _id(changes["site_id"], "Место")
        site = _site(conn, site_id)
        method = clean_text(changes.get("capture_method")) or old["method"]
        method_id = _method_id(conn, method)
        date = new_date or old["event_date"]

        if site["site_type"] != SITE_TYPE[method]:
            raise EditError(
                f"Способ «{method}» не подходит к месту №{site_id} «{site['name']}» "
                f"({site['site_type']}): нужно место типа «{SITE_TYPE[method]}»."
            )
        trawls = conn.execute(
            "SELECT count(*) FROM trawling WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        if method != "траление" and trawls:
            raise EditError(
                f"У выезда {title} есть траления ({trawls}) — способ «траление» сменить нельзя. "
                "Сначала удалите траления."
            )
        mesh = normalize_gear_size(changes.get("gear_mesh", old["mesh_cm"]))
        size = normalize_gear_size(changes.get("gear_size", old["size_m"]))
        if (mesh or size) and method != "траление":
            raise EditError(
                f"Сеть указывают только у тралений, а способ получения — «{method}». "
                "Уберите сеть в той же правке."
            )
        same = conn.execute(
            "SELECT event_id FROM sampling_event WHERE expedition_id = ? AND site_id = ? "
            "AND event_date = ? AND capture_method_id = ? AND event_id != ?",
            (expedition_id, site_id, date, method_id, event_id),
        ).fetchone()
        if same:
            raise EditError(
                f"Такой выезд уже есть: №{same['event_id']} ({date}, «{site['name']}»)."
            )
        gear_id = _gear_id(conn, mesh, size) if (mesh or size) else None
        new = dict(
            expedition_id=expedition_id, site_id=site_id, event_date=date,
            capture_method_id=method_id, gear_id=gear_id,
        )  # fmt: skip
        if "notes" in changes:
            new["notes"] = clean_text(changes["notes"])

        warnings = _extend_expedition_dates(conn, expedition, date)
        if date != old["event_date"]:
            n = conn.execute(
                "SELECT count(*) FROM species_identification si JOIN specimen s "
                "USING (specimen_id) WHERE s.event_id = ? AND si.identified_on = ?",
                (event_id, old["event_date"]),
            ).fetchone()[0]
            if n:
                warnings.append(
                    f"У {n} определений вида особей этого выезда дата — прежняя дата выезда "
                    f"({old['event_date']}); они не изменены."
                )
        changed = _apply(conn, "sampling_event", "event_id", event_id, old, new)
        shown = {
            "expedition_id": (_show(old["expedition"]), _show(expedition["name"])),
            "site_id": (_show(old["site"]), _show(site["name"])),
            "capture_method_id": (_show(old["method"]), _show(method)),
            "gear_id": (_gear_text(old["mesh_cm"], old["size_m"]), _gear_text(mesh, size)),
        }
        names = {**EVENT_FIELDS, "capture_method_id": "способ получения", "gear_id": "сеть"}
        lines = _describe(names, old, changed, shown)
        return _finish(conn, f"Выезд {title}", lines, event_id, warnings)


def _trawl_no(value) -> int | None:
    number = _row_int(value, "Номер траления")
    if number is not None and number <= 0:
        raise EditError(f"Номер траления — целое число больше 0 (введено {number}).")
    return number


def update_trawling(conn: sqlite3.Connection, trawling_id: int, **changes) -> EditResult:
    """Изменить траление: trawl_no, time_start, time_end, duration_min, speed_kmh,
    intermediate_depth_m, catch_note, notes (TRAWLING_FIELDS).

    Номер траления уникален в пределах выезда. Выезд траления не меняется
    (к нему привязаны особи и замеры воды) — для этого удалите траление и введите заново.
    """
    _check_fields(changes, TRAWLING_FIELDS)
    new = {}
    if "trawl_no" in changes:
        new["trawl_no"] = _trawl_no(changes["trawl_no"])
        if new["trawl_no"] is None:
            raise EditError("Номер траления — обязательное поле, пустым быть не может.")
    for column, what in (("time_start", "Время старта"), ("time_end", "Время финиша")):
        if column in changes:
            new[column] = parse_entry_time(changes[column], what)
    numbers = {
        "duration_min": ("Длительность", {"positive": True}),
        "speed_kmh": ("Скорость", {"minimum": 0}),
        "intermediate_depth_m": ("Промежуточная глубина", {"minimum": 0}),
    }
    for column, (what, limits) in numbers.items():
        if column in changes:
            new[column] = parse_entry_number(changes[column], what, **limits)
    for column in ("catch_note", "notes"):
        if column in changes:
            new[column] = clean_text(changes[column])

    with write_transaction(conn):
        old = _get(
            conn, "SELECT * FROM trawling WHERE trawling_id = ?", trawling_id,
            f"Траления №{trawling_id} нет.",
        )  # fmt: skip
        event = _event(conn, old["event_id"])
        if (
            "trawl_no" in new
            and conn.execute(
                "SELECT 1 FROM trawling WHERE event_id = ? AND trawl_no = ? AND trawling_id != ?",
                (old["event_id"], new["trawl_no"], trawling_id),
            ).fetchone()
        ):
            raise EditError(f"Траление {new['trawl_no']} на выезде {_title(event)} уже есть.")
        changed = _apply(conn, "trawling", "trawling_id", trawling_id, old, new)
        title = f"Траление {new.get('trawl_no', old['trawl_no'])} выезда {_title(event)}"
        return _finish(conn, title, _describe(TRAWLING_FIELDS, old, changed), trawling_id)


def update_water_measurement(
    conn: sqlite3.Connection, measurement_id: int, **changes
) -> EditResult:
    """Изменить замер воды: point_type, trawl_no, measured_time, coord_source, garmin_wp,
    notes и числа WATER_NUMBERS (lat, lon, depth_m, o2_pct, ppm, …) — WATER_FIELDS.

    trawl_no — траление того же выезда; у точки «точка» траления нет, у «старт»/«финиш» —
    обязательно. На одно траление — один «старт» и один «финиш».
    """
    _check_fields(changes, WATER_FIELDS)
    new = {}
    if "point_type" in changes:
        if changes["point_type"] not in POINT_TYPES:
            raise EditError(
                f"Тип точки «{changes['point_type']}» не подходит. "
                f"Возможные: {', '.join(POINT_TYPES)}."
            )
        new["point_type"] = changes["point_type"]
    if "measured_time" in changes:
        new["measured_time"] = parse_entry_time(changes["measured_time"], "Время замера")
    if "coord_source" in changes:
        new["coord_source"] = parse_coord_source(changes["coord_source"])
    for column in ("garmin_wp", "notes"):
        if column in changes:
            new[column] = clean_text(changes[column])
    for column, (what, low, high) in WATER_NUMBERS.items():
        if column in changes:
            new[column] = parse_entry_number(changes[column], what, minimum=low, maximum=high)
    trawl_no = _trawl_no(changes["trawl_no"]) if "trawl_no" in changes else None

    with write_transaction(conn):
        old = _get(
            conn,
            "SELECT w.*, t.trawl_no FROM water_measurement w "
            "LEFT JOIN trawling t ON t.trawling_id = w.trawling_id WHERE w.measurement_id = ?",
            measurement_id,
            f"Замера воды №{measurement_id} нет.",
        )
        event = _event(conn, old["event_id"])
        merged = {**dict(old), **new}
        if "trawl_no" not in changes:
            trawl_no = old["trawl_no"]
        point = merged["point_type"]
        if point == "точка" and trawl_no is not None:
            raise EditError(
                "Тип «точка» — замер не при тралении: уберите номер траления в той же правке."
            )
        if point != "точка" and trawl_no is None:
            raise EditError(f"Для точки «{point}» укажите номер траления.")
        trawling_id = None if trawl_no is None else _trawling(conn, event, trawl_no)
        if (
            trawling_id is not None
            and conn.execute(
                "SELECT 1 FROM water_measurement WHERE trawling_id = ? AND point_type = ? "
                "AND measurement_id != ?",
                (trawling_id, point, measurement_id),
            ).fetchone()
        ):
            raise EditError(f"Замер «{point}» траления {trawl_no} уже есть.")
        if (merged["lat"] is None) != (merged["lon"] is None):
            raise EditError("Координаты нужны обе — широта и долгота — или ни одной.")
        new["trawling_id"] = trawling_id
        changed = _apply(conn, "water_measurement", "measurement_id", measurement_id, old, new)
        shown = {"trawling_id": (_show(old["trawl_no"]), _show(trawl_no))}
        names = {**WATER_FIELDS, "trawling_id": "траление"}
        lines = _describe(names, old, changed, shown)
        title = f"Замер воды №{measurement_id} (выезд {_title(event)})"
        return _finish(conn, title, lines, measurement_id)


_SPECIMEN_FULL_SQL = """
    SELECT s.*, t.trawl_no, sp.code AS species_code, em.name AS event_method,
           cm.name AS own_method, cs.name AS own_site
    FROM specimen s
    JOIN sampling_event e       ON e.event_id = s.event_id
    JOIN capture_method em      ON em.capture_method_id = e.capture_method_id
    LEFT JOIN trawling t        ON t.trawling_id = s.trawling_id
    LEFT JOIN species sp        ON sp.species_id = s.species_id
    LEFT JOIN capture_method cm ON cm.capture_method_id = s.capture_method_id
    LEFT JOIN sampling_site cs  ON cs.site_id = s.capture_site_id
    WHERE s.specimen_id = ?
"""


def _relabel(conn: sqlite3.Connection, old: sqlite3.Row, text, warnings: list[str]) -> dict:
    """Новый номер пробы: распознать, проверить, что свободен; мягкие проблемы — в warnings."""
    parsed = parse_label(clean_text(text) or "")
    if parsed is None:
        known = ", ".join(s for s, _ in SUFFIXES.values() if s)
        raise EditError(
            f"Номер пробы «{text}» не распознан. Пишите «число суффикс», например "
            f"«179 pc»; суффиксы: {known}; у Pangasius можно без суффикса."
        )
    other = conn.execute(
        "SELECT specimen_id FROM specimen WHERE label = ? AND specimen_id != ?",
        (parsed.label, old["specimen_id"]),
    ).fetchone()
    if other:
        raise EditError(f"Номер «{parsed.label}» уже занят другой особью.")
    if parsed.label == old["label"]:
        return {}
    if suffix_mismatch(parsed.suffix, old["species_code"]):
        warnings.append(
            f"Суффикс в номере пробы не соответствует виду: «{parsed.suffix}», "
            f"а код вида — «{old['species_code'] or 'вид не определён'}»"
        )
    warnings += _numbering_warnings(
        conn, parsed.number, parsed.series, parsed.label, exclude_id=old["specimen_id"]
    )
    # записи журнала, которые знают особь только по номеру (без record_id), — не переписываем
    by_label = conn.execute(
        "SELECT count(*) FROM data_issue WHERE NOT (table_name = 'specimen' AND record_id = ?) "
        "AND (object_label = ? OR instr(', ' || object_label || ', ', ', ' || ? || ', ') > 0)",
        (old["specimen_id"], old["label"], old["label"]),
    ).fetchone()[0]
    if by_label:
        warnings.append(
            f"В журнале проблем {by_label} записей с прежним номером «{old['label']}» без "
            "ссылки на особь — они остаются под прежним номером."
        )
    samples = conn.execute(
        "SELECT count(*) FROM sample WHERE specimen_id = ? AND label LIKE ? || '%'",
        (old["specimen_id"], old["label"]),
    ).fetchone()[0]
    if samples:
        warnings.append(
            f"У {samples} образцов метка с прежним номером — метки образцов (надписи на "
            "пробирках) не меняются."
        )
    return dict(label=parsed.label, series=parsed.series, series_no=parsed.number)


def update_specimen(conn: sqlite3.Connection, specimen_id: int, **changes) -> EditResult:
    """Изменить особь (кроме промеров и вида): label, event_id, trawl_no, sex, is_dead, notes,
    capture_method, capture_site_id, capture_date (SPECIMEN_FIELDS).

    Промеры — entry.update_measurements, вид — edit.identify (история определений).
    label — новый номер пробы: свободен, серия и номер в серии пересчитываются; мягкие
    проблемы (суффикс не по виду, пропуск в серии) — в warnings и журнал.
    event_id — перенос в другой выезд: траление должно быть того же выезда — укажите
    trawl_no нового выезда или trawl_no=None в той же правке.
    capture_method / capture_site_id / capture_date — только если отличаются от выезда
    (совпадающие с выездом не записываются, как при вводе). trawl_no=None — траление неизвестно.
    """
    measured = sorted(set(changes) & set(MEASUREMENTS))
    if measured:
        raise EditError(
            f"Промеры ({', '.join(measured)}) меняет update_measurements — там свои проверки."
        )
    if "species" in changes or "species_id" in changes:
        raise EditError("Вид меняют новым определением (identify) — так сохраняется история.")
    _check_fields(changes, SPECIMEN_FIELDS)
    if "sex" in changes and clean_text(changes["sex"]) not in (None, *SEXES):
        raise EditError(f"Пол «{changes['sex']}» не подходит. Возможные: {', '.join(SEXES)}.")
    own_date = parse_entry_date(changes.get("capture_date"), "Дата поимки")

    with write_transaction(conn):
        old = _get(conn, _SPECIMEN_FULL_SQL, specimen_id, f"Особи №{specimen_id} нет.")
        warnings: list[str] = []
        new = {}
        if "label" in changes:
            new.update(_relabel(conn, old, changes["label"], warnings))
        event_id = old["event_id"]
        if "event_id" in changes:
            event_id = _id(changes["event_id"], "Выезд")
        event = _event(conn, event_id)
        moved = event_id != old["event_id"]
        if moved and old["trawling_id"] is not None and "trawl_no" not in changes:
            raise EditError(
                f"Особь привязана к тралению {old['trawl_no']} прежнего выезда: при переносе "
                "укажите траление нового выезда или уберите его в той же правке."
            )
        new["event_id"] = event_id

        method_id = old["capture_method_id"]
        if "capture_method" in changes:
            text = clean_text(changes["capture_method"])
            method_id = None if text is None else _method_id(conn, text)
        if method_id == event["capture_method_id"]:
            method_id = None  # как у выезда — не записываем (правило ввода и импорта)
        new["capture_method_id"] = method_id

        site_id = old["capture_site_id"]
        if "capture_site_id" in changes:
            site_id = _row_int(changes["capture_site_id"], "Место поимки")
            if site_id is not None:
                site_id = _site(conn, site_id)["site_id"]
        new["capture_site_id"] = None if site_id == event["site_id"] else site_id

        capture_date = own_date if "capture_date" in changes else old["capture_date"]
        new["capture_date"] = None if capture_date == event["event_date"] else capture_date

        trawl_no = _trawl_no(changes["trawl_no"]) if "trawl_no" in changes else old["trawl_no"]
        if moved and "trawl_no" not in changes:
            trawl_no = None  # прежнего траления нет (проверено выше)
        trawling_id = None
        if trawl_no is not None:
            if method_id is not None:
                raise EditError(
                    "Особь получена не тралением (свой способ получения) — траление не указывают."
                )
            trawling_id = _trawling(conn, event, trawl_no)
        new["trawling_id"] = trawling_id

        if "sex" in changes:
            new["sex"] = clean_text(changes["sex"])
        if "is_dead" in changes:
            new["is_dead"] = int(_row_flag(changes["is_dead"]))
        if "notes" in changes:
            new["notes"] = clean_text(changes["notes"])
        if moved:
            old_date = _event(conn, old["event_id"])["event_date"]
            same_date = conn.execute(
                "SELECT 1 FROM species_identification WHERE specimen_id = ? AND identified_on = ?",
                (specimen_id, old_date),
            ).fetchone()
            if old_date != event["event_date"] and same_date:
                warnings.append(
                    f"Дата определения вида — дата прежнего выезда ({old_date}); она не изменена."
                )

        changed = _apply(conn, "specimen", "specimen_id", specimen_id, old, new)
        changed.pop("series", None)  # серия и номер в серии — следствие номера пробы
        changed.pop("series_no", None)
        new_method = conn.execute(
            "SELECT name FROM capture_method WHERE capture_method_id = ?", (method_id,)
        ).fetchone()
        new_site = conn.execute(
            "SELECT name FROM sampling_site WHERE site_id = ?", (new["capture_site_id"],)
        ).fetchone()
        shown = {
            "event_id": (f"№{old['event_id']}", _title(event)),
            "trawling_id": (_show(old["trawl_no"]), _show(trawl_no if trawling_id else None)),
            "capture_method_id": (
                _show(old["own_method"]),
                _show(new_method[0] if new_method else None),
            ),
            "capture_site_id": (_show(old["own_site"]), _show(new_site[0] if new_site else None)),
            "is_dead": ("да" if old["is_dead"] else "нет", "да" if new.get("is_dead") else "нет"),
        }
        names = {
            **SPECIMEN_FIELDS, "trawling_id": "траление",
            "capture_method_id": "свой способ получения", "capture_site_id": "своё место поимки",
        }  # fmt: skip
        lines = _describe(names, old, changed, shown)
        label = new.get("label", old["label"])
        return _finish(conn, f"Особь {label}", lines, specimen_id, warnings)


def update_sample(conn: sqlite3.Connection, sample_id: int, **changes) -> EditResult:
    """Изменить образец: sample_type, organ, label (метка), preservative, notes (SAMPLE_FIELDS).

    Пустая метка — номер особи (как при вводе). Метка, которая есть у образца
    ДРУГОЙ особи, — ошибка (перепутали пробирки). Исходный текст Excel (raw_value) не меняется.
    """
    _check_fields(changes, SAMPLE_FIELDS)
    new = {}
    if "sample_type" in changes:
        if changes["sample_type"] not in SAMPLE_TYPES:
            raise EditError(
                f"Тип образца «{changes['sample_type']}» не подходит. "
                f"Возможные: {', '.join(SAMPLE_TYPES)}."
            )
        new["sample_type"] = changes["sample_type"]
    for column in ("organ", "preservative", "notes"):
        if column in changes:
            new[column] = clean_text(changes[column])

    with write_transaction(conn):
        old = _get(
            conn,
            "SELECT sa.*, sp.label AS specimen FROM sample sa "
            "JOIN specimen sp USING (specimen_id) WHERE sa.sample_id = ?",
            sample_id,
            f"Образца №{sample_id} нет.",
        )
        if "label" in changes:
            tag = clean_text(changes["label"]) or old["specimen"]
            other = conn.execute(
                "SELECT sp.label FROM sample sa JOIN specimen sp USING (specimen_id) "
                "WHERE sa.label = ? AND sa.specimen_id != ? LIMIT 1",
                (tag, old["specimen_id"]),
            ).fetchone()
            if other:
                raise EditError(f"Метка образца «{tag}» уже есть у особи {other['label']}.")
            new["label"] = tag
        changed = _apply(conn, "sample", "sample_id", sample_id, old, new)
        title = f"Образец №{sample_id} особи {old['specimen']}"
        return _finish(conn, title, _describe(SAMPLE_FIELDS, old, changed), sample_id)


def update_catch_record(conn: sqlite3.Connection, catch_id: int, **changes) -> EditResult:
    """Изменить запись прилова: raw_text, taxon_text, count_n, mass_g, trawl_no (CATCH_FIELDS).

    raw_text обязателен; trawl_no — траление того же выезда, None — улов всего выезда.
    """
    _check_fields(changes, CATCH_FIELDS)
    new = {}
    if "raw_text" in changes:
        new["raw_text"] = _required(changes["raw_text"], "Запись улова")
    if "taxon_text" in changes:
        new["taxon_text"] = clean_text(changes["taxon_text"])
    if "count_n" in changes:
        count = parse_entry_number(changes["count_n"], "Число особей", minimum=0)
        if count is not None and count != int(count):
            raise EditError(f"Число особей — целое (введено {count:g}).")
        new["count_n"] = None if count is None else int(count)
    if "mass_g" in changes:
        new["mass_g"] = parse_entry_number(changes["mass_g"], "Масса", minimum=0)
    trawl_no = _trawl_no(changes["trawl_no"]) if "trawl_no" in changes else None

    with write_transaction(conn):
        old = _get(
            conn,
            "SELECT c.*, t.trawl_no FROM catch_record c "
            "LEFT JOIN trawling t ON t.trawling_id = c.trawling_id WHERE c.catch_id = ?",
            catch_id,
            f"Записи прилова №{catch_id} нет.",
        )
        event = _event(conn, old["event_id"])
        shown = {}
        if "trawl_no" in changes:
            new["trawling_id"] = None if trawl_no is None else _trawling(conn, event, trawl_no)
            shown["trawling_id"] = (_show(old["trawl_no"]), _show(trawl_no))
        changed = _apply(conn, "catch_record", "catch_id", catch_id, old, new)
        names = {**CATCH_FIELDS, "trawling_id": "траление"}
        lines = _describe(names, old, changed, shown)
        return _finish(conn, f"Прилов №{catch_id} (выезд {_title(event)})", lines, catch_id)


# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """Число с существительным в нужной форме: (1 особь, 2 особи, 5 особей).

    >>> forms = ("особь", "особи", "особей")
    >>> [plural(n, forms) for n in (1, 3, 5, 11, 21, 112)]
    ['1 особь', '3 особи', '5 особей', '11 особей', '21 особь', '112 особей']
    """
    if n % 10 == 1 and n % 100 != 11:
        form = forms[0]
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{n} {form}"


SPECIMENS = ("особь", "особи", "особей")
TRAWLINGS = ("траление", "траления", "тралений")
WATERS = ("замер воды", "замера воды", "замеров воды")
CATCHES = ("запись прилова", "записи прилова", "записей прилова")
SAMPLES = ("образец", "образца", "образцов")
ANALYSES = ("результат анализа", "результата анализа", "результатов анализа")
EVENTS = ("выезд", "выезда", "выездов")
IDENTIFICATIONS = ("определение вида", "определения вида", "определений вида")
SHIPMENTS = ("отправка в лабораторию", "отправки в лаборатории", "отправок в лаборатории")


def _refuse_if_dependents(
    conn: sqlite3.Connection, what: str, record_id: int,
    queries: Iterable[tuple[str, tuple[str, str, str]]],
) -> None:  # fmt: skip
    """EditError «что мешает: …», если хоть один запрос (count с одним «?») не ноль.

    Запросы — фиксированный текст из кода, id — через «?».
    """
    found = []
    for sql, forms in queries:
        n = conn.execute(sql, (record_id,)).fetchone()[0]
        if n:
            found.append(plural(n, forms))
    if found:
        raise EditError(
            f"{what} удалить нельзя — есть зависимые записи: {', '.join(found)}. "
            "Сначала удалите их или перенесите."
        )


def _retire_issues(
    conn: sqlite3.Connection, table: str, record_id: int, what: str, label: str | None = None
) -> int:
    """Записи журнала об удалённой записи: закрыть (решённым — дописать пометку), стереть
    record_id. label — ещё и записи без record_id с этим номером в object_label (особь).
    Вернуть, сколько записей журнала затронуто.
    """
    note = f"запись удалена {dt.date.today().isoformat()} ({what})"
    rows = conn.execute(
        "SELECT issue_id, resolved, resolution FROM data_issue WHERE table_name = ? "
        "AND (record_id = ? OR (record_id IS NULL AND ? IS NOT NULL AND object_label = ?))",
        (table, record_id, label, label),
    ).fetchall()
    for r in rows:
        if r["resolved"]:
            resolution = f"{r['resolution'] or 'решено'}; {note}"
        else:
            resolution = note[0].upper() + note[1:]
        conn.execute(
            "UPDATE data_issue SET resolved = 1, resolution = ?, record_id = NULL "
            "WHERE issue_id = ?",
            (resolution, r["issue_id"]),
        )
    return len(rows)


def _deleted(
    conn: sqlite3.Connection, message: str, record_id: int, retired: int,
    also: list[str] | None = None,
) -> EditResult:  # fmt: skip
    also = [item for item in also or [] if not item.startswith("0 ")]
    if also:
        message += f" Вместе с ним удалены: {', '.join(also)}."
    if retired:
        message += f" Записей журнала проблем об удалённом закрыто: {retired}."
    return _done(conn, message, record_id)


def delete_expedition(conn: sqlite3.Connection, expedition_id: int) -> EditResult:
    """Удалить экспедицию без выездов."""
    with write_transaction(conn):
        old = _expedition(conn, expedition_id)
        what = f"экспедиция «{old['name']}»"
        _refuse_if_dependents(conn, f"Экспедицию «{old['name']}»", expedition_id, [
            ("SELECT count(*) FROM sampling_event WHERE expedition_id = ?", EVENTS),
        ])  # fmt: skip
        conn.execute("DELETE FROM expedition WHERE expedition_id = ?", (expedition_id,))
        retired = _retire_issues(conn, "expedition", expedition_id, what)
        return _deleted(conn, f"Удалена {what}.", expedition_id, retired)


def delete_site(conn: sqlite3.Connection, site_id: int) -> EditResult:
    """Удалить место, на которое нет выездов и особей с этим местом поимки.

    Город и провинция остаются в справочнике.
    """
    with write_transaction(conn):
        old = _site(conn, site_id)
        what = f"место №{site_id} «{old['name']}»"
        _refuse_if_dependents(conn, f"Место «{old['name']}»", site_id, [
            ("SELECT count(*) FROM sampling_event WHERE site_id = ?", EVENTS),
            ("SELECT count(*) FROM specimen WHERE capture_site_id = ?", SPECIMENS),
        ])  # fmt: skip
        conn.execute("DELETE FROM sampling_site WHERE site_id = ?", (site_id,))
        retired = _retire_issues(conn, "sampling_site", site_id, what)
        return _deleted(conn, f"Удалено {what}.", site_id, retired)


def delete_event(conn: sqlite3.Connection, event_id: int) -> EditResult:
    """Удалить выезд без особей, тралений, замеров воды и прилова.

    Даты экспедиции не сужаются (их можно поправить update_expedition).
    """
    with write_transaction(conn):
        event = _event(conn, event_id)
        what = f"выезд {_title(event)}"
        _refuse_if_dependents(conn, f"Выезд {_title(event)}", event_id, [
            ("SELECT count(*) FROM specimen WHERE event_id = ?", SPECIMENS),
            ("SELECT count(*) FROM trawling WHERE event_id = ?", TRAWLINGS),
            # замеры тралений входят в траления; здесь — только точки без траления
            ("SELECT count(*) FROM water_measurement WHERE event_id = ? AND trawling_id IS NULL",
             WATERS),
            ("SELECT count(*) FROM catch_record WHERE event_id = ?", CATCHES),
        ])  # fmt: skip
        conn.execute("DELETE FROM sampling_event WHERE event_id = ?", (event_id,))
        retired = _retire_issues(conn, "sampling_event", event_id, what)
        return _deleted(conn, f"Удалён {what}.", event_id, retired)


def delete_trawling(conn: sqlite3.Connection, trawling_id: int) -> EditResult:
    """Удалить траление вместе с его замерами воды (старт/финиш).

    Нельзя, если к тралению привязаны особи или прилов.
    """
    with write_transaction(conn):
        old = _get(
            conn, "SELECT * FROM trawling WHERE trawling_id = ?", trawling_id,
            f"Траления №{trawling_id} нет.",
        )  # fmt: skip
        event = _event(conn, old["event_id"])
        what = f"траление {old['trawl_no']} выезда {_title(event)}"
        _refuse_if_dependents(conn, f"Траление {old['trawl_no']}", trawling_id, [
            ("SELECT count(*) FROM specimen WHERE trawling_id = ?", SPECIMENS),
            ("SELECT count(*) FROM catch_record WHERE trawling_id = ?", CATCHES),
        ])  # fmt: skip
        waters = conn.execute(
            "SELECT measurement_id, point_type FROM water_measurement WHERE trawling_id = ?",
            (trawling_id,),
        ).fetchall()
        retired = 0
        for w in waters:
            conn.execute(
                "DELETE FROM water_measurement WHERE measurement_id = ?", (w["measurement_id"],)
            )
            retired += _retire_issues(
                conn, "water_measurement", w["measurement_id"], f"замер «{w['point_type']}», {what}"
            )
        conn.execute("DELETE FROM trawling WHERE trawling_id = ?", (trawling_id,))
        retired += _retire_issues(conn, "trawling", trawling_id, what)
        return _deleted(
            conn, f"Удалено {what}.", trawling_id, retired, [plural(len(waters), WATERS)]
        )


def delete_water_measurement(conn: sqlite3.Connection, measurement_id: int) -> EditResult:
    """Удалить замер воды."""
    with write_transaction(conn):
        old = _get(
            conn,
            "SELECT w.event_id, w.point_type, t.trawl_no FROM water_measurement w "
            "LEFT JOIN trawling t ON t.trawling_id = w.trawling_id WHERE w.measurement_id = ?",
            measurement_id,
            f"Замера воды №{measurement_id} нет.",
        )
        event = _event(conn, old["event_id"])
        where = f"траление {old['trawl_no']}, " if old["trawl_no"] is not None else ""
        what = f"замер воды №{measurement_id} ({where}{old['point_type']}, выезд {_title(event)})"
        conn.execute("DELETE FROM water_measurement WHERE measurement_id = ?", (measurement_id,))
        retired = _retire_issues(conn, "water_measurement", measurement_id, what)
        return _deleted(conn, f"Удалён {what}.", measurement_id, retired)


def delete_specimen(conn: sqlite3.Connection, specimen_id: int) -> EditResult:
    """Удалить особь вместе с историей определений вида. Нельзя, если у неё есть образцы.

    Номер пробы освобождается (next_label его не выдаст: он берёт наибольший номер + 1).
    """
    with write_transaction(conn):
        old = _get(
            conn, "SELECT specimen_id, label, event_id FROM specimen WHERE specimen_id = ?",
            specimen_id, f"Особи №{specimen_id} нет.",
        )  # fmt: skip
        what = f"особь {old['label']}"
        _refuse_if_dependents(conn, f"Особь {old['label']}", specimen_id, [
            ("SELECT count(*) FROM sample WHERE specimen_id = ?", SAMPLES),
        ])  # fmt: skip
        idents = [
            r[0]
            for r in conn.execute(
                "SELECT identification_id FROM species_identification WHERE specimen_id = ?",
                (specimen_id,),
            )
        ]
        conn.execute("DELETE FROM species_identification WHERE specimen_id = ?", (specimen_id,))
        conn.execute("DELETE FROM specimen WHERE specimen_id = ?", (specimen_id,))
        retired = _retire_issues(conn, "specimen", specimen_id, what, old["label"])
        for ident in idents:
            retired += _retire_issues(conn, "species_identification", ident, what)
        return _deleted(
            conn, f"Удалена {what}.", specimen_id, retired, [plural(len(idents), IDENTIFICATIONS)]
        )


def delete_sample(conn: sqlite3.Connection, sample_id: int) -> EditResult:
    """Удалить образец вместе с отметками об отправке. Нельзя, если есть результаты анализов."""
    with write_transaction(conn):
        old = _get(
            conn,
            "SELECT sa.label, sa.sample_type, sp.label AS specimen FROM sample sa "
            "JOIN specimen sp USING (specimen_id) WHERE sa.sample_id = ?",
            sample_id,
            f"Образца №{sample_id} нет.",
        )
        what = (
            f"образец №{sample_id} «{old['label']}» ({old['sample_type']}, особь {old['specimen']})"
        )
        _refuse_if_dependents(conn, f"Образец №{sample_id}", sample_id, [
            ("SELECT count(*) FROM analysis WHERE sample_id = ?", ANALYSES),
        ])  # fmt: skip
        shipments = conn.execute(
            "DELETE FROM sample_shipment WHERE sample_id = ?", (sample_id,)
        ).rowcount
        conn.execute("DELETE FROM sample WHERE sample_id = ?", (sample_id,))
        retired = _retire_issues(conn, "sample", sample_id, what)
        return _deleted(conn, f"Удалён {what}.", sample_id, retired, [plural(shipments, SHIPMENTS)])


def delete_shipment(conn: sqlite3.Connection, sample_id: int, lab_id: int) -> EditResult:
    """Удалить отметку «образец отправлен в лабораторию» (sample_shipment).

    У отправки нет своего id (ключ — образец + лаборатория), поэтому записей журнала
    с record_id у неё не бывает — журнал не меняется.
    """
    with write_transaction(conn):
        row = conn.execute(
            "SELECT sa.label, l.name FROM sample_shipment sh "
            "JOIN sample sa ON sa.sample_id = sh.sample_id JOIN lab l ON l.lab_id = sh.lab_id "
            "WHERE sh.sample_id = ? AND sh.lab_id = ?",
            (sample_id, lab_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Отправки образца №{sample_id} в лабораторию №{lab_id} нет.")
        conn.execute(
            "DELETE FROM sample_shipment WHERE sample_id = ? AND lab_id = ?", (sample_id, lab_id)
        )
        message = f"Удалена отправка образца №{sample_id} «{row['label']}» в «{row['name']}»."
        return _done(conn, message, sample_id)


def delete_catch_record(conn: sqlite3.Connection, catch_id: int) -> EditResult:
    """Удалить запись прилова."""
    with write_transaction(conn):
        old = _get(
            conn, "SELECT event_id, raw_text FROM catch_record WHERE catch_id = ?", catch_id,
            f"Записи прилова №{catch_id} нет.",
        )  # fmt: skip
        event = _event(conn, old["event_id"])
        what = f"прилов №{catch_id} «{old['raw_text']}» (выезд {_title(event)})"
        conn.execute("DELETE FROM catch_record WHERE catch_id = ?", (catch_id,))
        retired = _retire_issues(conn, "catch_record", catch_id, what)
        return _deleted(conn, f"Удалён {what}.", catch_id, retired)


def delete_identification(conn: sqlite3.Connection, identification_id: int) -> EditResult:
    """Удалить запись из истории определений вида (например, ошибочно добавленную).

    Последнюю оставшуюся запись удалить нельзя: у каждой особи есть хотя бы одно
    определение (правило импорта и ввода). Текущий вид особи (specimen.species_id)
    после удаления — вид самого нового оставшегося определения: так же, как identify
    делает текущим последнее добавленное.
    """
    with write_transaction(conn):
        old = _get(
            conn,
            "SELECT si.specimen_id, si.method, si.confidence, s.label, s.species_id AS current, "
            "coalesce(sp.scientific_name, '(не определён)') AS species "
            "FROM species_identification si JOIN specimen s USING (specimen_id) "
            "LEFT JOIN species sp ON sp.species_id = si.species_id "
            "WHERE si.identification_id = ?",
            identification_id,
            f"Определения вида №{identification_id} нет.",
        )
        left = conn.execute(
            "SELECT identification_id, species_id FROM species_identification "
            "WHERE specimen_id = ? AND identification_id != ? ORDER BY identification_id DESC",
            (old["specimen_id"], identification_id),
        ).fetchall()
        if not left:
            raise EditError(
                f"Это единственное определение вида особи {old['label']} — удалить нельзя. "
                "Чтобы исправить вид, добавьте новое определение вида."
            )
        what = (
            f"определение вида особи {old['label']}: {old['species']} "
            f"({old['method']}, {old['confidence']})"
        )
        conn.execute(
            "DELETE FROM species_identification WHERE identification_id = ?", (identification_id,)
        )
        current = left[0]["species_id"]  # самое новое из оставшихся
        message = f"Удалено {what}."
        if current != old["current"]:
            conn.execute(
                "UPDATE specimen SET species_id = ? WHERE specimen_id = ?",
                (current, old["specimen_id"]),
            )
            name = conn.execute(
                "SELECT scientific_name FROM species WHERE species_id = ?", (current,)
            ).fetchone()
            message += f" Текущий вид особи теперь: {name[0] if name else '(не определён)'}."
        retired = _retire_issues(conn, "species_identification", identification_id, what)
        return _deleted(conn, message, identification_id, retired)
