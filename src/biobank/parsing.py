"""Чистые функции разбора значений из полевой Excel-книги.

«Чистая» функция не читает файлы, не ходит в базу и не меняет ничего снаружи:
она только получает значение и возвращает результат. Поэтому её легко
проверить тестом: дал на вход строку — сравнил ответ.

Спорные места функции не исправляют молча, а возвращают текст предупреждения
(поле warning / warnings). Импорт потом превращает эти тексты в записи data_issue.

Логика перенесена из legacy/import_excel_v1.py, где она уже отлажена на этой книге.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------

BLANK_MARKS = ("", "-", "—", "–")


@dataclass(frozen=True)
class Parsed:
    """Результат разбора одного значения: само значение и, если что-то не так, предупреждение.

    frozen=True делает объект неизменяемым — случайно поменять поле нельзя.
    """

    value: object
    warning: str | None = None


def normalize_space(text: str) -> str:
    """Убрать лишние пробелы и переносы строк.

    >>> normalize_space("  Plotosus   canius \\n")
    'Plotosus canius'
    """
    return " ".join(text.split())


def is_blank(value) -> bool:
    """Пустая ячейка или прочерк.

    >>> is_blank(None), is_blank(" - "), is_blank("12 pc")
    (True, True, False)
    """
    return value is None or (isinstance(value, str) and value.strip() in BLANK_MARKS)


# ---------------------------------------------------------------------------
# Числа, время, даты
# ---------------------------------------------------------------------------

# Excel хранит даты как число дней; день 1 = 1900-01-01, значит «нулевой» день — 1899-12-31
EXCEL_EPOCH = dt.datetime(1899, 12, 31)

# Регулярное выражение (regex) — шаблон для поиска в тексте:
# -?        необязательный минус
# \d+       одна или больше цифр
# (?:\.\d+)? необязательная дробная часть
# (min|мин)? необязательная приписка единиц
_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)(?:min|мин)?")


def parse_number(value) -> Parsed:
    """Число из ячейки Excel; понимает «полевые» записи текстом.

    >>> parse_number("10,103733.").value
    10.103733
    >>> parse_number("105, 96480").value
    105.9648
    >>> parse_number("20 min").value
    20.0
    >>> parse_number("-").value is None
    True

    Если Excel превратил число в дату (29,9 → 29 января 1900 21:36),
    число восстанавливается, но возвращается предупреждение.
    """
    if value is None or isinstance(value, bool):
        return Parsed(None)
    if isinstance(value, (int, float)):
        return Parsed(float(value))
    if isinstance(value, dt.datetime):
        serial = round((value - EXCEL_EPOCH).total_seconds() / 86400, 4)
        return Parsed(
            serial, f"Число превратилось в дату; восстановлено как {serial:g}. Проверьте."
        )
    if not isinstance(value, str):
        return Parsed(None, f"Не удалось прочитать число: {value!r}")

    text = value.strip()
    if text in BLANK_MARKS:
        return Parsed(None)
    cleaned = text.replace("°", "").replace(" ", "").rstrip(".").replace(",", ".")
    match = _NUMBER_RE.fullmatch(cleaned)
    if match:
        return Parsed(float(match.group(1)))
    return Parsed(None, f"Не удалось прочитать число: {text!r}")


def is_text_number(value) -> bool:
    """Число записано в ячейке текстом (а не числом Excel).

    Такие ячейки читаются нормально, но их стоит поправить в исходнике,
    поэтому импорт считает их и пишет одну запись data_issue на лист.

    >>> is_text_number("29,3."), is_text_number(29.3), is_text_number("-")
    (True, False, False)
    """
    return isinstance(value, str) and not is_blank(value) and parse_number(value).value is not None


def parse_time(value) -> Parsed:
    """Время в виде строки 'ЧЧ:ММ'.

    >>> parse_time(dt.time(9, 5)).value
    '09:05'
    >>> parse_time("9.30").value
    '09:30'
    >>> r = parse_time(10.48)
    >>> r.value, r.warning is not None
    ('10:48', True)
    """
    if value is None:
        return Parsed(None)
    if isinstance(value, (dt.time, dt.datetime)):
        return Parsed(value.strftime("%H:%M"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        hours = int(value)
        minutes = round((value - hours) * 100)
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            return Parsed(None, f"Не удалось прочитать время: {value!r}")
        result = f"{hours:02d}:{minutes:02d}"
        return Parsed(result, f"Время записано числом; понято как {result}")
    if isinstance(value, str):
        match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value.strip())
        if match:
            return Parsed(f"{int(match.group(1)):02d}:{match.group(2)}")
        if is_blank(value):
            return Parsed(None)
    return Parsed(None, f"Не удалось прочитать время: {value!r}")


def parse_date(text) -> str | None:
    """Дата из строки-заголовка листа в формате ISO 'ГГГГ-ММ-ДД'.

    >>> parse_date("25.09.25 пр.Донтхап, г. Хонг-нга (Hong-Ngu)")
    '2025-09-25'
    >>> parse_date("22. 10. 25 пр. Виньлонг, г. Бенче")
    '2025-10-22'
    >>> parse_date("11.08.2026 пр. Кантхо")
    '2026-08-11'
    """
    if not text:
        return None
    match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{2,4})", str(text))
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Комментарии к ячейкам
# ---------------------------------------------------------------------------

# Служебная шапка, которую добавляет редактор таблиц:
# ======
# ID#AAACHv6r0RQ
# admin    (2026-09-29 14:21:50)
_COMMENT_HEADER_RE = re.compile(r"^=+\s*\nID#\S+\s*\n[^\n]*\(\d{4}-\d\d-\d\d[^)]*\)\s*\n?")


def clean_comment(text: str | None) -> str | None:
    """Текст комментария без служебной шапки, в одну строку.

    >>> clean_comment("======\\nID#AAA\\nadmin    (2026-09-29 14:21:50)\\nСеть:\\nячеи 5х5 см.")
    'Сеть: ячеи 5х5 см.'
    """
    if not text:
        return None
    body = normalize_space(_COMMENT_HEADER_RE.sub("", text))
    # пустой комментарий, в котором осталось только имя автора: «admin:»
    if not body or re.fullmatch(r"\w+:", body):
        return None
    return body


# ---------------------------------------------------------------------------
# Номера проб (метки особей)
# ---------------------------------------------------------------------------

# Кириллические буквы, похожие на латинские: в метках их путают ('40 рс', '1е')
_LOOKALIKES = str.maketrans({"р": "p", "с": "c", "е": "e", "Р": "P", "С": "C", "Е": "E"})

# суффикс (в нижнем регистре, латиницей) → (как писать в метке, серия нумерации)
SUFFIXES = {
    "": ("", "Pangasius"),
    "e": ("e", "Pangasius"),
    "sp": ("sp", "Pangasius"),
    "ph": ("ph", "Pangasius"),
    "mc": ("mc", "Pangasius"),
    "pc": ("pc", "pc"),
    "bg": ("Bg", "Bg"),
    "mr": ("mr", "mr"),
}


@dataclass(frozen=True)
class Label:
    """Разобранный номер пробы: '26 ph' → number=26, suffix='ph', series='Pangasius'."""

    label: str
    number: int
    suffix: str
    series: str


def parse_label(value) -> Label | None:
    """Нормализовать номер пробы. Если это не номер пробы — None.

    >>> parse_label(107.0).label
    '107'
    >>> parse_label("155  pc").label, parse_label("3 p.c.").label, parse_label("40 рс").label
    ('155 pc', '3 pc', '40 pc')
    >>> parse_label("1е").label   # кириллическая «е»
    '1 e'
    >>> parse_label("ХХ") is None
    True
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != int(value) or value <= 0:
            return None
        number = int(value)
        return Label(str(number), number, "", "Pangasius")
    text = str(value).strip()
    match = re.fullmatch(r"(\d+)(?:\.0)?\s*([^\W\d_][\w. ]*)?", text)
    if not match:
        return None
    raw_suffix = (match.group(2) or "").translate(_LOOKALIKES)
    key = raw_suffix.replace(".", "").replace(" ", "").lower()
    if key not in SUFFIXES:
        return None
    number = int(match.group(1))
    suffix, series = SUFFIXES[key]
    return Label(f"{number} {suffix}".strip(), number, suffix, series)


