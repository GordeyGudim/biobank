"""Безопасные правки базы: привязать траление, добавить определение вида, закрыть проблему.

Правила для каждой правки:
- всё проверяется ДО изменения; при ошибке — понятное сообщение (EditError), база не меняется;
- изменение делается в транзакции вместе с перепроверкой журнала проблем (checks.run_checks):
  например, когда у всех особей выезда указано траление, проблема «особи не привязаны»
  закрывается сама;
- перед правкой CLI делает резервную копию файла базы (backup_database).

Функции принимают подключение и не печатают ничего сами — их можно будет
вызывать и из будущего веб-интерфейса.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from biobank.checks import CheckResult, run_checks
from biobank.parsing import parse_label

METHODS = ("морфология", "ДНК", "другое")
CONFIDENCES = ("точно", "предп.", "до рода", "не определён")


class EditError(Exception):
    """Правку нельзя выполнить; текст ошибки — для пользователя."""


@dataclass
class EditResult:
    message: str
    checks: CheckResult


# ---------------------------------------------------------------------------
# Резервная копия
# ---------------------------------------------------------------------------


def backup_database(db_path: Path, backup_dir: Path | None = None) -> Path:
    """Скопировать базу в output/backups/biobank_ГГГГММДД_ЧЧММСС.db.

    Используется backup() из sqlite3, а не простое копирование файла:
    он делает согласованную копию, даже если базу в этот момент кто-то читает.
    """
    db_path = Path(db_path)
    backup_dir = backup_dir or db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{db_path.stem}_{stamp}.db"
    n = 1
    while target.exists():  # две правки в одну секунду
        target = backup_dir / f"{db_path.stem}_{stamp}_{n}.db"
        n += 1
    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return target


def manual_edits(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько в базе ручных правок, которых нет в Excel (пропадут при повторном импорте).

    Импорт никогда не заполняет trawling_id, создаёт ровно одно определение на особь
    и не закрывает проблемы вручную — по этим признакам правки и видны.
    """
    edits = {
        "особей привязано к тралениям": (
            "SELECT count(*) FROM specimen WHERE trawling_id IS NOT NULL"
        ),
        "определений вида добавлено": (
            "SELECT (SELECT count(*) FROM species_identification) - (SELECT count(*) FROM specimen)"
        ),
        "проблем закрыто вручную": "SELECT count(*) FROM data_issue WHERE resolved = 1 "
        "AND resolution NOT LIKE 'Проверка % больше не находит эту проблему'",
    }
    found = {name: conn.execute(sql).fetchone()[0] for name, sql in edits.items()}
    return {name: n for name, n in found.items() if n}


# ---------------------------------------------------------------------------
# Поиск особи
# ---------------------------------------------------------------------------


def find_specimen(conn: sqlite3.Connection, label: str) -> sqlite3.Row:
    """Особь по номеру пробы; номер нормализуется так же, как при импорте ('26ph' → '26 ph')."""
    parsed = parse_label(label)
    candidates = [parsed.label] if parsed else []
    candidates.append(" ".join(label.split()))
    for candidate in candidates:
        row = conn.execute(
            """
            SELECT s.specimen_id, s.label, s.event_id, s.trawling_id, s.capture_method_id,
                   s.species_id, e.source_sheet, e.event_date
            FROM specimen s JOIN sampling_event e USING (event_id)
            WHERE s.label = ?
            """,
            (candidate,),
        ).fetchone()
        if row:
            return row
    similar = conn.execute(
        "SELECT label FROM specimen WHERE label LIKE ? ORDER BY specimen_id LIMIT 5",
        (f"{label.split()[0]}%" if label.split() else "",),
    ).fetchall()
    hint = f" Похожие: {', '.join(r[0] for r in similar)}." if similar else ""
    raise EditError(f"Особь с номером «{label}» не найдена.{hint}")


# ---------------------------------------------------------------------------
# Правки
# ---------------------------------------------------------------------------


def link_trawl(conn: sqlite3.Connection, label: str, trawl_no: int) -> EditResult:
    """Привязать особь к тралению её же выезда."""
    specimen = find_specimen(conn, label)
    if specimen["capture_method_id"] is not None:
        method = conn.execute(
            "SELECT name FROM capture_method WHERE capture_method_id = ?",
            (specimen["capture_method_id"],),
        ).fetchone()[0]
        raise EditError(
            f"Особь {specimen['label']} получена не тралением ({method}) — "
            "привязывать к тралению нельзя."
        )
    trawls = conn.execute(
        "SELECT trawling_id, trawl_no FROM trawling WHERE event_id = ? ORDER BY trawl_no",
        (specimen["event_id"],),
    ).fetchall()
    if not trawls:
        raise EditError(
            f"У выезда «{specimen['source_sheet']}» ({specimen['event_date']}) нет тралений."
        )
    trawl = next((t for t in trawls if t["trawl_no"] == trawl_no), None)
    if trawl is None:
        numbers = ", ".join(str(t["trawl_no"]) for t in trawls)
        raise EditError(
            f"На выезде «{specimen['source_sheet']}» нет траления {trawl_no}. Есть: {numbers}."
        )

    previous = specimen["trawling_id"]
    with conn:
        conn.execute(
            "UPDATE specimen SET trawling_id = ? WHERE specimen_id = ?",
            (trawl["trawling_id"], specimen["specimen_id"]),
        )
        checks = run_checks(conn)
    if previous is None:
        message = f"Особь {specimen['label']} привязана к тралению {trawl_no}"
    elif previous == trawl["trawling_id"]:
        message = f"Особь {specimen['label']} уже была привязана к тралению {trawl_no}"
    else:
        old = conn.execute(
            "SELECT trawl_no FROM trawling WHERE trawling_id = ?", (previous,)
        ).fetchone()[0]
        message = f"Особь {specimen['label']}: траление {old} → {trawl_no}"
    return EditResult(f"{message} (выезд «{specimen['source_sheet']}»).", checks)


