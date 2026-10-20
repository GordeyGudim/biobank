"""Безопасные правки базы: привязать траление, добавить определение вида, закрыть проблему.

Правила для каждой правки:
- проверка и запись идут в одной транзакции BEGIN IMMEDIATE (см. write_transaction):
  между «проверили» и «записали» никто другой базу изменить не может;
- при ошибке — понятное сообщение (EditError), транзакция откатывается, база не меняется;
- вместе с изменением перепроверяется журнал проблем (checks.run_checks): например,
  когда у всех особей выезда указано траление, проблема «особи не привязаны» закрывается сама;
- apply_edit() перед правкой делает резервную копию файла базы (вид pre-op, см. db.py;
  лишние старые копии удаляются по правилу хранения).

Функции принимают подключение и не печатают ничего сами — их можно будет
вызывать и из будущего веб-интерфейса. Подключение должно быть БЕЗ открытой
транзакции: каждая правка сама начинает и завершает свою (иначе — EditError).

Кто правит — параметр user (User или user_id; None — системный режим команд CLI, см.
audit.py). Права проверяются внутри транзакции (нет прав — PermissionDenied), изменения
пишутся в журнал изменений change_log.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from biobank import audit
from biobank.audit import UserRef, write_transaction
from biobank.checks import CheckResult, run_checks
from biobank.db import (
    FUTURE_WARNING,
    PRE_OP,
    backup_database,
    connect,
    find_future_backups,
    now_utc,
    prune_backups,
)
from biobank.errors import EditError, NotFoundError, PermissionDenied
from biobank.parsing import normalize_space, parse_label
from biobank.permissions import MERGE_PEOPLE, RESOLVE, require

log = logging.getLogger(__name__)

METHODS = ("морфология", "ДНК", "другое")
CONFIDENCES = ("точно", "предп.", "до рода", "не определён")


# EditError, NotFoundError, PermissionDenied, write_transaction — здесь для совместимости:
# их по-прежнему импортируют из biobank.edit
__all__ = ["EditError", "NotFoundError", "PermissionDenied", "UserRef", "write_transaction"]


@dataclass
class EditResult:
    message: str
    checks: CheckResult
    backup: Path | None = None  # резервная копия до правки (заполняет apply_edit)
    record_id: int | None = None  # id созданной записи (функции ввода из entry.py)
    record_ids: list[int] = field(default_factory=list)  # id всех созданных (пакетный ввод)
    # мягкие предупреждения: запись сохранена, проблема — в журнале data_issue
    warnings: list[str] = field(default_factory=list)
    # служебные предупреждения не о данных (сбой часов и т.п.; заполняет apply_edit)
    notices: list[str] = field(default_factory=list)


def apply_edit(
    db_path: Path | str, action: Callable[[sqlite3.Connection], EditResult]
) -> EditResult:
    """Резервная копия базы → правка action(conn) → результат с путём к копии.

    Копия — вида pre-op (перед опасной операцией): пока нет журнала изменений (этап D3),
    она делается перед каждой правкой. После удачной правки старые pre-op копии
    подчищаются по правилу хранения (db.RETENTION: 30 дней, но не меньше 20 последних).
    Ошибка правки (EditError) и ошибка SQLite (база занята, повреждена…) — EditError;
    резервная копия при ошибке удаляется: правки не было, копия не нужна.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise NotFoundError(f"Базы нет: {db_path}. Сначала создайте её импортом из Excel.")
    backup = None
    try:
        backup = backup_database(db_path, kind=PRE_OP)
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
    try:
        prune_backups(db_path, PRE_OP, keep=[backup])
        future = find_future_backups(db_path, [PRE_OP])
    except OSError:
        future = []  # правка уже сохранена; лишние копии удалит следующая правка или backup
    if future:
        notice = f"{FUTURE_WARNING} ({len(future)} шт. в {future[0].parent})."
        result.notices.append(notice)
        log.warning(notice)  # на сервере попадёт в журнал сайта
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
        raise NotFoundError(f"Базы нет: {db_path}. Сначала создайте её импортом из Excel.")
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


def event_title(source_sheet: str | None, event_id: int, event_date: str | None) -> str:
    """Как назвать выезд в сообщении: лист Excel или номер (у выездов, введённых вручную).

    >>> event_title("Точка 1", 1, "2025-09-25"), event_title(None, 25, "2026-11-05")
    ('«Точка 1»', '№25 от 2026-11-05')
    """
    return f"«{source_sheet}»" if source_sheet else f"№{event_id} от {event_date}"


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


