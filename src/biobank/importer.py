"""Импорт книги Excel в базу по правилам маппинга из CLAUDE.md.

Порядок:
1. Прочитать книгу (excel_reader) — без базы.
2. Создать новую базу во ВРЕМЕННОМ файле и записать всё в одной транзакции.
3. Сравнить её со старой базой (prepare_import): если в старой есть данные не из
   Excel, команда import откажется без --force.
4. Только если всё прошло успешно — заменить старый файл новым.

Так при ошибке старая база остаётся целой, а полузаписанной базы не бывает.

Транзакция — группа изменений, которая применяется целиком или не применяется
совсем. В sqlite3 её даёт конструкция `with conn:` — при выходе без ошибки
делается COMMIT (сохранить), при исключении — ROLLBACK (откатить).
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

from biobank.checks import SOURCE_IMPORT, collapse_issues, run_checks
from biobank.db import (
    DEFAULT_DB_PATH,
    close_wal,
    connect,
    connect_read_only,
    create_database,
    ensure_not_live,
    list_tables,
    remove_temp_database,
)
from biobank.excel_reader import (
    CoordinateEntry,
    ExpeditionData,
    Issue,
    SheetData,
    SpecimenRow,
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
    SampleSpec,
    distance_km,
    is_blank,
    is_catch_note,
    is_dead_text,
    is_whole_specimen,
    is_whole_specimen_comment,
    market_city,
    parse_catch_counts,
    parse_catch_note,
    parse_histology,
    parse_label,
    parse_labs,
    parse_preservative,
    parse_sex,
    parse_shrimp_samples,
    parse_species,
    smear_looks_like_histology,
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
        if city is None:
            sheet.issues.append(
                Issue("место", "В имени листа рынка не указан город — место названо по листу",
                      sheet.name, None, None, sheet.name, "sampling_site")
            )  # fmt: skip
        name = f"Рынок {city.name}" if city else f"Рынок (город не указан, {source})"
        return _site_id(ctx, site_type, name, None, None, _city_id(ctx, region_id, city), source)

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
    for note in sheet.note_rows:
        if not is_catch_note(note.text):
            parts.append(note.text + (f" ({note.comment})" if note.comment else ""))
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
        for comment in trawl.catch_comments:
            for taxon, count in parse_catch_counts(comment):
                ctx.insert(
                    "catch_record",
                    event_id=event_id,
                    trawling_id=trawling_id,
                    taxon_text=taxon,
                    count_n=count,
                    raw_text=comment,
                )

    if sheet.fisher_point is not None:
        point = sheet.fisher_point
        if sheet.fisher_point_comment:
            point.notes.insert(0, sheet.fisher_point_comment)
        _import_water(ctx, event_id, None, point)

    _import_note_rows(ctx, sheet, event_id)
    _import_specimens(ctx, sheet, event_id, site_id)
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
        _check_missing_shipments(expedition)
        for sheet in expedition.sheets:
            _import_sheet(ctx, sheet, expedition_id, region_id, market_notes)


def _check_missing_shipments(expedition: ExpeditionData) -> None:
    """Лист с образцами, но без записи «куда отправлены», если на других листах она есть."""
    for attr, column, sample_word in (
        ("dna_labs", "dna", "ДНК"),
        ("smear_labs", "smear", "мазков"),
    ):
        if not any(getattr(s, attr) for s in expedition.sheets):
            continue  # в экспедиции вообще не записано — не проблема конкретного листа
        for sheet in expedition.sheets:
            has_samples = any(
                not is_blank(spec.values.get(column))
                and not is_whole_specimen(spec.values.get(column))
                for spec in sheet.specimens
            )
            if has_samples and not getattr(sheet, attr):
                sheet.issues.append(
                    Issue("связь",
                          f"Не записано, куда отправлены образцы {sample_word} "
                          "(на других листах экспедиции записано в комментарии к шапке)",
                          sheet.name, None, None, None, "sample_shipment")
                )  # fmt: skip


# ---------------------------------------------------------------------------
# Особи, определения вида, образцы, прилов
# ---------------------------------------------------------------------------


def _import_note_rows(ctx: ImportContext, sheet: SheetData, event_id: int) -> None:
    """Строки под таблицей особей: улов → catch_record, остальное уже в notes выезда."""
    for note in sheet.note_rows:
        if is_catch_note(note.text):
            catch = parse_catch_note(note.text)
            trawling_id = ctx.trawl_ids.get((sheet.name, catch.trawl_no))
            if catch.trawl_no is not None and trawling_id is None:
                sheet.issues.append(
                    Issue("связь", f"В заметке указано траление {catch.trawl_no}, а его нет",
                          sheet.name, note.cell, None, note.text, "catch_record")
                )  # fmt: skip
            ctx.insert(
                "catch_record",
                event_id=event_id,
                trawling_id=trawling_id,
                taxon_text=catch.taxon_text,
                count_n=catch.count,
                mass_g=catch.mass_g,
                raw_text=note.text + (f" ({note.comment})" if note.comment else ""),
            )
        elif "только гистология" in note.text.lower():
            sheet.issues.append(
                Issue("номера",
                      "Для этих номеров нет строк особей — вид и промеры не записаны",
                      sheet.name, note.cell, None, note.text, "sampling_event")
            )  # fmt: skip


def _specimen_capture(
    ctx: ImportContext, sheet: SheetData, spec: SpecimenRow, event_site: int, from_fisher: bool
) -> tuple[int | None, int | None]:
    """Способ и место поимки особи, ТОЛЬКО если они отличаются от выезда.

    Возвращает (capture_method_id, capture_site_id); (None, None) — как у выезда.
    """
    number = spec.label.number if spec.label else None
    for label_range, site_id in ctx.capture_sites.get(sheet.name, []):
        if label_range and number is not None and label_range[0] <= number <= label_range[1]:
            if site_id == event_site:
                return None, None
            method = None if sheet.capture_method == "рыбак" else ctx.method_ids["рыбак"]
            return method, site_id
    if from_fisher:
        fisher_sites = [s for r, s in ctx.capture_sites.get(sheet.name, []) if r is None]
        site_id = fisher_sites[-1] if fisher_sites else None
        return ctx.method_ids["рыбак"], site_id
    return None, None


def _whole_key(spec: SpecimenRow) -> str | None:
    """Колонка, где написано «целая рыба» / «заспиртовали целиком» (None — особь не целая)."""
    for key in ("dna", "histo", "smear", "gut", "tl", "sl", "mass"):
        if is_whole_specimen(spec.values.get(key)):
            return key
    return None


def _whole_comment_key(spec: SpecimenRow) -> str | None:
    """Ячейка, в комментарии к которой сказано, что особь сохранили целиком (или None)."""
    for key in ("label", "species", "dna", "gut", "histo", "smear", "tl", "sl", "mass"):
        if is_whole_specimen_comment(spec.comments.get(key)):
            return key
    return None


def _preservative(
    sheet: SheetData, spec: SpecimenRow, text, cell_key: str, header_key: str | None
) -> str | None:
    """Фиксатор образца — только если он назван в Excel (иначе None, не угадываем).

    Порядок: текст ячейки → комментарий к ней → комментарий к шапке колонки header_key
    (методика для всех проб листа). header_key отличается от cell_key, когда колонки
    гистологии и мазка перепутаны; None — шапку не смотреть.
    """
    return (
        parse_preservative(text)
        or parse_preservative(spec.comments.get(cell_key))
        or (sheet.column_preservatives.get(header_key) if header_key else None)
    )


def _specimen_samples(sheet: SheetData, spec: SpecimenRow, label: str) -> list[tuple]:
    """Образцы особи: [(SampleSpec, исходное значение, [лаборатории])]."""
    values = spec.values
    samples: list[tuple] = []
    whole_key = _whole_key(spec)
    if whole_key:
        whole = str(values[whole_key])
        # фиксатор целой особи: её ячейка и комментарии к ней, к номеру и к виду
        # (они о рыбе целиком); шапка колонки — о других образцах, её не берём
        preservative = _preservative(sheet, spec, whole, whole_key, None)
        for key in ("label", "species"):
            preservative = preservative or parse_preservative(spec.comments.get(key))
        samples.append((SampleSpec("целая особь", None, label, preservative), whole, []))
    elif comment_key := _whole_comment_key(spec):
        # в ячейках о целой особи ни слова, но сказано в комментарии (ХХ, 178 pc)
        comment = spec.comments[comment_key]
        note = f"целая особь — по комментарию к ячейке {spec.cells[comment_key]}"
        samples.append(
            (SampleSpec("целая особь", None, label, parse_preservative(comment), note),
             comment, [])
        )  # fmt: skip

    if spec.label and spec.label.series == "mr":
        for sample in parse_shrimp_samples(values.get("histo"), values.get("smear"), label):
            key = "histo" if sample.sample_type == "гистология" else "smear"
            if sample.preservative is None:
                # колонки у креветок означают другое — методика из шапки к ним не относится
                sample = replace(sample, preservative=_preservative(sheet, spec, None, key, None))
            samples.append((sample, values.get(key), []))
        return samples

    extra_labs = []
    for comment in spec.comments.values():
        extra_labs += [lab for lab in parse_labs(comment) if lab == LAB_VN_COMPANY]

    dna = values.get("dna")
    if not is_blank(dna) and not is_whole_specimen(dna):
        parsed = parse_label(dna)
        samples.append(
            (SampleSpec("ДНК", None, parsed.label if parsed else str(dna),
                        _preservative(sheet, spec, dna, "dna", "dna")),
             dna, sheet.dna_labs + extra_labs)
        )  # fmt: skip
    elif extra_labs:
        sheet.issues.append(
            Issue("связь", "Отправлено на ген. анализ, но образца ДНК в таблице нет",
                  sheet.name, spec.cells.get("label"), label, None, "specimen")
        )  # fmt: skip

    gut = values.get("gut")
    if not is_blank(gut) and not is_whole_specimen(gut):
        parsed = parse_label(gut)
        samples.append(
            (SampleSpec("кишечник", "кишечник", parsed.label if parsed else str(gut),
                        _preservative(sheet, spec, gut, "gut", "gut")), gut, [])
        )  # fmt: skip

    histo, smear = values.get("histo"), values.get("smear")
    histo_cell, smear_cell = "histo", "smear"  # где физически записан текст
    if smear_looks_like_histology(smear):
        sheet.issues.append(
            Issue("формат",
                  "В колонке «мазок крови» записан орган — колонки гистологии и мазка "
                  "перепутаны; импортировано как гистология + мазок",
                  sheet.name, spec.cells.get("smear"), label, str(smear), "sample")
        )  # fmt: skip
        histo, smear = smear, histo
        histo_cell, smear_cell = smear_cell, histo_cell

    result = parse_histology(histo, label)
    for warning in result.warnings:
        category = "номера" if "номер" in warning else "формат"
        sheet.issues.append(
            Issue(category, warning, sheet.name, spec.cells.get("histo"), label,
                  None if histo is None else str(histo), "sample")
        )  # fmt: skip
    histo_preservative = _preservative(sheet, spec, histo, histo_cell, "histo")
    samples += [
        (replace(sample, preservative=sample.preservative or histo_preservative), histo, [])
        for sample in result.samples
    ]

    if (
        not is_blank(smear)
        and not is_whole_specimen(smear)
        and not is_dead_text(smear)
        and not str(smear).strip().lower().startswith("нет")
    ):
        parsed = parse_label(smear)
        smear_label = parsed.label if parsed else str(smear)
        samples.append(
            # фиксация — из ячейки или методики в комментарии к шапке (листы 2025:
            # «…фиксировали в 96% этаноле»); не названа (2026) — NULL
            (SampleSpec("мазок крови", None, smear_label,
                        _preservative(sheet, spec, smear, smear_cell, "smear")),
             smear, sheet.smear_labs)
        )  # fmt: skip
    return samples


def _specimen_notes(spec: SpecimenRow) -> list[str]:
    notes = []
    for key, text in spec.comments.items():
        if key == "species" or (key == "smear" and "шприц" in text):
            continue  # вид → в определение; методика мазка одинакова на всех листах
        notes.append(text if key == "label" else f"{key}: {text}")
    smear = spec.values.get("smear")
    if isinstance(smear, str) and smear.strip().lower().startswith("нет"):
        notes.append(f"мазок: {smear.strip()}")
    return notes


def _import_specimens(ctx: ImportContext, sheet: SheetData, event_id: int, event_site: int):
    from_fisher_series = None  # после комментария «далее рыба, пойманная … рыбаком»
    event_date = sheet.event_date

    for spec in sheet.specimens:
        label = spec.label.label if spec.label else f"{spec.label_raw} ({sheet.name})"
        series = spec.label.series if spec.label else None

        label_comment = spec.comments.get("label", "").lower()
        if "далее" in label_comment and "рыбак" in label_comment:
            from_fisher_series = series
        from_fisher = from_fisher_series is not None and from_fisher_series == series
        method_id, site_id = _specimen_capture(ctx, sheet, spec, event_site, from_fisher)

        texts = [v for v in spec.values.values() if isinstance(v, str)]
        texts += list(spec.comments.values())
        is_dead = any(is_dead_text(text) for text in texts)
        sex = next((parse_sex(c) for c in spec.comments.values() if parse_sex(c)), None)

        species_raw = spec.values.get("species")
        species = parse_species(species_raw)
        species_id = ctx.species_ids.get(species.name) if species.name else None

        specimen_id = ctx.insert(
            "specimen",
            label=label,
            label_raw=spec.label_raw,
            series=series,
            series_no=spec.label.number if spec.label else None,
            event_id=event_id,
            trawling_id=None,  # в таблице не записано — не угадываем
            species_id=species_id,
            capture_method_id=method_id,
            capture_site_id=site_id,
            tl_cm=spec.tl_cm,
            sl_cm=spec.sl_cm,
            mass_g=spec.mass_g,
            mass_gutted_g=spec.mass_gutted_g,
            sex=sex,
            is_dead=int(is_dead),
            notes="; ".join(_specimen_notes(spec)) or None,
            source_row=spec.row,
        )

        id_notes = [n for n in (species.note, spec.comments.get("species")) if n]
        ctx.insert(
            "species_identification",
            specimen_id=specimen_id,
            species_id=species_id,
            method="морфология",
            confidence=species.confidence,
            identified_on=event_date,
            raw_text=None if species_raw is None else " ".join(str(species_raw).split()),
            notes="; ".join(id_notes) or None,
        )

        for sample, raw, labs in _specimen_samples(sheet, spec, label):
            sample_id = ctx.insert(
                "sample",
                specimen_id=specimen_id,
                sample_type=sample.sample_type,
                organ=sample.organ,
                label=sample.label,
                preservative=sample.preservative,
                raw_value=None if raw is None else str(raw),
                notes=sample.notes,
            )
            for lab in dict.fromkeys(labs):  # без повторов, порядок сохраняется
                ctx.insert("sample_shipment", sample_id=sample_id, lab_id=ctx.lab_ids[lab])

        # проблемы этой особи; особь без номера excel_reader называет «ХХ»
        own_label = spec.label.label if spec.label else "ХХ"
        for issue in sheet.issues:
            if issue.table_name == "specimen" and issue.object_label == own_label:
                issue.record_id = issue.record_id or specimen_id


# ---------------------------------------------------------------------------
# Журнал проблем
# ---------------------------------------------------------------------------


def _write_issues(ctx: ImportContext) -> int:
    issues = collapse_issues(ctx.issues)
    ctx.conn.executemany(
        "INSERT INTO data_issue (category, table_name, record_id, sheet_name, cell, "
        "object_label, raw_value, description, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (i.category, i.table_name, i.record_id, i.sheet, i.cell, i.object_label,
             i.raw_value, i.description, SOURCE_IMPORT)
            for i in issues
        ],
    )  # fmt: skip
    return len(issues)


# ---------------------------------------------------------------------------
# Сборка новой базы во временном файле
# ---------------------------------------------------------------------------

SUMMARY_TABLES = (
    "region", "city", "sampling_site", "capture_method", "gear", "species", "lab",
    "expedition", "sampling_event", "trawling", "water_measurement",
    "specimen", "species_identification", "sample", "sample_shipment", "catch_record",
    "data_issue",
)  # fmt: skip


def _build_database(expeditions: list[ExpeditionData], path: Path) -> dict[str, int]:
    """Создать базу в файле path и записать в неё книгу. Вернуть {таблица: число строк}.

    При ошибке файл удаляется — полузаписанной базы не остаётся.
    path — временный файл импорта: его (и его -wal/-shm от прошлого сбоя) удалять можно.
    """
    remove_temp_database(path)
    create_database(path)
    conn = connect(path)
    try:
        with conn:  # одна транзакция на весь импорт
            ctx = ImportContext(conn)
            _import_references(ctx)
            _import_expeditions(ctx, expeditions)
            _write_issues(ctx)
            run_checks(conn)  # в новой базе закрывать нечего — результат не зависит от даты
        # имена таблиц — из кода (SUMMARY_TABLES), не от пользователя
        summary = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in SUMMARY_TABLES
        }
    except Exception:
        conn.close()
        remove_temp_database(path)
        raise
    conn.close()
    return summary


# ---------------------------------------------------------------------------
# Защита: импорт пересоздаёт базу — не стереть то, чего нет в Excel
# ---------------------------------------------------------------------------
#
# Как узнать, что в базе есть данные не из Excel? Собрать новую базу из Excel
# (это всё равно нужно для импорта) и сравнить со старой ПО СОДЕРЖИМОМУ: каждая
# запись — набор значений колонок без собственного номера (id). Номер не
# сравниваем: он ничего не говорит о данных (у записей журнала проблем номера
# могут отличаться, хотя содержание то же).
#
# Ссылки на другие таблицы (sample_id, event_id…) тоже нельзя сравнивать как числа:
# если импорт создаст новую запись в начале (новый образец на первом листе), номера
# всех следующих сдвинутся, и каждая ссылка на них покажется изменённой. Поэтому
# таблицы обходятся по порядку «сначала та, на которую ссылаются» (по PRAGMA
# foreign_key_list), и для каждой строится соответствие «запись старой базы → запись
# новой базы». Ссылка в старой базе переводится в номер соответствующей записи новой
# базы и только потом сравнивается. Соответствие строится по шагам (_match):
# 1) записи с одинаковым содержимым (с уже переведёнными ссылками);
# 2) оставшиеся (изменённые) — по «устойчивому ключу» из UNIQUE-ограничений схемы
#    (особь — по label, траление — по выезду и номеру, отправка — по образцу и
#    лаборатории), если такой ключ ровно один в каждой базе;
# 3) по «детям»: у записи те же дочерние записи (у выезда — те же особи и траления,
#    у лаборатории — те же отправки), набор детей ровно один такой в каждой базе, а
#    сама запись отличается немногим (_EVIDENCE_OK). Дети берутся из первого прохода,
#    поэтому сравнение делается в два прохода (второй — только если есть расхождения);
# 4) по сходству всего содержимого (обычные колонки и ссылки): совпадают хотя бы
#    3 непустых поля и их втрое больше, чем отличий (_SIMILAR_OK), и пара взаимно
#    единственная лучшая. Короткие записи (справочники: одно-два поля) так не
#    сопоставляются — только по ключу или по детям.
# Неоднозначное не сопоставляется: тогда и записи, которые на него ссылаются,
# покажутся изменёнными — лучше показать лишнее, чем пропустить. Шаги 2–4 нужны,
# чтобы правка одного поля (место выезда, номер особи) не сделала «другими» все
# дочерние записи. Ссылка data_issue (table_name + record_id) внешним ключом не
# оформлена, но переводится так же.
#
# Почему сопоставление не может ослабить защиту: расхождения считаются только по
# содержимому записей (шаг 1), сопоставление влияет лишь на перевод ссылок. Пустой
# список значит, что в каждой таблице у каждой записи старой базы есть запись новой
# с тем же содержимым и ссылками на соответствующие записи — то есть базы совпадают с
# точностью до номеров. Шаги 2–4 трогают только изменённые записи, а изменённая запись
# сама уже даёт расхождение в своей таблице.
#
# Где числа могут быть неточными (но расхождение всё равно будет найдено): если
# изменённую запись не удалось сопоставить (изменилось много полей и детей), все
# ссылающиеся на неё записи тоже считаются изменёнными; если удалили одну запись и
# добавили почти такую же (другая особь с теми же промерами и образцами), это
# посчитается как правка одной записи, а её дети — как неизменённые.
#
TABLE_TITLES = {
    "region": "провинции",
    "city": "города",
    "sampling_site": "места",
    "capture_method": "способы получения",
    "gear": "сети",
    "species": "виды",
    "lab": "лаборатории",
    "expedition": "экспедиции",
    "sampling_event": "выезды",
    "trawling": "траления",
    "water_measurement": "замеры воды",
    "specimen": "особи",
    "species_identification": "определения вида",
    "sample": "образцы",
    "sample_shipment": "отправки образцов",
    "analysis": "результаты анализов",
    "catch_record": "прилов",
    "data_issue": "журнал проблем",
    # новое в схеме v3 (импорт эти таблицы не заполняет — всё в них появилось не из Excel)
    "person": "справочник людей",
    "expedition_member": "участники экспедиций",
    "app_user": "пользователи сайта",
    "user_role": "роли пользователей",
    "change_log": "журнал изменений",
}


@dataclass
class TableDifference:
    """Чем таблица в текущей базе отличается от свежего импорта Excel."""

    table: str
    only_old: int  # записей только в текущей базе: пропадут при импорте
    only_new: int  # записей только в Excel: удалены или изменены в базе, импорт их вернёт

    def describe(self) -> str:
        """Строка для человека: «особи (specimen): 1 — только в текущей базе (пропадут)»."""
        parts = []
        if self.only_old:
            parts.append(f"{self.only_old} — только в текущей базе (пропадут при импорте)")
        if self.only_new:
            parts.append(f"{self.only_new} — только в Excel (удалены или изменены в базе)")
        return f"{TABLE_TITLES.get(self.table, self.table)} ({self.table}): " + "; ".join(parts)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Колонки таблицы без собственного номера записи (одиночный INTEGER PRIMARY KEY).

    Составной ключ (sample_shipment: sample_id + lab_id) — это ссылки, их сравниваем.
    """
    rows = conn.execute(
        "SELECT name, pk FROM pragma_table_info(?) ORDER BY cid", (table,)
    ).fetchall()
    key_columns = [r["name"] for r in rows if r["pk"]]
    own_id = key_columns[0] if len(key_columns) == 1 else None
    return [r["name"] for r in rows if r["name"] != own_id]


