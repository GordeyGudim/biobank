"""Командная строка: python -m biobank <команда>.

Сейчас реализована только команда init; остальные добавляются по этапам.
"""

import argparse
import sys
from pathlib import Path

from biobank.db import DEFAULT_DB_PATH, connect, create_database, list_tables


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
