"""Безопасные правки базы: привязать траление, добавить определение вида, закрыть проблему.

Правила для каждой правки:
- проверка и запись идут в одной транзакции BEGIN IMMEDIATE (см. write_transaction):
  между «проверили» и «записали» никто другой базу изменить не может;
- при ошибке — понятное сообщение (EditError), транзакция откатывается, база не меняется;
- вместе с изменением перепроверяется журнал проблем (checks.run_checks): например,
  когда у всех особей выезда указано траление, проблема «особи не привязаны» закрывается сама;
- apply_edit() перед правкой делает резервную копию файла базы.

Функции принимают подключение и не печатают ничего сами — их можно будет
вызывать и из будущего веб-интерфейса. Подключение должно быть БЕЗ открытой
транзакции: каждая правка сама начинает и завершает свою (иначе — EditError).
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from biobank.checks import CheckResult, run_checks
from biobank.db import backup_database, connect
from biobank.parsing import parse_label

METHODS = ("морфология", "ДНК", "другое")
CONFIDENCES = ("точно", "предп.", "до рода", "не определён")


class EditError(Exception):
    """Правку нельзя выполнить; текст ошибки — для пользователя."""


class NotFoundError(EditError):
    """Нет такой записи (особи, проблемы, базы) — в вебе это страница 404."""


@dataclass
class EditResult:
    message: str
    checks: CheckResult
    backup: Path | None = None  # резервная копия до правки (заполняет apply_edit)


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Транзакция, которая сразу берёт блокировку записи: BEGIN IMMEDIATE.

    Обычный `with conn:` начинает транзакцию только на первом UPDATE/INSERT,
    а проверки (SELECT) до него идут вне транзакции. Если два человека правят
    одновременно, второй мог бы записать поверх того, что проверил до правки первого.
    С BEGIN IMMEDIATE второй ждёт, пока первый закончит, и проверяет уже новые данные.

    Внутри уже открытой транзакции так нельзя: SQLite не умеет вкладывать BEGIN в BEGIN,
    а COMMIT здесь сохранил бы и чужие, ещё не проверенные изменения вызывающего кода.
    Поэтому в этом случае — EditError, база не меняется.
    """
    if conn.in_transaction:
        raise EditError(
            "Подключение уже в транзакции — правку нельзя выполнить внутри чужой транзакции. "
            "Завершите её (commit или rollback) и повторите."
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def apply_edit(
    db_path: Path | str, action: Callable[[sqlite3.Connection], EditResult]
) -> EditResult:
    """Резервная копия базы → правка action(conn) → результат с путём к копии.

    Ошибка правки (EditError) и ошибка SQLite (база занята, повреждена…) — EditError;
    резервная копия при ошибке удаляется: правки не было, копия не нужна.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise NotFoundError(f"Базы нет: {db_path}. Сначала: python -m biobank import …")
    backup = None
    try:
        backup = backup_database(db_path)
        conn = connect(db_path)
        try:
            result = action(conn)
        finally:
            conn.close()
    except (EditError, sqlite3.Error) as error:
        if backup is not None:
            backup.unlink(missing_ok=True)
        if isinstance(error, EditError):
            raise
        raise EditError(f"Ошибка базы данных: {error}") from error
    result.backup = backup
    return result


def check_database(db_path: Path | str) -> CheckResult:
    """Перепроверить журнал проблем (run_checks) в транзакции BEGIN IMMEDIATE.

    Как и у правок: пока проверки читают базу и обновляют журнал, никто другой
    не может её изменить — журнал соответствует данным. Резервная копия не нужна:
    меняется только data_issue, и повторный запуск даёт тот же результат.
    Ошибка SQLite (база занята, повреждена…) — EditError, журнал не меняется.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise NotFoundError(f"Базы нет: {db_path}. Сначала: python -m biobank import …")
    conn = connect(db_path)
    try:
        with write_transaction(conn):
            return run_checks(conn)
    except sqlite3.Error as error:
        raise EditError(f"Ошибка базы данных, журнал не изменён: {error}") from error
    finally:
        conn.close()


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
    # подсказка: номера с тем же числом ('13ph' → 13, 13 e, 13 pc)
    number = re.match(r"\s*(\d+)", label)
    if number:
        n = str(int(number.group(1)))
        similar = conn.execute(
            "SELECT label FROM specimen WHERE label = ? OR label LIKE ? "
            "ORDER BY specimen_id LIMIT 5",
            (n, f"{n} %"),
        ).fetchall()
    else:  # номер без числа ('ХХ') — по началу текста
        similar = conn.execute(
            "SELECT label FROM specimen WHERE label LIKE ? ORDER BY specimen_id LIMIT 5",
            (f"{label.split()[0]}%" if label.split() else "",),
        ).fetchall()
    hint = f" Похожие: {', '.join(r[0] for r in similar)}." if similar else ""
    raise NotFoundError(f"Особь с номером «{label}» не найдена.{hint}")


# ---------------------------------------------------------------------------
# Правки
# ---------------------------------------------------------------------------


def link_trawl(conn: sqlite3.Connection, label: str, trawl_no: int) -> EditResult:
    """Привязать особь к тралению её же выезда.

    conn — подключение без открытой транзакции (иначе EditError, см. write_transaction).
    """
    with write_transaction(conn):
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
        old = next(t["trawl_no"] for t in trawls if t["trawling_id"] == previous)
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
    species_name = None — ровно тогда, когда уверенность «не определён».
    conn — подключение без открытой транзакции (иначе EditError, см. write_transaction).
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

    if species_name is None and confidence != "не определён":
        raise EditError("Без вида можно указать только уверенность «не определён».")
    if species_name is not None and confidence == "не определён":
        raise EditError(
            "При уверенности «не определён» вид не указывают: вместо вида напишите «?»."
        )

    with write_transaction(conn):
        specimen = find_specimen(conn, label)
        if species_name is None:
            species_id, species_text = None, "(не определён)"
        else:
            species = _find_species(conn, species_name)
            species_id, species_text = species["species_id"], species["scientific_name"]
        old = conn.execute(
            "SELECT scientific_name FROM species WHERE species_id = ?", (specimen["species_id"],)
        ).fetchone()
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
    """Отметить проблему из журнала решённой.

    conn — подключение без открытой транзакции (иначе EditError, см. write_transaction).
    """
    resolution = " ".join(resolution.split())
    if not resolution:
        raise EditError("Напишите, как решили проблему — пустой текст не подходит.")
    with write_transaction(conn):
        issue = conn.execute(
            "SELECT issue_id, description, resolved, resolution FROM data_issue WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        if issue is None:
            raise NotFoundError(
                f"Проблемы №{issue_id} нет в журнале. Список: python -m biobank report issues"
            )
        if issue["resolved"]:
            raise EditError(f"Проблема №{issue_id} уже решена: {issue['resolution']}")
        conn.execute(
            "UPDATE data_issue SET resolved = 1, resolution = ? WHERE issue_id = ?",
            (resolution, issue_id),
        )
        checks = run_checks(conn)
    return EditResult(f"Проблема №{issue_id} закрыта: {issue['description']}", checks)