# ссылки без внешнего ключа: таблица → (колонка с именем таблицы, колонка с номером)
POLYMORPHIC_LINKS = {"data_issue": ("table_name", "record_id")}


@dataclass(frozen=True)
class _ForeignKey:
    parent: str
    columns: tuple[str, ...]  # колонки в этой таблице
    parent_columns: tuple[str, ...]  # на какие колонки родителя ссылаются


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[_ForeignKey]:
    """Внешние ключи таблицы; составной (trawling_id, event_id) — одним ключом."""
    groups: dict[int, list] = defaultdict(list)
    for r in conn.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,)):
        groups[r["id"]].append(r)
    keys = []
    for rows in groups.values():
        rows.sort(key=lambda r: r["seq"])
        parent = rows[0]["table"]
        parent_columns = tuple(r["to"] for r in rows)
        if None in parent_columns:  # REFERENCES parent без колонок — на первичный ключ
            parent_columns = tuple(_primary_key(conn, parent))
        keys.append(_ForeignKey(parent, tuple(r["from"] for r in rows), parent_columns))
    return sorted(keys, key=lambda k: (k.parent, k.columns))


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute("SELECT name, pk FROM pragma_table_info(?)", (table,)).fetchall()
    return [r["name"] for r in sorted(rows, key=lambda r: r["pk"]) if r["pk"]]


