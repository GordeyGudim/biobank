"""Подключение к базе SQLite, создание пустой базы из db/schema.sql, резервные копии,
пометка «рабочая база».

Главное правило: в SQLite проверка внешних ключей по умолчанию ВЫКЛЮЧЕНА
и включается отдельно в каждом подключении командой PRAGMA foreign_keys = ON.
Поэтому все модули проекта открывают базу только через connect() или
connect_read_only() отсюда.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# src/biobank/db.py → parents[2] = корень репозитория
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
DEFAULT_DB_PATH = PROJECT_ROOT / "output" / "biobank.db"

# Сколько ждать, если базу сейчас пишет другой (сайт, вторая команда): 5 с, а не сразу
# ошибка «database is locked». Python и сам ставит 5 с (параметр timeout), здесь — явно.
BUSY_TIMEOUT_MS = 5000


class DatabaseBusyError(sqlite3.OperationalError):
    """Файл базы нельзя заменить или удалить: его держит открытым другая программа."""


class LiveDatabaseError(Exception):
    """База отмечена как рабочая (go-live) — пересоздавать её (import, init) нельзя."""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Открыть базу и включить проверку внешних ключей.

    row_factory = sqlite3.Row позволяет обращаться к колонкам по имени:
    row["label"] вместо row[0].
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # значение PRAGMA нельзя передать параметром «?»; подставляется число-константа из кода
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    return conn


# --- Подключение только на чтение -------------------------------------------
#
# Режим mode=ro не даёт менять сам файл базы, но через него можно:
# - ATTACH того же файла в режиме записи — и писать под другим именем;
# - VACUUM INTO 'файл' — скопировать базу куда угодно (внутри это тоже ATTACH);
# - PRAGMA foreign_keys = OFF — выключить проверки в подключении.
# Поэтому дополнительно ставим authorizer — функцию, которую SQLite вызывает
# при разборе каждого запроса для каждого действия (прочитать колонку, вызвать
# функцию, вставить строку…). Она отвечает «можно» (SQLITE_OK) или «нельзя»
# (SQLITE_DENY — запрос не выполнится, ошибка «not authorized»).
# Разрешаем только чтение: всё, чего нет в списке, запрещено.

_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,  # SELECT
    sqlite3.SQLITE_READ,  # чтение колонки таблицы
    sqlite3.SQLITE_FUNCTION,  # функции: count, coalesce, round…
    sqlite3.SQLITE_RECURSIVE,  # WITH RECURSIVE
}
# PRAGMA, которые только показывают сведения о базе (с аргументом и без)
_INFO_PRAGMAS = {
    "foreign_key_check", "foreign_key_list", "integrity_check", "quick_check",
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
}  # fmt: skip
# PRAGMA-настройки: прочитать можно (без аргумента), изменить — нет
_SETTING_PRAGMAS = {
    "foreign_keys", "user_version", "schema_version", "application_id", "encoding",
    "journal_mode", "page_size", "page_count", "freelist_count", "busy_timeout",
}  # fmt: skip


def _read_only_authorizer(action, arg1, arg2, db_name, trigger) -> int:
    """Разрешить только чтение (см. пояснение выше).

    Для PRAGMA arg1 — имя, arg2 — значение: «PRAGMA foreign_keys = OFF» → ('foreign_keys', 'OFF').
    """
    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in _INFO_PRAGMAS or (name in _SETTING_PRAGMAS and arg2 is None):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_read_only(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Подключение только на чтение: любая попытка что-то изменить даст ошибку.

    Две защиты: mode=ro (режим SQLite «read only», задаётся в адресе файла — URI)
    и authorizer, который пропускает только чтение (SELECT и справочные PRAGMA).
    Ошибка при попытке записи — sqlite3.DatabaseError «not authorized».

    База в режиме WAL: читателю нужен файл <база>-shm (общий индекс журнала) — SQLite
    создаёт его сам, поэтому папка базы должна быть доступна на запись. Закрываясь,
    подключение только на чтение не может убрать -wal и -shm — они остаются (это нормально).
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # до authorizer: потом менять настройки нельзя
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    conn.set_authorizer(_read_only_authorizer)
    return conn


# --- Режим WAL ---------------------------------------------------------------
#
# WAL (write-ahead log, «журнал упреждающей записи»): изменения сначала дописываются
# в файл <база>-wal, а в основной файл переносятся позже — это называется checkpoint
# («контрольная точка»). Плюс: читатели не ждут пишущего, а пишущий — читателей
# (сайт показывает страницы, пока кто-то сохраняет форму). Рядом с базой живут
# два служебных файла:
#   biobank.db-wal — изменения, ещё не перенесённые в основной файл (удалить = потерять!);
#   biobank.db-shm — общий индекс журнала для всех подключений.
# Последнее закрывшееся ПИШУЩЕЕ подключение переносит журнал и удаляет оба файла.
# Режим WAL записан в заголовке файла базы — его включают один раз, при создании.

WAL_SUFFIXES = ("-wal", "-shm")


def wal_files(db_path: Path | str) -> list[Path]:
    """Служебные файлы WAL рядом с базой (-wal, -shm), которые сейчас существуют."""
    db_path = Path(db_path)
    files = (db_path.with_name(db_path.name + suffix) for suffix in WAL_SUFFIXES)
    return [path for path in files if path.exists()]


def close_wal(db_path: Path | str) -> None:
    """Перенести журнал WAL в основной файл и убрать -wal/-shm — перед заменой файла базы.

    Если подменить файл базы, а старый -wal оставить, SQLite применил бы журнал старой
    базы к новой — это порча данных. Поэтому перед заменой или удалением файла:
    checkpoint(TRUNCATE) + закрыть подключение (последнее закрытие удаляет -wal и -shm).
    Если файлы остались — базу держит открытой другая программа (сайт, вторая команда):
    DatabaseBusyError, файл трогать нельзя. Нет файла или это не база SQLite — нечего делать.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return
    try:
        conn = connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        pass  # не база SQLite (или занята) — что делать, решает проверка ниже
    if wal_files(db_path):
        raise DatabaseBusyError(
            f"База {db_path} открыта другой программой (сайт, другая команда) — "
            "её файл сейчас нельзя заменить. Остановите программу и повторите."
        )


