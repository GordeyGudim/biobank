"""Подключение к базе SQLite, создание пустой базы из db/schema.sql, резервные копии.

Главное правило: в SQLite проверка внешних ключей по умолчанию ВЫКЛЮЧЕНА
и включается отдельно в каждом подключении командой PRAGMA foreign_keys = ON.
Поэтому все модули проекта открывают базу только через connect() или
connect_read_only() отсюда.
"""

import datetime as dt
import shutil
import sqlite3
from pathlib import Path

# src/biobank/db.py → parents[2] = корень репозитория
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
DEFAULT_DB_PATH = PROJECT_ROOT / "output" / "biobank.db"


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Открыть базу и включить проверку внешних ключей.

    row_factory = sqlite3.Row позволяет обращаться к колонкам по имени:
    row["label"] вместо row[0].
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
    "journal_mode", "page_size", "page_count", "freelist_count",
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
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # до authorizer: потом менять настройки нельзя
    conn.set_authorizer(_read_only_authorizer)
    return conn


def create_database(
    db_path: Path | str = DEFAULT_DB_PATH,
    schema_path: Path | str = SCHEMA_PATH,
    overwrite: bool = False,
) -> Path:
    """Создать пустую базу по схеме и вернуть путь к файлу.

    Если файл уже есть и overwrite=False — ошибка FileExistsError,
    чтобы случайно не стереть данные. Спрашивать пользователя — задача CLI.
    """
    db_path = Path(db_path)
    if db_path.exists():
        if not overwrite:
            raise FileExistsError(f"База уже существует: {db_path}")
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = Path(schema_path).read_text(encoding="utf-8")

    conn = connect(db_path)
    try:
        # executescript выполняет сразу много команд SQL, разделённых «;»
        conn.executescript(schema_sql)
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


def backup_database(db_path: Path | str, backup_dir: Path | None = None) -> Path:
    """Скопировать базу в <папка базы>/backups/<имя>_ГГГГММДД_ЧЧММСС.db.

    Используется backup() из sqlite3, а не простое копирование файла:
    он делает согласованную копию, даже если базу в этот момент кто-то читает.
    Исходная база открывается только на чтение — копирование её не изменит.
    Если файл — не база SQLite (повреждён, другой формат), он копируется как есть.
    Если файла нет — FileNotFoundError, папка backups/ при этом не создаётся.
    """
    db_path = Path(db_path)
    if not db_path.is_file():  # сначала источник — иначе осталась бы пустая папка backups/
        raise FileNotFoundError(f"Нечего копировать — файла базы нет: {db_path}")
    backup_dir = backup_dir or db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{db_path.stem}_{stamp}.db"
    n = 1
    while target.exists():  # две копии в одну секунду
        target = backup_dir / f"{db_path.stem}_{stamp}_{n}.db"
        n += 1
    source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    except sqlite3.DatabaseError:
        dest.close()
        target.unlink(missing_ok=True)
        shutil.copy2(db_path, target)
    finally:
        dest.close()
        source.close()
    return target