def _own_id_column(conn: sqlite3.Connection, table: str) -> str | None:
    """Собственный номер записи — одиночный первичный ключ (или None)."""
    key = _primary_key(conn, table)
    return key[0] if len(key) == 1 else None


def _unique_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, ...]]:
    """Наборы колонок UNIQUE-ограничений (и составного PRIMARY KEY), кроме тех, где есть
    собственный номер записи: он при сдвиге номеров другой и ничего не идентифицирует."""
    own_id = _own_id_column(conn, table)
    result = []
    for index in conn.execute("SELECT * FROM pragma_index_list(?)", (table,)):
        if not index["unique"] or index["partial"]:
            continue
        names = tuple(
            r["name"]
            for r in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index["name"],)
            )
        )
        if names and None not in names and own_id not in names:
            result.append(names)
    return sorted(result)


def _table_order(conn: sqlite3.Connection, tables: list[str]) -> list[str]:
    """Таблицы по порядку: сначала те, на которые ссылаются (топологическая сортировка)."""
    parents = {t: {k.parent for k in _foreign_keys(conn, t)} for t in tables}
    for table, table_parents in parents.items():
        if table in table_parents:
            raise sqlite3.DatabaseError(
                f"таблица {table} ссылается сама на себя — сравнить по содержимому нельзя"
            )
    for table in POLYMORPHIC_LINKS:  # ссылается на любую таблицу — в самый конец
        if table in parents:
            parents[table] = set(tables) - {table}
    order: list[str] = []
    while len(order) < len(tables):
        ready = sorted(t for t in tables if t not in order and parents[t] <= set(order))
        if not ready:
            raise sqlite3.DatabaseError("в схеме циклические внешние ключи — сравнить нельзя")
        order += ready
    return order