def create_database(
    db_path: Path | str = DEFAULT_DB_PATH,
    schema_path: Path | str = SCHEMA_PATH,
    overwrite: bool = False,
) -> Path:
    """Создать пустую базу по схеме (в режиме WAL) и вернуть путь к файлу.

    Если файл уже есть и overwrite=False — ошибка FileExistsError,
    чтобы случайно не стереть данные. Спрашивать пользователя — задача CLI.
    Рабочую базу (go-live) не перезаписывает и с overwrite=True — LiveDatabaseError.
    """
    db_path = Path(db_path)
    if db_path.exists():
        if not overwrite:
            raise FileExistsError(f"База уже существует: {db_path}")
        ensure_not_live(db_path)
        close_wal(db_path)  # не оставить журнал старой базы рядом с новой
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = Path(schema_path).read_text(encoding="utf-8")

    conn = connect(db_path)
    try:
        # executescript выполняет сразу много команд SQL, разделённых «;»
        conn.executescript(schema_sql)
        conn.execute("PRAGMA journal_mode = WAL")  # сохраняется в файле базы
    except sqlite3.Error:
        conn.close()
        db_path.unlink(missing_ok=True)  # не оставлять полусозданный файл
        raise
    conn.close()
    return db_path


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Имена всех пользовательских таблиц базы по алфавиту."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


# --- Резервные копии ---------------------------------------------------------
#
# Копии лежат в <папка базы>/backups/<вид>/<файл базы>_<вид>_ГГГГММДД_ЧЧММСС.db,
# например backups/daily/biobank.db_daily_20261019_030000.db:
#   daily  — ежедневная (ночной таймер на сервере: python -m biobank backup);
#   weekly — еженедельная (та же команда делает её сама раз в 7 дней);
#   pre-op — перед опасной операцией: правка, удаление, импорт, восстановление.
# Вид — и в папке, и в имени файла: скачанная или переложенная копия не теряет его.
# В имени — ПОЛНОЕ имя файла базы (biobank.db, а не biobank): у баз biobank.db и
# biobank.sqlite в одной папке копии не путаются, и очистка одной не удаляет копии другой.
# Копии — обычные файлы в предсказуемом месте: на этапе D4 их будет легко отправлять
# во внешнее хранилище (папка backups/daily/ → хранилище daily/ и т.д.).
# Копии старого формата (backups/biobank_ГГГГММДД_ЧЧММСС.db) очистка не трогает.

