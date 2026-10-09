"""Просмотр базы для веб-интерфейса: списки с фильтрами и постранично, карточки выезда и места.

Все функции только читают (подходит подключение db.connect_read_only) и возвращают данные
без оформления: словари (dict) для списков и CardSection (tables.py) для карточек.
Как их показать — решает шаблон веб-страницы.

Фильтры собираются из ФИКСИРОВАННЫХ кусков SQL (написаны здесь, в коде) и списка параметров:
значения пользователя попадают в запрос только через «?», никогда — в текст SQL.

Несуществующий id выезда, места, экспедиции — edit.NotFoundError (в вебе 404).
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass

from biobank.card import WATER_SQL
from biobank.checks import summary_by_category
from biobank.edit import CONFIDENCES, METHODS, NotFoundError, event_title
from biobank.entry import COORD_SOURCES, POINT_TYPES, SAMPLE_TYPES, SEXES, SITE_TYPES
from biobank.importer import CAPTURE_METHODS
from biobank.parsing import _LOOKALIKES, parse_label
from biobank.tables import CardSection, query_section

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 500


# ---------------------------------------------------------------------------
# Постраничный вывод
# ---------------------------------------------------------------------------


@dataclass
class Page:
    """Одна страница списка: записи + сколько всего и какая это страница."""

    items: list[dict]
    total: int  # записей всего (по всем страницам, с учётом фильтров)
    page: int  # номер этой страницы, с 1
    per_page: int

    @property
    def pages(self) -> int:
        """Всего страниц (хотя бы одна, даже если записей нет)."""
        return max(1, math.ceil(self.total / self.per_page))


def _paginate(conn: sqlite3.Connection, sql: str, params: list, page: int, per_page: int) -> Page:
    """Выполнить запрос постранично.

    Сначала считаем строки: SELECT count(*) FROM (запрос) — запрос как подзапрос.
    Потом берём нужный кусок: LIMIT — сколько строк, OFFSET — сколько пропустить.
    Номер страницы меньше 1 — первая страница, больше числа страниц — последняя;
    per_page приводится к 1…MAX_PER_PAGE.
    """
    per_page = min(max(int(per_page), 1), MAX_PER_PAGE)
    total = conn.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = min(max(int(page), 1), pages)
    rows = conn.execute(f"{sql} LIMIT ? OFFSET ?", [*params, per_page, (page - 1) * per_page])
    return Page([dict(r) for r in rows], total, page, per_page)


def _where(conditions: list[str]) -> str:
    """Условия (фиксированные куски SQL из кода) через AND; без условий — всегда истина."""
    return " AND ".join(conditions) if conditions else "1"


# ---------------------------------------------------------------------------
# Номера проб
# ---------------------------------------------------------------------------

# Порядок номеров: сначала по сериям (решение пользователя, 2026-10-09) — все Pangasius,
# потом pc, Bg, mr, затем новые серии по алфавиту; особи без серии ('ХХ') — в самом конце.
# Внутри серии — «естественный» порядок: 2 pc раньше 10 pc (а по алфавиту '10 pc' < '2 pc').
# CAST(label AS INTEGER) в SQLite берёт число из начала текста: '10 pc' → 10.
# Номера без числа — в конце: выражение NOT GLOB '[0-9]*' для них 1, для остальных 0.
NATURAL_LABEL_ORDER = (
    "CASE s.series WHEN 'Pangasius' THEN 0 WHEN 'pc' THEN 1 WHEN 'Bg' THEN 2 "
    "WHEN 'mr' THEN 3 ELSE 4 END, s.series IS NULL, s.series, "
    "s.label NOT GLOB '[0-9]*', CAST(s.label AS INTEGER), s.label"
)
# Для подсказок при вводе номера — без серий, только по числу: на «15» сначала все
# номера 15 (15, 15 pc, 15 sp), а не 150, 151… серии Pangasius.
NUMBER_LABEL_ORDER = "s.label NOT GLOB '[0-9]*', CAST(s.label AS INTEGER), s.label"


def normalize_label_prefix(text: str) -> str:
    """Начало номера пробы в том виде, как номера хранятся в базе.

    Полный номер — как при импорте (parse_label); неполный — пробел после числа,
    кириллические «р», «с», «е» → латинские, без точек.

    >>> normalize_label_prefix(" 155pc "), normalize_label_prefix("40 рс")
    ('155 pc', '40 pc')
    >>> normalize_label_prefix("155p"), normalize_label_prefix("3 p.")
    ('155 p', '3 p')
    >>> normalize_label_prefix("107.0"), normalize_label_prefix("ХХ")
    ('107', 'ХХ')
    """
    text = " ".join(str(text).split())
    parsed = parse_label(text)
    if parsed:
        return parsed.label
    match = re.fullmatch(r"(\d+)\s*(\D.*)", text)
    if match:
        suffix = match.group(2).translate(_LOOKALIKES).replace(".", "").strip()
        return f"{match.group(1)} {suffix}".strip()
    return text


# Начало номера совпадает (без учёта регистра латиницы): substr — без LIKE, поэтому
# символы «%» и «_» в запросе пользователя ничего особого не значат.
LABEL_PREFIX_SQL = "lower(substr(s.label, 1, length(?))) = lower(?)"


def suggest_labels(conn: sqlite3.Connection, prefix: str, limit: int = 10) -> list[str]:
    """Номера проб, начинающиеся с prefix, — для автодополнения поиска.

    Порядок по числу (2 pc раньше 10 pc; на «15» — 15, 15 pc, 15 sp раньше 150).
    Пустой prefix — пустой список.
    limit приводится к 1…50.
    """
    prefix = normalize_label_prefix(prefix)
    if not prefix:
        return []
    limit = min(max(int(limit), 1), 50)
    rows = conn.execute(
        f"SELECT s.label FROM specimen s WHERE {LABEL_PREFIX_SQL} "
        f"ORDER BY {NUMBER_LABEL_ORDER} LIMIT ?",
        (prefix, prefix, limit),
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Особи
# ---------------------------------------------------------------------------

# Открытые проблемы самой особи — так же, как в карточке (card.ISSUES_SQL), но без выезда:
# по id записи или по номеру в object_label (в свёрнутых записях — перечень через запятую).
OPEN_ISSUES_OF_SPECIMEN = """(
    SELECT count(*) FROM data_issue d
    WHERE d.resolved = 0
      AND ((d.table_name = 'specimen' AND d.record_id = s.specimen_id)
           OR d.object_label = s.label
           OR instr(', ' || d.object_label || ', ', ', ' || s.label || ', ') > 0)
)"""

# Способ и место поимки: свои у особи, если записаны, иначе — как у выезда (coalesce).
SPECIMENS_SQL = f"""
SELECT s.specimen_id, s.label,
       s.species_id, sp.scientific_name AS species, si.confidence,
       s.event_id, e.event_date, e.source_sheet,
       e.expedition_id, x.name AS expedition,
       coalesce(s.capture_site_id, e.site_id) AS site_id, coalesce(cs.name, es.name) AS site,
       coalesce(cm.name, em.name) AS capture_method,
       s.tl_cm, s.sl_cm, s.mass_g,
       (SELECT count(*) FROM sample sa WHERE sa.specimen_id = s.specimen_id) AS sample_count,
       {OPEN_ISSUES_OF_SPECIMEN} AS open_issue_count
