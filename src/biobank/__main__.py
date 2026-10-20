"""Командная строка: python -m biobank <команда>.

Команды: init, import, check, export, report, sql, show, link-trawl, identify, resolve-issue,
web (сайт); backup и restore (резервная копия и восстановление из неё),
go-live / undo-go-live (пометка «рабочая база»);
ввод новых данных (entry.py): add-expedition, add-site, add-event, add-trawling, add-water,
next-label, add-specimen, update-measurements, add-sample, ship-sample, add-analysis, add-catch;
пользователи сайта (users.py): create-user, reset-password, set-roles, deactivate-user,
activate-user, list-users.

Команды работают в системном режиме (user=None): права не проверяются — у того, кто
запускает команды на сервере, и так есть доступ к файлу базы; в журнал изменений
(change_log) они пишутся без пользователя (user_id = NULL).
"""

import argparse
import getpass
import shutil
import sqlite3
import sys
from pathlib import Path

from biobank.card import render_card, specimen_card
from biobank.checks import summary_by_category
from biobank.db import (
    BACKUP_KINDS,
    DAILY,
    DEFAULT_DB_PATH,
    FUTURE_WARNING,
    PRE_OP,
    RETENTION,
    LiveDatabaseError,
    backup_database,
    connect,
    connect_read_only,
    create_database,
    ensure_not_live,
    is_live,
    list_tables,
    prepare_restore,
    prune_backups,
    run_backup,
    set_live,
)
from biobank.edit import (
    CONFIDENCES,
    METHODS,
    EditError,
    apply_edit,
    check_database,
    identify,
    link_trawl,
    resolve_issue,
)
from biobank.entry import (
    POINT_TYPES,
    SAMPLE_TYPES,
    SEXES,
    SITE_TYPES,
    add_analysis,
    add_sample,
    add_trawling,
    add_water_measurement,
    create_catch_record,
    create_event,
    create_expedition,
    create_site,
    create_specimen,
    next_label,
    ship_sample,
    update_measurements,
)
from biobank.excel_reader import ExcelReadError
from biobank.export import export_csv, export_xlsx
from biobank.importer import prepare_import
from biobank.permissions import ROLES, normalize_role, roles_help
from biobank.reports import REPORTS, render_report
from biobank.tables import format_table, run_query
from biobank.users import (
    activate,
    create_user,
    deactivate,
    list_users,
    reset_password,
    set_roles,
)

WEB_HOST = "127.0.0.1"  # встроенный сервер — только для этого компьютера (см. cmd_web)

# Подсказки командной строки к сообщениям логики. Сами сообщения (entry.py, records.py)
# нейтральны — их показывает и сайт («отметьте «это новая лаборатория»»), а здесь к ним
# дописывается, каким флагом или командой сделать то же в командной строке.
# Пара: (кусок текста сообщения, подсказка).
CLI_HINTS = (
    ("отметьте «это новая лаборатория»", "флаг --new-lab"),
    ("отметьте «это новая провинция»", "флаг --new-region"),
    ("Укажите провинцию города", "флаг --region"),
    ("Укажите название места или город", "--name или --city вместе с --region"),
    ("подтвердите создание нового места", "флаг --new"),
    ("Сеть не указана", "--mesh, --size"),
    ("укажите номер явно", "--label"),
    ("Укажите, какие промеры изменить", "--tl, --sl, --mass, --gutted"),
    ("нет тралений — сначала добавьте", "python -m biobank add-trawling <выезд> <номер>"),
    ("Экспедиции №", 'список: python -m biobank sql "SELECT expedition_id, name FROM expedition"'),
    ("Места №", "список: python -m biobank report sites"),
    ("Выезда №", "список: python -m biobank sql "
                 '"SELECT event_id, event_date, source_sheet FROM sampling_event"'),
    ("Номера образцов — в карточке особи", "python -m biobank show <номер пробы>"),
    ("Базы нет:", "python -m biobank import data/source.xlsx"),
    ("нет в журнале", "список: python -m biobank report issues"),
    ("снимите пометку «рабочая база»", "python -m biobank undo-go-live (только при аварии)"),
)  # fmt: skip


def with_cli_hints(text: str) -> str:
    """Сообщение + подсказки CLI_HINTS, куски которых в нём есть (каждая — с новой строки).

    >>> with_cli_hints("Укажите провинцию города «Тест».")
    'Укажите провинцию города «Тест».\n  в командной строке: флаг --region'
    >>> with_cli_hints("Особь добавлена.")
    'Особь добавлена.'
    """
    hints = [hint for marker, hint in CLI_HINTS if marker in text]
    return "".join([text, *(f"\n  в командной строке: {hint}" for hint in hints)])