@dataclass
class _TableInfo:
    """Что нужно знать о таблице для сравнения (одинаково в обеих базах)."""

    name: str
    plain: list[str]  # обычные колонки: без своего номера и без колонок-ссылок
    keys: list[_ForeignKey]
    link: tuple[str, str] | None  # ссылка без внешнего ключа (data_issue)
    # устойчивые ключи: (номера колонок в plain, номера ссылок в refs) для каждого UNIQUE
    unique: list[tuple[tuple[int, ...], tuple[int, ...]]]

    @classmethod
    def read(cls, conn: sqlite3.Connection, table: str) -> _TableInfo:
        keys = _foreign_keys(conn, table)
        link = POLYMORPHIC_LINKS.get(table)
        ref_columns = {c for k in keys for c in k.columns} | ({link[1]} if link else set())
        plain = [c for c in _columns(conn, table) if c not in ref_columns]
        unique = []
        for names in _unique_columns(conn, table):
            plain_idx = tuple(i for i, c in enumerate(plain) if c in names)
            # ссылки, которые задевают ключ (составная (trawling_id, event_id) — целиком)
            ref_idx = tuple(i for i, k in enumerate(keys) if set(k.columns) & set(names))
            unique.append((plain_idx, ref_idx))
        return cls(table, plain, keys, link, unique)