FROM specimen s
JOIN sampling_event e       ON e.event_id = s.event_id
JOIN expedition x           ON x.expedition_id = e.expedition_id
JOIN capture_method em      ON em.capture_method_id = e.capture_method_id
JOIN sampling_site es       ON es.site_id = e.site_id
LEFT JOIN capture_method cm ON cm.capture_method_id = s.capture_method_id
LEFT JOIN sampling_site cs  ON cs.site_id = s.capture_site_id
LEFT JOIN species sp        ON sp.species_id = s.species_id
LEFT JOIN species_identification si ON si.identification_id = (
    SELECT max(identification_id) FROM species_identification
    WHERE specimen_id = s.specimen_id)
WHERE {{where}}
ORDER BY {{order}}
"""

# Варианты сортировки списка особей: имя → фиксированный кусок ORDER BY
SPECIMEN_ORDERS = {
    "label": NATURAL_LABEL_ORDER,
    "date": f"e.event_date, e.event_id, {NATURAL_LABEL_ORDER}",
}


def list_specimens(
    conn: sqlite3.Connection,
    *,
    expedition_id: int | None = None,
    event_id: int | None = None,
    site_id: int | None = None,
    species_id: int | None = None,
    unidentified: bool = False,
    label_prefix: str | None = None,
    has_open_issues: bool = False,
    capture_method: str | None = None,
    order: str = "label",
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page:
    """Особи с фильтрами, постранично.

    Фильтры (все необязательны, объединяются через И):
    - expedition_id, event_id — экспедиция, выезд;
    - site_id — место поимки: своё у особи (capture_site_id), иначе место выезда;
    - species_id — текущий вид; unidentified=True — вид не определён;
    - label_prefix — начало номера пробы ('15', '155pc', '40 рс');
    - has_open_issues=True — есть открытые проблемы в журнале;
    - capture_method — название способа ('траление', 'рыбак', 'рынок', 'аквахозяйство'):
      свой у особи, иначе способ выезда.
    Несуществующий id в фильтре — просто пустой список.

    order: 'label' — по номеру в естественном порядке (2 pc раньше 10 pc),
    'date' — по дате выезда, затем по номеру. Другое значение — ValueError.

    Запись (dict): specimen_id, label, species_id, species, confidence, event_id,
    event_date, source_sheet, event_title, expedition_id, expedition, site_id, site,
    capture_method, tl_cm, sl_cm, mass_g, sample_count, open_issue_count.
    """
    if order not in SPECIMEN_ORDERS:
        raise ValueError(
            f"Сортировка «{order}» не подходит. Возможные: {', '.join(SPECIMEN_ORDERS)}."
        )
    conditions: list[str] = []
    params: list = []
    if expedition_id is not None:
        conditions.append("e.expedition_id = ?")
        params.append(expedition_id)
    if event_id is not None:
        conditions.append("s.event_id = ?")
        params.append(event_id)
    if site_id is not None:
        conditions.append("coalesce(s.capture_site_id, e.site_id) = ?")
        params.append(site_id)
    if species_id is not None:
        conditions.append("s.species_id = ?")
        params.append(species_id)
    if unidentified:
        conditions.append("s.species_id IS NULL")
    if label_prefix and label_prefix.strip():
        prefix = normalize_label_prefix(label_prefix)
        conditions.append(LABEL_PREFIX_SQL)
        params += [prefix, prefix]
    if has_open_issues:
        conditions.append(f"{OPEN_ISSUES_OF_SPECIMEN} > 0")
    if capture_method:
        conditions.append("coalesce(cm.name, em.name) = ?")
        params.append(capture_method.strip())
    sql = SPECIMENS_SQL.format(where=_where(conditions), order=SPECIMEN_ORDERS[order])
    result = _paginate(conn, sql, params, page, per_page)
    for item in result.items:
        item["event_title"] = event_title(
            item["source_sheet"], item["event_id"], item["event_date"]
        )
    return result


# ---------------------------------------------------------------------------
# Справочники для фильтров
# ---------------------------------------------------------------------------


def list_species(conn: sqlite3.Connection) -> list[dict]:
    """Виды из справочника по алфавиту: species_id, scientific_name, family, genus, code,
    specimen_count (особей с этим текущим видом)."""
    rows = conn.execute(
        """
        SELECT sp.species_id, sp.scientific_name, sp.family, sp.genus, sp.code,
               (SELECT count(*) FROM specimen s WHERE s.species_id = sp.species_id)
                   AS specimen_count
        FROM species sp
        ORDER BY sp.scientific_name
        """
    )
    return [dict(r) for r in rows]


def list_capture_methods(conn: sqlite3.Connection) -> list[str]:
    """Названия способов получения (для фильтра capture_method в list_specimens)."""
    rows = conn.execute("SELECT name FROM capture_method ORDER BY capture_method_id")
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Экспедиции и выезды
# ---------------------------------------------------------------------------


def list_expeditions(conn: sqlite3.Connection) -> list[dict]:
    """Экспедиции по дате начала: expedition_id, name, date_start, date_end, notes,
    event_count (выездов), specimen_count (особей)."""
    rows = conn.execute(
        """
        SELECT x.expedition_id, x.name, x.date_start, x.date_end, x.notes,
               (SELECT count(*) FROM sampling_event e
                 WHERE e.expedition_id = x.expedition_id) AS event_count,
               (SELECT count(*) FROM specimen s JOIN sampling_event e USING (event_id)
                 WHERE e.expedition_id = x.expedition_id) AS specimen_count
        FROM expedition x
        ORDER BY x.date_start IS NULL, x.date_start, x.expedition_id
        """
    )
    return [dict(r) for r in rows]


EVENTS_SQL = """
SELECT e.event_id, e.event_date, e.source_sheet, e.notes,
       e.expedition_id, x.name AS expedition,
       e.site_id, st.name AS site, st.site_type,
       cm.name AS capture_method,
       g.mesh_cm AS gear_mesh_cm, g.size_m AS gear_size_m,
       (SELECT count(*) FROM trawling t WHERE t.event_id = e.event_id) AS trawl_count,
       (SELECT count(*) FROM specimen s WHERE s.event_id = e.event_id) AS specimen_count