def parse_label_range(text) -> tuple[int, int] | None:
    """Диапазон номеров в скобках: '2 рыбак (138-142)' → (138, 142), '1 рыбак (118)' → (118, 118).

    >>> parse_label_range("Координаты 1 (155-162)")
    (155, 162)
    """
    if not isinstance(text, str):
        return None
    match = re.search(r"\((\d+)\s*(?:[-–]\s*(\d+))?\)", text)
    if not match:
        return None
    low = int(match.group(1))
    return low, int(match.group(2) or low)


# ---------------------------------------------------------------------------
# Виды
# ---------------------------------------------------------------------------

# научное название → (семейство, род, код-суффикс в номере пробы)
SPECIES = {
    "Pangasius sp.": ("Pangasiidae", "Pangasius", "sp"),
    "Pangasius elongatus": ("Pangasiidae", "Pangasius", "e"),
    "Pangasius macronema": ("Pangasiidae", "Pangasius", "mc"),
    "Pangasius conchophilus": ("Pangasiidae", "Pangasius", None),
    "Pangasianodon hypophthalmus": ("Pangasiidae", "Pangasianodon", "ph"),
    "Plotosus canius": ("Plotosidae", "Plotosus", "pc"),
    "Bagarius sp.": ("Sisoridae", "Bagarius", "Bg"),
    "Macrobrachium rosenbergii": ("Palaemonidae", "Macrobrachium", "mr"),
}