@dataclass(frozen=True)
class _Record:
    rowid: int
    plain: tuple  # значения обычных колонок
    refs: tuple  # ссылки, переведённые в номера записей НОВОЙ базы
    parents: tuple = ()  # rowid родителей в СВОЕЙ базе (по внешним ключам) — для шага 3

    @property
    def content(self) -> tuple:
        return self.plain + self.refs


# ссылка, которая не совпадёт ни с одной ссылкой новой базы
_MISSING, _BROKEN = "нет в новой", "битая"


class _Side:
    """Записи одной базы для сравнения: строки таблиц и как их найти по значениям ссылок."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.rows: dict[str, list[sqlite3.Row]] = {}
        self._lookups: dict[tuple, dict[tuple, int]] = {}

    def load(self, table: str) -> list[sqlite3.Row]:
        # имя таблицы — из схемы базы (sqlite_master), а не от пользователя
        self.rows[table] = self.conn.execute(f"SELECT rowid AS _rowid_, * FROM {table}").fetchall()
        return self.rows[table]

    def lookup(self, table: str, columns: tuple[str, ...]) -> dict[tuple, int]:
        """Значения колонок → rowid записи (для перевода ссылок; таблица уже загружена).

        Словарь строится один раз на таблицу и набор колонок — не запрос на каждую строку.
        """
        if (table, columns) not in self._lookups:
            self._lookups[table, columns] = {
                tuple(r[c] for c in columns): r["_rowid_"] for r in self.rows[table]
            }
        return self._lookups[table, columns]

    def lookup_by_id(self, table) -> dict[tuple, int] | None:
        """Собственный номер записи → rowid; None — таблицы нет или у неё нет номера.

        У sample_shipment своего номера нет (ключ — sample_id + lab_id): ссылку
        data_issue на отправку переводить не во что, record_id сравнивается числом
        (сейчас у таких записей журнала record_id = NULL).
        """
        if table not in self.rows:
            return None
        own_id = _own_id_column(self.conn, table)
        return self.lookup(table, (own_id,)) if own_id else None


def _records(
    side: _Side, info: _TableInfo, mapping: dict[str, dict[int, int]] | None
) -> list[_Record]:
    """Записи таблицы; ссылки заменены на номер записи в НОВОЙ базе.

    mapping — соответствие «rowid старой базы → rowid новой» по уже сравненным
    таблицам (для старой базы); None — это сама новая база, ссылки остаются как есть.
    Ссылка на запись, у которой нет пары в новой базе, — ('нет в новой', rowid).
    """
    table = info.name

    def translate(parent: str, rowid: int):
        if parent == table:  # запись ссылается на запись той же таблицы
            raise sqlite3.DatabaseError(
                f"запись {table} ссылается на запись той же таблицы — сравнить нельзя"
            )
        if mapping is None:
            return rowid
        return mapping[parent].get(rowid, (_MISSING, rowid))

    lookups = [side.lookup(k.parent, k.parent_columns) for k in info.keys]
    link = info.link
    if link:  # таблицы, на которые ссылается data_issue: по одному словарю на таблицу
        targets = {name: side.lookup_by_id(name) for name in {r[link[0]] for r in side.rows[table]}}

    result = []
    for row in side.rows[table]:
        refs, parents = [], []
        for key, lookup in zip(info.keys, lookups, strict=True):
            values = tuple(row[c] for c in key.columns)
            parents.append(None if None in values else lookup.get(values))
            if None in values:  # ссылки нет (траление неизвестно) — NULL
                refs.append(None)
            elif values in lookup:
                refs.append(translate(key.parent, lookup[values]))
            else:  # битая ссылка (при foreign_keys = ON не бывает) — сравниваем как есть
                refs.append((_BROKEN, values))
        if link:
            parent, record_id = row[link[0]], row[link[1]]
            found = targets[parent]
            if record_id is None or found is None:  # нет ссылки или у таблицы нет номера
                refs.append(record_id)
            elif (record_id,) in found:
                refs.append(translate(parent, found[(record_id,)]))
            else:  # запись, о которой проблема, уже удалена
                refs.append((_BROKEN, record_id))
        plain = tuple(row[c] for c in info.plain)
        result.append(_Record(row["_rowid_"], plain, tuple(refs), tuple(parents)))
    return result


def _identifies(value) -> bool:
    """Годится ли значение в устойчивый ключ: NULL и ссылка «без пары» — нет."""
    return value is not None and not isinstance(value, tuple)


def _pair_unique(left_old: dict, left_new: dict, key_of) -> dict[int, int]:
    """Пары изменённых записей, у которых ключ key_of(запись) ровно один в каждой базе."""
    groups: dict[tuple, tuple[list, list]] = defaultdict(lambda: ([], []))
    for side, records in ((0, left_old), (1, left_new)):
        for record in records.values():
            key = key_of(record)
            if key is not None:
                groups[key][side].append(record.rowid)
    return {olds[0]: news[0] for olds, news in groups.values() if len(olds) == 1 and len(news) == 1}


def _similarity(a: _Record, b: _Record) -> tuple[int, int]:
    """(сколько непустых полей совпадает, сколько отличается) — по всему содержимому."""
    same = differ = 0
    for x, y in zip(a.content, b.content, strict=True):
        if x != y:
            differ += 1
        elif x is not None:
            same += 1
    return same, differ


def _similar_ok(same: int, differ: int) -> bool:
    """Шаг 4: запись узнаётся по сходству — много общего (≥ 3 полей) и втрое больше,
    чем отличий. Справочник с одним-двумя полями так не узнать — только по детям."""
    return same >= 3 and same >= 3 * differ


def _evidence_ok(same: int, differ: int) -> bool:
    """Шаг 3: дети те же — достаточно, чтобы сама запись отличалась немногим
    (переименовали лабораторию: 0 общих полей, 1 отличие — да; другая особь с теми же
    образцами, но другими промерами, полом и выездом — нет)."""
    return differ <= max(1, same // 3)


# больше пар сравнивать «каждую с каждой» не будем — только внутри одинаковых ссылок
_PAIR_LIMIT = 200_000


def _pair_similar(left_old: dict, left_new: dict) -> dict[int, int]:
    """Шаг 4: пары изменённых записей по сходству (_similar_ok), взаимно единственные лучшие.

    Сравниваются все оставшиеся записи таблицы; если их слишком много (_PAIR_LIMIT) —
    только записи с одинаковыми ссылками (образцы одной особи).
    """
    groups: dict[object, tuple[list, list]] = defaultdict(lambda: ([], []))
    by_refs = len(left_old) * len(left_new) > _PAIR_LIMIT
    for side, records in ((0, left_old), (1, left_new)):
        for record in records.values():
            groups[record.refs if by_refs else None][side].append(record)
    pairs = {}
    for olds, news in groups.values():
        best_new: dict[int, list] = {}  # rowid старой → [лучший счёт, кандидаты новой]
        best_old: dict[int, list] = {}
        for o in olds:
            for n in news:
                same, differ = _similarity(o, n)
                if not _similar_ok(same, differ):
                    continue
                score = same - differ
                for best, mine, other in (
                    (best_new, o.rowid, n.rowid),
                    (best_old, n.rowid, o.rowid),
                ):
                    if mine not in best or score > best[mine][0]:
                        best[mine] = [score, [other]]
                    elif score == best[mine][0]:
                        best[mine][1].append(other)
        for o_id, (_, candidates) in best_new.items():
            if len(candidates) == 1 and best_old[candidates[0]][1] == [o_id]:
                pairs[o_id] = candidates[0]
    return pairs


def _pair_by_children(
    left_old: dict, left_new: dict, old_children: dict, new_children: dict
) -> dict[int, int]:
    """Шаг 3: пары по одинаковому набору детей (ровно один такой в каждой базе)."""
    groups: dict[frozenset, tuple[list, list]] = defaultdict(lambda: ([], []))
    for side, records, children in ((0, left_old, old_children), (1, left_new, new_children)):
        for record in records.values():
            signature = children.get(record.rowid)
            if signature:
                groups[signature][side].append(record)
    pairs = {}
    for olds, news in groups.values():
        if len(olds) == 1 and len(news) == 1 and _evidence_ok(*_similarity(olds[0], news[0])):
            pairs[olds[0].rowid] = news[0].rowid
    return pairs


def _children(
    table: str, infos: dict[str, _TableInfo], records: dict[str, list[_Record]]
) -> dict[int, frozenset]:
    """rowid записи таблицы → набор её детей по содержимому (из первого прохода).

    Ребёнок — запись другой таблицы со ссылкой на эту; ссылка на саму запись в его
    содержимом заменена на «*», остальные ссылки уже переведены.
    """
    found: dict[int, Counter] = defaultdict(Counter)
    for child, info in infos.items():
        for i, key in enumerate(info.keys):
            if key.parent != table:
                continue
            for record in records[child]:
                parent = record.parents[i]
                if parent is not None:
                    refs = record.refs[:i] + ("*",) + record.refs[i + 1 :]
                    found[parent][child, i, record.plain + refs] += 1
    return {rowid: frozenset(counter.items()) for rowid, counter in found.items()}


def _match(
    info: _TableInfo,
    old_records: list[_Record],
    new_records: list[_Record],
    children: tuple[dict, dict] | None = None,
) -> dict:
    """Соответствие rowid старой → новой (шаги — в пояснении над TABLE_TITLES).

    children — наборы детей (старая база, новая) из первого прохода; None — первый проход.
    """
    by_content: dict[tuple, list[int]] = defaultdict(list)
    for record in sorted(new_records, key=lambda r: r.rowid):
        by_content[record.content].append(record.rowid)
    mapping: dict[int, int] = {}
    left_old = {}
    for record in sorted(old_records, key=lambda r: r.rowid):
        if by_content.get(record.content):
            mapping[record.rowid] = by_content[record.content].pop(0)
        else:
            left_old[record.rowid] = record
    left_new_ids = {rowid for rowids in by_content.values() for rowid in rowids}
    left_new = {r.rowid: r for r in new_records if r.rowid in left_new_ids}

    def take(pairs: dict[int, int]) -> None:
        for old_id, new_id in pairs.items():
            mapping[old_id] = new_id
            del left_old[old_id], left_new[new_id]

    # шаг 2: по устойчивому ключу (каждое UNIQUE-ограничение по очереди)
    for plain_idx, ref_idx in info.unique:

        def key_of(record: _Record, plain_idx=plain_idx, ref_idx=ref_idx):
            key = tuple(record.plain[i] for i in plain_idx) + tuple(record.refs[i] for i in ref_idx)
            return key if all(_identifies(v) for v in key) else None

        take(_pair_unique(left_old, left_new, key_of))
    if children and left_old and left_new:  # шаг 3: по детям
        take(_pair_by_children(left_old, left_new, *children))
    if left_old and left_new:  # шаг 4: по сходству
        take(_pair_similar(left_old, left_new))
    return mapping


def _compare_pass(
    sides: tuple[_Side, _Side],
    order: list[str],
    infos: dict[str, _TableInfo],
    first: dict[str, tuple[list, list]] | None,
) -> tuple[dict[str, tuple[list, list]], dict[str, tuple[int, int]]]:
    """Один проход по таблицам: записи обеих баз и числа расхождений по таблицам.

    first — записи первого прохода (для шага 3 «по детям»); None — это первый проход.
    """
    old_side, new_side = sides
    mapping: dict[str, dict[int, int]] = {}  # таблица → rowid старой → rowid новой
    records, counts = {}, {}
    for table in order:
        info = infos[table]
        old_records = _records(old_side, info, mapping)
        new_records = _records(new_side, info, None)
        children = None
        if first is not None:
            children = (
                _children(table, infos, {t: r[0] for t, r in first.items()}),
                _children(table, infos, {t: r[1] for t, r in first.items()}),
            )
        mapping[table] = _match(info, old_records, new_records, children)
        records[table] = (old_records, new_records)
        old_count = Counter(r.content for r in old_records)
        new_count = Counter(r.content for r in new_records)
        counts[table] = (
            sum((old_count - new_count).values()),
            sum((new_count - old_count).values()),
        )
    return records, counts


def compare_databases(old: sqlite3.Connection, new: sqlite3.Connection) -> list[TableDifference]:
    """Сравнить две базы по содержимому (см. пояснение выше). Пустой список — базы совпадают
    с точностью до номеров записей.

    Расхождение — запись, у которой в другой базе нет записи с тем же содержимым и теми же
    (по содержимому) ссылками: добавленная, удалённая, изменённая или привязанная к другой
    записи. Если у баз разная структура (нет таблицы, другие колонки или ссылки) или запись
    ссылается на запись той же таблицы — sqlite3.DatabaseError: сравнить нельзя, такую
    базу импорт без --force не заменяет.
    """
    old_tables, new_tables = set(list_tables(old)), set(list_tables(new))
    if old_tables != new_tables:
        names = ", ".join(sorted(old_tables ^ new_tables))
        raise sqlite3.DatabaseError(
            f"структура базы отличается от db/schema.sql (таблицы: {names})"
        )
    sides = (_Side(old), _Side(new))
    order = _table_order(new, sorted(new_tables))
    infos = {}
    for table in order:
        infos[table] = _TableInfo.read(new, table)
        if _TableInfo.read(old, table) != infos[table]:
            raise sqlite3.DatabaseError(f"структура таблицы {table} отличается от db/schema.sql")
        for side in sides:
            side.load(table)
    records, counts = _compare_pass(sides, order, infos, None)
    if any(any(c) for c in counts.values()):
        # второй проход: изменённые записи узнаются и по детям из первого прохода
        _, counts = _compare_pass(sides, order, infos, records)
    return [
        TableDifference(table, *counts[table]) for table in sorted(counts) if any(counts[table])
    ]


@dataclass
class PreparedImport:
    """Новая база уже собрана во временном файле; старая ещё не тронута.

    Дальше вызывающий код решает (CLI спрашивает, веб покажет кнопку):
    replace_database() — заменить старую базу новой, discard() — отказаться.
    Удобнее всего через with: при выходе временный файл удаляется сам.
    """

    db_path: Path  # рабочая база
    new_path: Path  # новая база из Excel (временный файл рядом с рабочей)
    summary: dict[str, int]  # сколько чего загружено
    exists: bool  # рабочая база уже есть — перед заменой нужна резервная копия
    differences: list[TableDifference] = field(default_factory=list)
    unreadable: str | None = None  # текущую базу не удалось сравнить: текст ошибки

    @property
    def needs_force(self) -> bool:
        """Заменять базу можно только с явным согласием (--force в CLI)."""
        return bool(self.differences) or self.unreadable is not None

    def replace_database(self) -> None:
        """Атомарно заменить рабочую базу новой: старая исчезает только в этот момент.

        Перед заменой:
        - ещё раз проверить пометку «рабочая база» (её могли поставить после
          prepare_import) — LiveDatabaseError;
        - закрыть журнал WAL обеих баз (db.close_wal): рядом с новым файлом не должен
          остаться -wal старого; если старую базу держит открытой другая программа
          (сайт) — DatabaseBusyError, база не меняется.
        Остаётся окно в доли миллисекунды между close_wal и os.replace: если другая
        программа откроет и изменит старую базу именно в этот миг, её -wal окажется рядом
        с новым файлом. Поэтому перед import сайт и все программы, работающие с базой,
        нужно остановить (файл-замок решим на этапе D4 вместе с systemd).
        """
        ensure_not_live(self.db_path)
        close_wal(self.new_path)
        close_wal(self.db_path)
        os.replace(self.new_path, self.db_path)

    def discard(self) -> None:
        """Удалить временный файл и его -wal/-shm (после замены их уже нет — это не ошибка)."""
        remove_temp_database(self.new_path)

    def __enter__(self) -> PreparedImport:
        return self

    def __exit__(self, *exc_info) -> None:
        self.discard()


def prepare_import(
    xlsx_path: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
    compare: bool = True,
) -> PreparedImport:
    """Прочитать книгу, собрать новую базу во временном файле и сравнить с текущей.

    Текущая база не меняется. Рабочую базу (go-live) пересоздавать нельзя ни при каких
    флагах — LiveDatabaseError сразу, книга даже не читается.
    Что видно при сравнении (compare=True):
    - любая запись, добавленная, удалённая или изменённая не через Excel, в любой
      таблице — в том числе исправленное значение в записи из Excel (TL особи)
      и вручную закрытая проблема журнала;
    Что НЕ видно:
    - ПОЧЕМУ записи отличаются: ручная правка, исправленный Excel или новая версия
      программы импорта выглядят одинаково — в любом случае нужен --force;
    - правки, сделанные после сравнения и до замены файла (окно — доли секунды;
      во время импорта с базой никто не должен работать).
    """
    db_path = Path(db_path)
    ensure_not_live(db_path)
    expeditions = read_workbook(xlsx_path)
    new_path = db_path.with_name(db_path.name + ".tmp")
    summary = _build_database(expeditions, new_path)
    prepared = PreparedImport(db_path, new_path, summary, exists=db_path.exists())
    if not (compare and prepared.exists):
        return prepared
    try:
        prepared.differences = _compare_files(db_path, new_path)
    except sqlite3.Error as error:  # не база SQLite или другая схема
        prepared.unreadable = str(error)
    except BaseException:
        prepared.discard()
        raise
    return prepared


def _compare_files(old_path: Path, new_path: Path) -> list[TableDifference]:
    """compare_databases для двух файлов; обе базы открываются только на чтение."""
    new = connect_read_only(new_path)
    try:
        old = connect_read_only(old_path)
        try:
            return compare_databases(old, new)
        finally:
            old.close()
    finally:
        new.close()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def import_workbook(
    xlsx_path: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Пересоздать базу и импортировать книгу. Вернуть {таблица: число строк}.

    Старая база заменяется БЕЗ сравнения и без вопросов (но рабочую — не заменяет:
    LiveDatabaseError). Команда import вместо этого вызывает prepare_import(), смотрит
    differences и делает резервную копию.
    """
    with prepare_import(xlsx_path, db_path, compare=False) as prepared:
        prepared.replace_database()
    return prepared.summary