FROM sampling_event e
JOIN expedition x     ON x.expedition_id = e.expedition_id
JOIN sampling_site st ON st.site_id = e.site_id
JOIN capture_method cm ON cm.capture_method_id = e.capture_method_id
LEFT JOIN gear g      ON g.gear_id = e.gear_id
WHERE {where}
ORDER BY e.event_date, e.event_id
"""


def _require(conn: sqlite3.Connection, sql: str, record_id: int, message: str) -> sqlite3.Row:
    """Запись по id или NotFoundError с текстом message."""
    row = conn.execute(sql, (record_id,)).fetchone()
    if row is None:
        raise NotFoundError(message)
    return row


def list_events(conn: sqlite3.Connection, expedition_id: int | None = None) -> list[dict]:
    """Выезды по дате (все или одной экспедиции).

    Запись (dict): event_id, event_date, source_sheet, title (лист или «№id от даты»),
    notes, expedition_id, expedition, site_id, site, site_type, capture_method,
    gear_mesh_cm, gear_size_m, trawl_count, specimen_count.
    Нет такой экспедиции — NotFoundError.
    """
    params: list = []
    conditions: list[str] = []
    if expedition_id is not None:
        _require(
            conn,
            "SELECT 1 FROM expedition WHERE expedition_id = ?",
            expedition_id,
            f"Экспедиции №{expedition_id} нет.",
        )
        conditions.append("e.expedition_id = ?")
        params.append(expedition_id)
    rows = conn.execute(EVENTS_SQL.format(where=_where(conditions)), params)
    items = [dict(r) for r in rows]
    for item in items:
        item["title"] = event_title(item["source_sheet"], item["event_id"], item["event_date"])
    return items


def _event(conn: sqlite3.Connection, event_id: int) -> dict:
    rows = conn.execute(EVENTS_SQL.format(where="e.event_id = ?"), (event_id,)).fetchall()
    if not rows:
        raise NotFoundError(f"Выезда №{event_id} нет.")
    event = dict(rows[0])
    event["title"] = event_title(event["source_sheet"], event["event_id"], event["event_date"])
    return event


def event_trawls(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    """Траления выезда по номеру (например, для выбора в форме «привязать траление»).

    Запись (dict): trawling_id, trawl_no, time_start, time_end, duration_min, catch_note.
    Пустой список — у выезда нет тралений. Нет такого выезда — NotFoundError.
    """
    _require(
        conn,
        "SELECT 1 FROM sampling_event WHERE event_id = ?",
        event_id,
        f"Выезда №{event_id} нет.",
    )
    rows = conn.execute(
        "SELECT trawling_id, trawl_no, time_start, time_end, duration_min, catch_note "
        "FROM trawling WHERE event_id = ? ORDER BY trawl_no",
        (event_id,),
    )
    return [dict(r) for r in rows]


EVENT_TRAWLS_SQL = """
SELECT t.trawl_no AS "№", t.time_start AS "старт", t.time_end AS "финиш",
       t.duration_min AS "мин", t.speed_kmh AS "км/ч",
       t.intermediate_depth_m AS "промеж. глубина, м",
       (SELECT count(*) FROM specimen s WHERE s.trawling_id = t.trawling_id) AS "особей",
       t.catch_note AS "улов", t.notes AS "примечания",
       t.trawling_id AS _trawling_id
