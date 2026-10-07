"""Проверки данных в готовой базе → журнал data_issue.

Импорт сам записывает проблемы, которые видны только в Excel (число стало датой,
перепутаны колонки, копипаст в метке). Здесь — проверки по содержимому базы:
их можно запускать снова после ручных правок (`python -m biobank check`).

Повторный запуск не плодит дубликаты:
- найденная проблема, которая уже есть в журнале, не добавляется второй раз
  (и если пользователь пометил её решённой — так и остаётся);
- открытая проблема, которую проверка больше не находит (данные исправили),
  автоматически закрывается с пометкой в resolution.

Проблемы проверок отличаются от проблем импорта текстом описания: у каждой
проверки он постоянный (числа — в raw_value), см. CHECK_DESCRIPTIONS.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from biobank.excel_reader import Issue

# Границы коэффициента упитанности K = 100·m / TL³ (у рыб обычно 0,4–1,0)
CONDITION_MIN, CONDITION_MAX = 0.2, 5.0
SL_MIN_SHARE = 0.6  # SL меньше 60 % TL — подозрительно мало

DESC_SL_GT_TL = "SL больше TL"
DESC_SL_TOO_SMALL = "SL слишком мала для такой TL"
DESC_GUTTED_GT_MASS = "Масса без внутренних органов больше полной массы"
DESC_COPIED = "Массы совпадают с длинами — вероятно, скопированы"
DESC_CONDITION = "Длина и масса не согласуются (коэффициент упитанности вне 0,2–5)"
DESC_SUFFIX = "Суффикс в номере пробы не соответствует виду"
DESC_DUPLICATE = "Номера повторяются в сквозной серии (разные суффиксы)"
DESC_GAPS = "Номера пропущены в сквозной серии"
DESC_NO_TRAWL = "Особи не привязаны к тралениям — восстановить по полевому журналу"

CHECK_DESCRIPTIONS = (
    DESC_SL_GT_TL, DESC_SL_TOO_SMALL, DESC_GUTTED_GT_MASS, DESC_COPIED, DESC_CONDITION,
    DESC_SUFFIX, DESC_DUPLICATE, DESC_GAPS, DESC_NO_TRAWL,
)  # fmt: skip

COLLAPSE_THRESHOLD = 4  # ≥ 4 одинаковых проблем на листе → одна запись с перечнем


def _fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:g}"


# ---------------------------------------------------------------------------
# Сами проверки. Каждая возвращает список Issue.
# ---------------------------------------------------------------------------

_SPECIMEN_SQL = """
    SELECT s.specimen_id, s.label, s.tl_cm, s.sl_cm, s.mass_g, s.mass_gutted_g,
           s.source_row, e.source_sheet
    FROM specimen s JOIN sampling_event e USING (event_id)
    ORDER BY s.specimen_id
"""


def check_measurements(conn: sqlite3.Connection) -> list[Issue]:
    """Промеры: SL и TL, массы, согласованность длины и массы."""
    issues = []
    for r in conn.execute(_SPECIMEN_SQL):
        tl, sl, m, mg = r["tl_cm"], r["sl_cm"], r["mass_g"], r["mass_gutted_g"]
        values = f"TL {_fmt(tl)}, SL {_fmt(sl)}, m {_fmt(m)}, m без внутр. {_fmt(mg)}"

        def add(description, *, _r=r, _values=values):
            issues.append(
                Issue("промеры", description, _r["source_sheet"], None, _r["label"],
                      _values, "specimen", _r["specimen_id"])
            )  # fmt: skip

        if tl and sl and sl > tl:
            add(DESC_SL_GT_TL)
        elif tl and sl and sl < SL_MIN_SHARE * tl:
            add(DESC_SL_TOO_SMALL)
        if m is not None and mg is not None and mg > m:
            add(DESC_GUTTED_GT_MASS)
        if tl and m is not None and m == tl and (mg is None or mg == sl):
            add(DESC_COPIED)
        if tl and m:
            k = 100 * m / tl**3
            if not CONDITION_MIN <= k <= CONDITION_MAX:
                add(DESC_CONDITION)
    return issues


def check_suffix_species(conn: sqlite3.Connection) -> list[Issue]:
    """Суффикс номера (e, ph, mc, pc, Bg, mr) должен соответствовать коду вида.

    Суффикс 'sp' и номер без суффикса ничего не утверждают о виде — их не проверяем.
    """
    rows = conn.execute(
        """
        SELECT s.specimen_id, s.label, sp.scientific_name, sp.code, e.source_sheet,
               trim(substr(s.label, instr(s.label, ' ') + 1)) AS suffix
        FROM specimen s
        JOIN sampling_event e USING (event_id)
        LEFT JOIN species sp USING (species_id)
        WHERE s.label LIKE '% %' AND s.label NOT LIKE 'ХХ%'
        ORDER BY s.specimen_id
        """
    )
    issues = []
    for r in rows:
        if r["suffix"] in ("sp", "") or r["suffix"] == r["code"]:
            continue
        issues.append(
            Issue("вид", DESC_SUFFIX, r["source_sheet"], None, r["label"],
                  r["scientific_name"] or "(не определён)", "specimen", r["specimen_id"])
        )  # fmt: skip
    return issues


def _ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 7, 9, 10] → '1–3, 7, 9–10'."""
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(f"{start}–{n}" if n != start else str(n))
            start = None
    return ", ".join(parts)


