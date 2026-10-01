"""Командная строка: python -m biobank <команда>.

Команды: init, import, check, export, report, sql, link-trawl, identify, resolve-issue.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from biobank.checks import run_checks, summary_by_category
from biobank.db import DEFAULT_DB_PATH, connect, create_database, list_tables
from biobank.edit import (
    CONFIDENCES,
    METHODS,
    EditError,
    backup_database,
    identify,
    link_trawl,
    manual_edits,
    resolve_issue,
)
from biobank.excel_reader import ExcelReadError
from biobank.export import export_csv, export_xlsx
from biobank.importer import import_workbook
from biobank.reports import (
    REPORTS,
    connect_read_only,
    format_table,
    render_report,
    run_query,
)


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
    if args.db.exists():
        conn = connect(args.db)
        try:
            edits = manual_edits(conn)
        except sqlite3.Error:
            edits = {}  # старая база другого формата — правок в ней искать негде
        finally:
            conn.close()
        if edits:
            print("ВНИМАНИЕ: импорт пересоздаёт базу из Excel, ручные правки пропадут:")
            for name, n in edits.items():
                print(f"  {name}: {n}")
            if not args.yes and not ask_yes_no("Продолжить импорт?"):
                print("Отменено, база не изменена.")
                return 1
            print(f"Резервная копия текущей базы: {backup_database(args.db)}")
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


def cmd_check(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(f"Ошибка: базы нет: {args.db}. Сначала: python -m biobank import …", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        with conn:
            result = run_checks(conn)
        rows = summary_by_category(conn)
    finally:
        conn.close()

    print(f"Проверки выполнены: новых проблем {result.added}, уже известных {result.known}, "
          f"закрыто (больше не находятся) {result.closed}.")  # fmt: skip
    print()
    print(f"  {'категория':<12} {'открыто':>8} {'решено':>7} {'всего':>6}")
    for r in rows:
        print(f"  {r['category']:<12} {r['open']:>8} {r['resolved']:>7} {r['total']:>6}")
    print()
    print("Подробности: python -m biobank report issues")
    return 0


def _require_db(path: Path) -> bool:
    if path.exists():
        return True
    print(f"Ошибка: базы нет: {path}. Сначала: python -m biobank import …", file=sys.stderr)
    return False


def cmd_export(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    if args.out is None:
        args.out = args.db.parent / ("biobank.xlsx" if args.format == "xlsx" else "csv")
    conn = connect_read_only(args.db)
    try:
        if args.format == "xlsx":
            written = export_xlsx(conn, args.out)
        else:
            written = export_csv(conn, args.out)
    finally:
        conn.close()
    print(f"Выгружено: {args.out}")
    print(format_table(["лист / файл", "строк"], written))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    conn = connect_read_only(args.db)
    try:
        print(render_report(conn, args.name, args.width))
    finally:
        conn.close()
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    conn = connect_read_only(args.db)
    try:
        columns, rows = run_query(conn, args.query)
    except sqlite3.Error as error:
        # база открыта только на чтение — UPDATE/DELETE/INSERT тоже окажутся здесь
        print(f"Ошибка SQL: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if not columns:
        print("Запрос ничего не вернул.")
        return 0
    print(format_table(columns, rows, max_width=args.width))
    print(f"строк: {len(rows)}")
    return 0


def _run_edit(args: argparse.Namespace, action) -> int:
    """Общая обёртка правок: резервная копия → правка → отчёт."""
    if not _require_db(args.db):
        return 1
    backup = backup_database(args.db)
    conn = connect(args.db)
    try:
        result = action(conn)
    except EditError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        print("База не изменена.", file=sys.stderr)
        backup.unlink()  # правки не было — копия не нужна
        return 1
    finally:
        conn.close()
    print(result.message)
    print(f"Резервная копия до правки: {backup}")
    if result.checks.added or result.checks.closed:
        print(f"Журнал проблем: новых {result.checks.added}, закрыто {result.checks.closed}.")
    return 0


def cmd_link_trawl(args: argparse.Namespace) -> int:
    return _run_edit(args, lambda conn: link_trawl(conn, args.label, args.trawl_no))


def cmd_identify(args: argparse.Namespace) -> int:
    species = None if args.species in ("?", "-") else args.species
    return _run_edit(
        args,
        lambda conn: identify(
            conn, args.label, species, args.method, args.confidence,
            identified_by=args.by, identified_on=args.date, notes=args.notes,
        ),
    )  # fmt: skip


def cmd_resolve_issue(args: argparse.Namespace) -> int:
    return _run_edit(args, lambda conn: resolve_issue(conn, args.issue_id, args.resolution))


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
    p_import.add_argument(
        "-y", "--yes", action="store_true", help="не спрашивать, даже если есть ручные правки"
    )
    p_import.set_defaults(func=cmd_import)

    p_check = sub.add_parser("check", help="запустить проверки данных и обновить журнал проблем")
    p_check.set_defaults(func=cmd_check)

    p_export = sub.add_parser("export", help="выгрузить все таблицы и сводки в xlsx или csv")
    p_export.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    p_export.add_argument(
        "--out", type=Path, help="файл .xlsx или папка для csv (по умолчанию — в output/)"
    )
    p_export.set_defaults(func=cmd_export)

    p_report = sub.add_parser("report", help="стандартный отчёт")
    p_report.add_argument("name", choices=list(REPORTS), help="какой отчёт")
    p_report.add_argument("--width", type=int, default=50, help="макс. ширина колонки")
    p_report.set_defaults(func=cmd_report)

    p_sql = sub.add_parser("sql", help="выполнить запрос только на чтение и напечатать результат")
    p_sql.add_argument("query", help='SQL-запрос в кавычках, напр. "SELECT * FROM species"')
    p_sql.add_argument("--width", type=int, default=50, help="макс. ширина колонки (символов)")
    p_sql.set_defaults(func=cmd_sql)

    p_link = sub.add_parser("link-trawl", help="привязать особь к тралению её же выезда")
    p_link.add_argument("label", help='номер пробы, напр. "26 ph"')
    p_link.add_argument("trawl_no", type=int, help="номер траления на этом выезде")
    p_link.set_defaults(func=cmd_link_trawl)

    p_ident = sub.add_parser(
        "identify", help="добавить определение вида и сделать его текущим видом особи"
    )
    p_ident.add_argument("label", help='номер пробы, напр. "26 ph"')
    p_ident.add_argument(
        "species", help='вид из справочника, напр. "Pangasius elongatus"; «?» — не определён'
    )
    p_ident.add_argument("--method", choices=METHODS, required=True)
    p_ident.add_argument("--confidence", choices=CONFIDENCES, required=True)
    p_ident.add_argument("--by", help="кто определил")
    p_ident.add_argument("--date", help="дата определения ГГГГ-ММ-ДД (по умолчанию сегодня)")
    p_ident.add_argument("--notes", help="примечание, напр. номер в GenBank")
    p_ident.set_defaults(func=cmd_identify)

    p_resolve = sub.add_parser("resolve-issue", help="отметить проблему из журнала решённой")
    p_resolve.add_argument("issue_id", type=int, help="номер проблемы (report issues)")
    p_resolve.add_argument("resolution", help="как решили, в кавычках")
    p_resolve.set_defaults(func=cmd_resolve_issue)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