BACKUP_KINDS = ("daily", "weekly", "pre-op")
DAILY, WEEKLY, PRE_OP = BACKUP_KINDS
_STAMP_FORMAT = "%Y%m%d_%H%M%S"


class BackupError(sqlite3.DatabaseError):
    """Резервная копия не сделана (файл копии не создан). Это и ошибка SQLite:
    код, который ловит sqlite3.Error (правки, import), ловит и её."""


@dataclass(frozen=True)
class Retention:
    """Правило хранения: keep_last самых новых копий + все моложе keep_days дней."""

    keep_last: int
    keep_days: int = 0  # 0 — по возрасту не хранить, только keep_last


# решение команды (2026-10-19)
RETENTION = {
    DAILY: Retention(keep_last=7),
    WEEKLY: Retention(keep_last=4),
    PRE_OP: Retention(keep_last=20, keep_days=30),
}


def _kind_dir(db_path: Path, kind: str, backup_dir: Path | str | None) -> Path:
    """Папка копий вида kind: <backup_dir или папка базы/backups>/<вид>."""
    if kind not in BACKUP_KINDS:
        raise ValueError(f"Неизвестный вид копии: {kind!r} (бывают: {', '.join(BACKUP_KINDS)})")
    root = Path(backup_dir) if backup_dir is not None else db_path.parent / "backups"
    return root / kind


def remove_temp_database(path: Path) -> None:
    """Удалить файл базы вместе с его -wal/-shm — только для СВОИХ временных файлов
    (недоделанная копия, временный файл импорта и восстановления), не для рабочей базы."""
    for each in (path, *wal_files(path)):
        each.unlink(missing_ok=True)


def backup_database(
    db_path: Path | str,
    backup_dir: Path | str | None = None,
    kind: str = PRE_OP,
    now: dt.datetime | None = None,
) -> Path:
    """Скопировать базу в <backup_dir>/<вид>/<файл базы>_<вид>_ГГГГММДД_ЧЧММСС.db, вернуть путь.

    backup_dir по умолчанию — <папка базы>/backups; now — время в имени (для тестов).
    Используется backup() из sqlite3, а не простое копирование файла:
    он делает согласованную копию, даже если базу в этот момент кто-то читает или пишет.
    В режиме WAL переносить журнал заранее (checkpoint) не нужно: backup() читает базу
    как обычный читатель — вместе с изменениями, ещё лежащими в -wal. Копия получается
    одним файлом (без -wal) и сохраняет режим WAL и пометку «рабочая база» из заголовка.
    Исходная база открывается только на чтение — копирование её не изменит.

    Любая ошибка SQLite (не база, нет доступа к -shm, занята) — BackupError, недоделанный
    файл копии удаляется. Просто скопировать файл «как есть» нельзя: без -wal копия
    молча оказалась бы неполной. Нет файла базы — FileNotFoundError, папка не создаётся.
    Старые копии здесь не удаляются — это prune_backups().
    """
    db_path = Path(db_path)
    if not db_path.is_file():  # сначала источник — иначе осталась бы пустая папка backups/
        raise FileNotFoundError(f"Нечего копировать — файла базы нет: {db_path}")
    folder = _kind_dir(db_path, kind, backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or dt.datetime.now()).strftime(_STAMP_FORMAT)
    target = folder / f"{db_path.name}_{kind}_{stamp}.db"
    n = 1
    while target.exists():  # две копии в одну секунду
        target = folder / f"{db_path.name}_{kind}_{stamp}_{n}.db"
        n += 1
    source = dest = None
    try:
        source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        dest = sqlite3.connect(target)
        source.backup(dest)
    except sqlite3.Error as error:
        if dest is not None:
            dest.close()
            dest = None
        remove_temp_database(target)
        raise BackupError(f"Резервная копия {db_path} не сделана: {error}") from error
    finally:
        if dest is not None:
            dest.close()
        if source is not None:
            source.close()
    return target


