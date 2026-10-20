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
# Версия схемы, под которую написан код (= PRAGMA user_version в db/schema.sql; тест следит).
# Копию с другой версией restore не восстанавливает: код с ней работать не умеет.
SCHEMA_VERSION = 3
DEFAULT_DB_PATH = PROJECT_ROOT / "output" / "biobank.db"

# Сколько ждать, если базу сейчас пишет другой (сайт, вторая команда): 5 с, а не сразу
# ошибка «database is locked». Python и сам ставит 5 с (параметр timeout), здесь — явно.
BUSY_TIMEOUT_MS = 5000


class DatabaseBusyError(sqlite3.OperationalError):
    """Файл базы нельзя заменить или удалить: его держит открытым другая программа."""


class LiveDatabaseError(Exception):
    """База отмечена как рабочая (go-live) — пересоздавать её (import, init) нельзя."""


def now_utc() -> str:
    """Текущее время в UTC в формате схемы для колонок …_at: 'ГГГГ-ММ-ДД ЧЧ:ММ:СС'.

    Схема проверяет формат (CHECK datetime(x) IS x): '2026-10-20T10:00:00+00:00' не примет.

    >>> len(now_utc()), now_utc()[4], now_utc()[10]
    (19, '-', ' ')
    """
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


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


# Колонки, которые подключение на чтение не отдаёт: вместо значения — NULL (SQLITE_IGNORE).
# Хеш пароля не нужен ни команде sql, ни сайту, ни отчётам; вход (users.authenticate)
# читает его через обычное подключение connect().
HIDDEN_COLUMNS = {("app_user", "password_hash")}


def _read_only_authorizer(action, arg1, arg2, db_name, trigger) -> int:
    """Разрешить только чтение (см. пояснение выше); скрытые колонки читаются как NULL.

    Для PRAGMA arg1 — имя, arg2 — значение: «PRAGMA foreign_keys = OFF» → ('foreign_keys', 'OFF').
    Для чтения колонки (SQLITE_READ) arg1 — таблица, arg2 — колонка.
    """
    if action == sqlite3.SQLITE_READ and (arg1, arg2) in HIDDEN_COLUMNS:
        return sqlite3.SQLITE_IGNORE
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


# Копия «из будущего» — время в имени позже «сейчас» больше чем на сутки (часы спешили
# или их перевели назад). Сутки — запас на переход на летнее время и небольшой сбой часов.
FUTURE_MARGIN = dt.timedelta(days=1)


def future_backups(stamps: dict[str, dt.datetime], now: dt.datetime) -> list[str]:
    """Копии «из будущего»: время в имени позже now + FUTURE_MARGIN.

    >>> now = dt.datetime(2026, 10, 19, 3, 0)
    >>> future_backups({"a": now + dt.timedelta(hours=23), "b": now + dt.timedelta(days=2)}, now)
    ['b']
    """
    return sorted(name for name, stamp in stamps.items() if stamp > now + FUTURE_MARGIN)


