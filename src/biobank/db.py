"""Подключение к базе SQLite и создание пустой базы из db/schema.sql.

Главное правило: в SQLite проверка внешних ключей по умолчанию ВЫКЛЮЧЕНА
и включается отдельно в каждом подключении командой PRAGMA foreign_keys = ON.
Поэтому все модули проекта открывают базу только через connect() отсюда.
"""

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
