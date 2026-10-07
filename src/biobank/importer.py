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
from dataclasses import dataclass, field
from pathlib import Path

from biobank.checks import collapse_issues, run_checks
from biobank.db import (
    DEFAULT_DB_PATH,
    connect,
    connect_read_only,
    create_database,
    list_tables,
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
    market_city,
    parse_catch_counts,
    parse_catch_note,
    parse_histology,
    parse_label,
    parse_labs,
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

SMEAR_PRESERVATIVE = "этанол 96% (фиксация)"


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


def _whole_text(spec: SpecimenRow) -> str | None:
    for key in ("dna", "histo", "smear", "gut", "tl", "sl", "mass"):
        value = spec.values.get(key)
        if is_whole_specimen(value):
            return str(value)
    return None


def _specimen_samples(sheet: SheetData, spec: SpecimenRow, label: str) -> list[tuple]:
    """Образцы особи: [(SampleSpec, исходное значение, [лаборатории])]."""
    values = spec.values
    samples: list[tuple] = []
    whole = _whole_text(spec)
    if whole:
        preservative = "этанол" if "спирт" in whole.lower() else None
        samples.append((SampleSpec("целая особь", None, label, preservative), whole, []))

    if spec.label and spec.label.series == "mr":
        for sample in parse_shrimp_samples(values.get("histo"), values.get("smear"), label):
            raw = values.get("histo") if sample.sample_type == "гистология" else values.get("smear")
            samples.append((sample, raw, []))
        return samples

    extra_labs = []
    for comment in spec.comments.values():
        extra_labs += [lab for lab in parse_labs(comment) if lab == LAB_VN_COMPANY]

    dna = values.get("dna")
    if not is_blank(dna) and not is_whole_specimen(dna):
        parsed = parse_label(dna)
        samples.append(
            (SampleSpec("ДНК", None, parsed.label if parsed else str(dna)), dna,
             sheet.dna_labs + extra_labs)
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
            (SampleSpec("кишечник", "кишечник", parsed.label if parsed else str(gut)), gut, [])
        )

    histo, smear = values.get("histo"), values.get("smear")
    if smear_looks_like_histology(smear):
        sheet.issues.append(
            Issue("формат",
                  "В колонке «мазок крови» записан орган — колонки гистологии и мазка "
                  "перепутаны; импортировано как гистология + мазок",
                  sheet.name, spec.cells.get("smear"), label, str(smear), "sample")
        )  # fmt: skip
        histo, smear = smear, histo

    result = parse_histology(histo, label)
    for warning in result.warnings:
        category = "номера" if "номер" in warning else "формат"
        sheet.issues.append(
            Issue(category, warning, sheet.name, spec.cells.get("histo"), label,
                  None if histo is None else str(histo), "sample")
        )  # fmt: skip
    samples += [(sample, histo, []) for sample in result.samples]

    if (
        not is_blank(smear)
        and not is_whole_specimen(smear)
        and not is_dead_text(smear)
        and not str(smear).strip().lower().startswith("нет")
    ):
        parsed = parse_label(smear)
        smear_label = parsed.label if parsed else str(smear)
        samples.append(
            (SampleSpec("мазок крови", None, smear_label, SMEAR_PRESERVATIVE), smear,
             sheet.smear_labs)
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
        "object_label, raw_value, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (i.category, i.table_name, i.record_id, i.sheet, i.cell, i.object_label,
             i.raw_value, i.description)
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
    """
    path.unlink(missing_ok=True)
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
        path.unlink(missing_ok=True)
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
# могут отличаться, хотя содержание то же). Ссылки на другие таблицы (event_id,
# specimen_id…) сравниваем — импорт нумерует записи всегда в одном порядке.
#
# Записи сравниваются как мультимножества (collections.Counter — «словарь
# запись → сколько раз встречается»): две одинаковые строки считаются двумя.
# - запись есть только в старой базе → её добавили или изменили не через Excel,
#   при импорте она ПРОПАДЁТ;
# - запись есть только в новой базе → её удалили или изменили в старой базе,
#   импорт вернёт её из Excel.
# Изменённая запись попадает в обе группы: старая версия пропадёт, версия из Excel вернётся.

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


def _records(conn: sqlite3.Connection, table: str, columns: list[str]) -> Counter:
    # имена таблицы и колонок — из схемы базы (sqlite_master), а не от пользователя
    sql = f"SELECT {', '.join(columns)} FROM {table}"
    return Counter(tuple(row) for row in conn.execute(sql))


def compare_databases(old: sqlite3.Connection, new: sqlite3.Connection) -> list[TableDifference]:
    """Сравнить две базы по содержимому (см. пояснение выше). Пустой список — совпадают.

    Если у баз разная структура (нет таблицы, другие колонки) — sqlite3.DatabaseError:
    сравнивать нечего, такую базу импорт без --force не заменяет.
    """
    old_tables, new_tables = set(list_tables(old)), set(list_tables(new))
    if old_tables != new_tables:
        names = ", ".join(sorted(old_tables ^ new_tables))
        raise sqlite3.DatabaseError(
            f"структура базы отличается от db/schema.sql (таблицы: {names})"
        )
    differences = []
    for table in sorted(new_tables):
        columns = _columns(new, table)
        if _columns(old, table) != columns:
            raise sqlite3.DatabaseError(f"структура таблицы {table} отличается от db/schema.sql")
        old_records, new_records = _records(old, table, columns), _records(new, table, columns)
        only_old = sum((old_records - new_records).values())
        only_new = sum((new_records - old_records).values())
        if only_old or only_new:
            differences.append(TableDifference(table, only_old, only_new))
    return differences


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
        """Атомарно заменить рабочую базу новой: старая исчезает только в этот момент."""
        os.replace(self.new_path, self.db_path)

    def discard(self) -> None:
        """Удалить временный файл (после replace_database его уже нет — это не ошибка)."""
        self.new_path.unlink(missing_ok=True)

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

    Текущая база не меняется. Что видно при сравнении (compare=True):
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

    Старая база заменяется БЕЗ проверки и без вопросов. Команда import вместо этого
    вызывает prepare_import(), смотрит differences и делает резервную копию.
    """
    with prepare_import(xlsx_path, db_path, compare=False) as prepared:
        prepared.replace_database()
    return prepared.summary