FROM trawling t
WHERE t.event_id = ?
ORDER BY t.trawl_no
"""

EVENT_SPECIMENS_SQL = f"""
SELECT s.label AS "№ пробы", coalesce(sp.scientific_name, '(не определён)') AS "вид",
       s.tl_cm AS "TL, см", s.sl_cm AS "SL, см", s.mass_g AS "m, г",
       s.mass_gutted_g AS "m без внутр., г", t.trawl_no AS "траление",
       cm.name AS "свой способ",
       (SELECT count(*) FROM sample sa WHERE sa.specimen_id = s.specimen_id) AS "образцов",
       s.specimen_id AS _specimen_id, s.species_id AS _species_id,
       s.trawling_id AS _trawling_id, s.capture_site_id AS _site_id
FROM specimen s
LEFT JOIN species sp        ON sp.species_id = s.species_id
LEFT JOIN trawling t        ON t.trawling_id = s.trawling_id
LEFT JOIN capture_method cm ON cm.capture_method_id = s.capture_method_id
WHERE s.event_id = ?
ORDER BY {NATURAL_LABEL_ORDER}
"""

EVENT_CATCH_SQL = """
SELECT t.trawl_no AS "траление", c.taxon_text AS "кто", c.count_n AS "штук",
       c.mass_g AS "масса, г", c.raw_text AS "в таблице записано",
       c.catch_id AS _catch_id, c.trawling_id AS _trawling_id
FROM catch_record c
LEFT JOIN trawling t ON t.trawling_id = c.trawling_id
WHERE c.event_id = ?
ORDER BY t.trawl_no IS NULL, t.trawl_no, c.catch_id
"""

# Проблемы выезда: сам выезд, его особи и траления (по id записи) и всё с его листа Excel.
# У выездов, введённых вручную, листа нет (NULL) — сравнение с NULL никогда не истинно.
EVENT_ISSUES_SQL = """
SELECT issue_id AS "id",
       CASE table_name WHEN 'sampling_event' THEN 'выезд'
                       WHEN 'trawling' THEN 'траление'
                       WHEN 'water_measurement' THEN 'замер воды'
                       WHEN 'sample' THEN 'образец'
                       WHEN 'sample_shipment' THEN 'отправка'
                       WHEN 'specimen' THEN 'особь'
                       ELSE table_name END AS "к чему",
       category AS "категория", cell AS "ячейка", object_label AS "объект",
       raw_value AS "значение", description AS "описание",
       CASE resolved WHEN 1 THEN 'решено: ' || coalesce(resolution, '') ELSE 'открыто' END
           AS "статус",
       issue_id AS _issue_id
FROM data_issue
WHERE (table_name = 'sampling_event' AND record_id = :event_id)
   OR (table_name = 'specimen' AND record_id IN
       (SELECT specimen_id FROM specimen WHERE event_id = :event_id))
   OR (table_name = 'trawling' AND record_id IN
       (SELECT trawling_id FROM trawling WHERE event_id = :event_id))
   OR sheet_name = :sheet