@dataclass(frozen=True)
class SpeciesParse:
    """Разбор колонки «Вид»: название из справочника (или None), уверенность, примечание."""

    name: str | None
    confidence: str  # 'точно' | 'предп.' | 'до рода' | 'не определён'
    note: str | None = None


def parse_species(raw) -> SpeciesParse:
    """Привести запись вида к справочнику SPECIES.

    >>> parse_species("пред. Pangasius elongatus")
    SpeciesParse(name='Pangasius elongatus', confidence='предп.', note=None)
    >>> parse_species("Pangasius sp").confidence
    'до рода'
    >>> parse_species("?")
    SpeciesParse(name=None, confidence='не определён', note=None)
    """
    if is_blank(raw):
        return SpeciesParse(None, "не определён")
    text = normalize_space(str(raw))
    low = text.lower()

    if text == "?" or "вид неизвестен" in low:
        return SpeciesParse(None, "не определён", None if text == "?" else text)
    if "не elongatus" in low:
        # «скорее всего не elongatus» — точно пангасиус, но вид под вопросом
        return SpeciesParse("Pangasius sp.", "до рода", text)

    confidence = "предп." if low.startswith(("пред.", "предп.")) else "точно"
    if "elongatus" in low:
        return SpeciesParse("Pangasius elongatus", confidence)
    if "macronema" in low:
        return SpeciesParse("Pangasius macronema", confidence)
    if "conchophilus" in low:
        return SpeciesParse("Pangasius conchophilus", confidence)
    if "hypophthalmus" in low:
        note = "в таблице опечатка «Pangasiodon»" if "pangasiodon" in low else None
        return SpeciesParse("Pangasianodon hypophthalmus", confidence, note)
    if low.startswith("pangasius sp"):
        return SpeciesParse("Pangasius sp.", "до рода")
    if "plotosus" in low:
        return SpeciesParse("Plotosus canius", confidence)
    if "bagarius" in low:
        return SpeciesParse("Bagarius sp.", confidence)
    if "macrobra" in low or "rozenberg" in low or "rosenberg" in low:
        return SpeciesParse(
            "Macrobrachium rosenbergii", confidence, "в таблице «macrobrahius rozenbergii»"
        )
    return SpeciesParse(None, "не определён", text)


