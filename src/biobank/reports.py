"""Стандартные отчёты и печать таблиц в терминале.

Каждый отчёт — это SQL-запрос. Их удобно читать как учебные примеры:
JOIN (соединение таблиц по ключу), GROUP BY (группировка), агрегаты count/avg.

Сводные запросы (SUMMARY_QUERIES) используются и в выгрузке (export.py).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

# ---------------------------------------------------------------------------
# Сводные запросы: «плоские» таблицы, удобные для просмотра в Excel
# ---------------------------------------------------------------------------

# Способ и место поимки особи: свои, если записаны, иначе — как у выезда.
# coalesce(a, b) возвращает первое не-NULL значение.
SPECIMEN_SUMMARY = """
SELECT s.label                         AS "№ пробы",
       x.name                          AS "экспедиция",
       e.event_date                    AS "дата",
       e.source_sheet                  AS "лист",
       coalesce(cm.name, em.name)      AS "способ получения",
       coalesce(cs.name, es.name)      AS "место",
       t.trawl_no                      AS "траление",
       sp.scientific_name              AS "вид",
       si.confidence                   AS "уверенность",
       s.tl_cm                         AS "TL, см",
       s.sl_cm                         AS "SL, см",
       s.mass_g                        AS "m, г",
       s.mass_gutted_g                 AS "m без внутр., г",
       s.sex                           AS "пол",
       CASE s.is_dead WHEN 1 THEN 'да' END AS "мёртвая",
       (SELECT group_concat(sample_type || coalesce(' (' || organ || ')', ''), '; ')
          FROM sample sa WHERE sa.specimen_id = s.specimen_id) AS "образцы",
       s.notes                         AS "примечания"
FROM specimen s
JOIN sampling_event e      ON e.event_id = s.event_id
JOIN expedition x          ON x.expedition_id = e.expedition_id
JOIN capture_method em     ON em.capture_method_id = e.capture_method_id
JOIN sampling_site es      ON es.site_id = e.site_id
LEFT JOIN capture_method cm ON cm.capture_method_id = s.capture_method_id
LEFT JOIN sampling_site cs ON cs.site_id = s.capture_site_id
LEFT JOIN trawling t       ON t.trawling_id = s.trawling_id
LEFT JOIN species sp       ON sp.species_id = s.species_id
LEFT JOIN species_identification si ON si.identification_id = (
    SELECT max(identification_id) FROM species_identification
    WHERE specimen_id = s.specimen_id)
ORDER BY e.event_date, s.specimen_id
"""

SAMPLE_SUMMARY = """
SELECT sa.label             AS "метка образца",
       sa.sample_type       AS "тип",
       sa.organ             AS "орган",
       sa.preservative      AS "фиксация",
       s.label              AS "особь",
       sp.scientific_name   AS "вид",
       e.event_date         AS "дата",
       e.source_sheet       AS "лист",
       (SELECT group_concat(l.name, '; ') FROM sample_shipment sh
          JOIN lab l ON l.lab_id = sh.lab_id WHERE sh.sample_id = sa.sample_id) AS "отправлен",
       sa.raw_value         AS "в таблице записано",
       sa.notes             AS "примечания"
FROM sample sa
JOIN specimen s       ON s.specimen_id = sa.specimen_id
JOIN sampling_event e ON e.event_id = s.event_id
LEFT JOIN species sp  ON sp.species_id = s.species_id
ORDER BY e.event_date, s.specimen_id, sa.sample_id
"""

WATER_SUMMARY = """
SELECT e.event_date      AS "дата",
       e.source_sheet    AS "лист",
       st.name           AS "место",
       t.trawl_no        AS "траление",
       w.point_type      AS "точка",
       w.measured_time   AS "время",
       w.lat             AS "широта",
       w.lon             AS "долгота",
       w.coord_source    AS "координаты по",
       w.depth_m         AS "глубина, м",
       w.o2_pct          AS "O2, %",
       w.ppm             AS "ppm",
       w.conductivity_ms AS "mS",
       w.conductivity_us AS "uS",
       w.ph              AS "pH",
       w.water_temp_c    AS "t воды, °C",
       w.notes           AS "примечания"