def backup_time(name: str, db_name: str, kind: str) -> dt.datetime | None:
    """Время копии из имени файла; None — файл не копия вида kind базы db_name.

    >>> backup_time("biobank.db_daily_20261019_030000.db", "biobank.db", "daily")
    datetime.datetime(2026, 10, 19, 3, 0)
    >>> backup_time("biobank.db_pre-op_20261019_030000_1.db", "biobank.db", "pre-op")  # 2-я
    datetime.datetime(2026, 10, 19, 3, 0)
    >>> backup_time("biobank_20261014_130534.db", "biobank.db", "pre-op") is None  # старый
    True
    >>> backup_time("biobank.sqlite_daily_20261019_030000.db", "biobank.db", "daily") is None
    True
    >>> backup_time("biobank.db_daily_20261019_030000.db", "biobank.db", "weekly") is None
    True
    """
    # шаблон собирается из имени базы и вида; re.escape — точки и дефисы в них буквально
    pattern = rf"{re.escape(db_name)}_{re.escape(kind)}_(\d{{8}}_\d{{6}})(?:_\d+)?\.db"
    match = re.fullmatch(pattern, name)
    if match is None:
        return None
    try:
        return dt.datetime.strptime(match.group(1), _STAMP_FORMAT)
    except ValueError:  # 20261399_… — похоже на копию, но не дата
        return None


def future_backups(stamps: dict[str, dt.datetime], now: dt.datetime) -> list[str]:
    """Копии «из будущего»: время в имени позже now (часы спешили или их перевели назад).

    >>> now = dt.datetime(2026, 10, 19, 3, 0)
    >>> future_backups({"a": now, "b": now + dt.timedelta(seconds=1)}, now)
    ['b']
    """
    return sorted(name for name, stamp in stamps.items() if stamp > now)


def expired_backups(stamps: dict[str, dt.datetime], kind: str, now: dt.datetime) -> list[str]:
    """Какие копии удалить по правилу RETENTION[kind]; stamps — {имя файла: время копии}.

    Чистая функция: решает, но ничего не удаляет. Остаются keep_last самых новых
    и все, что моложе keep_days дней; остальные — в ответе (по алфавиту).
    Копии «из будущего» (future_backups) не удаляются и не считаются в keep_last:
    иначе после сбоя часов они вытесняли бы каждую новую копию.

    >>> now = dt.datetime(2026, 10, 19, 3, 0)
    >>> daily = {f"d{i}": now - dt.timedelta(days=i) for i in range(9)}
    >>> expired_backups(daily, "daily", now)  # 7 последних остаются
    ['d7', 'd8']
    >>> pre_op = {f"p{i:02}": now - dt.timedelta(days=i) for i in range(40)}
    >>> len(expired_backups(pre_op, "pre-op", now))  # моложе 30 дней — 31 копия (0…30)
    9
    >>> few = {f"p{i}": now - dt.timedelta(days=100 + i) for i in range(20)}
    >>> expired_backups(few, "pre-op", now)  # старые, но их всего 20 — не меньше 20 последних
    []
    >>> ahead = {f"f{i}": now + dt.timedelta(days=365 + i) for i in range(7)}
    >>> expired_backups({**ahead, "today": now}, "daily", now)  # будущие — не в счёт
    []
    """
    rule = RETENTION[kind]
    current = {name: stamp for name, stamp in stamps.items() if stamp <= now}
    # новые — первыми; при одинаковом времени новее та, что с суффиксом _1, _2 (имя больше)
    newest = sorted(current, key=lambda name: (current[name], name), reverse=True)
    keep = set(newest[: rule.keep_last])
    if rule.keep_days:
        border = now - dt.timedelta(days=rule.keep_days)
        keep |= {name for name, stamp in current.items() if stamp >= border}
    return sorted(name for name in current if name not in keep)


def _backup_files(
    db_path: Path, kind: str, backup_dir: Path | str | None
) -> dict[str, dt.datetime]:
    """Копии вида kind этой базы: {имя файла: время из имени}. Чужие файлы не попадают."""
    folder = _kind_dir(db_path, kind, backup_dir)
    if not folder.is_dir():
        return {}
    db_real = db_path.resolve()
    stamps = {}
    for path in folder.iterdir():
        # только обычные файлы с именем копии; сама база — никогда (даже если лежит здесь)
        if path.is_symlink() or not path.is_file() or path.resolve() == db_real:
            continue
        stamp = backup_time(path.name, db_path.name, kind)
        if stamp is not None:
            stamps[path.name] = stamp
    return stamps