def expired_backups(stamps: dict[str, dt.datetime], kind: str, now: dt.datetime) -> list[str]:
    """Какие НАСТОЯЩИЕ копии удалить по правилу RETENTION[kind]; stamps — {имя: время}.

    Чистая функция: решает, но ничего не удаляет. Остаются keep_last самых новых
    и все, что моложе keep_days дней; остальные — в ответе (по алфавиту).
    Копии «из будущего» (future_backups) здесь не удаляются и не считаются в keep_last:
    иначе после сбоя часов они вытесняли бы каждую новую копию (их — excess_future).

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
    future = set(future_backups(stamps, now))
    current = {name: stamp for name, stamp in stamps.items() if name not in future}
    # новые — первыми; при одинаковом времени новее та, что с суффиксом _1, _2 (имя больше)
    newest = sorted(current, key=lambda name: (current[name], name), reverse=True)
    keep = set(newest[: rule.keep_last])
    if rule.keep_days:
        border = now - dt.timedelta(days=rule.keep_days)
        keep |= {name for name, stamp in current.items() if stamp >= border}
    return sorted(name for name in current if name not in keep)


def excess_future(future_mtimes: dict[str, float], current_count: int, kind: str) -> list[str]:
    """Какие копии «из будущего» удалить, чтобы при долгом сбое часов их не копилось без конца.

    future_mtimes — {имя: время изменения файла (st_mtime)} копий из будущего:
    время в имени у них неверное, поэтому новизна — по времени файла. Остаются
    keep_last самых новых; лишние удаляются, только если настоящих копий уже не меньше
    keep_last — иначе будущие могут оказаться единственными.

    >>> mtimes = {f"f{i}": float(i) for i in range(10)}
    >>> excess_future(mtimes, 7, "daily")  # 7 самых новых остаются
    ['f0', 'f1', 'f2']
    >>> excess_future(mtimes, 6, "daily")  # настоящих меньше 7 — ничего не удалять
    []
    """
    keep_last = RETENTION[kind].keep_last
    if current_count < keep_last:
        return []
    newest = sorted(future_mtimes, key=lambda name: (future_mtimes[name], name), reverse=True)
    return sorted(newest[keep_last:])


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
    старого формата и посторонние файлы не удаляются. Копии «из будущего» — только
    сверх keep_last (excess_future). keep — копии, которые не удалять ни в коем случае
    (только что созданные). now — «сейчас» (для тестов).
    """
    db_path = Path(db_path)
    now = now or dt.datetime.now()
    folder = _kind_dir(db_path, kind, backup_dir)
    protected = {Path(p).name for p in keep if Path(p).parent == folder}
    stamps = _backup_files(db_path, kind, backup_dir)
    future = future_backups(stamps, now)
    mtimes = {name: (folder / name).stat().st_mtime for name in future}
    doomed = expired_backups(stamps, kind, now)
    doomed += excess_future(mtimes, len(stamps) - len(future), kind)
    deleted = []
    for name in sorted(doomed):
        if name in protected:
            continue
        (folder / name).unlink()
        deleted.append(folder / name)
    return deleted


def find_future_backups(
    db_path: Path | str,
    kinds: Iterable[str] = BACKUP_KINDS,
    backup_dir: Path | str | None = None,
    now: dt.datetime | None = None,
) -> list[Path]:
    """Копии «из будущего» этой базы (по видам kinds) — повод проверить часы компьютера."""
    db_path = Path(db_path)
    now = now or dt.datetime.now()
    found = []
    for kind in kinds:
        folder = _kind_dir(db_path, kind, backup_dir)
        found += [folder / n for n in future_backups(_backup_files(db_path, kind, backup_dir), now)]
    return found


FUTURE_WARNING = (
    "копии из будущего (время в имени позже текущего больше чем на сутки) — проверьте "
    "часы компьютера. Хранятся не больше обычного числа таких копий, в правиле хранения "
    "они не учитываются"
)


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
    >>> weekly_due(dt.datetime(2026, 10, 19, 10, 0), dt.datetime(2026, 10, 19, 3, 0))  # часы
    False
    """
    return last is None or last > now + FUTURE_MARGIN or (now.date() - last.date()).days >= 7


@dataclass
class BackupReport:
    """Что сделала run_backup: созданные копии, удалённые по правилу хранения
    и копии «из будущего» (время в имени позже текущего больше чем на сутки — проверить часы)."""

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
    засчитываются (ни в правило хранения, ни как последняя еженедельная) и хранятся не
    больше keep_last на вид; они перечислены в report.future, чтобы человек проверил часы.
    """
    db_path = Path(db_path)
    now = now or dt.datetime.now()
    report = BackupReport()
    kinds = [kind]
    if kind == DAILY:
        weekly = _backup_files(db_path, WEEKLY, backup_dir)
        future = set(future_backups(weekly, now))
        last_weekly = max((t for n, t in weekly.items() if n not in future), default=None)
        if weekly_due(last_weekly, now):
            kinds.append(WEEKLY)
    for each in kinds:
        report.created.append(backup_database(db_path, backup_dir, each, now))
    for each in kinds:
        report.deleted += prune_backups(db_path, each, backup_dir, now, keep=report.created)
    report.future = find_future_backups(db_path, BACKUP_KINDS, backup_dir, now)  # все виды
    return report


