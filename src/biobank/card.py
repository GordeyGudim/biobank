"""Карточка особи: всё, что известно об одной рыбе, из всех таблиц сразу.

specimen_card() возвращает список разделов (CardSection из tables.py) — данные без
оформления, их сможет показать и будущий веб-интерфейс. render_card() печатает их в терминале.

Разделы: особь (промеры, вид, выезд, место), история определений, образцы и
лаборатории, результаты анализов, вода, проблемы из журнала data_issue.
"""

from __future__ import annotations

import sqlite3

from biobank.edit import find_specimen
from biobank.tables import CardSection, query_section, render_sections

# Основные сведения. coalesce(a, b) — первое не-NULL: свой способ/место особи,
# если записаны, иначе — как у выезда.
SPECIMEN_SQL = """
SELECT s.specimen_id, s.event_id, s.trawling_id,
       s.label, s.label_raw, sp.scientific_name AS species,
       x.name AS expedition, e.source_sheet, e.event_date, s.capture_date,
       em.name AS event_method, cm.name AS own_method,
       coalesce(cs.name, es.name) AS site,
       coalesce(cs.lat, es.lat) AS lat, coalesce(cs.lon, es.lon) AS lon,
       t.trawl_no, s.tl_cm, s.sl_cm, s.mass_g, s.mass_gutted_g,
       s.sex, s.is_dead, s.notes, s.source_row
FROM specimen s
JOIN sampling_event e       ON e.event_id = s.event_id
JOIN expedition x           ON x.expedition_id = e.expedition_id
JOIN capture_method em      ON em.capture_method_id = e.capture_method_id
JOIN sampling_site es       ON es.site_id = e.site_id
LEFT JOIN capture_method cm ON cm.capture_method_id = s.capture_method_id
LEFT JOIN sampling_site cs  ON cs.site_id = s.capture_site_id
LEFT JOIN trawling t        ON t.trawling_id = s.trawling_id
LEFT JOIN species sp        ON sp.species_id = s.species_id
WHERE s.specimen_id = ?
"""

IDENTIFICATIONS_SQL = """
SELECT si.identified_on AS "дата", si.method AS "метод", si.confidence AS "уверенность",
       coalesce(sp.scientific_name, '(не определён)') AS "вид",
       si.identified_by AS "кто", si.raw_text AS "в таблице записано", si.notes AS "примечания"
FROM species_identification si
LEFT JOIN species sp ON sp.species_id = si.species_id
WHERE si.specimen_id = ?
ORDER BY si.identification_id
"""

# GROUP BY sample_id + group_concat: один образец мог уйти в несколько лабораторий,
# склеиваем их в одну строку.
SAMPLES_SQL = """
SELECT sa.sample_type AS "тип", sa.organ AS "орган", sa.label AS "метка",
       sa.preservative AS "фиксация", group_concat(l.name, '; ') AS "отправлен",
       sa.raw_value AS "в таблице записано", sa.notes AS "примечания"
FROM sample sa
LEFT JOIN sample_shipment sh ON sh.sample_id = sa.sample_id
LEFT JOIN lab l              ON l.lab_id = sh.lab_id
WHERE sa.specimen_id = ?
GROUP BY sa.sample_id
ORDER BY sa.sample_id
"""

ANALYSES_SQL = """
SELECT sa.label AS "образец", a.analysis_type AS "анализ", a.analysed_on AS "дата",
       l.name AS "лаборатория", a.result AS "результат", a.accession AS "номер"
FROM analysis a
JOIN sample sa  ON sa.sample_id = a.sample_id
LEFT JOIN lab l ON l.lab_id = a.lab_id
WHERE sa.specimen_id = ?
ORDER BY a.analysis_id
"""

# Замеры воды: {where} — либо одно траление особи, либо весь выезд.
# 'старт' < 'точка' < 'финиш' по алфавиту — поэтому сортировка по point_type годится.
WATER_SQL = """
SELECT t.trawl_no AS "трал", w.point_type AS "точка", w.measured_time AS "время",
       w.lat AS "широта", w.lon AS "долгота", w.depth_m AS "глубина, м",
       w.o2_pct AS "O2, %", w.ppm AS "ppm", w.conductivity_ms AS "mS",
       w.conductivity_us AS "uS", w.ph AS "pH", w.water_temp_c AS "t воды",
       w.notes AS "примечания"
FROM water_measurement w
LEFT JOIN trawling t ON t.trawling_id = w.trawling_id
WHERE {where}
ORDER BY t.trawl_no IS NULL, t.trawl_no, w.point_type
"""