def prune_backups(
    db_path: Path | str,
    kind: str,
    backup_dir: Path | str | None = None,
    now: dt.datetime | None = None,
    keep: Iterable[Path] = (),
) -> list[Path]:
    """Удалить копии вида kind, которые не нужны по правилу RETENTION[kind]; вернуть удалённые.

    Смотрит только в папку своего вида и только на файлы с именем копии этой базы
    (<файл базы>_<вид>_ГГГГММДД_ЧЧММСС.db): сама база, копии других видов и других баз,
    старого формата, «из будущего» и посторонние файлы не удаляются. keep — копии,
    которые не удалять ни в коем случае (только что созданные). now — «сейчас» (для тестов).
    """
    db_path = Path(db_path)
    folder = _kind_dir(db_path, kind, backup_dir)
    protected = {Path(p).name for p in keep if Path(p).parent == folder}
    stamps = _backup_files(db_path, kind, backup_dir)
    deleted = []
    for name in expired_backups(stamps, kind, now or dt.datetime.now()):
        if name in protected:
            continue
        (folder / name).unlink()
        deleted.append(folder / name)
    return deleted


def weekly_due(last: dt.datetime | None, now: dt.datetime) -> bool:
    """Пора ли делать еженедельную копию: её ещё нет или последней 7+ календарных дней.

    Сравниваются даты, а не часы: если таймер сработал на минуту раньше, чем неделю
    назад, копия всё равно будет сделана в тот же день, а не на следующий.
    Последняя копия «из будущего» (часы сбились) — тоже пора: иначе еженедельные
    прекратились бы до тех пор, пока часы не догонят её время.

    >>> weekly_due(None, dt.datetime(2026, 10, 19, 3, 0))
    True
    >>> weekly_due(dt.datetime(2026, 10, 12, 3, 0, 5), dt.datetime(2026, 10, 19, 3, 0, 1))
    True
    >>> weekly_due(dt.datetime(2026, 10, 13, 3, 0), dt.datetime(2026, 10, 19, 23, 59))
    False
    >>> weekly_due(dt.datetime(2027, 10, 13, 3, 0), dt.datetime(2026, 10, 19, 3, 0))
    True
    """
    return last is None or last > now or (now.date() - last.date()).days >= 7


@dataclass
class BackupReport:
    """Что сделала run_backup: созданные копии, удалённые по правилу хранения
    и копии «из будущего» (время в имени позже текущего — проверить часы)."""

    created: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    future: list[Path] = field(default_factory=list)


def run_backup(
    db_path: Path | str,
    kind: str = DAILY,
    backup_dir: Path | str | None = None,
    now: dt.datetime | None = None,
) -> BackupReport:
    """Копия вида kind и очистка старых копий этого вида (команда backup, ночной таймер).

    К ежедневной копии раз в 7 дней добавляется еженедельная (weekly_due): серверу
    нужен один таймер, а не два, и пропущенная неделя (сервер был выключен) догоняется
    при следующем запуске. Еженедельную можно сделать и явно: kind="weekly".
    Сбой часов: копии этого запуска не удаляются никогда; копии «из будущего» не
    удаляются и не засчитываются (ни в правило хранения, ни как последняя еженедельная) —
    они перечислены в report.future, чтобы человек проверил часы.
    """
    db_path = Path(db_path)
    now = now or dt.datetime.now()
    report = BackupReport()
    kinds = [kind]
    if kind == DAILY:
        weekly = _backup_files(db_path, WEEKLY, backup_dir)
        last_weekly = max((t for t in weekly.values() if t <= now), default=None)
        if weekly_due(last_weekly, now):
            kinds.append(WEEKLY)
    for each in kinds:
        report.created.append(backup_database(db_path, backup_dir, each, now))
    for each in kinds:
        report.deleted += prune_backups(db_path, each, backup_dir, now, keep=report.created)
    for each in BACKUP_KINDS:  # предупреждать о часах — по всем видам копий
        stamps = _backup_files(db_path, each, backup_dir)
        folder = _kind_dir(db_path, each, backup_dir)
        report.future += [folder / name for name in future_backups(stamps, now)]
    return report