# ---------------------------------------------------------------------------
# Состояние особи
# ---------------------------------------------------------------------------

WHOLE_WORDS = ("целая рыба", "целиком", "полностью", "целая особь")


def is_whole_specimen(value) -> bool:
    """Особь зафиксирована целиком (вместо промеров/образцов — целая рыба).

    >>> is_whole_specimen("Заспиртовали полностью"), is_whole_specimen("17 pc, целиком в пробирку")
    (True, True)
    """
    return isinstance(value, str) and any(word in value.lower() for word in WHOLE_WORDS)


def is_dead_text(text) -> bool:
    """В тексте сказано, что особь мёртвая.

    >>> is_dead_text("- , особь мертвая"), is_dead_text("из погибшей особи"), is_dead_text("самка")
    (True, True, False)
    """
    return isinstance(text, str) and ("мертв" in text.lower() or "погиб" in text.lower())


def parse_sex(text) -> str | None:
    """Пол, только если он прямо назван: 'самка' / 'самец'. «с икрой» — не угадываем.

    >>> parse_sex("самка"), parse_sex("с икрой")
    ('самка', None)
    """
    if not isinstance(text, str):
        return None
    low = text.lower()
    if re.search(r"\bсамк", low):
        return "самка"
    if re.search(r"\bсамец|\bсамц", low):
        return "самец"
    return None


# ---------------------------------------------------------------------------
# Образцы: гистология и органы
# ---------------------------------------------------------------------------

# часть слова → название органа (по корню, чтобы ловить «печень», «печени» и т.п.)
ORGANS = (
    ("печен", "печень"),
    ("почк", "почки"),
    ("икр", "икра"),
    ("кишечник", "кишечник"),
    ("жабр", "жабры"),
    ("мышц", "мышцы"),
    ("hepatopancreas", "гепатопанкреас"),
)


def organ_of(text) -> str | None:
    """Орган, упомянутый в тексте (первый найденный).

    >>> organ_of("b - почки"), organ_of("26 ph")
    ('почки', None)
    """
    if not isinstance(text, str):
        return None
    low = text.lower()
    for root, organ in ORGANS:
        if root in low:
            return organ
    return None


@dataclass(frozen=True)
class SampleSpec:
    """Описание одного образца, который надо создать (ещё не запись в базе)."""

    sample_type: str  # 'ДНК' | 'гистология' | 'мазок крови' | 'кишечник' | 'целая особь' | 'другое'
    organ: str | None
    label: str | None
    preservative: str | None = None
    notes: str | None = None