def link_trawl(
    conn: sqlite3.Connection, label: str, trawl_no: int, *, user: UserRef = None
) -> EditResult:
    """Привязать особь к тралению её же выезда (это правка особи: права — на особь).

    conn — подключение без открытой транзакции (иначе EditError, см. write_transaction).
    """
    with write_transaction(conn, user):
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
        event = event_title(specimen["source_sheet"], specimen["event_id"], specimen["event_date"])
        if not trawls:
            raise EditError(f"У выезда {event} ({specimen['event_date']}) нет тралений.")
        trawl = next((t for t in trawls if t["trawl_no"] == trawl_no), None)
        if trawl is None:
            numbers = ", ".join(str(t["trawl_no"]) for t in trawls)
            raise EditError(f"На выезде {event} нет траления {trawl_no}. Есть: {numbers}.")

        previous = specimen["trawling_id"]
        audit.update(
            conn, "specimen", {"specimen_id": specimen["specimen_id"]},
            {"trawling_id": trawl["trawling_id"]},
        )  # fmt: skip
        checks = run_checks(conn)
    if previous is None:
        message = f"Особь {specimen['label']} привязана к тралению {trawl_no}"
    elif previous == trawl["trawling_id"]:
        message = f"Особь {specimen['label']} уже была привязана к тралению {trawl_no}"
    else:
        old = next(t["trawl_no"] for t in trawls if t["trawling_id"] == previous)
        message = f"Особь {specimen['label']}: траление {old} → {trawl_no}"
    return EditResult(f"{message} (выезд {event}).", checks)


def _words(name: str) -> list[str]:
    return name.lower().replace(".", " ").split()