# --- Пометка «рабочая база» (go-live) ----------------------------------------
#
# После перехода на сайт база — единственный источник правды, и пересоздать её
# из Excel (import, init) нельзя. Пометка хранится в заголовке самого файла базы —
# в числе PRAGMA application_id (4 байта, «какой программе принадлежит файл»).
# Поэтому она путешествует вместе с файлом: копия (backup) и восстановление из копии
# тоже помечены. Таблиц и колонок не добавляет. user_version не используем — в нём
# номер версии схемы (SCHEMA_VERSION).

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
# а на большой базе возможна порча. Восстановление идёт в два шага, как import:
# prepare_restore — копия переносится во временный файл рядом с базой и проверяется
# ИМЕННО она (то, что ляжет на место базы); replace_database — замена файла.


def _snapshot_uri(path: Path) -> str:
    """Адрес СВОЕГО временного файла «только чтение, файл не меняется» (immutable=1).

    В таком режиме SQLite не создаёт рядом -wal и -shm и не берёт блокировок — годится
    только для файла, который никто другой не открывает (временный файл restore).
    """
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _copy_source(source: Path, target: Path) -> None:
    """Перенести базу source в новый файл target через backup API (согласованный снимок).

    source открывается обычным mode=ro — с блокировками и с учётом его -wal: даже если
    этот файл сейчас кто-то пишет (например, ~/biobank_test.db под сайтом), снимок цельный.
    Если -wal/-shm рядом с source появились из-за этого чтения (до него их не было),
    их убирает _tidy_wal: SQLite удаляет их, только если базу больше никто не открыл, —
    чужие служебные файлы так не пострадают.
    """
    had_wal = bool(wal_files(source))
    snapshot = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        snapshot.backup(dest)
    finally:
        dest.close()
        snapshot.close()
    if not had_wal and wal_files(source):
        _tidy_wal(source)


def _tidy_wal(path: Path) -> None:
    """Убрать -wal/-shm, оставленные нашим чтением: открыть и закрыть пишущее подключение.

    Журнал был пуст (до чтения его не было), поэтому основной файл не меняется. Если папка
    только для чтения или базу кто-то открыл — служебные файлы просто остаются.
    """
    try:
        conn = connect(path)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


@dataclass
class PreparedRestore:
    """Копия уже перенесена во временный файл рядом с базой и проверена; база не тронута.

    Дальше вызывающий код решает (CLI спрашивает): replace_database() — заменить базу,
    discard() — отказаться. Удобнее через with: при выходе временный файл удаляется.
    """

    db_path: Path  # база, которую заменяем
    source: Path  # файл копии
    new_path: Path  # временный файл <база>.restore — ровно то, что ляжет на место базы
    source_live: bool  # копия помечена как рабочая
    current_live: bool  # текущая база помечена как рабочая (False — если её нет)
    backup: Path | None = None  # pre-op копия текущей базы (заполняет replace_database)

    @property
    def exists(self) -> bool:
        return self.db_path.exists()

    @property
    def live(self) -> bool:
        """Будет ли восстановленная база рабочей: пометка копии или текущей базы."""
        return self.source_live or self.current_live

    def replace_database(self) -> None:
        """Заменить базу проверенной копией.

        1. Если база есть: pre-op копия текущей базы (не получилась — BackupError,
           ничего не заменено) и close_wal: журнал текущей базы перенесён, -wal/-shm
           убраны; базу держит открытой другая программа — DatabaseBusyError.
        2. Текущая база была рабочей, а копия сделана до go-live — пометка ставится и на
           восстановленную: снять её можно только командой undo-go-live, не restore.
        3. os.replace — одна атомарная операция в той же папке; рядом с восстановленной
           базой не остаётся -wal/-shm от старой.
        Как и у import: между close_wal и os.replace есть окно в доли миллисекунды, когда
        другая программа могла бы открыть базу, — поэтому сайт и все программы, работающие
        с базой, перед restore нужно остановить (файл-замок — на этапе D4).
        """
        try:
            if self.exists:
                try:
                    self.backup = backup_database(self.db_path, kind=PRE_OP)
                except BackupError as error:
                    raise BackupError(
                        f"{error}. Восстановление отменено: текущую базу нельзя заменить, не "
                        "сохранив её. Если она повреждена — перенесите её файл (и -wal, -shm) "
                        "в другое место вручную и повторите."
                    ) from error
                # перечитать: пока CLI ждал ответа «да/нет», могли выполнить go-live;
                # до close_wal — чтение создаёт -wal/-shm, close_wal их уберёт
                self.current_live = is_live(self.db_path)
                close_wal(self.db_path)
            if self.current_live and not self.source_live:
                set_live(self.new_path, True)
            os.replace(self.new_path, self.db_path)
        except BaseException:
            if self.backup is not None:  # замены не было — копия не нужна
                self.backup.unlink(missing_ok=True)
                self.backup = None
            raise
        try:
            prune_backups(self.db_path, PRE_OP, keep=[self.backup] if self.backup else [])
        except OSError:
            pass  # база уже восстановлена; лишние копии удалит следующая операция

    def discard(self) -> None:
        """Удалить временный файл (после замены его уже нет — это не ошибка)."""
        remove_temp_database(self.new_path)

    def __enter__(self) -> PreparedRestore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.discard()