# --- Пометка «рабочая база» (go-live) ----------------------------------------
#
# После перехода на сайт база — единственный источник правды, и пересоздать её
# из Excel (import, init) нельзя. Пометка хранится в заголовке самого файла базы —
# в числе PRAGMA application_id (4 байта, «какой программе принадлежит файл»).
# Поэтому она путешествует вместе с файлом: копия (backup) и восстановление из копии
# тоже помечены. Таблиц и колонок не добавляет. user_version не используем — он
# пригодится для номера версии схемы при миграциях (этап D3).

LIVE_APPLICATION_ID = 0x42424C56  # байты «BBLV»: BioBank, LiVe


def is_live(db_path: Path | str) -> bool:
    """Отмечена ли база как рабочая. Нет файла или это не база SQLite — False.

    База занята дольше BUSY_TIMEOUT_MS — ошибка, а не False: при сомнении
    не разрешать пересоздание.
    Ограничение: у файла с повреждённым заголовком пометку прочитать нельзя — тоже False,
    то есть ПОВРЕЖДЁННУЮ рабочую базу пометка не защищает. Вторая защита остаётся:
    перед заменой (import, restore) всегда делается резервная копия, а если её сделать
    нельзя (BackupError — у файла с испорченным заголовком так и будет), замены нет.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return False
    conn = connect_read_only(db_path)
    try:
        return conn.execute("PRAGMA application_id").fetchone()[0] == LIVE_APPLICATION_ID
    except sqlite3.OperationalError:  # занята, нет доступа — не угадывать
        raise
    except sqlite3.DatabaseError:  # «file is not a database»
        return False
    finally:
        conn.close()


def ensure_not_live(db_path: Path | str) -> None:
    """LiveDatabaseError, если база отмечена как рабочая (перед import и init)."""
    if is_live(db_path):
        raise LiveDatabaseError(
            f"База {db_path} отмечена как рабочая: она — единственный источник данных, "
            "а пересоздание (импорт из Excel, создание пустой базы) стёрло бы всё, что в "
            "неё внесено. Пересоздавать рабочую базу запрещено. Если это авария и базу "
            "действительно нужно собрать заново — сначала снимите пометку «рабочая база»."
        )


def set_live(db_path: Path | str, live: bool) -> bool:
    """Поставить (live=True) или снять пометку «рабочая база». False — уже так и было.

    В транзакции BEGIN IMMEDIATE. Проверяет, что это база биобанка (есть таблица
    specimen) и что application_id не занят другой программой — иначе ValueError.
    Нет файла — FileNotFoundError. Спрашивать подтверждение — задача CLI.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Базы нет: {db_path}")
    wanted = LIVE_APPLICATION_ID if live else 0
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "specimen" not in list_tables(conn):
                raise ValueError(f"{db_path} — не база биобанка (нет таблицы specimen).")
            current = conn.execute("PRAGMA application_id").fetchone()[0]
            if current not in (0, LIVE_APPLICATION_ID):
                raise ValueError(
                    f"В заголовке {db_path} уже записан чужой application_id = {current} — "
                    "пометку не меняю."
                )
            changed = current != wanted
            if changed:
                # значение PRAGMA нельзя передать параметром «?»; это число-константа из кода
                conn.execute(f"PRAGMA application_id = {int(wanted)}")
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    finally:
        conn.close()
    return changed


# --- Восстановление из копии (restore) ----------------------------------------
#
# Почему не «cp копия база»: в режиме WAL рядом с базой может лежать -wal с изменениями,
# сделанными ПОСЛЕ копии (последним закрылся читатель — сайт, — и журнал остался).
# SQLite применит этот чужой журнал к скопированному файлу: «отменённая» правка вернётся,
# а на большой базе возможна порча. restore_database делает замену безопасно.


@dataclass
class BackupInfo:
    """Проверенный файл копии: путь и помечен ли он как рабочая база."""

    path: Path
    live: bool