@dataclass
class HistologyResult:
    samples: list[SampleSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def smear_looks_like_histology(smear) -> bool:
    """В колонке «мазок крови» записан орган — значит, колонки перепутаны.

    >>> smear_looks_like_histology("1 pc - печень"), smear_looks_like_histology("1 pc")
    (True, False)
    """
    return organ_of(smear) is not None


def _histology_part_label(head: str, base_label: str) -> str | None:
    """Метка образца из начала фрагмента: '26 ph б' → '26 ph б', 'b' → '<особь> b'."""
    head = normalize_space(head)
    if not head:
        return base_label
    # только буква органа: 'b', 'с'
    if re.fullmatch(r"[^\W\d_]", head):
        return f"{base_label} {head}"
    # суффикс без номера и буква: 'pc b' (номер подразумевается тот же)
    match = re.fullmatch(r"[^\W\d_][\w.]*\s+([^\W\d_])", head)
    if match and not head[0].isdigit():
        return f"{base_label} {match.group(1)}"
    # номер пробы целиком: '16sp', '10е', '241'
    label = parse_label(head)
    if label:
        return label.label
    # номер пробы + буква органа: '26 ph б', '13а', '3 p.c. a', '15sp а'
    match = re.fullmatch(r"(\d+(?:\s*[^\W\d_][\w.]*?)?)\s*([^\W\d_])", head)
    if match:
        label = parse_label(match.group(1))
        if label:
            return f"{label.label} {match.group(2)}"
    return None


def parse_histology(text, specimen_label: str) -> HistologyResult:
    """Разобрать ячейку «гистология» на отдельные образцы — по строке на орган.

    specimen_label — нормализованная метка особи, нужна для фрагментов без номера
    ('b - почки' → '<метка> b').

    >>> result = parse_histology("26 ph a - печень, 26 ph б - почки", "26 ph")
    >>> [(s.organ, s.label) for s in result.samples]
    [('печень', '26 ph a'), ('почки', '26 ph б')]
    >>> [(s.organ, s.label) for s in parse_histology("13а - печень, почек нет", "13").samples]
    [('печень', '13 а')]
    """
    result = HistologyResult()
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        label = parse_label(text)
        result.samples.append(SampleSpec("гистология", None, label.label if label else str(text)))
        return result
    if is_blank(text) or is_whole_specimen(text) or not isinstance(text, str):
        return result

    full = normalize_space(text)
    notes = "в пакете" if "пакет" in full.lower() else None
    # делим по запятым, «+» и перед номером без запятой ('15sp а - печень 15sp б - почки')
    parts = [p.strip() for p in re.split(r",|\+|\s(?=\d)", full) if p.strip()]

    for part in parts:
        low = part.lower()
        if "гистология" in low:
            result.samples.append(SampleSpec("гистология", None, specimen_label, notes=notes))
            continue
        if "нет" in low or "не брали" in low:
            continue  # «почек нет»
        organ = organ_of(part)
        head = re.split(r"\s*[-–(]", part)[0]
        if organ and organ_of(head):
            head = ""  # фрагмент — это само название органа: '+ кишечник'
        label = _histology_part_label(head, specimen_label)
        if label is None:
            result.warnings.append(f"Не удалось разобрать фрагмент гистологии: «{part}»")
            continue
        if organ is None and parse_label(head) is None:
            result.warnings.append(f"Не удалось разобрать фрагмент гистологии: «{part}»")
            continue
        sample_type = "кишечник" if organ == "кишечник" else "гистология"
        result.samples.append(SampleSpec(sample_type, organ, label, notes=notes))

    if not result.samples and not result.warnings:
        result.warnings.append(f"Не удалось разобрать гистологию: «{full}»")

    # копипаст: в метке образца стоит номер другой особи ('27 ph a - печень, 26 ph б - почки')
    own = parse_label(specimen_label)
    if own:
        numbers = {int(n) for n in re.findall(r"\d+", full)}
        if numbers and numbers != {own.number}:
            result.warnings.append(
                "В метке гистологии стоит номер другой пробы (вероятно, копипаст)"
            )
    return result


def parse_shrimp_samples(histology, smear, specimen_label: str) -> list[SampleSpec]:
    """Образцы креветок (серия mr): колонки таблицы использованы по-другому.

    В «гистологии» — кусочки мышц и жабр в формалине, в «мазке» — внутренности в заморозке.

    >>> samples = parse_shrimp_samples(
    ...     "Кусок мышц, кусок жабр в 10% form.", "Кишечник hepatopancreas ... заморозили", "1 mr")
    >>> [s.organ for s in samples]
    ['мышцы', 'жабры', 'кишечник, гепатопанкреас, карапакс']
    """
    samples = []
    if isinstance(histology, str) and not is_blank(histology):
        for part in histology.split(","):
            organ = organ_of(part)
            if organ:
                samples.append(SampleSpec("гистология", organ, specimen_label, "формалин 10%"))
    if isinstance(smear, str) and not is_blank(smear):
        samples.append(
            SampleSpec("другое", "кишечник, гепатопанкреас, карапакс", specimen_label, "заморозка")
        )
    return samples


# ---------------------------------------------------------------------------
# Лаборатории (куда отправлены образцы)
# ---------------------------------------------------------------------------

LAB_RUSSIA = "Россия"
LAB_TROPCENTER = "Тропцентр"
LAB_CANTHO = "Кантхо (секвенирование)"
LAB_VN_COMPANY = "Вьетнамская компания (ген. анализ)"


def parse_labs(comment) -> list[str]:
    """Лаборатории из комментария к шапке DNA/мазка или к ячейке особи.

    >>> parse_labs("1 - в Россию, 1 - в тропцентре, 1 - в Кантхо на секвенировании")
    ['Россия', 'Тропцентр', 'Кантхо (секвенирование)']
    >>> parse_labs("Отправили на ген.анализ во Вьетамскую компанию")
    ['Вьетнамская компания (ген. анализ)']
    """
    if not isinstance(comment, str):
        return []
    low = comment.lower()
    labs = []
    if "росси" in low:
        labs.append(LAB_RUSSIA)
    if "тропцентр" in low:
        labs.append(LAB_TROPCENTER)
    if "кантхо" in low and "секвен" in low:
        labs.append(LAB_CANTHO)
    if "ген" in low and "компани" in low:
        labs.append(LAB_VN_COMPANY)
    return labs


# ---------------------------------------------------------------------------
# Траления, сеть, координаты
# ---------------------------------------------------------------------------


def parse_trawl_point(text) -> tuple[int | None, str] | None:
    """Строка в колонке «№ траления»: (номер, 'старт'|'финиш'|'точка') или None.

    >>> parse_trawl_point("старт 3"), parse_trawl_point("1 финиш")
    ((3, 'старт'), (1, 'финиш'))
    >>> parse_trawl_point("точка лова рыбака")
    (None, 'точка')
    """
    if not isinstance(text, str):
        return None
    low = normalize_space(text.lower())
    match = re.fullmatch(r"(?:(\d+)\s*(старт|финиш)|(старт|финиш)\s*(\d+))", low)
    if match:
        return int(match.group(1) or match.group(4)), match.group(2) or match.group(3)
    if "точка лова рыбака" in low:
        return None, "точка"
    return None


def parse_gear(comment) -> tuple[str | None, str | None]:
    """Размер ячеи и размер сети из комментария к шапке «№ траления».

    Кириллическая «х» заменяется латинской «x», как в схеме ('5x5').

    >>> parse_gear("Сеть: размер ячеи 3,5х3,5 см., сама сеть 6,5х12 м.")
    ('3,5x3,5', '6,5x12')
    """
    if not isinstance(comment, str):
        return None, None
    size_re = r"([\d,]+\s*[хx]\s*[\d,]+)"
    mesh = re.search(r"ячеи\s*" + size_re, comment)
    size = re.search(r"сеть\s*" + size_re, comment)

    def norm(m):
        return m.group(1).replace(" ", "").replace("х", "x") if m else None

    return norm(mesh), norm(size)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние между двумя точками (широта, долгота) в километрах.

    Приближение для небольших расстояний — точности хватает, чтобы решить,
    «то же это место (≤ 200 м) или нет».

    >>> round(distance_km((10.0, 106.0), (10.0, 106.01)), 2)
    1.1
    """
    lat1, lon1 = a
    lat2, lon2 = b
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


# ---------------------------------------------------------------------------
# Улов и прилов
# ---------------------------------------------------------------------------

# корень названия в тексте → как записать таксон
_CATCH_TAXA = (
    (r"plotosus|плотосус\w*", "Plotosus"),
    (r"pangasius|пангасиус\w*", "Pangasius"),
    (r"piaractus", "Piaractus"),
    (r"стоматопод\w*", "Stomatopoda"),
)
_BOUGHT_WORDS = ("купил", "взял", "у рыбак", "у другого")
_SHORE_WORDS = ("берег", "завод", "сказал", "перерыв")
_CATCH_WORDS = (
    "рыб", "улов", "поймал", "трал", "pangas", "plotos", "пангас", "плотос",
    "креветок", "карпов", "parambasis", "polynemus", "piaractus", "экземпляр",
)  # fmt: skip


def parse_catch_counts(text) -> list[tuple[str, int]]:
    """Числа особей из комментария к тралению.

    Купленное или взятое у рыбаков — не улов траления, такие фрагменты пропускаются.

    >>> parse_catch_counts("2 плотосуса, 2 пангасиуса, 1 стоматопода")
    [('Plotosus', 2), ('Pangasius', 2), ('Stomatopoda', 1)]
    >>> parse_catch_counts("5 pangasius sp, scaptophagus; купили у другого рыбака 2 плотосуса")
    [('Pangasius', 5)]
    """
    if not isinstance(text, str):
        return []
    found = []
    for segment in text.lower().split(";"):
        if any(word in segment for word in _BOUGHT_WORDS):
            continue
        for match in re.finditer(r"(\d+)\s+(\w+)", segment):
            count, word = int(match.group(1)), match.group(2)
            for pattern, taxon in _CATCH_TAXA:
                if count > 0 and re.fullmatch(pattern, word):
                    found.append((taxon, count))
    return found


def is_catch_comment(text) -> bool:
    """Комментарий к тралению — про улов (→ catch_note), а не про берег/обстановку (→ notes).

    >>> is_catch_comment("трал пустой, рыбы нет"), is_catch_comment("На правом берегу драги")
    (True, False)
    >>> is_catch_comment("Купили 5 Plotosus у рыбаков")
    False
    """
    if not isinstance(text, str):
        return False
    if parse_catch_counts(text):
        return True
    low = text.lower()
    if any(word in low for word in _BOUGHT_WORDS + _SHORE_WORDS):
        return False
    return any(word in low for word in _CATCH_WORDS)


@dataclass(frozen=True)
class CatchNote:
    """Заметка об улове под таблицей особей."""

    taxon_text: str | None
    count: int | None
    mass_g: float | None
    trawl_no: int | None


_CATCH_NOTE_RE = re.compile(r"шт|^m\s|вес|креветк|краб|угорь|рыба-жаба|рыбы sp")


def is_catch_note(text) -> bool:
    """Строка под таблицей особей — заметка об улове (а не служебная заметка).

    >>> is_catch_note("угорь - 4 шт"), is_catch_note("52 - 65 (вкл) - только гистология")
    (True, False)
    """
    if not isinstance(text, str):
        return False
    low = normalize_space(text.lower())
    return bool(_CATCH_NOTE_RE.search(low)) and not low.startswith("пробы ")


def parse_catch_note(text: str) -> CatchNote:
    """Разобрать заметку об улове.

    >>> parse_catch_note("arius maculatus - 39 шт")
    CatchNote(taxon_text='arius maculatus', count=39, mass_g=None, trawl_no=None)
    >>> parse_catch_note("m креветок: 1754 г.")
    CatchNote(taxon_text='креветок', count=None, mass_g=1754.0, trawl_no=None)
    >>> parse_catch_note("краб (3 траление) в DNA пробирке 30кр")
    CatchNote(taxon_text='краб', count=None, mass_g=None, trawl_no=3)
    """
    t = normalize_space(text)
    low = t.lower()

    match = re.search(r"\((\d+)\s*траление\)", low)
    trawl_no = int(match.group(1)) if match else None

    # масса: «m креветок: 1754 г.», «Общий вес рыбы: 1737г.», «m рыбы 2417»
    if low.startswith("m ") or "вес" in low:
        numbers = re.findall(r"\d+(?:[.,]\d+)?", t)
        mass = float(numbers[-1].replace(",", ".")) if numbers else None
        taxon = re.sub(r"^(m|общий вес)\s+", "", t, flags=re.IGNORECASE)
        taxon = re.split(r":|\s\d", taxon)[0].strip() or None
        return CatchNote(taxon, None, mass, trawl_no)

    count = None
    match = re.search(r"(\d+)\s*шт", low)
    if match:
        count = int(match.group(1))
    else:
        match = re.match(r"(\d+)\s+", t)  # «2 креветки (4 траление) …»
        if match:
            count = int(match.group(1))
            t = t[match.end() :]
    taxon = re.split(r"\s[-–]|-\s*\d|\(", t)[0].strip(" -:,")
    return CatchNote(taxon or None, count, None, trawl_no)


# ---------------------------------------------------------------------------
# Листы и города
# ---------------------------------------------------------------------------


def sheet_capture_method(sheet_name: str) -> str:
    """Способ получения рыбы по имени листа.

    >>> sheet_capture_method("Рынок. Чавинь"), sheet_capture_method("Рыбак 1 точка")
    ('рынок', 'рыбак')
    >>> sheet_capture_method("Аквахозяйство 1"), sheet_capture_method("Точка 6.")
    ('аквахозяйство', 'траление')
    """
    low = sheet_name.lower()
    if "рынок" in low:
        return "рынок"
    if "рыбак" in low:
        return "рыбак"
    if "аквахоз" in low:
        return "аквахозяйство"
    return "траление"


@dataclass(frozen=True)
class City:
    name: str
    name_latin: str | None = None


# Варианты написания → единое название. Ключ — в нижнем регистре.
# Латиница указана только там, где она есть в исходной таблице.
CITY_ALIASES = {
    "хонг-нга": City("Хонг-нгу", "Hong Ngu"),
    "хонг-на": City("Хонг-нгу", "Hong Ngu"),
    "хонг-нгу": City("Хонг-нгу", "Hong Ngu"),
    "као-лань": City("Као-Лань", "Cao Lanh"),
    "thot not": City("Thot Not", "Thot Not"),
}


def normalize_city(name: str) -> City:
    """Единое написание города.

    >>> normalize_city("Хонг-на")
    City(name='Хонг-нгу', name_latin='Hong Ngu')
    >>> normalize_city("Кай-Бе")
    City(name='Кай-Бе', name_latin=None)
    """
    name = normalize_space(name)
    return CITY_ALIASES.get(name.lower(), City(name))


def parse_city(header) -> City | None:
    """Город из строки-заголовка листа (после «г.»). Нет города — None.

    >>> parse_city("25.09.25 пр.Донтхап, г. Хонг-нга (Hong-Ngu)").name
    'Хонг-нгу'
    >>> parse_city("11.08.2026 пр. Кантхо (г.Thot not)").name
    'Thot Not'
    >>> parse_city("20.08.2026  пр.Кантхо") is None
    True
    """
    if not isinstance(header, str):
        return None
    # (?<![\w-]) — перед «г.» не должно быть буквы, иначе поймали бы конец другого слова
    match = re.search(r"(?<![\w-])г\.\s*([^,.()]+)", header)
    if not match or not match.group(1).strip():
        return None
    return normalize_city(match.group(1))


def market_city(sheet_name: str) -> City | None:
    """Город рынка из имени листа: 'Рынок.Бенче' → Бенче.

    >>> market_city("Рынок. Чавинь").name
    'Чавинь'
    """
    match = re.match(r"рынок\.?\s*(.+)", sheet_name.strip(), flags=re.IGNORECASE)
    return normalize_city(match.group(1)) if match else None