def _words(name: str) -> list[str]:
    return name.lower().replace(".", " ").split()


def _find_species(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    """Вид из справочника: полное название или сокращение рода ('P. elongatus')."""
    rows = conn.execute("SELECT species_id, scientific_name FROM species").fetchall()
    wanted = _words(name)
    for row in rows:
        if _words(row["scientific_name"]) == wanted:
            return row
    if len(wanted) == 2 and len(wanted[0]) == 1:  # сокращение: «P. elongatus»
        matches = [
            r for r in rows
            if _words(r["scientific_name"])[0].startswith(wanted[0])
            and _words(r["scientific_name"])[1:] == wanted[1:]
        ]  # fmt: skip
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = "; ".join(r["scientific_name"] for r in matches)
            raise EditError(f"Сокращение «{name}» подходит к нескольким видам: {names}.")
    names = "; ".join(r["scientific_name"] for r in rows)
    raise EditError(
        f"Вида «{name}» нет в справочнике. Есть: {names}. "
        "Новый вид добавьте в справочник species (это отдельное решение)."
    )


def identify(
    conn: sqlite3.Connection,
    label: str,
    species_name: str | None,
    method: str,
    confidence: str,
    identified_by: str | None = None,
    identified_on: str | None = None,
    notes: str | None = None,
) -> EditResult:
    """Добавить запись в историю определений и сделать её текущим видом особи.

    Старые определения не удаляются — история сохраняется.
    species_name = None допустимо только при уверенности «не определён».
    """
    if method not in METHODS:
        raise EditError(f"Метод «{method}» не подходит. Возможные: {', '.join(METHODS)}.")
    if confidence not in CONFIDENCES:
        raise EditError(
            f"Уверенность «{confidence}» не подходит. Возможные: {', '.join(CONFIDENCES)}."
        )
    if identified_on is not None:
        try:
            identified_on = dt.date.fromisoformat(identified_on).isoformat()
        except ValueError:
            raise EditError(
                f"Дата «{identified_on}» не в формате ГГГГ-ММ-ДД (например 2026-10-01)."
            ) from None
    else:
        identified_on = dt.date.today().isoformat()

    specimen = find_specimen(conn, label)
    if species_name is None:
        if confidence != "не определён":
            raise EditError("Без вида можно указать только уверенность «не определён».")
        species_id, species_text = None, "(не определён)"
    else:
        species = _find_species(conn, species_name)
        species_id, species_text = species["species_id"], species["scientific_name"]

    old = conn.execute(
        "SELECT scientific_name FROM species WHERE species_id = ?", (specimen["species_id"],)
    ).fetchone()
    with conn:
        conn.execute(
            "INSERT INTO species_identification (specimen_id, species_id, method, confidence, "
            "identified_by, identified_on, raw_text, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (specimen["specimen_id"], species_id, method, confidence, identified_by,
             identified_on, species_name, notes),
        )  # fmt: skip
        conn.execute(
            "UPDATE specimen SET species_id = ? WHERE specimen_id = ?",
            (species_id, specimen["specimen_id"]),
        )
        checks = run_checks(conn)
    was = old[0] if old else "(не определён)"
    return EditResult(
        f"Особь {specimen['label']}: {was} → {species_text} ({method}, {confidence}).", checks
    )


def resolve_issue(conn: sqlite3.Connection, issue_id: int, resolution: str) -> EditResult:
    """Отметить проблему из журнала решённой."""
    resolution = " ".join(resolution.split())
    if not resolution:
        raise EditError("Напишите, как решили проблему — пустой текст не подходит.")
    issue = conn.execute(
        "SELECT issue_id, description, resolved, resolution FROM data_issue WHERE issue_id = ?",
        (issue_id,),
    ).fetchone()
    if issue is None:
        raise EditError(
            f"Проблемы №{issue_id} нет в журнале. Список: python -m biobank report issues"
        )
    if issue["resolved"]:
        raise EditError(f"Проблема №{issue_id} уже решена: {issue['resolution']}")
    with conn:
        conn.execute(
            "UPDATE data_issue SET resolved = 1, resolution = ? WHERE issue_id = ?",
            (resolution, issue_id),
        )
        checks = run_checks(conn)
    return EditResult(f"Проблема №{issue_id} закрыта: {issue['description']}", checks)