FROM water_measurement w
JOIN sampling_event e  ON e.event_id = w.event_id
JOIN sampling_site st  ON st.site_id = e.site_id
LEFT JOIN trawling t   ON t.trawling_id = w.trawling_id
ORDER BY e.event_date, t.trawl_no, w.point_type DESC
"""

SUMMARY_QUERIES = {
    "Особи — сводно": SPECIMEN_SUMMARY,
    "Образцы — сводно": SAMPLE_SUMMARY,
    "Вода — сводно": WATER_SUMMARY,
}

# ---------------------------------------------------------------------------
# Отчёты для команды `report`
# ---------------------------------------------------------------------------

REPORT_SPECIES = """
SELECT coalesce(sp.scientific_name, '(не определён)') AS "вид",
       x.name                  AS "экспедиция",
       count(*)                AS "особей",
       round(avg(s.tl_cm), 1)  AS "ср. TL, см",
       round(avg(s.mass_g), 1) AS "ср. m, г"
FROM specimen s
JOIN sampling_event e ON e.event_id = s.event_id
JOIN expedition x     ON x.expedition_id = e.expedition_id
LEFT JOIN species sp  ON sp.species_id = s.species_id
GROUP BY sp.scientific_name, x.expedition_id
ORDER BY sp.scientific_name IS NULL, sp.scientific_name, x.date_start
"""

REPORT_SPECIES_TOTAL = """
SELECT coalesce(sp.scientific_name, '(не определён)') AS "вид",
       count(*) AS "особей всего"
FROM specimen s
LEFT JOIN species sp ON sp.species_id = s.species_id
GROUP BY sp.scientific_name
ORDER BY count(*) DESC
"""

REPORT_SAMPLES = """
SELECT sa.sample_type           AS "тип",
       coalesce(sa.organ, '')   AS "орган",
       count(*)                 AS "образцов",
       count(DISTINCT sa.specimen_id) AS "особей",
       sum(EXISTS (SELECT 1 FROM sample_shipment sh WHERE sh.sample_id = sa.sample_id))
                                AS "отправлено"
FROM sample sa
GROUP BY sa.sample_type, sa.organ
ORDER BY sa.sample_type, count(*) DESC
"""

REPORT_SAMPLES_BY_LAB = """
SELECT l.name AS "лаборатория", sa.sample_type AS "тип", count(*) AS "образцов"
FROM sample_shipment sh
JOIN lab l     ON l.lab_id = sh.lab_id
JOIN sample sa ON sa.sample_id = sh.sample_id
GROUP BY l.name, sa.sample_type
ORDER BY l.lab_id, sa.sample_type
"""

REPORT_WATER = """
SELECT e.event_date            AS "дата",
       e.source_sheet          AS "лист",
       count(DISTINCT t.trawling_id) AS "тралений",
       round(avg(w.o2_pct), 1) AS "O2, %",
       round(avg(w.ph), 2)     AS "pH",
       round(avg(w.ppm), 1)    AS "ppm",
       round(avg(w.water_temp_c), 1) AS "t воды",
       round(min(w.depth_m), 1) || '–' || round(max(w.depth_m), 1) AS "глубина, м"
FROM water_measurement w
JOIN sampling_event e ON e.event_id = w.event_id
LEFT JOIN trawling t  ON t.trawling_id = w.trawling_id
GROUP BY e.event_id
ORDER BY e.event_date
"""

REPORT_SITES = """
SELECT st.site_id     AS "id",
       st.site_type   AS "тип",
       st.name        AS "место",
       st.lat         AS "широта",
       st.lon         AS "долгота",
       count(DISTINCT e.event_id) AS "выездов",
       group_concat(DISTINCT e.event_date) AS "даты",
       (SELECT count(*) FROM specimen s
          WHERE s.capture_site_id = st.site_id
             OR (s.capture_site_id IS NULL AND s.event_id IN
                 (SELECT event_id FROM sampling_event WHERE site_id = st.site_id))
       ) AS "особей"
