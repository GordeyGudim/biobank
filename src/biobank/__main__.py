"""Командная строка: python -m biobank <команда>.

Команды добавляются по этапам: init, import.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from biobank.db import DEFAULT_DB_PATH, connect, create_database, list_tables
from biobank.excel_reader import ExcelReadError
from biobank.importer import import_workbook


def ask_yes_no(question: str) -> bool:
    """Спросить пользователя «да/нет» в терминале."""
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def cmd_init(args: argparse.Namespace) -> int:
    db_path: Path = args.db
    if db_path.exists() and not args.yes:
        if not ask_yes_no(f"Файл {db_path} уже существует. Удалить и создать заново?"):
            print("Отменено.")
            return 1

    create_database(db_path, overwrite=True)

    conn = connect(db_path)
    tables = list_tables(conn)
    conn.close()
    print(f"Создана пустая база: {db_path}")
    print(f"Таблиц: {len(tables)} — {', '.join(tables)}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    xlsx: Path = args.xlsx
    if not xlsx.exists():
        print(f"Ошибка: файл не найден: {xlsx}", file=sys.stderr)
        return 1
    try:
        summary = import_workbook(xlsx, args.db)
    except (ExcelReadError, sqlite3.Error) as error:
        print(f"Ошибка импорта, база не изменена: {error}", file=sys.stderr)
        return 1

    print(f"Импорт завершён: {args.db}")
    width = max(len(name) for name in summary)
    for table, count in summary.items():
        print(f"  {table:<{width}}  {count:>5}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biobank",
        description="Информационный банк биологических данных (экспедиции во Вьетнам)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"путь к файлу базы (по умолчанию {DEFAULT_DB_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<команда>")

    p_init = sub.add_parser("init", help="создать пустую базу из db/schema.sql")
    p_init.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    p_init.set_defaults(func=cmd_init)

    p_import = sub.add_parser("import", help="пересоздать базу и импортировать книгу Excel")
    p_import.add_argument("xlsx", type=Path, help="путь к книге, напр. data/source.xlsx")
    p_import.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
