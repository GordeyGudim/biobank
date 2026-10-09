"""Ввод новых данных: экспедиции, места, выезды, траления, вода, особи, образцы, прилов.

Excel-импорт — разовый перенос накопленных данных; новые экспедиции учёные вносят
этими функциями (сейчас из командной строки, потом — из форм веб-интерфейса).

Правила те же, что у правок в edit.py:
- проверка и запись — в одной транзакции BEGIN IMMEDIATE (edit.write_transaction);
  apply_edit() перед вводом делает резервную копию файла базы;
- проверки при вводе двух уровней:
  * жёсткие — сохранить нельзя (TL ≤ 0, траление чужого выезда, занятая метка,
    несуществующий выезд…): EditError с понятным текстом, в базу ничего не записано;
  * мягкие — запись сохраняется (SL > TL, суффикс номера не совпадает с видом…),
    а проблема попадает в журнал data_issue теми же проверками, что и у импорта
    (checks.run_checks в той же транзакции); тексты — в EditResult.warnings;
- функции принимают подключение и простые значения (числа можно строкой «29,3» —
  как из формы), ничего не печатают и возвращают EditResult с id созданной записи.

Выезды, введённые вручную, — без листа Excel: source_sheet = NULL. Пометку
«ввод вручную» туда не пишем: проверки сворачивают одинаковые проблемы одного
«листа» в одну запись (checks.collapse_issues), и все ручные выезды слились бы в один лист.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from biobank.checks import (
    DESC_SUFFIX,
    FISH_LIMITS,
    MeasurementLimits,
    measurement_problems,
    run_checks,
    suffix_mismatch,
)
from biobank.edit import (
    CONFIDENCES,
    EditError,
    EditResult,
    NotFoundError,
    event_title,
    find_species,
    find_specimen,
    write_transaction,
)
from biobank.importer import CAPTURE_METHODS, SAME_SITE_KM, SITE_TYPE
from biobank.parsing import (
    SUFFIXES,
    distance_km,
    is_blank,
    normalize_city,
    normalize_space,
    parse_date,
    parse_label,
    parse_number,
    parse_time,
)

SITE_TYPES = tuple(SITE_TYPE.values())  # 'участок реки', 'рынок', 'аквахозяйство', 'точка рыбака'
SAMPLE_TYPES = ("ДНК", "гистология", "мазок крови", "кишечник", "целая особь", "другое")
POINT_TYPES = ("старт", "финиш", "точка")
SEXES = ("самка", "самец")
# первое слово (в нижнем регистре) → как записано при импорте
COORD_SOURCES = {"garmin": "Garmin", "google": "Google Maps"}
GEAR_TYPE = "трал"

# Числовые колонки замера воды, которые можно передать в add_water_measurement,
# → (название в сообщениях, наименьшее, наибольшее значение)
WATER_NUMBERS = {
    "lat": ("Широта", -90, 90),
    "lon": ("Долгота", -180, 180),
    "depth_m": ("Глубина", 0, None),
    "o2_pct": ("O2", 0, None),
    "ppm": ("ppm", 0, None),
    "conductivity_ms": ("mS", 0, None),
    "conductivity_us": ("uS", 0, None),
    "ph": ("pH", 0, 14),
    "water_temp_c": ("t воды", None, None),
}

# Промеры особи: колонка → как называть в сообщениях
MEASUREMENTS = {"tl_cm": "TL", "sl_cm": "SL", "mass_g": "m", "mass_gutted_g": "m без внутр."}


# ---------------------------------------------------------------------------
# Разбор введённых значений (чистые функции)
# ---------------------------------------------------------------------------


def clean_text(value) -> str | None:
    """Текст из формы: лишние пробелы убрать, пустая строка — None.

    >>> clean_text("  на правом   берегу "), clean_text("   "), clean_text(None)
    ('на правом берегу', None, None)
    """
    if value is None:
        return None
    text = normalize_space(str(value))
    return text or None


def parse_entry_date(value, what: str = "Дата", today: dt.date | None = None) -> str | None:
    """Дата из формы → 'ГГГГ-ММ-ДД'. Понимает ISO и полевую запись «дд.мм.гг(гг)».

    Пусто — None. Дата в будущем — ошибка: данные вносят после возвращения из экспедиции.

    >>> parse_entry_date("2026-08-11"), parse_entry_date("11.08.2026"), parse_entry_date("")
    ('2026-08-11', '2026-08-11', None)
    >>> parse_entry_date("5.11.26", today=dt.date(2026, 12, 1))
    '2026-11-05'
    """
    text = clean_text(value)
    if text is None:
        return None
    try:
        date = dt.date.fromisoformat(text)
    except ValueError:
        iso = parse_date(text)
        if iso is None or not text[:1].isdigit():
            raise EditError(
                f"{what} «{text}» не распознана. Пишите ГГГГ-ММ-ДД (2026-11-05) "
                "или ДД.ММ.ГГГГ (05.11.2026)."
            ) from None
        date = dt.date.fromisoformat(iso)
    if date > (today or dt.date.today()):
        raise EditError(f"{what} {date.isoformat()} ещё не наступила — проверьте год и месяц.")
    return date.isoformat()


def parse_entry_number(
    value, what: str, *, minimum: float | None = None, positive: bool = False,
    maximum: float | None = None,
) -> float | None:  # fmt: skip
    """Число из формы: 29.3, «29,3», «20 min»; пусто или «-» — None.

    minimum — не меньше этого; positive — строго больше 0; maximum — не больше.
    Нарушение — EditError (жёсткая ошибка, сохранить нельзя).

    >>> parse_entry_number("29,3", "TL"), parse_entry_number("-", "TL"), parse_entry_number(5, "m")
    (29.3, None, 5.0)
    >>> parse_entry_number("0", "TL", positive=True)
    Traceback (most recent call last):
    ...
    biobank.edit.EditError: TL должна быть больше 0 (введено 0).
    """
    if isinstance(value, str) and is_blank(value.strip()):
        return None
    parsed = parse_number(value)
    if parsed.warning or (parsed.value is None and value is not None):
        raise EditError(f"{what}: «{value}» — не число.")
    number = parsed.value
    if number is None:
        return None
    if positive and number <= 0:
        raise EditError(f"{what} должна быть больше 0 (введено {number:g}).")
    if minimum is not None and number < minimum:
        raise EditError(f"{what} не может быть меньше {minimum:g} (введено {number:g}).")
    if maximum is not None and number > maximum:
        raise EditError(f"{what} не может быть больше {maximum:g} (введено {number:g}).")
    return number


def parse_entry_time(value, what: str) -> str | None:
    """Время из формы → 'ЧЧ:ММ'. Понимает «9:30» и «9.30».

    >>> parse_entry_time("9.30", "Время старта"), parse_entry_time("", "Время старта")
    ('09:30', None)
    """
    text = clean_text(value)
    if text is None:
        return None
    parsed = parse_time(text)
    hours = int(parsed.value[:2]) if parsed.value else 99
    minutes = int(parsed.value[3:]) if parsed.value else 99
    if parsed.value is None or hours > 23 or minutes > 59:
        raise EditError(f"{what}: «{text}» — не время. Пишите ЧЧ:ММ, например 09:30.")
    return parsed.value


def normalize_gear_size(text) -> str | None:
    """Размер ячеи или сети как при импорте: '3,5 х 3,5' → '3,5x3,5' (латинская x).

    >>> normalize_gear_size("3,5 х 3,5"), normalize_gear_size("15X20"), normalize_gear_size("")
    ('3,5x3,5', '15x20', None)
    """
    text = clean_text(text)
    if text is None:
        return None
    return text.replace(" ", "").replace("х", "x").replace("Х", "x").replace("X", "x")


def default_site_name(
    site_type: str, city: str | None, lat: float | None = None, lon: float | None = None
) -> str | None:
    """Название места по правилам импорта; None — название нужно указать самому.

    Участку реки и рынку нужен город; точке рыбака и аквахозяйству — нет (как при импорте).

    >>> default_site_name("участок реки", "Кантхо"), default_site_name("рынок", "Бенче")
    ('Участок у Кантхо', 'Рынок Бенче')
    >>> default_site_name("точка рыбака", "Кантхо", 9.5187059, 106.2082146)
    'Точка рыбака у Кантхо (9.5187, 106.2082)'
    >>> default_site_name("точка рыбака", None, 9.5187059, 106.2082146)
    'Точка рыбака (9.5187, 106.2082)'
    >>> default_site_name("участок реки", None) is None
    True
    """
    where = f" у {city}" if city else ""
    if site_type == "участок реки":
        return f"Участок у {city}" if city else None
    if site_type == "рынок":
        return f"Рынок {city}" if city else None
    if site_type == "аквахозяйство":
        return f"Аквахозяйство{where}"
    coords = f" ({lat:.4f}, {lon:.4f})" if lat is not None and lon is not None else ""
    return f"Точка рыбака{where}{coords}"


@dataclass
class SpecimenCheck:
    """Итог проверки особи при вводе: жёсткие ошибки и мягкие предупреждения."""

    errors: list[str] = field(default_factory=list)  # сохранить нельзя
    warnings: list[str] = field(default_factory=list)  # сохранить можно, проблема → журнал


def validate_specimen(
    tl_cm: float | None,
    sl_cm: float | None,
    mass_g: float | None,
    mass_gutted_g: float | None,
    *,
    suffix: str | None = None,
    species_code: str | None = None,
    limits: MeasurementLimits = FISH_LIMITS,
) -> SpecimenCheck:
    """Проверить промеры (и суффикс номера) особи до записи в базу.

    Мягкие проверки — те же функции, что и в журнале проблем (checks.measurement_problems,
    checks.suffix_mismatch): пороги в одном месте, предупреждение при вводе и запись
    data_issue после сохранения совпадают. suffix=None — суффикс не проверять
    (например, при исправлении промеров).

    >>> validate_specimen(30.0, 27.0, 150.0, 140.0, suffix="pc", species_code="pc")
    SpecimenCheck(errors=[], warnings=[])
    >>> validate_specimen(10.2, 10.3, 11.0, 8.6).warnings
    ['SL больше TL']
    >>> validate_specimen(0, None, -1, None).errors
    ['TL должна быть больше 0 (введено 0).', 'm не может быть меньше 0 (введено -1).']
    >>> validate_specimen(None, None, None, None, suffix="ph", species_code="pc").warnings
    ['Суффикс в номере пробы не соответствует виду: «ph», а код вида — «pc»']
    """
    result = SpecimenCheck()
    for column, value in (("tl_cm", tl_cm), ("sl_cm", sl_cm)):
        if value is not None and value <= 0:
            result.errors.append(
                f"{MEASUREMENTS[column]} должна быть больше 0 (введено {value:g})."
            )
    for column, value in (("mass_g", mass_g), ("mass_gutted_g", mass_gutted_g)):
        if value is not None and value < 0:
            result.errors.append(
                f"{MEASUREMENTS[column]} не может быть меньше 0 (введено {value:g})."
            )
    if result.errors:
        return result
    result.warnings += measurement_problems(tl_cm, sl_cm, mass_g, mass_gutted_g, limits)
    if suffix is not None and suffix_mismatch(suffix, species_code):
        result.warnings.append(
            f"{DESC_SUFFIX}: «{suffix}», а код вида — «{species_code or 'вид не определён'}»"
        )
    return result


# ---------------------------------------------------------------------------
# Поиск записей (внутри транзакции)
# ---------------------------------------------------------------------------


def _insert(conn: sqlite3.Connection, table: str, **values) -> int:
    """INSERT одной строки; вернуть её id.

    Имена таблицы и колонок — из кода этого модуля (не от пользователя),
    значения — через «?», как в importer.ImportContext.insert.
    """
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )
    return cursor.lastrowid


def _expedition(conn: sqlite3.Connection, expedition_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT expedition_id, name, date_start, date_end FROM expedition WHERE expedition_id = ?",
        (expedition_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Экспедиции №{expedition_id} нет. Список: "
            'python -m biobank sql "SELECT expedition_id, name FROM expedition"'
        )
    return row


def _site(conn: sqlite3.Connection, site_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT site_id, name, site_type FROM sampling_site WHERE site_id = ?", (site_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Места №{site_id} нет. Список: python -m biobank report sites")
    return row


_EVENT_SQL = """
    SELECT e.event_id, e.event_date, e.source_sheet, e.site_id, e.capture_method_id,
           m.name AS method
    FROM sampling_event e JOIN capture_method m USING (capture_method_id)
    WHERE e.event_id = ?