ORDER BY resolved, issue_id
"""


def _coords(lat: float | None, lon: float | None) -> str | None:
    return None if lat is None else f"{lat:.10g}, {lon:.10g}"


def event_card(conn: sqlite3.Connection, event_id: int) -> list[CardSection]:
    """Всё о выезде: сведения, траления, замеры воды, особи, прилов, проблемы.

    Разделы — CardSection; у строк в row_ids id записей для ссылок
    (trawling_id, measurement_id, specimen_id, species_id, site_id, catch_id, issue_id).
    Нет такого выезда — NotFoundError.
    """
    e = _event(conn, event_id)
    site = conn.execute(
        "SELECT lat, lon FROM sampling_site WHERE site_id = ?", (e["site_id"],)
    ).fetchone()
    gear = None
    if e["gear_mesh_cm"] or e["gear_size_m"]:
        gear = f"ячея {e['gear_mesh_cm'] or '?'} см, сеть {e['gear_size_m'] or '?'} м"
    fields = [
        ("выезд", e["title"]),
        ("экспедиция", e["expedition"]),
        ("дата", e["event_date"]),
        ("способ получения", e["capture_method"]),
        ("место", e["site"]),
        ("координаты места", _coords(site["lat"], site["lon"])),
        ("сеть", gear),
        ("тралений", e["trawl_count"]),
        ("особей", e["specimen_count"]),
        ("лист Excel", e["source_sheet"]),
        ("примечания", e["notes"]),
    ]
    links = {
        "выезд": {"event_id": e["event_id"]},
        "экспедиция": {"expedition_id": e["expedition_id"]},
        "место": {"site_id": e["site_id"]},
    }
    main = CardSection(
        "Выезд",
        ["поле", "значение"],
        fields,
        is_fields=True,
        row_ids=[links.get(name, {}) for name, _ in fields],
    )
    eid = (event_id,)
    return [
        main,
        query_section(conn, "Траления", EVENT_TRAWLS_SQL, eid),
        query_section(
            conn, "Замеры воды", WATER_SQL.format(where="w.event_id = ?"), eid, "(замеров нет)"
        ),
        query_section(conn, "Особи", EVENT_SPECIMENS_SQL, eid),
        query_section(conn, "Прилов и общий улов", EVENT_CATCH_SQL, eid),
        query_section(
            conn,
            "Проблемы в журнале (выезд, его особи и траления)",
            EVENT_ISSUES_SQL,
            {"event_id": event_id, "sheet": e["source_sheet"]},
        ),
    ]


# ---------------------------------------------------------------------------
# Места
# ---------------------------------------------------------------------------

# Город и регион — LEFT JOIN: у некоторых мест города нет (не указан на листе).
# Особи места: своё место поимки, а если его нет — место выезда (как в отчёте sites).
SITES_SQL = """
SELECT st.site_id, st.name, st.site_type, st.lat, st.lon, st.description,
       st.city_id, c.name AS city, r.region_id, r.name AS region,
       (SELECT count(*) FROM sampling_event e WHERE e.site_id = st.site_id) AS event_count,
       (SELECT min(event_date) FROM sampling_event e WHERE e.site_id = st.site_id) AS first_date,
       (SELECT max(event_date) FROM sampling_event e WHERE e.site_id = st.site_id) AS last_date,
       (SELECT count(*) FROM specimen s JOIN sampling_event e USING (event_id)
         WHERE coalesce(s.capture_site_id, e.site_id) = st.site_id) AS specimen_count
FROM sampling_site st
LEFT JOIN city c   ON c.city_id = st.city_id
LEFT JOIN region r ON r.region_id = c.region_id
WHERE {where}
ORDER BY st.site_id
"""


def list_sites(conn: sqlite3.Connection) -> list[dict]:
    """Места по id (= порядку появления).

    Запись (dict): site_id, name, site_type, lat, lon, description, city_id, city,
    region_id, region (город и регион могут быть None), event_count, first_date,
    last_date, specimen_count.
    """
    return [dict(r) for r in conn.execute(SITES_SQL.format(where="1"))]


SITE_EVENTS_SQL = """
SELECT e.event_date AS "дата", x.name AS "экспедиция",
       coalesce('«' || e.source_sheet || '»', '№' || e.event_id) AS "выезд",
       cm.name AS "способ получения",
       (SELECT count(*) FROM trawling t WHERE t.event_id = e.event_id) AS "тралений",
       (SELECT count(*) FROM specimen s WHERE s.event_id = e.event_id) AS "особей",
       e.event_id AS _event_id, e.expedition_id AS _expedition_id
FROM sampling_event e
JOIN expedition x      ON x.expedition_id = e.expedition_id
JOIN capture_method cm ON cm.capture_method_id = e.capture_method_id
WHERE e.site_id = ?
ORDER BY e.event_date, e.event_id
"""

# Особи, пойманные здесь, но записанные в выезд с другим местом (163–178 pc и т.п.)
SITE_OTHER_SPECIMENS_SQL = f"""
SELECT s.label AS "№ пробы", coalesce(sp.scientific_name, '(не определён)') AS "вид",
       e.event_date AS "дата",
       coalesce('«' || e.source_sheet || '»', '№' || e.event_id) AS "выезд",
       s.specimen_id AS _specimen_id, s.event_id AS _event_id
FROM specimen s
JOIN sampling_event e ON e.event_id = s.event_id
LEFT JOIN species sp  ON sp.species_id = s.species_id
WHERE s.capture_site_id = ? AND e.site_id != s.capture_site_id
ORDER BY {NATURAL_LABEL_ORDER}
"""

SITE_ISSUES_SQL = """
SELECT issue_id AS "id", category AS "категория", sheet_name AS "лист",
       description AS "описание",
       CASE resolved WHEN 1 THEN 'решено: ' || coalesce(resolution, '') ELSE 'открыто' END
           AS "статус",
       issue_id AS _issue_id