def _snapshot_uri(path: Path) -> str:
    """Адрес файла копии «только чтение, файл не меняется» (immutable=1).

    В таком режиме SQLite не создаёт рядом -wal и -shm — в папке копий не остаётся
    мусора. Годится только для закрытых копий, которые никто не пишет.
    """
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def inspect_backup(source: Path | str) -> BackupInfo:
    """Проверить файл копии перед восстановлением: целая база биобанка?

    Нет файла — FileNotFoundError. Не база SQLite, повреждена (PRAGMA integrity_check
    не «ok»), нет таблицы specimen или рядом непустой -wal (значит, это не закрытая
    копия, а чья-то работающая база) — ValueError с объяснением.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"Файла копии нет: {source}")
    wal = source.with_name(source.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise ValueError(
            f"Рядом с {source} лежит непустой {wal.name}: это не закрытая копия, а "
            "работающая база. Сделайте из неё копию командой backup и восстанавливайте из копии."
        )
    conn = sqlite3.connect(_snapshot_uri(source), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        check = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        tables = list_tables(conn)
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise ValueError(f"{source} — не база SQLite или повреждена: {error}") from error
    finally:
        conn.close()
    if check != ["ok"]:
        raise ValueError(f"Копия {source} повреждена (integrity_check: {check[0]} …).")
    if "specimen" not in tables:
        raise ValueError(f"{source} — не база биобанка (нет таблицы specimen).")
    return BackupInfo(source, app_id == LIVE_APPLICATION_ID)


@dataclass
class RestoreResult:
    """Что сделала restore_database."""

    db_path: Path  # восстановленная база
    source: Path  # из какой копии
    backup: Path | None  # pre-op копия базы, которая была до восстановления (None — базы не было)
    live: bool  # восстановленная база помечена как рабочая (пометка переехала с копией)


def restore_database(
    db_path: Path | str, source: Path | str, backup_dir: Path | str | None = None
) -> RestoreResult:
    """Заменить базу db_path копией source. Спрашивать подтверждение — задача CLI.

    1. inspect_backup(source): целая база биобанка, иначе ValueError.
    2. Копия переносится (backup API) во временный файл <база>.restore в папке базы —
       чтобы сама замена была одной атомарной операцией os.replace в той же папке.
    3. Если база есть: pre-op копия текущей базы (не получилась — BackupError, ничего
       не заменено) и close_wal: журнал текущей базы перенесён, -wal/-shm убраны;
       если базу держит открытой другая программа — DatabaseBusyError, ничего не заменено.
    4. os.replace — рядом с восстановленной базой нет -wal/-shm от старой.
    Как и у import: между close_wal и os.replace есть окно в доли миллисекунды, когда
    другая программа могла бы открыть базу, — поэтому сайт и все программы, работающие
    с базой, перед restore нужно остановить (файл-замок — на этапе D4).
    Пометка «рабочая база» переезжает вместе с копией (см. RestoreResult.live).
    """
    db_path = Path(db_path)
    source = Path(source)
    if db_path.exists() and source.exists() and os.path.samefile(db_path, source):
        raise ValueError(f"Копия и база — один и тот же файл: {source}")
    info = inspect_backup(source)
    tmp = db_path.with_name(db_path.name + ".restore")
    remove_temp_database(tmp)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    try:
        snapshot = sqlite3.connect(_snapshot_uri(source), uri=True)
        dest = sqlite3.connect(tmp)
        try:
            snapshot.backup(dest)
        finally:
            dest.close()
            snapshot.close()
        if db_path.exists():
            try:
                backup = backup_database(db_path, backup_dir, PRE_OP)
            except BackupError as error:
                raise BackupError(
                    f"{error}. Восстановление отменено: текущую базу нельзя заменить, не "
                    "сохранив её. Если она повреждена — перенесите её файл (и -wal, -shm) "
                    "в другое место вручную и повторите."
                ) from error
            close_wal(db_path)
        os.replace(tmp, db_path)
    except BaseException:
        remove_temp_database(tmp)
        if backup is not None:  # замены не было — копия не нужна
            backup.unlink(missing_ok=True)
        raise
    try:
        prune_backups(db_path, PRE_OP, backup_dir, keep=[backup] if backup else [])
    except OSError:
        pass  # база уже восстановлена; лишние копии удалит следующая операция
    return RestoreResult(db_path, source, backup, info.live)