def schema_version_message(source: Path | str, version: int) -> str:
    """Почему копию другой версии схемы восстанавливать нельзя.

    >>> print(schema_version_message("old.db", 0))  # doctest: +ELLIPSIS
    Копия old.db — схема версии 0 (до v3: user_version не задан), а программа работает со ...
    """
    shown = f"{version} (до v3: user_version не задан)" if version == 0 else str(version)
    text = (
        f"Копия {source} — схема версии {shown}, а программа работает со схемой "
        f"версии {SCHEMA_VERSION}. "
    )
    if version < SCHEMA_VERSION:
        return text + (
            "Такую копию нельзя восстановить напрямую: сначала её нужно перевести на новую "
            "схему (миграция) — это отдельная работа. Возьмите копию, сделанную после перехода."
        )
    return text + "Копия сделана более новой версией программы — обновите программу."


def prepare_restore(db_path: Path | str, source: Path | str) -> PreparedRestore:
    """Перенести копию source во временный файл <база>.restore и проверить его.

    Нет файла копии — FileNotFoundError. Копия и база — один файл, копия не база SQLite,
    повреждена (PRAGMA integrity_check не «ok»), не база биобанка (нет таблицы
    specimen) или другой версии схемы (PRAGMA user_version ≠ SCHEMA_VERSION: копия,
    сделанная до перехода на новую схему) — ValueError с объяснением; временный файл
    при этом удаляется.
    Сама база не меняется. Текущая база занята дольше BUSY_TIMEOUT_MS — ошибка SQLite.
    """
    db_path = Path(db_path)
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"Файла копии нет: {source}")
    if db_path.exists() and os.path.samefile(db_path, source):
        raise ValueError(f"Копия и база — один и тот же файл: {source}")
    current_live = is_live(db_path)
    new_path = db_path.with_name(db_path.name + ".restore")
    remove_temp_database(new_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            _copy_source(source, new_path)
        except sqlite3.DatabaseError as error:
            raise ValueError(f"{source} — не база SQLite или повреждена: {error}") from error
        conn = sqlite3.connect(_snapshot_uri(new_path), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            check = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
            tables = list_tables(conn)
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise ValueError(f"{source} — не база SQLite или повреждена: {error}") from error
        finally:
            conn.close()
        if check != ["ok"]:
            raise ValueError(f"Копия {source} повреждена (integrity_check: {check[0]} …).")
        if "specimen" not in tables:
            raise ValueError(f"{source} — не база биобанка (нет таблицы specimen).")
        if version != SCHEMA_VERSION:
            raise ValueError(schema_version_message(source, version))
    except BaseException:
        remove_temp_database(new_path)
        raise
    return PreparedRestore(db_path, source, new_path, app_id == LIVE_APPLICATION_ID, current_live)


def restore_database(db_path: Path | str, source: Path | str) -> PreparedRestore:
    """Восстановить базу из копии без вопросов: prepare_restore + replace_database.

    Вернуть PreparedRestore: backup — pre-op копия прежней базы, live — рабочая ли
    восстановленная база.
    """
    with prepare_restore(db_path, source) as prepared:
        prepared.replace_database()
    return prepared