def check_numbering(conn: sqlite3.Connection) -> list[Issue]:
    """Сквозная нумерация серий: повторы номеров и пропуски."""
    by_series: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in conn.execute(
        "SELECT series, series_no, label FROM specimen WHERE series_no IS NOT NULL "
        "ORDER BY series, series_no, label"
    ):
        by_series[r["series"]][r["series_no"]].append(r["label"])

    issues = []
    for series, numbers in sorted(by_series.items()):
        duplicates = {n: labels for n, labels in numbers.items() if len(labels) > 1}
        if duplicates:
            listed = "; ".join(" = ".join(labels) for labels in duplicates.values())
            issues.append(
                Issue("номера", DESC_DUPLICATE, None, None, f"серия {series}",
                      listed, "specimen")
            )  # fmt: skip
        missing = [n for n in range(1, max(numbers) + 1) if n not in numbers]
        if missing:
            issues.append(
                Issue("номера", DESC_GAPS, None, None, f"серия {series}",
                      _ranges(missing), "specimen")
            )  # fmt: skip
    return issues


def check_trawl_links(conn: sqlite3.Connection) -> list[Issue]:
    """Выезды-траления, где у особей не указано траление (по записи на выезд).

    Число особей в запись не пишем: иначе каждая привязка меняла бы запись,
    и журнал засорялся бы закрытыми дублями. Запись закрывается сама,
    когда привязаны все особи выезда (или её закрывают вручную).
    """
    rows = conn.execute(
        """
        SELECT e.event_id, e.source_sheet
        FROM sampling_event e
        JOIN specimen s ON s.event_id = e.event_id
            AND s.trawling_id IS NULL AND s.capture_method_id IS NULL
        WHERE EXISTS (SELECT 1 FROM trawling t WHERE t.event_id = e.event_id)
        GROUP BY e.event_id
        ORDER BY e.event_id
        """
    )
    return [
        Issue("связь", DESC_NO_TRAWL, r["source_sheet"], None, None, None,
              "sampling_event", r["event_id"])
        for r in rows
    ]  # fmt: skip


CHECKS = (check_measurements, check_suffix_species, check_numbering, check_trawl_links)


# ---------------------------------------------------------------------------
# Запуск и обновление журнала
# ---------------------------------------------------------------------------