FROM data_issue
WHERE table_name = 'sampling_site' AND record_id = ?
ORDER BY resolved, issue_id
"""


def site_card(conn: sqlite3.Connection, site_id: int) -> list[CardSection]:
    """Место и все его выезды; особи, пойманные здесь на выездах в другие места; проблемы.

    Нет такого места — NotFoundError.
    """
    rows = conn.execute(SITES_SQL.format(where="st.site_id = ?"), (site_id,)).fetchall()
    if not rows:
        raise NotFoundError(f"Места №{site_id} нет.")
    s = rows[0]
    fields = [
        ("название", s["name"]),
        ("тип", s["site_type"]),
        ("город", s["city"]),
        ("регион", s["region"]),
        ("координаты", _coords(s["lat"], s["lon"])),
        ("описание", s["description"]),
        ("выездов", s["event_count"]),
        ("особей", s["specimen_count"]),
    ]
    main = CardSection(
        "Место",
        ["поле", "значение"],
        fields,
        is_fields=True,
        row_ids=[{"site_id": s["site_id"]} if name == "название" else {} for name, _ in fields],
    )
    sid = (site_id,)
    return [
        main,
        query_section(conn, "Выезды на это место", SITE_EVENTS_SQL, sid),
        query_section(
            conn, "Особи, пойманные здесь на выездах в другие места", SITE_OTHER_SPECIMENS_SQL, sid
        ),
        query_section(conn, "Проблемы в журнале (место)", SITE_ISSUES_SQL, sid),
    ]


# ---------------------------------------------------------------------------
# Журнал проблем
# ---------------------------------------------------------------------------


def list_issues(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    resolved: bool | None = None,
    table_name: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page:
    """Записи журнала data_issue с фильтрами, постранично.

    resolved: None — все, False — открытые, True — решённые.
    Порядок: сначала открытые, затем по категории и номеру.
    Запись (dict) — все колонки data_issue: issue_id, category, table_name, record_id,
    sheet_name, cell, object_label, raw_value, description, resolved (0/1), resolution.
    """
    conditions: list[str] = []
    params: list = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if resolved is not None:
        conditions.append("resolved = ?")
        params.append(1 if resolved else 0)
    if table_name:
        conditions.append("table_name = ?")
        params.append(table_name)
    sql = (
        "SELECT issue_id, category, table_name, record_id, sheet_name, cell, object_label, "
        "raw_value, description, resolved, resolution FROM data_issue "
        f"WHERE {_where(conditions)} ORDER BY resolved, category, issue_id"
    )
    return _paginate(conn, sql, params, page, per_page)


def issue_categories(conn: sqlite3.Connection) -> list[dict]:
    """Категории журнала со счётчиками: category, total, open, resolved
    (сначала категории с большим числом открытых)."""
    return [dict(r) for r in summary_by_category(conn)]


def get_issue(conn: sqlite3.Connection, issue_id: int) -> dict:
    """Одна запись журнала — все колонки data_issue (как в list_issues).

    Нет такой записи — NotFoundError.
    """
    row = _require(
        conn,
        "SELECT issue_id, category, table_name, record_id, sheet_name, cell, object_label, "
        "raw_value, description, resolved, resolution FROM data_issue WHERE issue_id = ?",
        issue_id,
        f"Проблемы №{issue_id} нет в журнале.",
    )
    return dict(row)


# ---------------------------------------------------------------------------
# Траление
# ---------------------------------------------------------------------------

TRAWLING_SQL = """
SELECT t.*, e.event_date, e.source_sheet, e.expedition_id, x.name AS expedition
FROM trawling t
JOIN sampling_event e ON e.event_id = t.event_id
JOIN expedition x     ON x.expedition_id = e.expedition_id
WHERE t.trawling_id = ?
"""

TRAWLING_SPECIMENS_SQL = f"""
SELECT s.label AS "№ пробы", coalesce(sp.scientific_name, '(не определён)') AS "вид",
       s.tl_cm AS "TL, см", s.sl_cm AS "SL, см", s.mass_g AS "m, г",
       s.mass_gutted_g AS "m без внутр., г",
       (SELECT count(*) FROM sample sa WHERE sa.specimen_id = s.specimen_id) AS "образцов",
       s.specimen_id AS _specimen_id, s.species_id AS _species_id
FROM specimen s
LEFT JOIN species sp ON sp.species_id = s.species_id
WHERE s.trawling_id = ?
ORDER BY {NATURAL_LABEL_ORDER}
"""

TRAWLING_CATCH_SQL = """
SELECT c.taxon_text AS "кто", c.count_n AS "штук", c.mass_g AS "масса, г",
       c.raw_text AS "в таблице записано", c.catch_id AS _catch_id