def ask_yes_no(question: str) -> bool:
    """Спросить пользователя «да/нет» в терминале."""
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def cmd_init(args: argparse.Namespace) -> int:
    db_path: Path = args.db
    try:
        ensure_not_live(db_path)  # рабочую базу пересоздавать нельзя — и спрашивать незачем
        if db_path.exists() and not args.yes:
            if not ask_yes_no(f"Файл {db_path} уже существует. Удалить и создать заново?"):
                print("Отменено.")
                return 1
        create_database(db_path, overwrite=True)
    except (LiveDatabaseError, sqlite3.Error) as error:
        print(f"Ошибка: {with_cli_hints(str(error))}", file=sys.stderr)
        print("База не изменена.", file=sys.stderr)
        return 1

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
        # новая база собирается во временном файле и сравнивается с текущей
        prepared = prepare_import(xlsx, args.db)
    except LiveDatabaseError as error:
        print(f"Импорт запрещён (даже с --force): {with_cli_hints(str(error))}", file=sys.stderr)
        return 1
    except (ExcelReadError, sqlite3.Error) as error:
        print(f"Ошибка импорта, база не изменена: {error}", file=sys.stderr)
        return 1
    with prepared:  # при выходе временный файл удаляется, если базу не заменили
        if prepared.needs_force:
            if prepared.unreadable:
                print(f"ВНИМАНИЕ: не удалось сравнить базу {args.db} с Excel:")
                print(f"  {prepared.unreadable}")
            else:
                print(
                    "ВНИМАНИЕ: текущая база отличается от Excel — данные, введённые или "
                    "исправленные не через Excel, при импорте пропадут (число записей):"
                )
                for difference in prepared.differences:
                    print(f"  {difference.describe()}")
                print(
                    "Изменённая запись считается и «только в базе», и «только в Excel». "
                    "Если вы исправили сам Excel или обновили программу — это тоже различия."
                )
            if not args.force:
                print(
                    "Импорт отменён, база не изменена. Если базу действительно нужно пересоздать "
                    "из Excel — повторите с --force (резервная копия будет сделана).",
                    file=sys.stderr,
                )
                return 1
            if not args.yes and not ask_yes_no("Пересоздать базу из Excel?"):
                print("Отменено, база не изменена.")
                return 1
        try:
            if prepared.exists:
                print(f"Резервная копия текущей базы: {backup_database(args.db, kind=PRE_OP)}")
                prune_backups(args.db, PRE_OP)
            prepared.replace_database()
        except (LiveDatabaseError, OSError, sqlite3.Error) as error:
            print(f"Ошибка: {with_cli_hints(str(error))}", file=sys.stderr)
            print("Импорт отменён, база не изменена.", file=sys.stderr)
            return 1

    print(f"Импорт завершён: {args.db}")
    width = max(len(name) for name in prepared.summary)
    for table, count in prepared.summary.items():
        print(f"  {table:<{width}}  {count:>5}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    try:
        result = check_database(args.db)  # проверки в транзакции BEGIN IMMEDIATE
    except EditError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    conn = connect_read_only(args.db)
    try:
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


def cmd_backup(args: argparse.Namespace) -> int:
    """Резервная копия + очистка старых копий того же вида (для ночного таймера на сервере)."""
    if not _require_db(args.db):
        return 1
    try:
        report = run_backup(args.db, args.kind)
    except (OSError, sqlite3.Error) as error:
        print(f"Ошибка резервного копирования: {error}", file=sys.stderr)
        return 1  # код ошибки — таймер на сервере увидит, что копия не сделана
    for path in report.created:
        print(f"Резервная копия: {path}")
    print(f"Удалено старых копий по правилу хранения: {len(report.deleted)}")
    for path in report.deleted:
        print(f"  {path}")
    if report.future:
        # в stderr и с кодом 2: копии сделаны, но таймер на сервере должен заметить сбой часов
        print(f"Внимание: {FUTURE_WARNING} ({len(report.future)} шт.):", file=sys.stderr)
        for path in report.future:
            print(f"  {path}", file=sys.stderr)
        return 2
    return 0


def _live_status(db_path: Path) -> bool | None:
    """is_live для команд: None и сообщение — если базу не удалось прочитать (занята…)."""
    try:
        return is_live(db_path)
    except sqlite3.Error as error:
        print(f"Ошибка: не удалось прочитать базу {db_path}: {error}", file=sys.stderr)
        print("Если базу сейчас пишет другая программа — повторите позже.", file=sys.stderr)
        return None


def cmd_go_live(args: argparse.Namespace) -> int:
    """Отметить базу как рабочую: после этого import и init на ней запрещены."""
    if not _require_db(args.db):
        return 1
    live = _live_status(args.db)
    if live is None:
        return 1
    if live:
        print(f"База {args.db} уже отмечена как рабочая.")
        return 0
    print(
        f"База {args.db} станет РАБОЧЕЙ — единственным источником данных. После этого\n"
        "  import (пересоздание из Excel) и init на ней запрещены, даже с --force;\n"
        "  сайт, правки и резервные копии работают как обычно; пометка сохраняется в\n"
        "  самом файле базы — копии и восстановленная из копии база тоже рабочие."
    )
    if not args.yes and not ask_yes_no("Отметить базу как рабочую?"):
        print("Отменено, база не изменена.")
        return 1
    try:
        set_live(args.db, True)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print(f"База {args.db} отмечена как рабочая.")
    return 0


def cmd_undo_go_live(args: argparse.Namespace) -> int:
    """Снять пометку «рабочая база» — только при аварии; всегда спрашивает подтверждение."""
    if not _require_db(args.db):
        return 1
    live = _live_status(args.db)
    if live is None:
        return 1
    if not live:
        print(f"База {args.db} не отмечена как рабочая — снимать нечего.")
        return 0
    print(
        "ВНИМАНИЕ: после снятия пометки команда import сможет пересоздать базу из Excel\n"
        "  и стереть всё, что внесено через сайт. Снимайте только при аварии, когда базу\n"
        "  действительно нужно собрать заново, и сначала сделайте копию: python -m biobank "
        "backup --kind pre-op"
    )
    if not ask_yes_no(f"Снять пометку «рабочая база» с {args.db}?"):
        print("Отменено, пометка осталась.")
        return 1
    try:
        set_live(args.db, False)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print(f"Пометка снята: {args.db} больше не рабочая база (import снова возможен).")
    return 0


def _yes_no(flag: bool) -> str:
    return "да" if flag else "нет"


def cmd_restore(args: argparse.Namespace) -> int:
    """Заменить базу резервной копией (безопасно для режима WAL, в отличие от cp)."""
    try:
        # копия переносится во временный файл и проверяется до вопроса пользователю
        prepared = prepare_restore(args.db, args.backup)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        print("База не изменена.", file=sys.stderr)
        return 1
    with prepared:  # при выходе временный файл удаляется, если базу не заменили
        if prepared.exists:
            print(
                f"База {args.db} (рабочая: {_yes_no(prepared.current_live)}) будет ЗАМЕНЕНА "
                f"копией\n  {prepared.source} (рабочая: {_yes_no(prepared.source_live)})."
            )
            print("  Всё, что внесено после этой копии, пропадёт из базы; текущая база перед")
            print("  заменой сохранится как копия pre-op.")
            if prepared.current_live and not prepared.source_live:
                print("  Копия сделана до go-live: пометка «рабочая база» будет поставлена и на")
                print("  восстановленную базу (снять — только командой undo-go-live).")
        else:
            print(f"Базы {args.db} нет — она будет создана из копии {prepared.source}.")
        print("  Перед восстановлением остановите сайт и все программы, работающие с базой.")
        if not args.yes and not ask_yes_no("Восстановить базу из копии?"):
            print("Отменено, база не изменена.")
            return 1
        try:
            prepared.replace_database()
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            print("База не изменена.", file=sys.stderr)
            return 1
    print(f"База {prepared.db_path} восстановлена из копии {prepared.source}.")
    if prepared.backup is not None:
        print(f"Резервная копия базы до восстановления: {prepared.backup}")
    if prepared.current_live and not prepared.source_live:
        print("Пометка «рабочая база» сохранена: она поставлена на восстановленную базу.")
    if prepared.live:
        print("Восстановленная база — рабочая (go-live): import и init на ней запрещены.")
    else:
        print("Восстановленная база не помечена как рабочая (import на ней возможен).")
    return 0


def screen_width() -> int | None:
    """Ширина окна терминала; None — если вывод идёт в файл (там переносить незачем)."""
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size().columns


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
        print(render_report(conn, args.name, args.width, screen_width()))
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
        # подключение только на чтение: UPDATE/DELETE/INSERT, ATTACH, PRAGMA с записью
        # SQLite отклоняет с ошибкой «not authorized» (VACUUM INTO — «authorization denied»)
        if "authoriz" in str(error):
            error = f"команда sql только читает данные, этот запрос запрещён ({error})"
        print(f"Ошибка SQL: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if not columns:
        print("Запрос ничего не вернул.")
        return 0
    print(format_table(columns, rows, max_width=args.width, total_width=screen_width()))
    print(f"строк: {len(rows)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    conn = connect_read_only(args.db)
    try:
        sections = specimen_card(conn, args.label)
    except EditError as error:  # особь не найдена (NotFoundError)
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(render_card(sections, args.width, screen_width()))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Запустить сайт на этом компьютере: http://127.0.0.1:<порт>/."""
    if not _require_db(args.db):
        return 1
    try:
        # Flask — необязательная зависимость: импортируем только здесь, чтобы
        # остальные команды работали и без него
        from biobank.web.app import create_app
    except ImportError:
        print("Для сайта нужен Flask: .venv/bin/pip install -e '.[web]'", file=sys.stderr)
        return 1
    print(f"Сайт: http://{WEB_HOST}:{args.port}/  (остановить — Ctrl+C)")
    # Вход по паролю есть, но встроенный сервер Flask — для работы на своём компьютере:
    # без HTTPS пароль шёл бы по сети открытым текстом. На сервере сайт ставят за прокси
    # с HTTPS (nginx…) и настраивают переменными окружения — README, раздел «Сайт».
    # secure_cookies: ключ --secure-cookies или BIOBANK_SECURE_COOKIES=1 (None — из окружения)
    secure = True if args.secure_cookies else None
    create_app(args.db, secure_cookies=secure).run(host=WEB_HOST, port=args.port, debug=False)
    return 0


def _run_edit(args: argparse.Namespace, action) -> int:
    """Общая обёртка правок: apply_edit (резервная копия → правка) → отчёт."""
    try:
        result = apply_edit(args.db, action)
    except EditError as error:
        print(f"Ошибка: {with_cli_hints(str(error))}", file=sys.stderr)
        print("База не изменена.", file=sys.stderr)
        return 1
    print(result.message)
    for warning in result.warnings:
        print(f"Внимание: {with_cli_hints(warning)}")
    print(f"Резервная копия до правки: {result.backup}")
    for notice in result.notices:
        print(f"Внимание: {notice}", file=sys.stderr)
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


# --- Ввод новых данных (entry.py) --------------------------------------------


def cmd_add_expedition(args: argparse.Namespace) -> int:
    return _run_edit(
        args, lambda conn: create_expedition(conn, args.name, args.start, args.end, args.notes)
    )


def cmd_add_site(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: create_site(
            conn, args.site_type, name=args.name, lat=args.lat, lon=args.lon, city=args.city,
            region=args.region, description=args.description, new_region=args.new_region,
            allow_nearby=args.new,
        ),
    )  # fmt: skip


def cmd_add_event(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: create_event(
            conn, args.expedition_id, args.site_id, args.date, args.method,
            gear_mesh=args.mesh, gear_size=args.size, notes=args.notes,
        ),
    )  # fmt: skip


def cmd_add_trawling(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: add_trawling(
            conn, args.event_id, args.trawl_no, time_start=args.start, time_end=args.end,
            duration_min=args.duration, speed_kmh=args.speed, intermediate_depth_m=args.depth,
            catch_note=args.catch, notes=args.notes,
        ),
    )  # fmt: skip


def cmd_add_water(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: add_water_measurement(
            conn, args.event_id, args.point_type, args.trawl, measured_time=args.time,
            coord_source=args.source, garmin_wp=args.wp, notes=args.notes,
            lat=args.lat, lon=args.lon, depth_m=args.depth, o2_pct=args.o2, ppm=args.ppm,
            conductivity_ms=args.ms, conductivity_us=args.us, ph=args.ph,
            water_temp_c=args.temp,
        ),
    )  # fmt: skip


def cmd_next_label(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    conn = connect_read_only(args.db)
    try:
        print(next_label(conn, "" if args.suffix == "-" else args.suffix))
    except EditError as error:
        print(f"Ошибка: {with_cli_hints(str(error))}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def cmd_add_specimen(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: create_specimen(
            conn, args.event_id, args.species, args.label, confidence=args.confidence,
            tl_cm=args.tl, sl_cm=args.sl, mass_g=args.mass, mass_gutted_g=args.gutted,
            sex=args.sex, is_dead=args.dead, trawl_no=args.trawl, capture_method=args.method,
            capture_site_id=args.site, capture_date=args.date, identified_by=args.by,
            notes=args.notes,
        ),
    )  # fmt: skip


def cmd_update_measurements(args: argparse.Namespace) -> int:
    # передаём только указанные промеры; «-» — стереть значение
    given = {"tl_cm": args.tl, "sl_cm": args.sl, "mass_g": args.mass, "mass_gutted_g": args.gutted}
    changes = {column: value for column, value in given.items() if value is not None}
    return _run_edit(args, lambda conn: update_measurements(conn, args.label, **changes))


def cmd_add_sample(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: add_sample(
            conn, args.label, args.sample_type, organ=args.organ, sample_label=args.tag,
            preservative=args.preservative, notes=args.notes, labs=args.lab,
        ),
    )  # fmt: skip


def cmd_ship_sample(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: ship_sample(conn, args.sample_id, args.lab, args.date, new_lab=args.new_lab),
    )


def cmd_add_analysis(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: add_analysis(
            conn, args.sample_id, args.analysis_type, lab=args.lab, analysed_on=args.date,
            method=args.method, result=args.result, accession=args.accession, notes=args.notes,
        ),
    )  # fmt: skip


def cmd_add_catch(args: argparse.Namespace) -> int:
    return _run_edit(
        args,
        lambda conn: create_catch_record(
            conn, args.event_id, raw_text=args.text, taxon_text=args.taxon, count_n=args.count,
            mass_g=args.mass, trawl_no=args.trawl,
        ),
    )  # fmt: skip


# --- Пользователи сайта (users.py) --------------------------------------------


def ask_new_password() -> str | None:
    """Пароль — только с клавиатуры, дважды и без показа на экране (getpass).

    Не аргументом командной строки: аргументы видны другим пользователям сервера
    (список процессов) и остаются в истории команд терминала.
    """
    first = getpass.getpass("Пароль (не меньше 10 символов): ")
    second = getpass.getpass("Пароль ещё раз: ")
    if first != second:
        print("Ошибка: пароли не совпадают. Пользователь не создан.", file=sys.stderr)
        return None
    return first


def _run_users(args: argparse.Namespace, action) -> int:
    """Общая обёртка команд пользователей: подключение → action(conn) → ошибка или 0."""
    if not _require_db(args.db):
        return 1
    conn = connect(args.db)
    try:
        action(conn)
    except EditError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        print("База не изменена.", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print(f"Ошибка базы данных: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _roles_text(user) -> str:
    return ", ".join(role for role in ROLES if role in user.roles)


def cmd_create_user(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    password = ask_new_password()
    if password is None:
        return 1

    def action(conn):
        user = create_user(
            conn, args.login, password, args.name, args.role, args.person,
            must_change_password=args.must_change,
        )  # fmt: skip
        print(f"Создан пользователь {user.login} «{user.display_name}»: {_roles_text(user)}.")
        if user.must_change_password:
            print("При первом входе сайт попросит сменить пароль.")

    return _run_users(args, action)


def cmd_reset_password(args: argparse.Namespace) -> int:
    def action(conn):
        temporary = reset_password(conn, args.login)
        print(f"Временный пароль для {args.login}: {temporary}")
        print("Он показан один раз. При входе сайт попросит сменить его.")

    return _run_users(args, action)


def cmd_set_roles(args: argparse.Namespace) -> int:
    def action(conn):
        user = set_roles(conn, args.login, args.role)
        print(f"Роли {user.login}: {_roles_text(user)}.")

    return _run_users(args, action)


def cmd_deactivate_user(args: argparse.Namespace) -> int:
    def action(conn):
        user = deactivate(conn, args.login)
        print(f"Вход для {user.login} закрыт. Записи и история пользователя сохранены.")

    return _run_users(args, action)


def cmd_activate_user(args: argparse.Namespace) -> int:
    def action(conn):
        user = activate(conn, args.login)
        print(f"Вход для {user.login} открыт.")

    return _run_users(args, action)


def cmd_list_users(args: argparse.Namespace) -> int:
    if not _require_db(args.db):
        return 1
    conn = connect_read_only(args.db)
    try:
        users = list_users(conn)
    finally:
        conn.close()
    if not users:
        print("Пользователей нет. Первого админа: python -m biobank create-user <логин> "
              "--role админ --name «Имя»")  # fmt: skip
        return 0
    rows = [
        (u.user_id, u.login, u.display_name, _roles_text(u), "да" if u.is_active else "нет",
         u.last_login_at or "—")
        for u in users
    ]  # fmt: skip
    columns = ["№", "логин", "имя", "роли", "вход открыт", "последний вход (UTC)"]
    print(format_table(columns, rows, total_width=screen_width()))
    return 0


def role_arg(text: str) -> str:
    """--role: русское или английское имя роли в любом регистре → русское (как в базе)."""
    try:
        return normalize_role(text)
    except EditError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _add_user_parsers(sub) -> None:
    roles = roles_help()
    p = sub.add_parser(
        "create-user",
        help="новый пользователь сайта (пароль спросит дважды)",
        description="Пароль вводится с клавиатуры (не аргументом). Так создаётся и первый админ.",
    )
    p.add_argument("login", help="логин: латиница, цифры, «.», «_», «-»")
    p.add_argument("--name", required=True, help="имя для подписи в карточках, напр. «Иванова А.»")
    p.add_argument("--role", action="append", required=True, type=role_arg, metavar="РОЛЬ",
                   help=f"роль (можно несколько --role): {roles}")  # fmt: skip
    p.add_argument("--person", type=int, help="этот же человек в справочнике людей (person_id)")
    p.add_argument(
        "--must-change",
        action="store_true",
        help="при первом входе попросить сменить пароль (если его придумали вы)",
    )
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("reset-password", help="новый временный пароль (покажет один раз)")
    p.add_argument("login")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("set-roles", help="заменить роли пользователя")
    p.add_argument("login")
    p.add_argument("--role", action="append", required=True, type=role_arg, metavar="РОЛЬ",
                   help=f"роль (можно несколько --role): {roles}")  # fmt: skip
    p.set_defaults(func=cmd_set_roles)

    p = sub.add_parser("deactivate-user", help="закрыть вход пользователю (записи остаются)")
    p.add_argument("login")
    p.set_defaults(func=cmd_deactivate_user)

    p = sub.add_parser("activate-user", help="снова открыть вход пользователю")
    p.add_argument("login")
    p.set_defaults(func=cmd_activate_user)

    p = sub.add_parser("list-users", help="пользователи сайта и их роли")
    p.set_defaults(func=cmd_list_users)


def _add_entry_parsers(sub) -> None:
    """Команды ввода новых данных. Числа можно писать с запятой: 29,3."""
    p = sub.add_parser("add-expedition", help="новая экспедиция")
    p.add_argument("name", help='название, напр. "Кантхо 2027"')
    p.add_argument("--start", help="дата начала ГГГГ-ММ-ДД (иначе — по первому выезду)")
    p.add_argument("--end", help="дата окончания ГГГГ-ММ-ДД")
    p.add_argument("--notes", help="примечание")
    p.set_defaults(func=cmd_add_expedition)

    p = sub.add_parser("add-site", help="новое место (не создаёт дубль места ближе 200 м)")
    p.add_argument("site_type", choices=SITE_TYPES, help="тип места")
    p.add_argument("--name", help="название (по умолчанию «Участок у <город>», «Рынок <город>»…)")
    p.add_argument("--lat", help="широта, напр. 10.0452")
    p.add_argument("--lon", help="долгота, напр. 105.7469")
    p.add_argument("--city", help="город")
    p.add_argument("--region", help="провинция города (из справочника region)")
    p.add_argument("--description", help="описание места")
    p.add_argument("--new-region", action="store_true", help="провинции нет — создать")
    p.add_argument("--new", action="store_true", help="рядом есть похожее место, но это другое")
    p.set_defaults(func=cmd_add_site)

    p = sub.add_parser("add-event", help="новый выезд (день в одном месте)")
    p.add_argument("expedition_id", type=int, help="номер экспедиции (expedition_id)")
    p.add_argument("site_id", type=int, help="номер места (site_id)")
    p.add_argument("date", help="дата выезда ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")
    p.add_argument("method", help="способ получения: траление, рыбак, рынок, аквахозяйство")
    p.add_argument("--mesh", help='сеть: размер ячеи, см, напр. "3,5x3,5" (только траление)')
    p.add_argument("--size", help='сеть: размер, м, напр. "6,5x12"')
    p.add_argument("--notes", help="примечание")
    p.set_defaults(func=cmd_add_event)

    p = sub.add_parser("add-trawling", help="траление выезда")
    p.add_argument("event_id", type=int, help="номер выезда (event_id)")
    p.add_argument("trawl_no", type=int, help="номер траления в этот день")
    p.add_argument("--start", help="время старта ЧЧ:ММ")
    p.add_argument("--end", help="время финиша ЧЧ:ММ")
    p.add_argument("--duration", help="длительность, мин")
    p.add_argument("--speed", help="скорость, км/ч")
    p.add_argument("--depth", help="промежуточная глубина, м")
    p.add_argument("--catch", help='что поймали, напр. "2 plotosus", "трал пустой"')
    p.add_argument("--notes", help="примечание (берег, заводы…)")
    p.set_defaults(func=cmd_add_trawling)

    p = sub.add_parser("add-water", help="замер воды: старт/финиш траления или точка")
    p.add_argument("event_id", type=int, help="номер выезда (event_id)")
    p.add_argument("point_type", choices=POINT_TYPES, help="старт, финиш или точка")
    p.add_argument("--trawl", type=int, help="номер траления (для старта и финиша)")
    p.add_argument("--time", help="время замера ЧЧ:ММ")
    p.add_argument("--lat", help="широта")
    p.add_argument("--lon", help="долгота")
    p.add_argument("--source", help="источник координат: Garmin или Google Maps")
    p.add_argument("--wp", help="№ точки на приборе Garmin")
    p.add_argument("--depth", help="глубина, м")
    p.add_argument("--o2", help="O2, %%")
    p.add_argument("--ppm", help="ppm")
    p.add_argument("--ms", help="электропроводность, mS")
    p.add_argument("--us", help="электропроводность, uS")
    p.add_argument("--ph", help="pH")
    p.add_argument("--temp", help="температура воды, °C")
    p.add_argument("--notes", help='примечание, напр. "hanna – 4,45"')
    p.set_defaults(func=cmd_add_water)

    p = sub.add_parser("next-label", help="следующий свободный номер пробы в серии")
    p.add_argument(
        "suffix", help="суффикс: pc, ph, e, sp, mc, Bg, mr; «-» — Pangasius без суффикса"
    )
    p.set_defaults(func=cmd_next_label)

    p = sub.add_parser("add-specimen", help="новая особь (сразу с определением вида)")
    p.add_argument("event_id", type=int, help="номер выезда (event_id)")
    p.add_argument(
        "species", help='вид, напр. "Plotosus canius", "P. elongatus"; «?» — не определён'
    )
    p.add_argument(
        "--label", help='номер пробы, напр. "179 pc" (по умолчанию — следующий свободный)'
    )
    p.add_argument("--confidence", choices=CONFIDENCES, help="по умолчанию: точно (sp. — до рода)")
    p.add_argument("--tl", help="TL, см")
    p.add_argument("--sl", help="SL, см")
    p.add_argument("--mass", help="m, г")
    p.add_argument("--gutted", help="m без внутренних органов, г")
    p.add_argument("--sex", choices=SEXES)
    p.add_argument("--dead", action="store_true", help="особь мёртвая")
    p.add_argument("--trawl", type=int, help="номер траления этого выезда, если известен")
    p.add_argument("--method", help="способ получения, если отличается от выезда (напр. рыбак)")
    p.add_argument("--site", type=int, help="место поимки (site_id), если отличается от выезда")
    p.add_argument("--date", help="дата поимки, если отличается от даты выезда")
    p.add_argument("--by", help="кто определил вид (имя; новое — в справочник людей)")
    p.add_argument("--notes", help="примечание")
    p.set_defaults(func=cmd_add_specimen)

    p = sub.add_parser("update-measurements", help="исправить промеры особи («-» — стереть)")
    p.add_argument("label", help='номер пробы, напр. "160"')
    p.add_argument("--tl", help="TL, см")
    p.add_argument("--sl", help="SL, см")
    p.add_argument("--mass", help="m, г")
    p.add_argument("--gutted", help="m без внутренних органов, г")
    p.set_defaults(func=cmd_update_measurements)

    p = sub.add_parser("add-sample", help="образец особи (пробирка, стекло, целая особь)")
    p.add_argument("label", help='номер пробы особи, напр. "179 pc"')
    p.add_argument("sample_type", choices=SAMPLE_TYPES, help="тип образца")
    p.add_argument("--organ", help="орган: печень, почки, икра, жабры, мышцы…")
    p.add_argument("--tag", help='метка образца, напр. "179 pc a" (по умолчанию — номер особи)')
    p.add_argument("--preservative", help='фиксация, напр. "формалин 10%%", "заморозка"')
    p.add_argument("--lab", action="append", default=[], help="куда отправлен (можно несколько)")
    p.add_argument("--notes", help="примечание")
    p.set_defaults(func=cmd_add_sample)

    p = sub.add_parser("ship-sample", help="отметить отправку образца в лабораторию")
    p.add_argument(
        "sample_id", type=int, help="номер образца (sample_id, есть в выводе add-sample)"
    )
    p.add_argument("lab", help='лаборатория, напр. "Россия", "Тропцентр"')
    p.add_argument("--date", help="дата отправки ГГГГ-ММ-ДД")
    p.add_argument("--new-lab", action="store_true", help="лаборатории нет — создать")
    p.set_defaults(func=cmd_ship_sample)

    p = sub.add_parser("add-analysis", help="результат исследования образца")
    p.add_argument("sample_id", type=int, help="номер образца (sample_id)")
    p.add_argument("analysis_type", help='вид анализа, напр. "секвенирование COI"')
    p.add_argument("--lab", help="лаборатория")
    p.add_argument("--date", help="дата анализа ГГГГ-ММ-ДД")
    p.add_argument("--method", help="методика")
    p.add_argument("--result", help="результат")
    p.add_argument("--accession", help="номер в GenBank и т.п.")
    p.add_argument("--notes", help="примечание")
    p.set_defaults(func=cmd_add_analysis)

    p = sub.add_parser("add-catch", help="прилов / общий улов выезда или траления")
    p.add_argument("event_id", type=int, help="номер выезда (event_id)")
    p.add_argument("--text", help='запись как в журнале, напр. "arius maculatus - 39 шт"')
    p.add_argument("--taxon", help="кто пойман")
    p.add_argument("--count", help="сколько штук")
    p.add_argument("--mass", help="масса, г")
    p.add_argument("--trawl", type=int, help="номер траления этого выезда")
    p.set_defaults(func=cmd_add_catch)


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
        "--force",
        action="store_true",
        help="пересоздать базу, даже если в ней есть записи не из Excel (они пропадут)",
    )
    p_import.add_argument(
        "-y", "--yes", action="store_true", help="вместе с --force: не спрашивать подтверждение"
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

    p_show = sub.add_parser("show", help="карточка особи: всё о ней из всех таблиц")
    p_show.add_argument("label", help='номер пробы, напр. "155 pc"')
    p_show.add_argument(
        "--width", type=int, default=1000, help="обрезать текст в ячейке до стольких символов"
    )
    p_show.set_defaults(func=cmd_show)

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
    p_ident.add_argument("--by", help="кто определил (имя; новое — в справочник людей)")
    p_ident.add_argument("--date", help="дата определения ГГГГ-ММ-ДД (по умолчанию сегодня)")
    p_ident.add_argument("--notes", help="примечание, напр. номер в GenBank")
    p_ident.set_defaults(func=cmd_identify)

    p_resolve = sub.add_parser("resolve-issue", help="отметить проблему из журнала решённой")
    p_resolve.add_argument("issue_id", type=int, help="номер проблемы (report issues)")
    p_resolve.add_argument("resolution", help="как решили, в кавычках")
    p_resolve.set_defaults(func=cmd_resolve_issue)

    rules = ", ".join(
        f"{kind} — {rule.keep_last} последних"
        + (f" и все за {rule.keep_days} дней" if rule.keep_days else "")
        for kind, rule in RETENTION.items()
    )
    p_backup = sub.add_parser(
        "backup",
        help="резервная копия базы и очистка старых копий",
        description=f"Копия в <папка базы>/backups/<вид>/. Хранятся: {rules}. "
        "К ежедневной копии раз в 7 дней добавляется еженедельная.",
    )
    p_backup.add_argument(
        "--kind", choices=BACKUP_KINDS, default=DAILY, help="вид копии (по умолчанию daily)"
    )
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser(
        "restore",
        help="заменить базу резервной копией (вместо cp; спросит подтверждение)",
        description="Проверяет копию (целая база биобанка), сохраняет текущую базу как копию "
        "pre-op и заменяет файл так, чтобы рядом не остался журнал -wal старой базы. "
        "Сайт и другие программы, работающие с базой, перед этим нужно остановить.",
    )
    p_restore.add_argument("backup", type=Path, help="файл копии, напр. output/backups/daily/…")
    p_restore.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    p_restore.set_defaults(func=cmd_restore)

    p_live = sub.add_parser(
        "go-live", help="отметить базу как рабочую: import и init на ней будут запрещены"
    )
    p_live.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    p_live.set_defaults(func=cmd_go_live)

    p_unlive = sub.add_parser(
        "undo-go-live", help="снять пометку «рабочая база» (только при аварии; спросит)"
    )
    p_unlive.set_defaults(func=cmd_undo_go_live)

    p_web = sub.add_parser("web", help="веб-интерфейс на этом компьютере (127.0.0.1)")
    p_web.add_argument("--port", type=int, default=5000, help="порт (по умолчанию 5000)")
    p_web.add_argument(
        "--secure-cookies",
        action="store_true",
        help="cookie входа только по HTTPS (за прокси с HTTPS; то же — BIOBANK_SECURE_COOKIES=1)",
    )
    p_web.set_defaults(func=cmd_web)

    _add_entry_parsers(sub)
    _add_user_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