# Проблемы самой особи и её выезда. Свёрнутые записи хранят перечень номеров
# через запятую ('5 e, 6 e, 7 e') — ищем номер в этом перечне через instr().
ISSUES_SQL = """
SELECT issue_id AS "id",
       CASE table_name WHEN 'sampling_event' THEN 'выезд'
                       WHEN 'sample' THEN 'образец'
                       WHEN 'specimen' THEN 'особь'
                       ELSE table_name END AS "к чему",
       category AS "категория", cell AS "ячейка", raw_value AS "значение",
       description AS "описание",
       CASE resolved WHEN 1 THEN 'решено: ' || coalesce(resolution, '') ELSE 'открыто' END
           AS "статус"
FROM data_issue
WHERE (table_name = 'specimen' AND record_id = :specimen_id)
   OR object_label = :label
   OR instr(', ' || object_label || ', ', ', ' || :label || ', ') > 0
   OR (table_name = 'sampling_event' AND record_id = :event_id)
ORDER BY resolved, issue_id
"""


def _number(value: float | None, unit: str) -> str | None:
    return None if value is None else f"{value:g} {unit}"


def _main_section(s: sqlite3.Row) -> CardSection:
    method = s["own_method"] or s["event_method"]
    if s["own_method"]:
        method += f" (выезд — {s['event_method']})"
    coords = None if s["lat"] is None else f"{s['lat']:.10g}, {s['lon']:.10g}"
    date = s["event_date"]
    if s["capture_date"]:
        date = f"{s['capture_date']} (выезд — {s['event_date']})"
    fields = [
        ("номер пробы", s["label"]),
        ("в Excel записано", s["label_raw"]),
        ("вид (текущий)", s["species"] or "(не определён)"),
        ("экспедиция", s["expedition"]),
        ("выезд (лист)", s["source_sheet"]),
        ("дата", date),
        ("способ получения", method),
        ("место", s["site"]),
        ("координаты места", coords),
        ("траление", s["trawl_no"] if s["trawl_no"] is not None else "неизвестно"),
        ("TL", _number(s["tl_cm"], "см")),
        ("SL", _number(s["sl_cm"], "см")),
        ("m", _number(s["mass_g"], "г")),
        ("m без внутр.", _number(s["mass_gutted_g"], "г")),
        ("пол", s["sex"]),
        ("мёртвая", "да" if s["is_dead"] else "нет"),
        ("примечания", s["notes"]),
        ("строка в Excel", s["source_row"]),
    ]
    return CardSection("Особь", ["поле", "значение"], fields, is_fields=True)


def _water_section(conn: sqlite3.Connection, s: sqlite3.Row) -> CardSection:
    # {where} — один из двух фиксированных фрагментов кода, значения — через «?»
    if s["trawling_id"] is not None:
        title = f"Вода: траление {s['trawl_no']}"
        where, value = "w.trawling_id = ?", s["trawling_id"]
    else:
        title = "Вода: все замеры выезда"
        where, value = "w.event_id = ?", s["event_id"]
    section = query_section(conn, title, WATER_SQL.format(where=where), (value,), "(замеров нет)")
    if s["trawling_id"] is None and s["event_method"] == "траление":
        section.note = "Траление особи неизвестно — рыба поймана в одном из этих тралений."
    if s["trawling_id"] is None and s["own_method"]:
        section.note = (
            f"Особь получена не так, как выезд ({s['own_method']}) — "
            "замеры выезда могут к ней не относиться."
        )
    return section


def specimen_card(conn: sqlite3.Connection, label: str) -> list[CardSection]:
    """Все сведения об особи по номеру пробы ('155 pc', '155pc', '26 ph').

    Если особи нет — NotFoundError с подсказкой похожих номеров (из find_specimen).
    """
    found = find_specimen(conn, label)
    s = conn.execute(SPECIMEN_SQL, (found["specimen_id"],)).fetchone()
    sid = (s["specimen_id"],)
    # именованные параметры (:label) — значения подставляются из словаря
    issue_params = {"specimen_id": s["specimen_id"], "label": s["label"], "event_id": s["event_id"]}
    return [
        _main_section(s),
        query_section(conn, "История определений вида", IDENTIFICATIONS_SQL, sid),
        query_section(conn, "Образцы", SAMPLES_SQL, sid),
        query_section(conn, "Результаты анализов", ANALYSES_SQL, sid, "(пока нет)"),
        _water_section(conn, s),
        query_section(conn, "Проблемы в журнале (особь и её выезд)", ISSUES_SQL, issue_params),
    ]


def render_card(
    sections: list[CardSection], max_width: int = 1000, total_width: int | None = None
) -> str:
    """Текст карточки для печати в терминале (total_width — ширина экрана)."""
    return render_sections(sections, max_width, total_width)