FROM catch_record c
WHERE c.trawling_id = ?
ORDER BY c.catch_id
"""

# Проблемы траления: по id самого траления и его замеров воды, а записи импорта
# (у них нет record_id) — по листу и подписи «траление N …» («траление 4 старт»).
TRAWLING_ISSUES_SQL = """
SELECT issue_id AS "id",
       CASE table_name WHEN 'trawling' THEN 'траление'
                       WHEN 'water_measurement' THEN 'замер воды'
                       ELSE table_name END AS "к чему",
       category AS "категория", cell AS "ячейка", object_label AS "объект",
       raw_value AS "значение", description AS "описание",
       CASE resolved WHEN 1 THEN 'решено: ' || coalesce(resolution, '') ELSE 'открыто' END
           AS "статус",
       issue_id AS _issue_id
FROM data_issue
WHERE (table_name = 'trawling' AND record_id = :trawling_id)
   OR (table_name = 'water_measurement' AND record_id IN
       (SELECT measurement_id FROM water_measurement WHERE trawling_id = :trawling_id))
   OR (table_name IN ('trawling', 'water_measurement') AND sheet_name = :sheet
       AND (object_label = :object
            OR substr(object_label, 1, length(:object) + 1) = :object || ' '))
ORDER BY resolved, issue_id
"""


def trawling_card(conn: sqlite3.Connection, trawling_id: int) -> list[CardSection]:
    """Всё о тралении: сведения, замеры воды, привязанные особи, прилов, проблемы.

    Разделы — CardSection; row_ids: event_id, expedition_id (в сведениях),
    measurement_id, specimen_id, species_id, catch_id, issue_id.
    Нет такого траления — NotFoundError.
    """
    t = _require(conn, TRAWLING_SQL, trawling_id, f"Траления №{trawling_id} нет.")
    fields = [
        ("траление", t["trawl_no"]),
        ("выезд", event_title(t["source_sheet"], t["event_id"], t["event_date"])),
        ("экспедиция", t["expedition"]),
        ("дата", t["event_date"]),
        ("старт", t["time_start"]),
        ("финиш", t["time_end"]),
        ("длительность, мин", t["duration_min"]),
        ("скорость, км/ч", t["speed_kmh"]),
        ("промежуточная глубина, м", t["intermediate_depth_m"]),
        ("улов", t["catch_note"]),
        ("примечания", t["notes"]),
    ]
    links = {
        "траление": {"trawling_id": t["trawling_id"]},
        "выезд": {"event_id": t["event_id"]},
        "экспедиция": {"expedition_id": t["expedition_id"]},
    }
    main = CardSection(
        "Траление",
        ["поле", "значение"],
        fields,
        is_fields=True,
        row_ids=[links.get(name, {}) for name, _ in fields],
    )
    tid = (trawling_id,)
    issue_params = {
        "trawling_id": trawling_id,
        "sheet": t["source_sheet"],
        "object": f"траление {t['trawl_no']}",
    }
    return [
        main,
        query_section(
            conn, "Замеры воды", WATER_SQL.format(where="w.trawling_id = ?"), tid, "(замеров нет)"
        ),
        query_section(conn, "Особи из этого траления", TRAWLING_SPECIMENS_SQL, tid),
        query_section(conn, "Прилов", TRAWLING_CATCH_SQL, tid),
        query_section(conn, "Проблемы в журнале (траление и его замеры)", TRAWLING_ISSUES_SQL,
                      issue_params),
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Справочники для полей выбора в формах
# ---------------------------------------------------------------------------


def list_labs(conn: sqlite3.Connection) -> list[dict]:
    """Лаборатории по id: lab_id, name, sample_count (сколько образцов туда отправлено)."""
    rows = conn.execute(
        """
        SELECT l.lab_id, l.name,
               (SELECT count(*) FROM sample_shipment sh WHERE sh.lab_id = l.lab_id)
                   AS sample_count
        FROM lab l
        ORDER BY l.lab_id
        """
    )
    return [dict(r) for r in rows]


def list_gear(conn: sqlite3.Connection) -> list[dict]:
    """Сети по id: gear_id, gear_type, mesh_cm, size_m, event_count (выездов с этой сетью)."""
    rows = conn.execute(
        """
        SELECT g.gear_id, g.gear_type, g.mesh_cm, g.size_m,
               (SELECT count(*) FROM sampling_event e WHERE e.gear_id = g.gear_id) AS event_count
        FROM gear g
        ORDER BY g.gear_id
        """
    )
    return [dict(r) for r in rows]


def list_regions(conn: sqlite3.Connection) -> list[dict]:
    """Провинции по id: region_id, name, country, city_count."""
    rows = conn.execute(
        """
        SELECT r.region_id, r.name, r.country,
               (SELECT count(*) FROM city c WHERE c.region_id = r.region_id) AS city_count
        FROM region r
        ORDER BY r.region_id
        """
    )
    return [dict(r) for r in rows]


def list_cities(conn: sqlite3.Connection, region_id: int | None = None) -> list[dict]:
    """Города (все или одной провинции) по названию: city_id, name, name_latin,
    region_id, region, site_count (мест у города). Нет такой провинции — NotFoundError."""
    conditions: list[str] = []
    params: list = []
    if region_id is not None:
        _require(
            conn,
            "SELECT 1 FROM region WHERE region_id = ?",
            region_id,
            f"Провинции №{region_id} нет.",
        )
        conditions.append("c.region_id = ?")
        params.append(region_id)
    rows = conn.execute(
        f"""
        SELECT c.city_id, c.name, c.name_latin, c.region_id, r.name AS region,
               (SELECT count(*) FROM sampling_site st WHERE st.city_id = c.city_id) AS site_count
        FROM city c JOIN region r ON r.region_id = c.region_id
        WHERE {_where(conditions)}
        ORDER BY c.name, r.name
        """,
        params,
    )
    return [dict(r) for r in rows]


def list_shipments(conn: sqlite3.Connection, sample_id: int) -> list[dict]:
    """Куда отправлен образец: sample_id, lab_id, lab, sent_on (для кнопки «удалить отправку»).

    Нет такого образца — NotFoundError.
    """
    _require(
        conn, "SELECT 1 FROM sample WHERE sample_id = ?", sample_id, f"Образца №{sample_id} нет."
    )
    rows = conn.execute(
        "SELECT sh.sample_id, sh.lab_id, l.name AS lab, sh.sent_on FROM sample_shipment sh "
        "JOIN lab l ON l.lab_id = sh.lab_id WHERE sh.sample_id = ? ORDER BY l.name",
        (sample_id,),
    )
    return [dict(r) for r in rows]


def field_choices() -> dict[str, list[str]]:
    """Допустимые значения полей выбора — из тех же констант, по которым проверяет ввод.

    site_type, point_type, sample_type, sex, coord_source (entry.py);
    method, confidence — определения вида (edit.py); capture_method — четыре способа
    получения (importer.py; список из базы — list_capture_methods).
    """
    return {
        "site_type": list(SITE_TYPES),
        "point_type": list(POINT_TYPES),
        "sample_type": list(SAMPLE_TYPES),
        "sex": list(SEXES),
        "coord_source": list(COORD_SOURCES.values()),
        "method": list(METHODS),
        "confidence": list(CONFIDENCES),
        "capture_method": list(CAPTURE_METHODS),
    }


# ---------------------------------------------------------------------------
# Текущие значения записи — чтобы заполнить форму правки
# ---------------------------------------------------------------------------

# вид записи → (запрос по id, как назвать в сообщении «нет такой записи»).
# Колонки названы так же, как поля функций правки (records.update_…): значения
# можно сразу подставить в форму и отправить обратно.
RECORD_SQL = {
    "expedition": ("SELECT * FROM expedition WHERE expedition_id = ?", "Экспедиции"),
    "site": (
        """SELECT st.*, c.name AS city, r.name AS region FROM sampling_site st
           LEFT JOIN city c ON c.city_id = st.city_id
           LEFT JOIN region r ON r.region_id = c.region_id WHERE st.site_id = ?""",
        "Места",
    ),
    "event": (
        """SELECT e.*, m.name AS capture_method, g.mesh_cm AS gear_mesh, g.size_m AS gear_size
           FROM sampling_event e
           JOIN capture_method m ON m.capture_method_id = e.capture_method_id
           LEFT JOIN gear g ON g.gear_id = e.gear_id WHERE e.event_id = ?""",
        "Выезда",
    ),
    "trawling": ("SELECT * FROM trawling WHERE trawling_id = ?", "Траления"),
    "water_measurement": (
        """SELECT w.*, t.trawl_no FROM water_measurement w
           LEFT JOIN trawling t ON t.trawling_id = w.trawling_id WHERE w.measurement_id = ?""",
        "Замера воды",
    ),
    "specimen": (
        """SELECT s.*, t.trawl_no, m.name AS capture_method FROM specimen s
           LEFT JOIN trawling t ON t.trawling_id = s.trawling_id
           LEFT JOIN capture_method m ON m.capture_method_id = s.capture_method_id
           WHERE s.specimen_id = ?""",
        "Особи",
    ),
    "sample": ("SELECT * FROM sample WHERE sample_id = ?", "Образца"),
    "catch_record": (
        """SELECT c.*, t.trawl_no FROM catch_record c
           LEFT JOIN trawling t ON t.trawling_id = c.trawling_id WHERE c.catch_id = ?""",
        "Записи прилова",
    ),
    "identification": (
        "SELECT * FROM species_identification WHERE identification_id = ?",
        "Определения вида",
    ),
}


def get_record(conn: sqlite3.Connection, kind: str, record_id: int) -> dict:
    """Все колонки одной записи + понятные поля для формы правки.

    kind — вид записи (ключ RECORD_SQL): expedition, site, event, trawling,
    water_measurement, specimen, sample, catch_record, identification.
    Дополнительно к колонкам таблицы: у места — city, region (названия); у выезда —
    capture_method, gear_mesh, gear_size; у замера, особи, прилова — trawl_no; у особи —
    capture_method (свой способ; None — как у выезда).
    Неизвестный kind — ValueError; нет записи — NotFoundError.
    """
    if kind not in RECORD_SQL:
        raise ValueError(f"Вид записи «{kind}» не известен. Возможные: {', '.join(RECORD_SQL)}.")
    sql, what = RECORD_SQL[kind]
    return dict(_require(conn, sql, record_id, f"{what} №{record_id} нет."))
