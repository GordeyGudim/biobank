"""Импорт книги Excel в базу по правилам маппинга из CLAUDE.md.

Порядок:
1. Прочитать книгу (excel_reader) — без базы.
2. Создать новую базу во ВРЕМЕННОМ файле и записать всё в одной транзакции.
3. Только если всё прошло успешно — заменить старый файл новым.

Так при ошибке старая база остаётся целой, а полузаписанной базы не бывает.

Транзакция — группа изменений, которая применяется целиком или не применяется
совсем. В sqlite3 её даёт конструкция `with conn:` — при выходе без ошибки
делается COMMIT (сохранить), при исключении — ROLLBACK (откатить).
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from biobank.db import DEFAULT_DB_PATH, SCHEMA_PATH, connect, create_database
from biobank.excel_reader import (
    CoordinateEntry,
    ExpeditionData,
    Issue,
    SheetData,
    WaterPoint,
    read_workbook,
)
from biobank.parsing import (
    LAB_CANTHO,
    LAB_RUSSIA,
    LAB_TROPCENTER,
    LAB_VN_COMPANY,
    SPECIES,
    City,
    distance_km,
    market_city,
)

CAPTURE_METHODS = ("траление", "рыбак", "рынок", "аквахозяйство")
LABS = (LAB_RUSSIA, LAB_TROPCENTER, LAB_CANTHO, LAB_VN_COMPANY)

SAME_SITE_KM = 0.2  # места одного типа ближе 200 м считаются одним местом
OUTLIER_KM = 30  # точка траления дальше 30 км от остальных точек выезда — подозрительна

# тип места по способу получения рыбы
SITE_TYPE = {
    "траление": "участок реки",
    "рынок": "рынок",
    "аквахозяйство": "аквахозяйство",
    "рыбак": "точка рыбака",
}

# Сколько одинаковых проблем на одном листе сворачивать в одну запись
COLLAPSE_THRESHOLD = 4


@dataclass
class ImportContext:
    """Всё, что импорт узнал по ходу: id созданных записей и найденные проблемы."""

    conn: sqlite3.Connection
    issues: list[Issue] = field(default_factory=list)
    region_ids: dict[str, int] = field(default_factory=dict)
    city_ids: dict[tuple[int, str], int] = field(default_factory=dict)
    method_ids: dict[str, int] = field(default_factory=dict)
    species_ids: dict[str, int] = field(default_factory=dict)
    lab_ids: dict[str, int] = field(default_factory=dict)
    gear_ids: dict[tuple, int] = field(default_factory=dict)
    # места: (тип, название, lat, lon, site_id) — для поиска «то же место»
    sites: list[tuple[str, str, float | None, float | None, int]] = field(default_factory=list)
    event_ids: dict[str, int] = field(default_factory=dict)  # лист → event_id
    trawl_ids: dict[tuple[str, int], int] = field(default_factory=dict)  # (лист, №) → id
    # места поимки внутри выезда: лист → [(диапазон номеров или None, site_id)]
    capture_sites: dict[str, list[tuple[tuple[int, int] | None, int]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def insert(self, table: str, **values) -> int:
        """INSERT одной строки; вернуть её id.

        Имена колонок берутся из кода (не от пользователя), значения передаются
        через «?» — так SQLite сам экранирует их, и подстановка строк не нужна.
        """
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cursor = self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
        )
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------


def _import_references(ctx: ImportContext) -> None:
    for name in CAPTURE_METHODS:
        ctx.method_ids[name] = ctx.insert("capture_method", name=name)
    for name, (family, genus, code) in SPECIES.items():
        ctx.species_ids[name] = ctx.insert(
            "species", scientific_name=name, family=family, genus=genus, code=code
        )
    for name in LABS:
        ctx.lab_ids[name] = ctx.insert("lab", name=name)


def _city_id(ctx: ImportContext, region_id: int, city: City | None) -> int | None:
    if city is None:
        return None
    key = (region_id, city.name)
    if key not in ctx.city_ids:
        ctx.city_ids[key] = ctx.insert(
            "city", region_id=region_id, name=city.name, name_latin=city.name_latin
        )
    return ctx.city_ids[key]


def _gear_id(ctx: ImportContext, gear: tuple[str | None, str | None] | None) -> int | None:
    if gear is None:
        return None
    if gear not in ctx.gear_ids:
        mesh, size = gear
        ctx.gear_ids[gear] = ctx.insert("gear", gear_type="трал", mesh_cm=mesh, size_m=size)
    return ctx.gear_ids[gear]


# ---------------------------------------------------------------------------
# Места
# ---------------------------------------------------------------------------


def _site_id(
    ctx: ImportContext,
    site_type: str,
    name: str,
    lat: float | None,
    lon: float | None,
    city_id: int | None,
    description: str,
    coord_note: str | None = None,
) -> int:
    """Найти существующее место или создать новое.

    То же место — если совпадает тип и:
    - для участков реки — название («Участок у Кантхо» в 2025 и 2026 — один участок,
      экспедиции возвращаются на те же точки, хотя тралят не в метр в метр);
    - для остальных — расстояние не больше 200 м (или одинаковое имя, если координат нет).
    Координаты места — от первого выезда на него; у последующих выездов они не меняются,
    поэтому coord_note (откуда взяты координаты) пишется только при создании места.
    """
    for s_type, s_name, s_lat, s_lon, s_id in ctx.sites:
        if s_type != site_type:
            continue
        if site_type == "участок реки":
            same = s_name == name
        elif lat is None or s_lat is None:
            same = lat is None and s_lat is None and s_name == name
        else:
            same = distance_km((lat, lon), (s_lat, s_lon)) <= SAME_SITE_KM
        if same:
            ctx.conn.execute(
                "UPDATE sampling_site SET description = description || '; ' || ? WHERE site_id = ?",
                (description, s_id),
            )
            return s_id
    site_id = ctx.insert(
        "sampling_site",
        city_id=city_id,
        name=name,
        site_type=site_type,
        lat=lat,
        lon=lon,
        description=f"{description}; {coord_note}" if coord_note else description,
    )
    ctx.sites.append((site_type, name, lat, lon, site_id))
    return site_id


def _fisher_site_name(city: City | None, lat: float, lon: float) -> str:
    where = f" у {city.name}" if city else ""
    return f"Точка рыбака{where} ({lat:.4f}, {lon:.4f})"


def _find_outliers(sheet: SheetData) -> set[int]:
    """Точки-выбросы выезда (> 30 км от медианы остальных) → записи data_issue.

    Медиана — «серединное» значение; в отличие от среднего, один выброс её почти не сдвигает.
    Возвращает id() объектов-точек, чтобы не брать их как координаты места.
    """
    points = [
        (trawl.trawl_no, p)
        for trawl in sheet.trawls.values()
        for p in trawl.points.values()
        if p.lat is not None and p.lon is not None
    ]
    if len(points) < 4:
        return set()
    median = (
        statistics.median(p.lat for _, p in points),
        statistics.median(p.lon for _, p in points),
    )
    outliers = set()
    for trawl_no, point in points:
        dist = distance_km((point.lat, point.lon), median)
        if dist > OUTLIER_KM:
            outliers.add(id(point))
            sheet.issues.append(
                Issue(
                    "координаты",
                    f"Точка в {dist:.0f} км от остальных точек этого выезда; "
                    "не использована как координата места",
                    sheet.name,
                    point.cell,
                    f"траление {trawl_no} {point.point_type}",
                    f"{point.lat}, {point.lon}",
                    "water_measurement",
                )
            )
    return outliers


def _first_trawl_point(sheet: SheetData) -> tuple[WaterPoint, int] | None:
    """Координата места-участка: старт первого траления выезда (реальный замер).

    Если у старта нет координат или он выброс — финиш того же траления,
    дальше — следующее траление.
    """
    outliers = _find_outliers(sheet)
    for trawl_no in sorted(sheet.trawls):
        for point_type in ("старт", "финиш"):
            point = sheet.trawls[trawl_no].points.get(point_type)
            if point and point.lat is not None and point.lon is not None:
                if id(point) not in outliers:
                    return point, trawl_no
    return None


def _event_site(ctx: ImportContext, sheet: SheetData, region_id: int) -> int:
    """Место выезда по правилам для каждого способа получения."""
    city_id = _city_id(ctx, region_id, sheet.city)
    method = sheet.capture_method
    site_type = SITE_TYPE[method]
    source = f"лист «{sheet.name}»"

    if method == "рынок":
        city = market_city(sheet.name)
        return _site_id(
            ctx, site_type, f"Рынок {city.name}", None, None,
            _city_id(ctx, region_id, city), source,
        )  # fmt: skip

    if method == "траление":
        first = _first_trawl_point(sheet)
        lat, lon = (first[0].lat, first[0].lon) if first else (None, None)
        coord_note = None
        if first:
            point, trawl_no = first
            coord_note = (
                f"координаты места — {point.point_type} траления {trawl_no} "
                f"(лист «{sheet.name}», ячейка {point.cell}); "
                "точки всех тралений — в water_measurement"
            )
        if sheet.city:
            name = f"Участок у {sheet.city.name}"
        else:
            name = f"Участок (город не указан, {source})"
            sheet.issues.append(
                Issue("место", "В заголовке листа не указан город — место названо по листу",
                      sheet.name, "A1", None, sheet.header_text, "sampling_site")
            )  # fmt: skip
        return _site_id(ctx, site_type, name, lat, lon, city_id, source, coord_note)

    # аквахозяйство и рыбак — по координатам с листа (первые подписанные координаты)
    coord = _main_coordinates(sheet)
    if coord is None:
        sheet.issues.append(
            Issue("координаты", "На листе нет координат места", sheet.name,
                  None, None, None, "sampling_site")
        )  # fmt: skip
        lat = lon = None
    else:
        lat, lon = coord.lat, coord.lon
        source = f"{source}, «{coord.text}»"
    if method == "аквахозяйство":
        where = f" у {sheet.city.name}" if sheet.city else ""
        name = f"Аквахозяйство{where}"
    else:
        name = _fisher_site_name(sheet.city, lat, lon) if lat is not None else "Точка рыбака"
    return _site_id(ctx, site_type, name, lat, lon, city_id, source)


def _main_coordinates(sheet: SheetData) -> CoordinateEntry | None:
    """Координаты места выезда на листах «Рыбак…»/«Аквахозяйство».

    На «Рыбак (самостоятельно 22.08)» их две: место выезда — первая («Координаты 1»),
    вторая — место поимки части особей.
    """
    return sheet.coordinates[0] if sheet.coordinates else None


def _capture_sites(ctx: ImportContext, sheet: SheetData, region_id: int, event_site: int):
    """Места поимки внутри выезда, отличные от места выезда (точки рыбаков)."""
    city_id = _city_id(ctx, region_id, sheet.city)
    main = _main_coordinates(sheet) if sheet.capture_method != "траление" else None
    for coord in sheet.coordinates:
        if coord is main:
            ctx.capture_sites[sheet.name].append((coord.label_range, event_site))
            continue
        site_id = _site_id(
            ctx, "точка рыбака", _fisher_site_name(sheet.city, coord.lat, coord.lon),
            coord.lat, coord.lon, city_id, f"лист «{sheet.name}», «{coord.text}»",
        )  # fmt: skip
        ctx.capture_sites[sheet.name].append((coord.label_range, site_id))
    point = sheet.fisher_point
    if point is not None and point.lat is not None:
        site_id = _site_id(
            ctx, "точка рыбака", _fisher_site_name(sheet.city, point.lat, point.lon),
            point.lat, point.lon, city_id, f"лист «{sheet.name}», «точка лова рыбака»",
        )  # fmt: skip
        ctx.capture_sites[sheet.name].append((None, site_id))


# ---------------------------------------------------------------------------
# Экспедиции, выезды, траления, вода
# ---------------------------------------------------------------------------


def _fill_market_dates(expedition: ExpeditionData) -> dict[str, str]:
    """Даты рынков (в таблице их нет): дата ближайшего предыдущего выезда.

    Возвращает {лист: примечание} для записи в sampling_event.notes.
    """
    notes = {}
    previous: SheetData | None = None
    for sheet in expedition.sheets:
        if sheet.event_date is None and previous is not None:
            sheet.event_date = previous.event_date
            note = (
                f"Дата на листе не указана; взята дата предыдущего выезда "
                f"(лист «{previous.name}», {previous.event_date})"
            )
            notes[sheet.name] = note
            sheet.issues.append(Issue("дата", note, sheet.name, None, None, None, "sampling_event"))
        if sheet.event_date is not None:
            previous = sheet
    return notes


def _expedition_name(expedition: ExpeditionData) -> str:
    years = sorted({s.event_date[:4] for s in expedition.sheets if s.event_date})
    return f"{expedition.region} {'–'.join(years)}" if years else expedition.region


def _event_notes(sheet: SheetData, extra: str | None) -> str | None:
    parts = []
    if extra:
        parts.append(extra)
    if sheet.header_text:
        parts.append(f"Заголовок листа: {sheet.header_text}")
    parts.extend(sheet.header_notes)
    return "; ".join(parts) or None


def _import_water(ctx: ImportContext, event_id: int, trawling_id: int | None, point: WaterPoint):
    ctx.insert(
        "water_measurement",
        event_id=event_id,
        trawling_id=trawling_id,
        point_type=point.point_type,
        measured_time=point.measured_time,
        lat=point.lat,
        lon=point.lon,
        coord_source=point.coord_source,
        garmin_wp=point.garmin_wp,
        depth_m=point.depth_m,
        o2_pct=point.o2_pct,
        ppm=point.ppm,
        conductivity_ms=point.conductivity_ms,
        conductivity_us=point.conductivity_us,
        ph=point.ph,
        water_temp_c=point.water_temp_c,
        notes="; ".join(point.notes) or None,
    )


def _import_sheet(ctx: ImportContext, sheet: SheetData, expedition_id: int, region_id: int,
                  market_notes: dict[str, str]) -> None:  # fmt: skip
    site_id = _event_site(ctx, sheet, region_id)
    is_trawl = sheet.capture_method == "траление"
    event_id = ctx.insert(
        "sampling_event",
        expedition_id=expedition_id,
        site_id=site_id,
        event_date=sheet.event_date,
        capture_method_id=ctx.method_ids[sheet.capture_method],
        gear_id=_gear_id(ctx, sheet.gear) if is_trawl else None,
        source_sheet=sheet.name,
        notes=_event_notes(sheet, market_notes.get(sheet.name)),
    )
    ctx.event_ids[sheet.name] = event_id
    _capture_sites(ctx, sheet, region_id, site_id)

    for issue in sheet.issues:
        if issue.table_name == "sampling_event":
            issue.record_id = event_id

    for trawl_no in sorted(sheet.trawls):
        trawl = sheet.trawls[trawl_no]
        speeds = trawl.speeds
        notes = list(trawl.notes)
        if len(set(speeds)) > 1:
            notes.append("скорость на старте/финише: " + " / ".join(f"{s:g}" for s in speeds))
        trawling_id = ctx.insert(
            "trawling",
            event_id=event_id,
            trawl_no=trawl_no,
            time_start=trawl.time_start,
            time_end=trawl.time_end,
            duration_min=trawl.duration_min,
            speed_kmh=round(statistics.fmean(speeds), 2) if speeds else None,
            intermediate_depth_m=trawl.intermediate_depth_m,
            catch_note="; ".join(trawl.catch_comments) or None,
            notes="; ".join(notes) or None,
        )
        ctx.trawl_ids[(sheet.name, trawl_no)] = trawling_id
        for issue in trawl.issues:
            issue.record_id = trawling_id
        ctx.issues.extend(trawl.issues)
        for point_type in ("старт", "финиш"):
            if point_type in trawl.points:
                _import_water(ctx, event_id, trawling_id, trawl.points[point_type])

    if sheet.fisher_point is not None:
        point = sheet.fisher_point
        if sheet.fisher_point_comment:
            point.notes.insert(0, sheet.fisher_point_comment)
        _import_water(ctx, event_id, None, point)

    ctx.issues.extend(sheet.issues)


def _import_expeditions(ctx: ImportContext, expeditions: list[ExpeditionData]) -> None:
    for expedition in expeditions:
        if expedition.region not in ctx.region_ids:
            ctx.region_ids[expedition.region] = ctx.insert("region", name=expedition.region)
        region_id = ctx.region_ids[expedition.region]
        market_notes = _fill_market_dates(expedition)
        dates = [s.event_date for s in expedition.sheets if s.event_date]
        expedition_id = ctx.insert(
            "expedition",
            name=_expedition_name(expedition),
            date_start=min(dates) if dates else None,
            date_end=max(dates) if dates else None,
        )
        for sheet in expedition.sheets:
            _import_sheet(ctx, sheet, expedition_id, region_id, market_notes)


# ---------------------------------------------------------------------------
# Журнал проблем
# ---------------------------------------------------------------------------


def collapse_issues(issues: list[Issue]) -> list[Issue]:
    """Свернуть повторяющиеся проблемы: ≥ 4 одинаковых на листе → одна запись с перечнем."""
    groups: dict[tuple, list[Issue]] = defaultdict(list)
    for issue in issues:
        groups[(issue.category, issue.sheet, issue.description, issue.table_name)].append(issue)
    result = []
    for (category, sheet, description, table), items in groups.items():
        if len(items) < COLLAPSE_THRESHOLD:
            result.extend(items)
            continue
        objects = ", ".join(i.object_label or i.cell or "?" for i in items)
        cells = [i.cell for i in items if i.cell]
        result.append(
            Issue(
                category,
                f"{description} ({len(items)} шт.)",
                sheet,
                f"{cells[0]}…{cells[-1]}" if cells else None,
                objects,
                items[0].raw_value,
                table,
            )
        )
    return result


def _write_issues(ctx: ImportContext) -> int:
    issues = collapse_issues(ctx.issues)
    ctx.conn.executemany(
        "INSERT INTO data_issue (category, table_name, record_id, sheet_name, cell, "
        "object_label, raw_value, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (i.category, i.table_name, i.record_id, i.sheet, i.cell, i.object_label,
             i.raw_value, i.description)
            for i in issues
        ],
    )  # fmt: skip
    return len(issues)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

SUMMARY_TABLES = (
    "region", "city", "sampling_site", "capture_method", "gear", "species", "lab",
    "expedition", "sampling_event", "trawling", "water_measurement",
    "specimen", "species_identification", "sample", "sample_shipment", "catch_record",
    "data_issue",
)  # fmt: skip


def import_workbook(
    xlsx_path: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
    schema_path: Path | str = SCHEMA_PATH,
) -> dict[str, int]:
    """Пересоздать базу и импортировать книгу. Вернуть {таблица: число строк}."""
    db_path = Path(db_path)
    expeditions = read_workbook(xlsx_path)

    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    create_database(tmp_path, schema_path)
    conn = connect(tmp_path)
    try:
        with conn:  # одна транзакция на весь импорт
            ctx = ImportContext(conn)
            _import_references(ctx)
            _import_expeditions(ctx, expeditions)
            _write_issues(ctx)
        summary = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in SUMMARY_TABLES
        }
    except Exception:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp_path, db_path)  # атомарная замена: старая база исчезает только сейчас
    return summary