def find_species(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    """Вид из справочника: полное название или сокращение рода ('P. elongatus')."""
    rows = conn.execute("SELECT species_id, scientific_name, code FROM species").fetchall()
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


def _clean(text: str | None) -> str | None:
    """Пробелы по краям и двойные внутри убраны; пустая строка — None.

    >>> _clean("  Слынько   Е.Е. "), _clean("  "), _clean(None)
    ('Слынько Е.Е.', None, None)
    """
    if text is None:
        return None
    return normalize_space(str(text)) or None


def find_or_create_person(conn: sqlite3.Connection, name: str | None) -> tuple[int | None, bool]:
    """Человек из справочника person по имени: (person_id, создан ли сейчас).

    Ищется ТОЧНОЕ совпадение full_name (после уборки лишних пробелов); нет такого —
    создаётся новая запись person (только full_name). Пустое имя — (None, False).
    Если людей с таким именем несколько (одно имя в разных организациях) — EditError:
    по имени не понять, о ком речь. Вызывать внутри транзакции записи.
    Похожие имена («Иванов А.» и «Иванов А.А.») сейчас не сравниваются — будет новый человек.
    """
    full_name = _clean(name)
    if full_name is None:
        return None, False
    rows = conn.execute(
        "SELECT person_id, organization FROM person WHERE full_name = ?", (full_name,)
    ).fetchall()
    if len(rows) > 1:
        places = "; ".join(r["organization"] or "без организации" for r in rows)
        raise EditError(
            f"В справочнике людей несколько «{full_name}» ({places}) — по имени не понять, "
            "кто из них. Уточните запись в справочнике."
        )
    if rows:
        return rows[0]["person_id"], False
    return audit.insert(conn, "person", full_name=full_name), True


def merge_people(
    conn: sqlite3.Connection, keep_id: int, remove_id: int, *, user: UserRef = None
) -> EditResult:
    """Объединить двух людей справочника (дубль из-за опечатки): remove_id → keep_id.

    Все ссылки на remove_id (кто определил вид, участник экспедиции, учётная запись сайта)
    переводятся на keep_id, затем remove_id удаляется. Если оба были участниками одной
    экспедиции — остаётся участие keep_id (роль в экспедиции у remove_id переносится,
    только если у keep_id она пустая). Если у обоих есть учётные записи сайта — EditError:
    это два пользователя, их сначала разбирает админ.
    Права: только куратор (слияние людей). Всё — в журнал изменений.
    """
    if keep_id == remove_id:
        raise EditError("Это один и тот же человек — объединять нечего.")
    with write_transaction(conn, user) as actor:
        if actor is not None:
            require(actor, MERGE_PEOPLE, "person")
        people = {
            r["person_id"]: r
            for r in conn.execute(
                "SELECT person_id, full_name FROM person WHERE person_id IN (?, ?)",
                (keep_id, remove_id),
            )
        }
        for person_id in (keep_id, remove_id):
            if person_id not in people:
                raise NotFoundError(f"Человека №{person_id} нет в справочнике.")
        accounts = conn.execute(
            "SELECT person_id, user_id FROM app_user WHERE person_id IN (?, ?)",
            (keep_id, remove_id),
        ).fetchall()
        if {r["person_id"] for r in accounts} == {keep_id, remove_id}:
            raise EditError(
                "У обоих людей есть учётные записи сайта — это, похоже, два разных "
                "пользователя. Сначала разберитесь с учётными записями (админ)."
            )
        moved = 0
        for r in conn.execute(
            "SELECT identification_id FROM species_identification WHERE identified_by = ?",
            (remove_id,),
        ).fetchall():
            audit.update(
                conn, "species_identification", {"identification_id": r[0]},
                {"identified_by": keep_id}, derived=True,
            )  # fmt: skip
            moved += 1
        for r in conn.execute(
            "SELECT expedition_id, role_text FROM expedition_member WHERE person_id = ?",
            (remove_id,),
        ).fetchall():
            key = {"expedition_id": r["expedition_id"], "person_id": keep_id}
            kept = conn.execute(
                "SELECT role_text FROM expedition_member WHERE expedition_id = ? AND person_id = ?",
                (r["expedition_id"], keep_id),
            ).fetchone()
            if kept is None:
                audit.insert(conn, "expedition_member", **key, role_text=r["role_text"])
            elif kept["role_text"] is None and r["role_text"] is not None:
                audit.update(
                    conn, "expedition_member", key, {"role_text": r["role_text"]}, derived=True
                )
            audit.delete(
                conn, "expedition_member",
                {"expedition_id": r["expedition_id"], "person_id": remove_id}, derived=True,
            )  # fmt: skip
        for r in accounts:
            if r["person_id"] == remove_id:
                audit.update(
                    conn, "app_user", {"user_id": r["user_id"]}, {"person_id": keep_id},
                    derived=True,
                )  # fmt: skip
        audit.delete(conn, "person", {"person_id": remove_id}, derived=True)
        checks = run_checks(conn)
    message = (
        f"«{people[remove_id]['full_name']}» (№{remove_id}) объединён с "
        f"«{people[keep_id]['full_name']}» (№{keep_id}); перенесено определений вида: {moved}."
    )
    return EditResult(message, checks, record_id=keep_id)


def identify(
    conn: sqlite3.Connection,
    label: str,
    species_name: str | None,
    method: str,
    confidence: str,
    identified_by: str | None = None,
    identified_on: str | None = None,
    notes: str | None = None,
    *,
    user: UserRef = None,
) -> EditResult:
    """Добавить запись в историю определений и сделать её текущим видом особи.

    Старые определения не удаляются — история сохраняется.
    species_name = None — ровно тогда, когда уверенность «не определён».
    identified_by — имя того, кто определил: ищется в справочнике людей (person) точно по
    имени, нового человека функция добавляет сама (find_or_create_person).
    Права: создание определения вида. Текущий вид особи меняется как следствие — поэтому
    исследователь может добавить определение и к чужой особи (и к особи из Excel).
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

    with write_transaction(conn, user):
        specimen = find_specimen(conn, label)
        if species_name is None:
            species_id, species_text = None, "(не определён)"
        else:
            species = find_species(conn, species_name)
            species_id, species_text = species["species_id"], species["scientific_name"]
        old = conn.execute(
            "SELECT scientific_name FROM species WHERE species_id = ?", (specimen["species_id"],)
        ).fetchone()
        person_id, new_person = find_or_create_person(conn, identified_by)
        audit.insert(
            conn, "species_identification", specimen_id=specimen["specimen_id"],
            species_id=species_id, method=method, confidence=confidence,
            identified_by=person_id, identified_on=identified_on, raw_text=species_name,
            notes=_clean(notes),
        )  # fmt: skip
        audit.update(  # текущий вид — следствие нового определения
            conn, "specimen", {"specimen_id": specimen["specimen_id"]},
            {"species_id": species_id}, derived=True,
        )  # fmt: skip
        checks = run_checks(conn)
    was = old[0] if old else "(не определён)"
    message = f"Особь {specimen['label']}: {was} → {species_text} ({method}, {confidence})."
    if new_person:
        message += f" В справочник людей добавлен(а) «{_clean(identified_by)}»."
    return EditResult(message, checks)


def add_issue(
    conn: sqlite3.Connection,
    *,
    category: str,
    description: str,
    table_name: str | None = None,
    record_id: int | None = None,
    object_label: str | None = None,
    raw_value: str | None = None,
    user: UserRef,
) -> EditResult:
    """Человек добавляет проблему в журнал (source = 'пользователь'): «сверить TL с журналом».

    Права: создание — исследователь и куратор; закрыть её потом может сам автор или куратор
    (permissions.can RESOLVE). created_by / created_at и журнал изменений — audit.insert.
    table_name + record_id — к какой записи относится (обе или ни одной; запись должна
    существовать); object_label — номер пробы / траление для людей.
    user обязателен (None — системный режим CLI — надо передать явно).
    """
    category, description = _clean(category), _clean(description)
    if not category:
        raise EditError("Укажите категорию проблемы (например «промеры»).")
    if not description:
        raise EditError("Опишите проблему — пустой текст не подходит.")
    if (table_name is None) != (record_id is None):
        raise EditError("Запись указывают вместе: таблица и номер записи — или ни того ни другого.")
    with write_transaction(conn, user):
        if table_name is not None:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if table_name not in tables:
                raise EditError(f"Таблицы «{table_name}» нет.")
            key = _primary_key_column(conn, table_name)
            # имя таблицы и колонки — из самой базы (проверено выше), номер — через «?»
            if not conn.execute(
                f"SELECT 1 FROM {table_name} WHERE {key} = ?", (record_id,)
            ).fetchone():
                raise NotFoundError(f"Записи №{record_id} в таблице {table_name} нет.")
        issue_id = audit.insert(
            conn, "data_issue", category=category, description=description,
            table_name=table_name, record_id=record_id, object_label=_clean(object_label),
            raw_value=_clean(raw_value), source="пользователь",
        )  # fmt: skip
        checks = run_checks(conn)
    return EditResult(f"Проблема №{issue_id} добавлена в журнал: {description}", checks,
                      record_id=issue_id)  # fmt: skip


def _primary_key_column(conn: sqlite3.Connection, table: str) -> str:
    """Единственная колонка первичного ключа таблицы; составной ключ — EditError."""
    keys = [r[0] for r in conn.execute(
        "SELECT name FROM pragma_table_info(?) WHERE pk > 0", (table,))]  # fmt: skip
    if len(keys) != 1:
        raise EditError(f"У таблицы {table} нет одного номера записи — укажите объект текстом.")
    return keys[0]


def resolve_issue(
    conn: sqlite3.Connection, issue_id: int, resolution: str, *, user: UserRef = None
) -> EditResult:
    """Отметить проблему из журнала решённой.

    conn — подключение без открытой транзакции (иначе EditError, см. write_transaction).
    """
    resolution = " ".join(resolution.split())
    if not resolution:
        raise EditError("Напишите, как решили проблему — пустой текст не подходит.")
    with write_transaction(conn, user) as actor:
        issue = conn.execute(
            "SELECT issue_id, description, resolved, resolution, source, created_by "
            "FROM data_issue WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        if issue is None:
            raise NotFoundError(f"Проблемы №{issue_id} нет в журнале.")
        if actor is not None:  # куратор — любые; исследователь — только добавленные им
            require(actor, RESOLVE, "data_issue", issue)
        if issue["resolved"]:
            raise EditError(f"Проблема №{issue_id} уже решена: {issue['resolution']}")
        audit.update(  # права проверены выше (своё действие «закрытие проблемы»)
            conn, "data_issue", {"issue_id": issue_id},
            {"resolved": 1, "resolution": resolution, "resolved_at": now_utc(),
             "resolved_by": actor.user_id if actor else None},
            derived=True,
        )  # fmt: skip
        checks = run_checks(conn)
    return EditResult(f"Проблема №{issue_id} закрыта: {issue['description']}", checks)