FROM sampling_site st
LEFT JOIN sampling_event e ON e.site_id = st.site_id
GROUP BY st.site_id
ORDER BY st.site_id
"""

REPORT_ISSUES = """
SELECT issue_id      AS "id",
       category      AS "категория",
       sheet_name    AS "лист",
       cell          AS "ячейка",
       object_label  AS "объект",
       raw_value     AS "значение",
       description   AS "описание"
FROM data_issue
WHERE resolved = 0
ORDER BY category, issue_id
"""

# имя отчёта → [(заголовок, запрос), ...]
REPORTS: dict[str, list[tuple[str, str]]] = {
    "species": [
        ("Особи по видам и экспедициям", REPORT_SPECIES),
        ("Итого по видам", REPORT_SPECIES_TOTAL),
    ],
    "samples": [
        ("Образцы по типам и органам", REPORT_SAMPLES),
        ("Отправки по лабораториям", REPORT_SAMPLES_BY_LAB),
    ],
    "water": [("Параметры воды по выездам (средние по точкам старт/финиш)", REPORT_WATER)],
    "sites": [("Места и их посещения", REPORT_SITES)],
    "issues": [("Открытые проблемы данных", REPORT_ISSUES)],
}


# ---------------------------------------------------------------------------
# Печать таблицы в терминале
# ---------------------------------------------------------------------------


def _cell_text(value, max_width: int) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.10g}"  # 10 значащих цифр: координаты не обрезаются
    else:
        text = " ".join(str(value).split())
    return text if len(text) <= max_width else text[: max_width - 1] + "…"


def format_table(columns: Sequence[str], rows: Sequence[Sequence], max_width: int = 50) -> str:
    """Таблица с рамкой из символов, как `sqlite3 -box`.

    >>> print(format_table(["вид", "n"], [("Plotosus canius", 173), ("?", 2)]))
    ┌─────────────────┬─────┐
    │ вид             │   n │
    ├─────────────────┼─────┤
    │ Plotosus canius │ 173 │
    │ ?               │   2 │
    └─────────────────┴─────┘
    """
    texts = [[_cell_text(v, max_width) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in texts:
        widths = [max(w, len(t)) for w, t in zip(widths, row, strict=True)]
    # числа выравниваем вправо, текст — влево
    numeric = [
        all(isinstance(row[i], (int, float)) or row[i] is None for row in rows) and rows
        for i in range(len(columns))
    ]

    def line(cells):
        parts = [
            c.rjust(w) if num else c.ljust(w)
            for c, w, num in zip(cells, widths, numeric, strict=True)
        ]
        return "│ " + " │ ".join(parts) + " │"

    def border(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [border("┌", "┬", "┐"), line(columns), border("├", "┼", "┤")]
    out += [line(row) for row in texts]
    out.append(border("└", "┴", "┘"))
    return "\n".join(out)


def run_query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> tuple[list, list]:
    """Выполнить запрос; вернуть (имена колонок, строки)."""
    cursor = conn.execute(sql, params)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    return columns, cursor.fetchall()


def render_report(conn: sqlite3.Connection, name: str, max_width: int = 50) -> str:
    """Текст отчёта для печати."""
    parts = []
    for title, sql in REPORTS[name]:
        columns, rows = run_query(conn, sql)
        parts.append(f"{title} (строк: {len(rows)})")
        parts.append(format_table(columns, rows, max_width) if rows else "  (пусто)")
    return "\n\n".join(parts)


def connect_read_only(db_path) -> sqlite3.Connection:
    """Подключение только на чтение: любая попытка изменить данные даст ошибку.

    mode=ro — режим SQLite «read only», задаётся в адресе файла (URI).
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