def collapse_issues(issues: list[Issue]) -> list[Issue]:
    """Свернуть повторяющиеся проблемы: ≥ 4 одинаковых на листе → одна запись с перечнем.

    Проблемы без листа Excel (записи, введённые не из Excel, и проверки всей базы)
    не сворачиваются: «один лист» для них не означает «одно место в таблице».

    Порядок сохраняется: запись без листа остаётся на своём месте, группа встаёт туда,
    где была её первая запись. От порядка зависят номера (issue_id) в свежей базе —
    на них ссылаются README и заметки пользователя.
    """
    groups: dict[tuple, list[Issue]] = defaultdict(list)
    # порядок вывода: отдельная проблема (Issue) или ключ группы (tuple)
    order: list[Issue | tuple] = []
    for issue in issues:
        if issue.sheet is None:
            order.append(issue)
            continue
        key = (issue.category, issue.sheet, issue.description, issue.table_name)
        if key not in groups:
            order.append(key)
        groups[key].append(issue)

    result = []
    for item in order:
        if isinstance(item, Issue):
            result.append(item)
        else:
            result.extend(_collapse_group(item, groups[item]))
    return result


def _collapse_group(key: tuple, items: list[Issue]) -> list[Issue]:
    """Группа одинаковых проблем листа: меньше порога — как есть, иначе одна запись."""
    if len(items) < COLLAPSE_THRESHOLD:
        return items
    category, sheet, description, table = key
    objects = ", ".join(i.object_label or i.cell or "?" for i in items)
    cells = [i.cell for i in items if i.cell]
    return [
        Issue(
            category,
            f"{description} ({len(items)} шт.)",
            sheet,
            f"{cells[0]}…{cells[-1]}" if cells else None,
            objects,
            items[0].raw_value,
            table,
        )
    ]


def find_issues(conn: sqlite3.Connection) -> list[Issue]:
    """Запустить все проверки и вернуть найденное (без записи в базу)."""
    found = []
    for check in CHECKS:
        found.extend(check(conn))
    return collapse_issues(found)


def is_check_issue(description: str) -> bool:
    """Запись журнала создана проверкой (а не импортом)."""
    return description.startswith(CHECK_DESCRIPTIONS)


@dataclass
class CheckResult:
    added: int  # новые проблемы
    known: int  # уже были в журнале
    closed: int  # были открыты, но больше не находятся


# Ключ проблемы: по нему повторный запуск узнаёт уже известную запись журнала.
# table_name и record_id различают записи без листа Excel (например, два выезда,
# введённые через веб, — у обоих source_sheet пустой).
_KEY_COLUMNS = (
    "category", "sheet_name", "object_label", "raw_value", "description", "table_name", "record_id",
)  # fmt: skip


def _issue_key(i: Issue) -> tuple:
    return (i.category, i.sheet, i.object_label, i.raw_value, i.description, i.table_name,
            i.record_id)  # fmt: skip


def run_checks(conn: sqlite3.Connection, today: str | None = None) -> CheckResult:
    """Обновить data_issue по результатам проверок. Вызывать внутри транзакции."""
    today = today or dt.date.today().isoformat()
    found = find_issues(conn)
    existing = {}
    # список колонок — фиксированный текст из кода, не данные пользователя
    for r in conn.execute(f"SELECT issue_id, resolved, {', '.join(_KEY_COLUMNS)} FROM data_issue"):
        if is_check_issue(r["description"]):
            existing[tuple(r[c] for c in _KEY_COLUMNS)] = r

    added = known = 0
    found_keys = set()
    for i in found:
        key = _issue_key(i)
        found_keys.add(key)
        if key in existing:
            known += 1
            continue
        conn.execute(
            "INSERT INTO data_issue (category, table_name, record_id, sheet_name, cell, "
            "object_label, raw_value, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (i.category, i.table_name, i.record_id, i.sheet, i.cell, i.object_label,
             i.raw_value, i.description),
        )  # fmt: skip
        added += 1

    closed = 0
    for key, r in existing.items():
        if key not in found_keys and not r["resolved"]:
            conn.execute(
                "UPDATE data_issue SET resolved = 1, resolution = ? WHERE issue_id = ?",
                (f"Проверка {today} больше не находит эту проблему", r["issue_id"]),
            )
            closed += 1
    return CheckResult(added, known, closed)


def summary_by_category(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Сколько проблем в каждой категории: всего, открыто, решено."""
    return conn.execute(
        """
        SELECT category,
               count(*) AS total,
               sum(resolved = 0) AS open,
               sum(resolved = 1) AS resolved
        FROM data_issue
        GROUP BY category
        ORDER BY open DESC, category
        """
    ).fetchall()