"""


def _event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = conn.execute(_EVENT_SQL, (event_id,)).fetchone()
    if row is None:
        raise NotFoundError(
            f"Выезда №{event_id} нет. Список: python -m biobank sql "
            '"SELECT event_id, event_date, source_sheet FROM sampling_event"'
        )
    return row


def _title(event: sqlite3.Row) -> str:
    return event_title(event["source_sheet"], event["event_id"], event["event_date"])


def _method_id(conn: sqlite3.Connection, name: str) -> int:
    rows = conn.execute("SELECT capture_method_id, name FROM capture_method").fetchall()
    for row in rows:
        if row["name"] == clean_text(name):
            return row["capture_method_id"]
    names = ", ".join(r["name"] for r in rows) or ", ".join(CAPTURE_METHODS)
    raise EditError(f"Способа получения «{name}» нет. Возможные: {names}.")


def _trawling(conn: sqlite3.Connection, event: sqlite3.Row, trawl_no: int) -> int:
    """id траления с этим номером на ЭТОМ выезде — траление чужого выезда не найдётся."""
    rows = conn.execute(
        "SELECT trawling_id, trawl_no FROM trawling WHERE event_id = ? ORDER BY trawl_no",
        (event["event_id"],),
    ).fetchall()
    for row in rows:
        if row["trawl_no"] == trawl_no:
            return row["trawling_id"]
    if not rows:
        raise EditError(
            f"У выезда {_title(event)} нет тралений. Сначала: python -m biobank add-trawling …"
        )
    numbers = ", ".join(str(r["trawl_no"]) for r in rows)
    raise EditError(f"На выезде {_title(event)} нет траления {trawl_no}. Есть: {numbers}.")


def _lab_id(conn: sqlite3.Connection, name: str, create: bool = False) -> tuple[int, bool]:
    """(lab_id, создана ли сейчас). Новая лаборатория — только с create=True."""
    name = clean_text(name)
    if name is None:
        raise EditError("Укажите лабораторию.")
    row = conn.execute("SELECT lab_id FROM lab WHERE name = ?", (name,)).fetchone()
    if row:
        return row["lab_id"], False
    if not create:
        names = "; ".join(r[0] for r in conn.execute("SELECT name FROM lab ORDER BY lab_id"))
        raise EditError(
            f"Лаборатории «{name}» нет в справочнике. Есть: {names}. "
            "Новую добавьте с флагом --new-lab (проверьте, что это не опечатка)."
        )
    return _insert(conn, "lab", name=name), True


def _done(conn: sqlite3.Connection, message: str, record_id: int,
          warnings: list[str] | None = None) -> EditResult:  # fmt: skip
    """Перепроверить журнал в той же транзакции и собрать результат."""
    checks = run_checks(conn)
    return EditResult(message, checks, record_id=record_id, warnings=warnings or [])


# ---------------------------------------------------------------------------
# Экспедиции и места
# ---------------------------------------------------------------------------


def create_expedition(
    conn: sqlite3.Connection,
    name: str,
    date_start: str | None = None,
    date_end: str | None = None,
    notes: str | None = None,
) -> EditResult:
    """Новая экспедиция. Даты можно не указывать — их заполнит первый выезд (create_event)."""
    name = clean_text(name)
    if name is None:
        raise EditError("Укажите название экспедиции, например «Кантхо 2027».")
    start = parse_entry_date(date_start, "Дата начала")
    end = parse_entry_date(date_end, "Дата окончания")
    if start and end and start > end:
        raise EditError(f"Дата начала {start} позже даты окончания {end}.")
    with write_transaction(conn):
        if conn.execute("SELECT 1 FROM expedition WHERE name = ?", (name,)).fetchone():
            raise EditError(f"Экспедиция «{name}» уже есть — выберите другое название.")
        expedition_id = _insert(
            conn, "expedition", name=name, date_start=start, date_end=end, notes=clean_text(notes)
        )
        return _done(conn, f"Создана экспедиция №{expedition_id} «{name}».", expedition_id)


@dataclass(frozen=True)
class NearbySite:
    """Уже существующее место, которое, возможно, то же самое, что вводят сейчас."""

    site_id: int
    name: str
    distance_km: float | None  # None — совпало по названию, а не по расстоянию

    def describe(self) -> str:
        where = "то же название" if self.distance_km is None else f"{self.distance_km * 1000:.0f} м"
        return f"№{self.site_id} «{self.name}» ({where})"


def find_nearby_sites(
    conn: sqlite3.Connection,
    site_type: str,
    lat: float | None,
    lon: float | None,
    name: str | None = None,
    radius_km: float = SAME_SITE_KM,
) -> list[NearbySite]:
    """Места того же типа, которые по правилам импорта считаются тем же местом.

    Правило дедупликации из импорта (importer._site_id):
    - участки реки — одно место при одинаковом названии, независимо от расстояния;
    - остальные — ближе radius_km (200 м), а без координат — одинаковое название.
    Только чтение: решает вызывающий код (выбрать существующее место или создать новое).
    """
    found = []
    rows = conn.execute(
        "SELECT site_id, name, lat, lon FROM sampling_site WHERE site_type = ? ORDER BY site_id",
        (site_type,),
    )
    for row in rows:
        if name is not None and row["name"] == name:
            distance = None
        elif None in (lat, lon, row["lat"], row["lon"]):
            continue
        else:
            distance = distance_km((lat, lon), (row["lat"], row["lon"]))
            if distance > radius_km:
                continue
        found.append(NearbySite(row["site_id"], row["name"], distance))
    return found


def _city_id(
    conn: sqlite3.Connection, city: str | None, region: str | None, new_region: bool
) -> tuple[int | None, list[str]]:
    """id города (найти или создать) и список того, что создано, — для сообщения."""
    city, region = clean_text(city), clean_text(region)
    if city is None:
        if region is not None:
            raise EditError(
                "Провинция указывается вместе с городом (у места хранится только город)."
            )
        return None, []
    if region is None:
        raise EditError(f"Укажите провинцию города «{city}» (--region).")
    created = []
    row = conn.execute("SELECT region_id FROM region WHERE name = ?", (region,)).fetchone()
    if row:
        region_id = row["region_id"]
    elif new_region:
        region_id = _insert(conn, "region", name=region)
        created.append(f"провинция «{region}»")
    else:
        names = ", ".join(r[0] for r in conn.execute("SELECT name FROM region ORDER BY region_id"))
        raise EditError(
            f"Провинции «{region}» нет. Есть: {names}. "
            "Новую добавьте с флагом --new-region (проверьте, что это не опечатка)."
        )
    normalized = normalize_city(city)  # «Хонг-на» → «Хонг-нгу», как при импорте
    row = conn.execute(
        "SELECT city_id FROM city WHERE region_id = ? AND name = ?", (region_id, normalized.name)
    ).fetchone()
    if row:
        return row["city_id"], created
    city_id = _insert(
        conn, "city", region_id=region_id, name=normalized.name, name_latin=normalized.name_latin
    )
    created.append(f"город «{normalized.name}»")
    return city_id, created


def create_site(
    conn: sqlite3.Connection,
    site_type: str,
    *,
    name: str | None = None,
    lat=None,
    lon=None,
    city: str | None = None,
    region: str | None = None,
    description: str | None = None,
    new_region: bool = False,
    allow_nearby: bool = False,
) -> EditResult:
    """Новое место (участок реки, рынок, аквахозяйство, точка рыбака).

    Если такое место уже есть (find_nearby_sites: тот же тип и ближе 200 м или то же
    название) — EditError со списком: экспедиции возвращаются на те же точки, и новый
    выезд нужно привязать к существующему месту, а не плодить дубли. Если это всё же
    другое место — allow_nearby=True (в CLI флаг --new).
    Название по умолчанию — по правилам импорта («Участок у <город>»).
    """
    if site_type not in SITE_TYPES:
        raise EditError(f"Тип места «{site_type}» не подходит. Возможные: {', '.join(SITE_TYPES)}.")
    lat = parse_entry_number(lat, "Широта", minimum=-90, maximum=90)
    lon = parse_entry_number(lon, "Долгота", minimum=-180, maximum=180)
    if (lat is None) != (lon is None):
        raise EditError("Координаты нужны обе — широта и долгота — или ни одной.")
    city_text = clean_text(city)
    city_name = normalize_city(city_text).name if city_text else None
    name = clean_text(name) or default_site_name(site_type, city_name, lat, lon)
    if name is None:
        raise EditError("Укажите название места (--name) или город (--city и --region).")

    with write_transaction(conn):
        nearby = find_nearby_sites(conn, site_type, lat, lon, name)
        if nearby and not allow_nearby:
            listed = "; ".join(site.describe() for site in nearby)
            raise EditError(
                f"Такое место, похоже, уже есть: {listed}. Укажите его номер при вводе выезда "
                "(add-event … <место>). Если это действительно другое место — повторите с --new."
            )
        city_id, created = _city_id(conn, city, region, new_region)
        site_id = _insert(
            conn, "sampling_site", city_id=city_id, name=name, site_type=site_type,
            lat=lat, lon=lon, description=clean_text(description) or "ввод вручную",
        )  # fmt: skip
        message = f"Создано место №{site_id} «{name}» ({site_type})."
        if created:
            message += f" Добавлены: {', '.join(created)}."
        warnings = [f"Рядом есть место {site.describe()}" for site in nearby]
        return _done(conn, message, site_id, warnings)


# ---------------------------------------------------------------------------
# Выезды, траления, вода
# ---------------------------------------------------------------------------


def _gear_id(conn: sqlite3.Connection, mesh: str | None, size: str | None) -> int:
    """Сеть из справочника gear (найти или создать). `IS` сравнивает и с NULL."""
    row = conn.execute(
        "SELECT gear_id FROM gear WHERE gear_type = ? AND mesh_cm IS ? AND size_m IS ?",
        (GEAR_TYPE, mesh, size),
    ).fetchone()
    if row:
        return row["gear_id"]
    return _insert(conn, "gear", gear_type=GEAR_TYPE, mesh_cm=mesh, size_m=size)


def _extend_expedition_dates(
    conn: sqlite3.Connection, expedition: sqlite3.Row, date: str
) -> list[str]:
    """Расширить даты экспедиции, чтобы они включали дату выезда (как при импорте: min/max).

    Если дата выходит за уже записанные даты — предупреждение (возможно, опечатка в годе).
    """
    start, end = expedition["date_start"], expedition["date_end"]
    new_start = min(start, date) if start else date
    new_end = max(end, date) if end else date
    if (new_start, new_end) == (start, end):
        return []
    conn.execute(
        "UPDATE expedition SET date_start = ?, date_end = ? WHERE expedition_id = ?",
        (new_start, new_end, expedition["expedition_id"]),
    )
    if not (start and end):
        return []
    return [
        f"Дата выезда {date} вне дат экспедиции «{expedition['name']}» "
        f"({start} – {end}); даты экспедиции изменены: {new_start} – {new_end}."
    ]


def create_event(
    conn: sqlite3.Connection,
    expedition_id: int,
    site_id: int,
    event_date: str,
    capture_method: str,
    *,
    gear_mesh: str | None = None,
    gear_size: str | None = None,
    notes: str | None = None,
) -> EditResult:
    """Новый выезд: экспедиция, место, дата, способ получения; сеть — только у тралений.

    Тип места должен соответствовать способу (траление → участок реки, рыбак → точка
    рыбака…), как при импорте. Даты экспедиции расширяются, чтобы включать дату выезда
    (при импорте даты экспедиции — min/max дат выездов); если дата выходит за уже
    записанные даты — предупреждение.
    """
    date = parse_entry_date(event_date, "Дата выезда")
    if date is None:
        raise EditError("Укажите дату выезда (ГГГГ-ММ-ДД).")
    mesh, size = normalize_gear_size(gear_mesh), normalize_gear_size(gear_size)
    method = clean_text(capture_method)
    if (mesh or size) and method != "траление":
        raise EditError(f"Сеть указывают только у тралений, а способ получения — «{method}».")

    with write_transaction(conn):
        expedition = _expedition(conn, expedition_id)
        site = _site(conn, site_id)
        method_id = _method_id(conn, method)
        if site["site_type"] != SITE_TYPE[method]:
            raise EditError(
                f"Способ «{method}» не подходит к месту №{site_id} «{site['name']}» "
                f"({site['site_type']}): нужно место типа «{SITE_TYPE[method]}»."
            )
        same = conn.execute(
            "SELECT event_id FROM sampling_event WHERE expedition_id = ? AND site_id = ? "
            "AND event_date = ? AND capture_method_id = ?",
            (expedition_id, site_id, date, method_id),
        ).fetchone()
        if same:
            raise EditError(
                f"Такой выезд уже есть: №{same['event_id']} ({date}, «{site['name']}»)."
            )
        gear_id = _gear_id(conn, mesh, size) if (mesh or size) else None
        event_id = _insert(
            conn, "sampling_event", expedition_id=expedition_id, site_id=site_id,
            event_date=date, capture_method_id=method_id, gear_id=gear_id,
            source_sheet=None, notes=clean_text(notes),
        )  # fmt: skip

        warnings = _extend_expedition_dates(conn, expedition, date)
        if method == "траление" and gear_id is None:
            warnings.append("Сеть не указана (--mesh, --size) — её можно будет добавить позже.")
        message = (
            f"Создан выезд №{event_id}: {date}, «{site['name']}», {method} "
            f"(экспедиция «{expedition['name']}»)."
        )
        return _done(conn, message, event_id, warnings)


def add_trawling(
    conn: sqlite3.Connection,
    event_id: int,
    trawl_no: int,
    *,
    time_start: str | None = None,
    time_end: str | None = None,
    duration_min=None,
    speed_kmh=None,
    intermediate_depth_m=None,
    catch_note: str | None = None,
    notes: str | None = None,
) -> EditResult:
    """Траление выезда-траления; номер траления в пределах выезда уникален."""
    if not isinstance(trawl_no, int) or trawl_no <= 0:
        raise EditError(f"Номер траления — целое число больше 0 (введено {trawl_no!r}).")
    values = dict(
        time_start=parse_entry_time(time_start, "Время старта"),
        time_end=parse_entry_time(time_end, "Время финиша"),
        duration_min=parse_entry_number(duration_min, "Длительность", positive=True),
        speed_kmh=parse_entry_number(speed_kmh, "Скорость", minimum=0),
        intermediate_depth_m=parse_entry_number(
            intermediate_depth_m, "Промежуточная глубина", minimum=0
        ),
        catch_note=clean_text(catch_note),
        notes=clean_text(notes),
    )
    with write_transaction(conn):
        event = _event(conn, event_id)
        if event["method"] != "траление":
            raise EditError(
                f"Выезд {_title(event)} — {event['method']}, а не траление: тралений у него нет."
            )
        if conn.execute(
            "SELECT 1 FROM trawling WHERE event_id = ? AND trawl_no = ?", (event_id, trawl_no)
        ).fetchone():
            raise EditError(f"Траление {trawl_no} на выезде {_title(event)} уже есть.")
        trawling_id = _insert(conn, "trawling", event_id=event_id, trawl_no=trawl_no, **values)
        return _done(
            conn, f"Добавлено траление {trawl_no} выезда {_title(event)} (id {trawling_id}).",
            trawling_id,
        )  # fmt: skip


def parse_coord_source(value) -> str | None:
    """Источник координат как при импорте: по первому слову, без учёта регистра.

    >>> parse_coord_source("garmin"), parse_coord_source("Google maps"), parse_coord_source("")
    ('Garmin', 'Google Maps', None)
    """
    source = clean_text(value)
    if source is None:
        return None
    if source.lower().split()[0] not in COORD_SOURCES:
        names = ", ".join(COORD_SOURCES.values())
        raise EditError(f"Источник координат «{source}» не подходит. Возможные: {names}.")
    return COORD_SOURCES[source.lower().split()[0]]


def add_water_measurement(
    conn: sqlite3.Connection,
    event_id: int,
    point_type: str,
    trawl_no: int | None = None,
    *,
    measured_time: str | None = None,
    coord_source: str | None = None,
    garmin_wp: str | None = None,
    notes: str | None = None,
    **numbers,
) -> EditResult:
    """Замер воды: 'старт'/'финиш' траления (trawl_no обязателен) или 'точка' без траления.

    numbers — числовые колонки из WATER_NUMBERS (lat=…, ph=…, o2_pct=…); другие имена — ошибка.
    """
    if point_type not in POINT_TYPES:
        raise EditError(
            f"Тип точки «{point_type}» не подходит. Возможные: {', '.join(POINT_TYPES)}."
        )
    if point_type == "точка" and trawl_no is not None:
        raise EditError("Тип «точка» — замер не при тралении: номер траления не указывают.")
    if point_type != "точка" and trawl_no is None:
        raise EditError(f"Для точки «{point_type}» укажите номер траления.")
    unknown = sorted(set(numbers) - set(WATER_NUMBERS))
    if unknown:
        raise EditError(f"Неизвестные поля замера: {', '.join(unknown)}.")
    values = {}
    for column, (what, low, high) in WATER_NUMBERS.items():
        values[column] = parse_entry_number(numbers.get(column), what, minimum=low, maximum=high)
    if (values["lat"] is None) != (values["lon"] is None):
        raise EditError("Координаты нужны обе — широта и долгота — или ни одной.")
    source = parse_coord_source(coord_source)

    with write_transaction(conn):
        event = _event(conn, event_id)
        trawling_id = None if trawl_no is None else _trawling(conn, event, trawl_no)
        if (
            trawling_id is not None
            and conn.execute(
                "SELECT 1 FROM water_measurement WHERE trawling_id = ? AND point_type = ?",
                (trawling_id, point_type),
            ).fetchone()
        ):
            raise EditError(f"Замер «{point_type}» траления {trawl_no} уже есть.")
        measurement_id = _insert(
            conn, "water_measurement", event_id=event_id, trawling_id=trawling_id,
            point_type=point_type, measured_time=parse_entry_time(measured_time, "Время замера"),
            coord_source=source, garmin_wp=clean_text(garmin_wp), notes=clean_text(notes),
            **values,
        )  # fmt: skip
        where = f"траление {trawl_no}, {point_type}" if trawl_no else "точка без траления"
        return _done(
            conn, f"Добавлен замер воды №{measurement_id} (выезд {_title(event)}, {where}).",
            measurement_id,
        )  # fmt: skip


# ---------------------------------------------------------------------------
# Особи
# ---------------------------------------------------------------------------


def _series_of(suffix: str) -> tuple[str, str]:
    """(суффикс как в метке, серия) по суффиксу; неизвестный суффикс — EditError."""
    parsed = parse_label(f"1 {suffix}".strip())
    if parsed is None:
        known = ", ".join(s or "(без суффикса)" for s, _ in SUFFIXES.values())
        raise EditError(f"Суффикс «{suffix}» не известен. Возможные: {known}.")
    return parsed.suffix, parsed.series


@dataclass
class _Reserved:
    """Что уже заняли строки пакетного ввода (create_specimens); у одиночного ввода — пусто.

    Номера и метки из пакета ещё не записаны в базу, поэтому проверки «номер занят»,
    «следующий свободный», «номер пропущен» смотрят и в базу, и сюда.
    """

    labels: dict[str, int] = field(default_factory=dict)  # номер пробы → строка пакета
    # серия → номер → номера проб пакета с этим числом (явные — заранее, «авто» — по ходу)
    numbers: dict[str, dict[int, set[str]]] = field(default_factory=dict)
    sample_labels: dict[str, str] = field(default_factory=dict)  # метка образца → номер особи

    def add_number(self, series: str, number: int, label: str) -> None:
        self.numbers.setdefault(series, {}).setdefault(number, set()).add(label)


def _top_number(conn: sqlite3.Connection, series: str, reserved: _Reserved | None) -> int:
    """Наибольший номер серии в базе и в пакете (0 — номеров ещё нет)."""
    top = conn.execute(
        "SELECT max(series_no) FROM specimen WHERE series = ?", (series,)
    ).fetchone()[0]
    in_batch = reserved.numbers.get(series, {}) if reserved else {}
    return max([top or 0, *in_batch])


def _next_label(conn: sqlite3.Connection, suffix: str, reserved: _Reserved | None = None) -> str:
    suffix, series = _series_of(clean_text(suffix) or "")
    return f"{_top_number(conn, series, reserved) + 1} {suffix}".strip()


def next_label(conn: sqlite3.Connection, suffix: str) -> str:
    """Следующий свободный номер пробы в серии суффикса.

    Серии — как при импорте (parsing.SUFFIXES): у Pangasius одна сквозная нумерация
    на все суффиксы (e, sp, ph, mc и без суффикса), у Plotosus — своя («N pc») и т.д.
    Следующий номер = наибольший номер серии + 1 (пропуски не заполняются).
    """
    return _next_label(conn, suffix)


def _numbering_warnings(
    conn: sqlite3.Connection, number: int, series: str, label: str,
    reserved: _Reserved | None = None, exclude_id: int | None = None,
) -> list[str]:  # fmt: skip
    """Мягкие проблемы нумерации (их же найдёт checks.check_numbering).

    exclude_id — особь, которой меняют номер: её прежний номер не считается.
    `IS NOT ?` с None — истина для всех строк.
    """
    in_batch = reserved.numbers.get(series, {}) if reserved else {}
    same = [
        r["label"]
        for r in conn.execute(
            "SELECT label FROM specimen WHERE series = ? AND series_no = ? "
            "AND specimen_id IS NOT ?",
            (series, number, exclude_id),
        )
    ]
    same += sorted(in_batch.get(number, set()) - {label} - set(same))
    warnings = []
    if same:
        listed = ", ".join(same)
        warnings.append(f"Номер {number} в серии {series} уже есть с другим суффиксом: {listed}")
    top = conn.execute(
        "SELECT max(series_no) FROM specimen WHERE series = ? AND specimen_id IS NOT ?",
        (series, exclude_id),
    ).fetchone()[0]
    # пропуск — номер между последним в базе и введённым, которого нет и в пакете
    if any(n not in in_batch for n in range((top or 0) + 1, number)):
        warnings.append(f"Номера пропущены в серии {series}: последний {top or 0}, введён {number}")
    return warnings


def _default_confidence(scientific_name: str) -> str:
    """Как при импорте: «Pangasius sp.» — до рода, иначе — точно."""
    return "до рода" if scientific_name.endswith(" sp.") else "точно"


@dataclass
class _SampleDraft:
    """Проверенный образец, готовый к записи (add_sample, пакетный ввод)."""

    values: dict  # колонки sample, кроме specimen_id
    labs: list[tuple[int, str]]  # (lab_id, название) — куда сразу отправлен


def _draft_sample(
    conn: sqlite3.Connection,
    specimen_label: str,
    specimen_id: int | None,
    sample_type: str,
    *,
    organ: str | None = None,
    sample_label: str | None = None,
    preservative: str | None = None,
    notes: str | None = None,
    labs: tuple[str, ...] | list[str] = (),
    reserved: _Reserved | None = None,
) -> _SampleDraft:
    """Проверить образец до записи; specimen_id = None — особь ещё не записана (пакет)."""
    if sample_type not in SAMPLE_TYPES:
        raise EditError(
            f"Тип образца «{sample_type}» не подходит. Возможные: {', '.join(SAMPLE_TYPES)}."
        )
    if isinstance(labs, str):  # одна лаборатория строкой, а не списком
        labs = [labs]
    tag = clean_text(sample_label) or specimen_label
    # `IS NOT ?` с None — истина для всех строк: у новой особи образцов в базе ещё нет
    other = conn.execute(
        "SELECT sp.label FROM sample sa JOIN specimen sp USING (specimen_id) "
        "WHERE sa.label = ? AND sa.specimen_id IS NOT ? LIMIT 1",
        (tag, specimen_id),
    ).fetchone()
    owner = other["label"] if other else None
    batch_owner = reserved.sample_labels.get(tag) if reserved else None
    if owner is None and batch_owner not in (None, specimen_label):
        owner = batch_owner  # у другой особи того же пакета
    if owner:
        raise EditError(f"Метка образца «{tag}» уже есть у особи {owner}.")
    found = []
    for lab in dict.fromkeys(labs):  # без повторов, порядок сохраняется
        found.append((_lab_id(conn, lab)[0], clean_text(lab)))
    values = dict(
        sample_type=sample_type, organ=clean_text(organ), label=tag,
        preservative=clean_text(preservative), raw_value=None, notes=clean_text(notes),
    )  # fmt: skip
    return _SampleDraft(values, found)


def _write_sample(conn: sqlite3.Connection, specimen_id: int, draft: _SampleDraft) -> int:
    sample_id = _insert(conn, "sample", specimen_id=specimen_id, **draft.values)
    for lab_id, _ in draft.labs:
        _insert(conn, "sample_shipment", sample_id=sample_id, lab_id=lab_id)
    return sample_id


@dataclass
class _SpecimenDraft:
    """Проверенная особь, готовая к записи (create_specimen, create_specimens)."""

    label: str
    series: str
    number: int
    specimen: dict  # колонки specimen
    identification: dict  # колонки species_identification, кроме specimen_id
    summary: str  # «Plotosus canius, точно» — для сообщения
    warnings: list[str]
    samples: list[_SampleDraft] = field(default_factory=list)


def _draft_specimen(
    conn: sqlite3.Connection,
    event: sqlite3.Row,
    species: str | None,
    label: str | None = None,
    *,
    confidence: str | None = None,
    tl_cm=None,
    sl_cm=None,
    mass_g=None,
    mass_gutted_g=None,
    sex: str | None = None,
    is_dead: bool = False,
    trawl_no: int | None = None,
    capture_method: str | None = None,
    capture_site_id: int | None = None,
    capture_date: str | None = None,
    identified_by: str | None = None,
    notes: str | None = None,
    limits: MeasurementLimits = FISH_LIMITS,
    reserved: _Reserved | None = None,
) -> _SpecimenDraft:
    """Все проверки новой особи (жёсткие — EditError, мягкие — в warnings), без записи.

    Только читает базу: годится и для предпросмотра (check_specimens).
    reserved — номера и метки, уже занятые строками того же пакета.
    """
    species_text = clean_text(species)
    if species_text in (None, "?", "-"):
        species_text = None
    if confidence is not None and confidence not in CONFIDENCES:
        raise EditError(
            f"Уверенность «{confidence}» не подходит. Возможные: {', '.join(CONFIDENCES)}."
        )
    if species_text is None and confidence not in (None, "не определён"):
        raise EditError("Без вида можно указать только уверенность «не определён».")
    if species_text is not None and confidence == "не определён":
        raise EditError(
            "При уверенности «не определён» вид не указывают: вместо вида напишите «?»."
        )
    if sex is not None and clean_text(sex) not in SEXES:
        raise EditError(f"Пол «{sex}» не подходит. Возможные: {', '.join(SEXES)}.")
    if trawl_no is not None and (not isinstance(trawl_no, int) or trawl_no <= 0):
        raise EditError(f"Номер траления — целое число больше 0 (введено {trawl_no!r}).")
    measured = {
        column: parse_entry_number(value, MEASUREMENTS[column])
        for column, value in (("tl_cm", tl_cm), ("sl_cm", sl_cm), ("mass_g", mass_g),
                              ("mass_gutted_g", mass_gutted_g))
    }  # fmt: skip
    own_date = parse_entry_date(capture_date, "Дата поимки")

    if species_text is None:
        species_id, scientific_name, code = None, None, None
        confidence = "не определён"
    else:
        row = find_species(conn, species_text)
        species_id, scientific_name, code = row["species_id"], row["scientific_name"], row["code"]
        confidence = confidence or _default_confidence(scientific_name)

    if label is None:
        if not code:
            raise EditError(
                f"У вида «{scientific_name or 'не определён'}» нет кода для номера пробы — "
                "укажите номер явно (--label)."
            )
        label = _next_label(conn, code, reserved)
    parsed = parse_label(clean_text(label))
    if parsed is None:
        known = ", ".join(s for s, _ in SUFFIXES.values() if s)
        raise EditError(
            f"Номер пробы «{label}» не распознан. Пишите «число суффикс», например "
            f"«179 pc»; суффиксы: {known}; у Pangasius можно без суффикса."
        )
    if conn.execute("SELECT 1 FROM specimen WHERE label = ?", (parsed.label,)).fetchone():
        free = _next_label(conn, parsed.suffix, reserved)
        raise EditError(f"Номер «{parsed.label}» уже занят. Следующий свободный: {free}.")
    if reserved and parsed.label in reserved.labels:
        free = _next_label(conn, parsed.suffix, reserved)
        row = reserved.labels[parsed.label]
        raise EditError(
            f"Номер «{parsed.label}» уже есть в этом вводе (строка {row}). "
            f"Следующий свободный: {free}."
        )

    check = validate_specimen(
        *measured.values(), suffix=parsed.suffix, species_code=code, limits=limits
    )
    if check.errors:
        raise EditError(" ".join(check.errors))
    warnings = check.warnings + _numbering_warnings(
        conn, parsed.number, parsed.series, parsed.label, reserved
    )

    method_id = None
    if clean_text(capture_method) is not None:
        method_id = _method_id(conn, capture_method)
        if method_id == event["capture_method_id"]:
            method_id = None  # как у выезда — не записываем (правило импорта)
    site_id = None
    if capture_site_id is not None:
        site_id = _site(conn, capture_site_id)["site_id"]
        if site_id == event["site_id"]:
            site_id = None
    trawling_id = None
    if trawl_no is not None:
        if method_id is not None:
            raise EditError(
                "Особь получена не тралением (указан другой способ) — траление не указывают."
            )
        trawling_id = _trawling(conn, event, trawl_no)
    if own_date == event["event_date"]:
        own_date = None

    specimen = dict(
        label=parsed.label, label_raw=None,  # не из Excel
        series=parsed.series, series_no=parsed.number, event_id=event["event_id"],
        trawling_id=trawling_id, species_id=species_id, capture_method_id=method_id,
        capture_site_id=site_id, capture_date=own_date, sex=clean_text(sex),
        is_dead=int(bool(is_dead)), notes=clean_text(notes), source_row=None, **measured,
    )  # fmt: skip
    identification = dict(
        species_id=species_id, method="морфология", confidence=confidence,
        identified_by=clean_text(identified_by),
        identified_on=own_date or event["event_date"], raw_text=species_text,
    )  # fmt: skip
    summary = f"{scientific_name or 'вид не определён'}, {confidence}"
    return _SpecimenDraft(
        parsed.label, parsed.series, parsed.number, specimen, identification, summary, warnings
    )


def _write_specimen(conn: sqlite3.Connection, draft: _SpecimenDraft) -> int:
    """Записать особь, её первое определение вида и образцы; вернуть specimen_id."""
    specimen_id = _insert(conn, "specimen", **draft.specimen)
    _insert(conn, "species_identification", specimen_id=specimen_id, **draft.identification)
    for sample in draft.samples:
        _write_sample(conn, specimen_id, sample)
    return specimen_id


def create_specimen(
    conn: sqlite3.Connection,
    event_id: int,
    species: str | None,
    label: str | None = None,
    *,
    confidence: str | None = None,
    tl_cm=None,
    sl_cm=None,
    mass_g=None,
    mass_gutted_g=None,
    sex: str | None = None,
    is_dead: bool = False,
    trawl_no: int | None = None,
    capture_method: str | None = None,
    capture_site_id: int | None = None,
    capture_date: str | None = None,
    identified_by: str | None = None,
    notes: str | None = None,
    limits: MeasurementLimits = FISH_LIMITS,
) -> EditResult:
    """Новая особь вместе с первой записью истории определений (метод «морфология»).

    species — вид из справочника (полностью или «P. elongatus»); None или «?» — не определён.
    label — номер пробы; None — следующий свободный в серии кода вида (next_label).
    confidence — по умолчанию как при импорте: «точно», у «… sp.» — «до рода».
    capture_method / capture_site_id — только если отличаются от выезда (как при импорте;
    совпадающие с выездом не записываются). trawl_no — траление этого же выезда.
    Несколько особей одним сохранением — create_specimens.
    """
    with write_transaction(conn):
        event = _event(conn, event_id)
        draft = _draft_specimen(
            conn, event, species, label, confidence=confidence, tl_cm=tl_cm, sl_cm=sl_cm,
            mass_g=mass_g, mass_gutted_g=mass_gutted_g, sex=sex, is_dead=is_dead,
            trawl_no=trawl_no, capture_method=capture_method, capture_site_id=capture_site_id,
            capture_date=capture_date, identified_by=identified_by, notes=notes, limits=limits,
        )  # fmt: skip
        specimen_id = _write_specimen(conn, draft)
        message = f"Добавлена особь {draft.label} (выезд {_title(event)}): {draft.summary}."
        return _done(conn, message, specimen_id, draft.warnings)


# ---------------------------------------------------------------------------
# Пакетный ввод особей «за столом после экспедиции»
# ---------------------------------------------------------------------------

# Поля строки пакета (create_specimens) — те же, что у create_specimen, плюс образцы
SPECIMEN_ROW_FIELDS = (
    "label", "species", "confidence", "tl_cm", "sl_cm", "mass_g", "mass_gutted_g", "sex",
    "is_dead", "trawl_no", "capture_method", "capture_site_id", "capture_date",
    "identified_by", "notes", "samples",
)  # fmt: skip
# Поля образца в строке пакета — как у add_sample
SAMPLE_ROW_FIELDS = ("sample_type", "organ", "sample_label", "preservative", "notes", "labs")
AUTO_LABELS = ("авто", "auto")  # вместо номера: следующий свободный в серии вида
MAX_BATCH_ROWS = 500


@dataclass
class RowCheck:
    """Итог проверки одной строки пакета."""

    row: int  # номер строки, с 1 (как в форме)
    label: str | None  # номер пробы; для «авто» — какой будет присвоен (None — не удалось)
    errors: list[str] = field(default_factory=list)  # сохранить нельзя
    warnings: list[str] = field(default_factory=list)  # сохранить можно, проблема → журнал


@dataclass
class BatchCheck:
    """Итог проверки всего пакета: по записи на каждую непустую строку."""

    rows: list[RowCheck]

    @property
    def ok(self) -> bool:
        """Нет ни одной жёсткой ошибки — пакет можно сохранить."""
        return not any(r.errors for r in self.rows)

    @property
    def error_rows(self) -> list[int]:
        return [r.row for r in self.rows if r.errors]

    @property
    def warning_count(self) -> int:
        return sum(len(r.warnings) for r in self.rows)


class BatchError(EditError):
    """В пакете есть жёсткие ошибки — ничего не записано; подробности по строкам — в check."""

    def __init__(self, check: BatchCheck):
        rows = ", ".join(map(str, check.error_rows))
        where = "строке" if len(check.error_rows) == 1 else "строках"
        details = "; ".join(f"строка {r.row}: {' '.join(r.errors)}" for r in check.rows if r.errors)
        super().__init__(f"Ошибки в {where} {rows} — ничего не сохранено. {details}")
        self.check = check


def _row_int(value, what: str) -> int | None:
    """Целое из формы: 3, «3», « 3 »; пусто — None.

    >>> _row_int(" 3 ", "Траление"), _row_int("", "Траление"), _row_int(None, "Траление")
    (3, None, None)
    """
    if value is None or isinstance(value, int) and not isinstance(value, bool):
        return value
    text = clean_text(value)
    if text is None:
        return None
    if not text.isdigit():
        raise EditError(f"{what}: «{text}» — не целое число.")
    return int(text)


def _row_flag(value) -> bool:
    """Да/нет из формы: флажок (on), 1, «да»; пусто, 0, «нет» — нет.

    >>> _row_flag("on"), _row_flag("да"), _row_flag(""), _row_flag("0"), _row_flag(None)
    (True, True, False, False, False)
    """
    if value is None or isinstance(value, bool):
        return bool(value)
    text = (clean_text(value) or "").lower()
    if text in ("", "0", "нет", "false", "off"):
        return False
    if text in ("1", "да", "true", "on"):
        return True
    raise EditError(f"«Мёртвая»: «{value}» — напишите «да» или «нет».")


def _is_blank_row(row) -> bool:
    """Строка формы, в которой ничего не заполнено, — пропускается."""
    return isinstance(row, Mapping) and all(
        (clean_text(v) is None) if not isinstance(v, (list, tuple, bool)) else not v
        for v in row.values()
    )


def _draft_row(
    conn: sqlite3.Connection,
    event: sqlite3.Row,
    row: Mapping,
    limits: MeasurementLimits,
    reserved: _Reserved,
) -> _SpecimenDraft:
    """Одна строка пакета → черновик особи с образцами (EditError — жёсткая ошибка строки)."""
    unknown = sorted(set(row) - set(SPECIMEN_ROW_FIELDS))
    if unknown:
        raise EditError(f"Неизвестные поля: {', '.join(unknown)}.")
    label = clean_text(row.get("label"))
    if label is not None and label.lower() in AUTO_LABELS:
        label = None
    draft = _draft_specimen(
        conn, event, row.get("species"), label,
        confidence=clean_text(row.get("confidence")),
        tl_cm=row.get("tl_cm"), sl_cm=row.get("sl_cm"), mass_g=row.get("mass_g"),
        mass_gutted_g=row.get("mass_gutted_g"), sex=clean_text(row.get("sex")),
        is_dead=_row_flag(row.get("is_dead")),
        trawl_no=_row_int(row.get("trawl_no"), "Номер траления"),
        capture_method=row.get("capture_method"),
        capture_site_id=_row_int(row.get("capture_site_id"), "Место поимки"),
        capture_date=row.get("capture_date"), identified_by=row.get("identified_by"),
        notes=row.get("notes"), limits=limits, reserved=reserved,
    )  # fmt: skip
    for sample in row.get("samples") or ():
        if not isinstance(sample, Mapping):
            raise EditError("Образец — набор полей (тип, орган, метка…).")
        unknown = sorted(set(sample) - set(SAMPLE_ROW_FIELDS))
        if unknown:
            raise EditError(f"Неизвестные поля образца: {', '.join(unknown)}.")
        if _is_blank_row(sample):
            continue
        sample_draft = _draft_sample(
            conn, draft.label, None, clean_text(sample.get("sample_type")) or "",
            organ=sample.get("organ"), sample_label=sample.get("sample_label"),
            preservative=sample.get("preservative"), notes=sample.get("notes"),
            labs=sample.get("labs") or (), reserved=reserved,
        )  # fmt: skip
        draft.samples.append(sample_draft)
        reserved.sample_labels.setdefault(sample_draft.values["label"], draft.label)
    return draft


def _check_batch(
    conn: sqlite3.Connection, event: sqlite3.Row, rows, limits: MeasurementLimits
) -> tuple[BatchCheck, list[_SpecimenDraft]]:
    """Проверить все строки пакета; черновики — только строк без ошибок."""
    if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Sequence):
        raise EditError("Строки пакета — список (по словарю полей на строку).")
    numbered = [(i, row) for i, row in enumerate(rows, start=1) if not _is_blank_row(row)]
    if not numbered:
        raise EditError("Нет ни одной заполненной строки.")
    if len(numbered) > MAX_BATCH_ROWS:
        raise EditError(
            f"Слишком много строк ({len(numbered)}): за раз не больше {MAX_BATCH_ROWS}."
        )

    reserved = _Reserved()
    # явные номера пакета — заранее: «авто» не должен выдать номер, записанный ниже в пакете
    for _, row in numbered:
        text = clean_text(row.get("label")) if isinstance(row, Mapping) else None
        parsed = parse_label(text) if text and text.lower() not in AUTO_LABELS else None
        if parsed:
            reserved.add_number(parsed.series, parsed.number, parsed.label)

    checks, drafts = [], []
    for i, row in numbered:
        try:
            if not isinstance(row, Mapping):
                raise EditError("Строка — набор полей (номер, вид, промеры…).")
            draft = _draft_row(conn, event, row, limits, reserved)
        except EditError as error:  # и NotFoundError: нет места поимки и т.п.
            label = clean_text(row.get("label")) if isinstance(row, Mapping) else None
            checks.append(RowCheck(i, label, [str(error)]))
            continue
        reserved.labels[draft.label] = i
        reserved.add_number(draft.series, draft.number, draft.label)
        checks.append(RowCheck(i, draft.label, [], draft.warnings))
        drafts.append(draft)
    return BatchCheck(checks), drafts


def check_specimens(
    conn: sqlite3.Connection,
    event_id: int,
    rows: Sequence[Mapping],
    limits: MeasurementLimits = FISH_LIMITS,
) -> BatchCheck:
    """Проверить пакет особей БЕЗ записи — для предпросмотра в форме.

    Только читает базу (подходит db.connect_read_only). Те же проверки, что у
    create_specimens; номера «авто» показываются такими, какими их присвоит сохранение
    (если до сохранения никто не добавит особей в ту же серию).
    Нет выезда — NotFoundError; пустой пакет — EditError.
    """
    return _check_batch(conn, _event(conn, event_id), rows, limits)[0]


def create_specimens(
    conn: sqlite3.Connection,
    event_id: int,
    rows: Sequence[Mapping],
    limits: MeasurementLimits = FISH_LIMITS,
) -> EditResult:
    """Несколько особей выезда одним сохранением: всё или ничего.

    rows — список словарей, по одному на строку формы. Поля (SPECIMEN_ROW_FIELDS) — как
    у create_specimen: label (номер; пусто или «авто» — следующий свободный в серии вида),
    species, confidence, tl_cm, sl_cm, mass_g, mass_gutted_g, sex, is_dead, trawl_no,
    capture_method, capture_site_id, capture_date, identified_by, notes; числа можно строкой.
    samples — необязательный список образцов (SAMPLE_ROW_FIELDS, как у add_sample).
    Пустые строки пропускаются; номер строки в сообщениях — как в форме (с 1).

    Сначала проверяются все строки (в той же транзакции, что и запись): есть хоть одна
    жёсткая ошибка — BatchError (подвид EditError) с разбором по строкам (error.check),
    ничего не записано. Иначе — все особи записаны, журнал перепроверен один раз;
    record_ids — id особей по порядку строк, warnings — «строка N (номер): …».
    """
    with write_transaction(conn):
        event = _event(conn, event_id)
        check, drafts = _check_batch(conn, event, rows, limits)
        if not check.ok:
            raise BatchError(check)
        ids = [_write_specimen(conn, draft) for draft in drafts]
        samples = sum(len(d.samples) for d in drafts)
        message = (
            f"Добавлено особей: {len(ids)} (выезд {_title(event)}): "
            f"{', '.join(d.label for d in drafts)}"
            + (f"; образцов: {samples}." if samples else ".")
        )
        warnings = [f"строка {r.row} ({r.label}): {w}" for r in check.rows for w in r.warnings]
        result = _done(conn, message, None, warnings)
        result.record_ids = ids
        return result


def update_measurements(
    conn: sqlite3.Connection,
    label: str,
    limits: MeasurementLimits = FISH_LIMITS,
    **changes,
) -> EditResult:
    """Изменить промеры особи: tl_cm=…, sl_cm=…, mass_g=…, mass_gutted_g=….

    Меняются только переданные промеры; None (или «-») — стереть значение.
    Жёсткие ошибки (TL ≤ 0, масса < 0) — EditError; мягкие (SL > TL…) — в журнал.
    Исправленная опечатка закрывает свою проблему в журнале автоматически (run_checks).
    """
    unknown = sorted(set(changes) - set(MEASUREMENTS))
    if unknown:
        raise EditError(
            f"Неизвестные промеры: {', '.join(unknown)}. Есть: {', '.join(MEASUREMENTS)}."
        )
    if not changes:
        raise EditError("Укажите, какие промеры изменить (--tl, --sl, --mass, --gutted).")
    new = {
        column: parse_entry_number(value, MEASUREMENTS[column]) for column, value in changes.items()
    }

    with write_transaction(conn):
        specimen = find_specimen(conn, label)
        old = conn.execute(
            "SELECT tl_cm, sl_cm, mass_g, mass_gutted_g FROM specimen WHERE specimen_id = ?",
            (specimen["specimen_id"],),
        ).fetchone()
        merged = {column: new.get(column, old[column]) for column in MEASUREMENTS}
        check = validate_specimen(*merged.values(), limits=limits)
        if check.errors:
            raise EditError(" ".join(check.errors))
        # список колонок — только из MEASUREMENTS (проверено выше), значения — через «?»
        assignments = ", ".join(f"{column} = ?" for column in new)
        conn.execute(
            f"UPDATE specimen SET {assignments} WHERE specimen_id = ?",
            (*new.values(), specimen["specimen_id"]),
        )

        def show(x):
            return "—" if x is None else f"{x:g}"

        changed = [
            f"{MEASUREMENTS[c]} {show(old[c])} → {show(new[c])}" for c in new if old[c] != new[c]
        ]
        what = ", ".join(changed) if changed else "промеры не изменились"
        return _done(
            conn, f"Особь {specimen['label']}: {what}.", specimen["specimen_id"], check.warnings
        )


# ---------------------------------------------------------------------------
# Образцы, лаборатории, анализы, прилов
# ---------------------------------------------------------------------------


def add_sample(
    conn: sqlite3.Connection,
    label: str,
    sample_type: str,
    *,
    organ: str | None = None,
    sample_label: str | None = None,
    preservative: str | None = None,
    notes: str | None = None,
    labs: tuple[str, ...] | list[str] = (),
) -> EditResult:
    """Образец особи (пробирка, стекло, целая особь); labs — куда сразу отправлен.

    Метка образца по умолчанию — номер особи (как у ДНК и мазков при импорте).
    Метка, которая уже есть у образца ДРУГОЙ особи, — ошибка (перепутали пробирки).
    """
    if sample_type not in SAMPLE_TYPES:
        raise EditError(
            f"Тип образца «{sample_type}» не подходит. Возможные: {', '.join(SAMPLE_TYPES)}."
        )
    with write_transaction(conn):
        specimen = find_specimen(conn, label)
        draft = _draft_sample(
            conn, specimen["label"], specimen["specimen_id"], sample_type, organ=organ,
            sample_label=sample_label, preservative=preservative, notes=notes, labs=labs,
        )  # fmt: skip
        sample_id = _write_sample(conn, specimen["specimen_id"], draft)
        tag, sent = draft.values["label"], [name for _, name in draft.labs]
        message = f"Добавлен образец №{sample_id} «{tag}» ({sample_type}) особи {specimen['label']}"
        message += f", отправлен: {', '.join(sent)}." if sent else "."
        return _done(conn, message, sample_id)


def _sample(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT sa.sample_id, sa.label, sa.sample_type, sp.label AS specimen "
        "FROM sample sa JOIN specimen sp USING (specimen_id) WHERE sa.sample_id = ?",
        (sample_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Образца №{sample_id} нет. Номера образцов — в карточке: show <номер>."
        )
    return row


def ship_sample(
    conn: sqlite3.Connection,
    sample_id: int,
    lab: str,
    sent_on: str | None = None,
    new_lab: bool = False,
) -> EditResult:
    """Отметить, что образец отправлен в лабораторию (sample_shipment)."""
    date = parse_entry_date(sent_on, "Дата отправки")
    with write_transaction(conn):
        sample = _sample(conn, sample_id)
        lab_id, created = _lab_id(conn, lab, create=new_lab)
        if conn.execute(
            "SELECT 1 FROM sample_shipment WHERE sample_id = ? AND lab_id = ?", (sample_id, lab_id)
        ).fetchone():
            raise EditError(
                f"Образец №{sample_id} уже отмечен как отправленный в «{clean_text(lab)}»."
            )
        _insert(conn, "sample_shipment", sample_id=sample_id, lab_id=lab_id, sent_on=date)
        message = (
            f"Образец №{sample_id} «{sample['label']}» ({sample['sample_type']}, особь "
            f"{sample['specimen']}) отправлен: {clean_text(lab)}"
            + (f", {date}" if date else "")
            + (" (новая лаборатория)." if created else ".")
        )
        return _done(conn, message, sample_id)


def add_analysis(
    conn: sqlite3.Connection,
    sample_id: int,
    analysis_type: str,
    *,
    lab: str | None = None,
    analysed_on: str | None = None,
    method: str | None = None,
    result: str | None = None,
    accession: str | None = None,
    notes: str | None = None,
) -> EditResult:
    """Результат исследования образца (analysis): секвенирование, гистология…"""
    analysis_type = clean_text(analysis_type)
    if analysis_type is None:
        raise EditError("Укажите вид анализа, например «секвенирование COI».")
    date = parse_entry_date(analysed_on, "Дата анализа")
    with write_transaction(conn):
        sample = _sample(conn, sample_id)
        lab_id = _lab_id(conn, lab)[0] if clean_text(lab) else None
        analysis_id = _insert(
            conn, "analysis", sample_id=sample_id, lab_id=lab_id, analysis_type=analysis_type,
            analysed_on=date, method=clean_text(method), result=clean_text(result),
            accession=clean_text(accession), notes=clean_text(notes),
        )  # fmt: skip
        return _done(
            conn,
            f"Добавлен анализ №{analysis_id} «{analysis_type}» образца «{sample['label']}» "
            f"(особь {sample['specimen']}).",
            analysis_id,
        )


def create_catch_record(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    raw_text: str | None = None,
    taxon_text: str | None = None,
    count_n=None,
    mass_g=None,
    trawl_no: int | None = None,
) -> EditResult:
    """Прилов / общий улов выезда или траления (catch_record).

    raw_text — запись как в журнале («arius maculatus - 39 шт»); если не указана,
    собирается из таксона, числа и массы. Купленное у рыбаков — не прилов (правило импорта).
    """
    taxon = clean_text(taxon_text)
    count = parse_entry_number(count_n, "Число особей", minimum=0)
    if count is not None and count != int(count):
        raise EditError(f"Число особей — целое (введено {count:g}).")
    count = None if count is None else int(count)
    mass = parse_entry_number(mass_g, "Масса", minimum=0)
    text = clean_text(raw_text)
    if text is None:
        parts = [p for p in (taxon, count is not None and f"{count} шт",
                             mass is not None and f"{mass:g} г") if p]  # fmt: skip
        if not parts:
            raise EditError("Укажите, что поймали: текст, таксон, число или массу.")
        text = " - ".join(parts)
    with write_transaction(conn):
        event = _event(conn, event_id)
        trawling_id = None if trawl_no is None else _trawling(conn, event, trawl_no)
        catch_id = _insert(
            conn, "catch_record", event_id=event_id, trawling_id=trawling_id,
            taxon_text=taxon, count_n=count, mass_g=mass, raw_text=text,
        )  # fmt: skip
        where = f", траление {trawl_no}" if trawl_no else ""
        return _done(
            conn, f"Добавлен прилов №{catch_id} (выезд {_title(event)}{where}): {text}.", catch_id
        )
