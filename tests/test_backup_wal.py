"""Этап D1: режим WAL, резервные копии по правилам хранения, пометка «рабочая база».

Все базы — во временной папке (tmp_path); output/ тесты не трогают.
Время копий задаётся параметром now — тесты не ждут (без sleep).
"""

import datetime as dt
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest
from conftest import SOURCE, count

from biobank import db as dbmod
from biobank import edit
from biobank.__main__ import main
from biobank.db import (
    BUSY_TIMEOUT_MS,
    LIVE_APPLICATION_ID,
    BackupError,
    DatabaseBusyError,
    LiveDatabaseError,
    backup_database,
    connect,
    connect_read_only,
    create_database,
    is_live,
    prepare_restore,
    prune_backups,
    restore_database,
    run_backup,
    set_live,
    wal_files,
)
from biobank.importer import import_workbook, prepare_import

NOW = dt.datetime(2026, 10, 19, 3, 0, 0)


@pytest.fixture
def path(db_path, tmp_path):
    """Копия импортированной базы: tmp_path/output/biobank.db."""
    p = tmp_path / "output" / "biobank.db"
    p.parent.mkdir()
    shutil.copy(db_path, p)
    return p


@pytest.fixture
def empty(tmp_path):
    """Пустая база по схеме — быстрее импортированной, для тестов копий."""
    return create_database(tmp_path / "base" / "biobank.db")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def journal_mode(p):
    conn = connect_read_only(p)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def names(folder):
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def touch_backups(folder, db_name, kind, ages_days):
    """Подставные копии базы db_name с временем «NOW минус N дней» (содержимое не важно)."""
    folder.mkdir(parents=True, exist_ok=True)
    for age in ages_days:
        stamp = (NOW - dt.timedelta(days=age)).strftime("%Y%m%d_%H%M%S")
        (folder / f"{db_name}_{kind}_{stamp}.db").write_text("копия")


# ---------------------------------------------------------------------------
# 1. Режим WAL
# ---------------------------------------------------------------------------


def test_new_database_is_wal_and_leaves_no_side_files(tmp_path):
    p = create_database(tmp_path / "biobank.db")
    # init закрыл единственное пишущее подключение — -wal и -shm удалены
    assert names(tmp_path) == ["biobank.db"]
    assert journal_mode(p) == "wal"
    assert wal_files(p)  # чтение оставило служебные файлы…
    conn = connect(p)
    conn.execute("SELECT count(*) FROM specimen").fetchall()
    conn.close()
    assert names(tmp_path) == ["biobank.db"]  # …а пишущее подключение, закрывшись, убрало


def test_imported_database_is_wal(db_path):
    assert journal_mode(db_path) == "wal"


def test_busy_timeout_in_connections(empty):
    for open_ in (connect, connect_read_only):
        conn = open_(empty)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        finally:
            conn.close()


def test_read_only_works_without_writers_and_wal_files(path):
    """Пишущих нет, -wal и -shm нет: чтение работает, SQLite сам создаёт -shm."""
    assert wal_files(path) == []
    before = sha(path)
    conn = connect_read_only(path)
    try:
        assert count(conn, "SELECT count(*) FROM specimen") == 462
        assert count(conn, "PRAGMA journal_mode") == "wal"  # чтение настройки разрешено
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("PRAGMA journal_mode = DELETE")  # изменение — нет
    finally:
        conn.close()
    assert sha(path) == before  # основной файл не менялся
    # подключение только на чтение не может убрать служебные файлы — это нормально
    assert {p.name for p in wal_files(path)} <= {"biobank.db-wal", "biobank.db-shm"}


def test_reader_not_blocked_by_writer_transaction(path, monkeypatch):
    """Один держит транзакцию записи — другой читает сразу (без ожидания) старые данные."""
    writer = connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE specimen SET tl_cm = 1 WHERE label = '160'")
    monkeypatch.setattr(dbmod, "BUSY_TIMEOUT_MS", 0)  # читатель не ждёт: занято — ошибка сразу
    reader = connect_read_only(path)
    try:
        assert count(reader, "SELECT tl_cm FROM specimen WHERE label = '160'") == 3.1
        writer.commit()
        assert count(reader, "SELECT tl_cm FROM specimen WHERE label = '160'") == 1
    finally:
        reader.close()
        writer.close()


def test_writer_not_blocked_by_open_reader(path):
    """Читатель посреди долгого SELECT не мешает записи (в старом режиме запись ждала бы)."""
    reader = connect_read_only(path)
    cursor = reader.execute("SELECT specimen_id FROM specimen")
    cursor.fetchone()  # SELECT не дочитан — у читателя открыт снимок базы
    writer = connect(path)
    writer.execute("PRAGMA busy_timeout = 0")  # не ждать: если заблокировано — ошибка сразу
    try:
        with writer:
            writer.execute("UPDATE specimen SET tl_cm = 1 WHERE label = '160'")
        assert len(cursor.fetchall()) == 461  # читатель дочитал свой снимок
        assert count(reader, "SELECT tl_cm FROM specimen WHERE label = '160'") == 1
    finally:
        writer.close()
        reader.close()


def test_backup_includes_changes_still_in_wal(path, tmp_path):
    """Изменение сохранено, но ещё лежит в -wal (пишущий не закрыт): копия его содержит."""
    writer = connect(path)
    try:
        with writer:
            writer.execute("UPDATE specimen SET tl_cm = 1 WHERE label = '160'")
        assert path.with_name("biobank.db-wal").stat().st_size > 0  # не перенесено
        backup = backup_database(path, tmp_path / "backups", now=NOW)
    finally:
        writer.close()
    assert wal_files(backup) == []  # копия — один файл
    assert journal_mode(backup) == "wal"
    conn = connect_read_only(backup)
    try:
        assert count(conn, "SELECT tl_cm FROM specimen WHERE label = '160'") == 1
    finally:
        conn.close()


def test_backup_during_open_write_transaction(path, tmp_path):
    """Копия во время чужой транзакции записи не ждёт её и берёт сохранённое состояние."""
    writer = connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE specimen SET tl_cm = 1 WHERE label = '160'")
    try:
        backup = backup_database(path, tmp_path / "backups", now=NOW)
    finally:
        writer.rollback()
        writer.close()
    conn = connect_read_only(backup)
    try:
        assert count(conn, "SELECT tl_cm FROM specimen WHERE label = '160'") == 3.1
    finally:
        conn.close()


def test_import_refuses_to_replace_db_open_elsewhere(path):
    """Базу держит открытой другая программа (сайт): заменять файл нельзя — ошибка."""
    other = connect_read_only(path)
    other.execute("SELECT 1 FROM specimen LIMIT 1").fetchall()  # подключено, но без транзакции
    before = sha(path)
    try:
        with prepare_import(SOURCE, path) as prepared:
            with pytest.raises(DatabaseBusyError, match="открыта другой программой"):
                prepared.replace_database()
    finally:
        other.close()
    assert sha(path) == before
    assert not list(path.parent.glob("*.tmp*"))  # временный файл и его -wal/-shm убраны


def test_import_replaces_db_with_wal_leftovers(path):
    """После чтения рядом лежат -wal/-shm (никто не подключён): импорт их убирает."""
    conn = connect_read_only(path)
    conn.execute("SELECT 1").fetchone()
    conn.close()
    assert wal_files(path)  # остались от чтения
    import_workbook(SOURCE, path)
    assert wal_files(path) == []  # журнал старой базы не остался рядом с новой
    assert not list(path.parent.glob("*.tmp*"))
    conn = connect_read_only(path)
    try:
        assert count(conn, "SELECT count(*) FROM specimen") == 462
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_init_over_db_with_wal_leftovers(empty):
    conn = connect_read_only(empty)
    conn.execute("SELECT 1").fetchone()
    conn.close()
    create_database(empty, overwrite=True)
    assert wal_files(empty) == []
    assert journal_mode(empty) == "wal"


# ---------------------------------------------------------------------------
# 2. Резервные копии: виды, имена, правила хранения
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["daily", "weekly", "pre-op"])
def test_backup_name_and_folder_show_kind(empty, kind):
    target = backup_database(empty, kind=kind, now=NOW)
    assert target == empty.parent / "backups" / kind / f"biobank.db_{kind}_20261019_030000.db"
    conn = connect_read_only(target)
    try:
        assert len(dbmod.list_tables(conn)) == 18
    finally:
        conn.close()


def test_default_kind_is_pre_op(empty):
    assert backup_database(empty).parent.name == "pre-op"


def test_unknown_kind_refused(empty):
    with pytest.raises(ValueError, match="Неизвестный вид копии"):
        backup_database(empty, kind="monthly")
    assert not (empty.parent / "backups").exists()


def test_prune_daily_keeps_7_latest(empty):
    folder = empty.parent / "backups" / "daily"
    touch_backups(folder, "biobank.db", "daily", range(10))
    deleted = prune_backups(empty, "daily", now=NOW)
    assert len(deleted) == 3
    assert len(names(folder)) == 7
    oldest_kept = min(names(folder))
    assert oldest_kept == "biobank.db_daily_20261013_030000.db"  # 6 дней назад


def test_prune_weekly_keeps_4_latest(empty):
    folder = empty.parent / "backups" / "weekly"
    touch_backups(folder, "biobank.db", "weekly", [0, 7, 14, 21, 28, 35])
    assert len(prune_backups(empty, "weekly", now=NOW)) == 2
    assert len(names(folder)) == 4


@pytest.mark.parametrize(
    "ages, left",
    [
        (range(25), 25),  # все моложе 30 дней — все остаются
        (range(40, 65), 20),  # все старые — 20 последних
        ([*range(5), *range(40, 70)], 20),  # 5 свежих + 15 новейших старых
        ([*range(25), *range(40, 50)], 25),  # 25 свежих, старые не нужны
        ([*range(20), 30, 31], 21),  # ровно 30 дней — ещё хранится, 31 — уже нет
    ],
)
def test_prune_pre_op_30_days_but_at_least_20(empty, ages, left):
    folder = empty.parent / "backups" / "pre-op"
    touch_backups(folder, "biobank.db", "pre-op", ages)
    prune_backups(empty, "pre-op", now=NOW)
    assert len(names(folder)) == left


def test_prune_touches_only_own_backups(empty):
    """Ни база, ни чужие файлы, ни копии другого вида и старого формата не удаляются."""
    root = empty.parent / "backups"
    folder = root / "daily"
    touch_backups(folder, "biobank.db", "daily", range(10, 30))
    foreign = [
        root / "biobank_20261014_130534.db",  # старый формат — лежит в backups/
        folder / "biobank_20261014_130534.db",  # старый формат в папке вида
        folder / "biobank.db_pre-op_20200101_000000.db",  # другой вид
        folder / "other.db_daily_20200101_000000.db",  # другая база
        folder / "biobank.sqlite_daily_20200101_000000.db",  # база с тем же stem
        folder / "biobank_daily_20200101_000000.db",  # без расширения базы в имени
        folder / "biobank.db_daily_20200101_000000.db.bak",  # другое расширение
        folder / "biobank.db_daily_20201399_000000.db",  # не дата
        folder / "заметки.txt",
    ]
    for p in foreign:
        p.write_text("чужое")
    (folder / "biobank.db_daily_20200101_000000_9.db").mkdir()  # папка с именем копии
    db_before = sha(empty)
    prune_backups(empty, "daily", now=NOW)
    assert all(p.exists() for p in foreign)
    assert (folder / "biobank.db_daily_20200101_000000_9.db").is_dir()
    assert sha(empty) == db_before
    assert len([n for n in names(folder) if n.startswith("biobank.db_daily_2026")]) == 7


def test_prune_never_deletes_the_database_itself(empty):
    """Ссылка на саму базу с именем копии в папке копий — не удаляется, база цела."""
    folder = empty.parent / "backups" / "daily"
    touch_backups(folder, "biobank.db", "daily", range(10))
    link = folder / "biobank.db_daily_20200101_000000.db"
    link.symlink_to(empty)
    prune_backups(empty, "daily", now=NOW)
    assert link.is_symlink() and empty.exists()


def test_prune_missing_folder(empty):
    assert prune_backups(empty, "weekly", now=NOW) == []
    assert not (empty.parent / "backups").exists()


def test_run_backup_makes_weekly_once_a_week(empty):
    """Ежедневный запуск 15 дней подряд: 7 ежедневных, еженедельные — в дни 0, 7, 14."""
    for day in range(15):
        report = run_backup(empty, now=NOW + dt.timedelta(days=day, seconds=day))
        assert len(report.created) == (2 if day % 7 == 0 else 1)
    root = empty.parent / "backups"
    assert len(names(root / "daily")) == 7
    assert names(root / "weekly") == [
        "biobank.db_weekly_20261019_030000.db",
        "biobank.db_weekly_20261026_030007.db",
        "biobank.db_weekly_20261102_030014.db",
    ]


def test_run_backup_weekly_not_late_by_seconds(empty):
    """Таймер сработал на секунду «раньше», чем неделю назад, — еженедельная всё равно сегодня."""
    run_backup(empty, now=NOW)
    report = run_backup(empty, now=NOW + dt.timedelta(days=7, seconds=-1))
    assert [p.parent.name for p in report.created] == ["daily", "weekly"]


def test_run_backup_explicit_kinds(empty):
    assert [p.parent.name for p in run_backup(empty, "weekly", now=NOW).created] == ["weekly"]
    assert [p.parent.name for p in run_backup(empty, "pre-op", now=NOW).created] == ["pre-op"]
    assert not (empty.parent / "backups" / "daily").exists()


def test_run_backup_prunes_and_reports(empty):
    touch_backups(empty.parent / "backups" / "daily", "biobank.db", "daily", range(1, 10))
    report = run_backup(empty, now=NOW)
    assert len(report.deleted) == 3  # 9 старых + 1 новая = 10 → 7
    assert all(not p.exists() for p in report.deleted)


def test_cli_backup(empty, capsys):
    assert main(["--db", str(empty), "backup"]) == 0
    out = capsys.readouterr().out
    assert "backups/daily/biobank.db_daily_" in out and "backups/weekly/" in out
    assert "Удалено старых копий по правилу хранения: 0" in out
    assert main(["--db", str(empty), "backup", "--kind", "pre-op"]) == 0
    assert "backups/pre-op/biobank.db_pre-op_" in capsys.readouterr().out


def test_cli_backup_missing_db(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "nope.db"), "backup"]) == 1
    assert names(tmp_path) == []


def test_apply_edit_backup_is_pre_op_and_pruned(path):
    """Копия перед правкой — вида pre-op; старые pre-op подчищаются, старый формат — нет."""
    folder = path.parent / "backups" / "pre-op"
    touch_backups(folder, "biobank.db", "pre-op", range(40, 70))
    old_format = path.parent / "backups" / "biobank_20261014_130534.db"
    old_format.write_text("старая копия")
    result = edit.apply_edit(path, lambda c: edit.link_trawl(c, "5 pc", 3))
    assert result.backup.parent == folder
    assert result.backup.name.startswith("biobank.db_pre-op_")
    assert len(names(folder)) == 20  # новая + 19 новейших старых
    assert result.backup.exists()
    assert old_format.exists()


# ---------------------------------------------------------------------------
# 3. Пометка «рабочая база» (go-live)
# ---------------------------------------------------------------------------


def test_set_live_and_is_live(empty):
    assert not is_live(empty)
    assert set_live(empty, True) is True
    assert is_live(empty)
    assert set_live(empty, True) is False  # уже рабочая
    conn = connect_read_only(empty)
    try:
        assert count(conn, "PRAGMA application_id") == LIVE_APPLICATION_ID
        assert count(conn, "PRAGMA user_version") == 0  # user_version — для миграций (D3)
    finally:
        conn.close()
    assert set_live(empty, False) is True
    assert not is_live(empty)


def test_is_live_on_missing_and_non_database(tmp_path):
    assert not is_live(tmp_path / "nope.db")
    bad = tmp_path / "bad.db"
    bad.write_text("это не база")
    assert not is_live(bad)
    assert names(tmp_path) == ["bad.db"]


def test_set_live_refuses_foreign_files(tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    with pytest.raises(ValueError, match="не база биобанка"):
        set_live(other, True)
    with pytest.raises(FileNotFoundError):
        set_live(tmp_path / "nope.db", True)


def test_set_live_refuses_other_application_id(empty):
    conn = connect(empty)
    conn.execute("PRAGMA application_id = 12345")
    conn.close()
    with pytest.raises(ValueError, match="чужой application_id = 12345"):
        set_live(empty, True)
    assert not is_live(empty)


def test_live_mark_travels_with_backup_and_restore(empty, tmp_path):
    set_live(empty, True)
    backup = backup_database(empty, now=NOW)
    assert is_live(backup)
    restored = tmp_path / "restored" / "biobank.db"
    restored.parent.mkdir()
    shutil.copy(backup, restored)  # восстановление из копии — копирование файла
    assert is_live(restored)


def test_import_refused_on_live_db(path, monkeypatch):
    set_live(path, True)
    before = sha(path)

    def must_not_read(*args, **kwargs):
        raise AssertionError("книгу читать не нужно — отказ сразу")

    monkeypatch.setattr("biobank.importer.read_workbook", must_not_read)
    with pytest.raises(LiveDatabaseError, match="отмечена как рабочая"):
        prepare_import(SOURCE, path)
    with pytest.raises(LiveDatabaseError):
        import_workbook(SOURCE, path)
    assert sha(path) == before
    assert not list(path.parent.glob("*.tmp*"))


def test_replace_rechecks_live_mark(path):
    """Пометку поставили между сборкой новой базы и заменой — замены не будет."""
    with prepare_import(SOURCE, path) as prepared:
        set_live(path, True)
        with pytest.raises(LiveDatabaseError):
            prepared.replace_database()
    assert is_live(path)


def test_cli_import_on_live_db_refused_even_with_force(path, capsys):
    set_live(path, True)
    before = sha(path)
    assert main(["--db", str(path), "import", str(SOURCE), "--force", "-y"]) == 1
    err = capsys.readouterr().err
    assert "даже с --force" in err
    assert "undo-go-live" in err  # подсказка, как снять пометку
    assert sha(path) == before
    assert not (path.parent / "backups").exists()  # и копию делать незачем


def test_create_database_refuses_live_db(empty):
    set_live(empty, True)
    with pytest.raises(LiveDatabaseError):
        create_database(empty, overwrite=True)
    assert is_live(empty)


def test_cli_init_refuses_live_db(empty, monkeypatch, capsys):
    set_live(empty, True)
    monkeypatch.setattr("builtins.input", lambda _="": pytest.fail("спрашивать незачем"))
    assert main(["--db", str(empty), "init", "-y"]) == 1
    err = capsys.readouterr().err
    assert "отмечена как рабочая" in err and "undo-go-live" in err
    assert is_live(empty)


def test_cli_go_live_asks_and_marks(empty, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    assert main(["--db", str(empty), "go-live"]) == 1
    assert not is_live(empty)
    monkeypatch.setattr("builtins.input", lambda _="": "да")
    assert main(["--db", str(empty), "go-live"]) == 0
    assert is_live(empty)
    assert main(["--db", str(empty), "go-live", "-y"]) == 0  # повторно — не ошибка
    assert "уже отмечена" in capsys.readouterr().out


def test_cli_undo_go_live_always_asks(empty, monkeypatch, capsys):
    set_live(empty, True)
    questions = []
    monkeypatch.setattr("builtins.input", lambda q="": questions.append(q) or "n")
    assert main(["--db", str(empty), "undo-go-live"]) == 1
    assert is_live(empty)
    monkeypatch.setattr("builtins.input", lambda q="": questions.append(q) or "y")
    assert main(["--db", str(empty), "undo-go-live"]) == 0
    assert not is_live(empty)
    assert len(questions) == 2
    with pytest.raises(SystemExit):  # флага «без вопроса» нет
        main(["--db", str(empty), "undo-go-live", "-y"])


def test_cli_go_live_missing_db(tmp_path):
    assert main(["--db", str(tmp_path / "nope.db"), "go-live", "-y"]) == 1
    assert names(tmp_path) == []


def test_edits_and_checks_work_on_live_db(path):
    set_live(path, True)
    result = edit.apply_edit(path, lambda c: edit.link_trawl(c, "5 pc", 3))
    assert "5 pc" in result.message
    edit.check_database(path)
    assert is_live(path)  # правка пометку не снимает


def test_web_works_on_live_db(path):
    pytest.importorskip("flask")
    from biobank.web.app import create_app

    set_live(path, True)
    client = create_app(path).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/specimen/5 pc").status_code == 200


# ---------------------------------------------------------------------------
# 4. Найдено на ревью D1
# ---------------------------------------------------------------------------


def region_names(p):
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return sorted(row[0] for row in conn.execute("SELECT name FROM region"))
    finally:
        conn.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="у root права доступа к файлам не действуют")
def test_backup_fails_loudly_when_wal_unreadable(empty):
    """t2: нет доступа к -shm (а изменения ещё в -wal) — ошибка и код ≠ 0, а не неполная
    копия основного файла. Копию делает другой процесс: в этом -shm уже открыт."""
    writer = connect(empty)
    try:
        with writer:
            writer.execute("INSERT INTO region(name) VALUES ('ТОЛЬКО_В_WAL')")
        shm = empty.with_name("biobank.db-shm")
        shm.chmod(0)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "biobank", "--db", str(empty), "backup"],
                capture_output=True,
                text=True,
            )
        finally:
            shm.chmod(0o644)
    finally:
        writer.close()
    assert result.returncode == 1
    assert "не сделана" in result.stderr
    for kind in ("daily", "weekly"):
        assert names(empty.parent / "backups" / kind) == []  # недоделанные копии удалены


def test_restore_after_site_read_gives_backup_data(tmp_path):
    """t6: после копии — ошибочная правка; последним закрылся читатель (сайт), и рядом
    остался непустой -wal. restore даёт ровно данные копии (cp вернул бы правку)."""
    p = create_database(tmp_path / "biobank.db")
    with connect(p) as w:
        w.executemany("INSERT INTO region(name) VALUES (?)", [("до 1",), ("до 2",)])
    w.close()
    backup = backup_database(p, kind="daily", now=NOW)
    reader = connect_read_only(p)
    reader.execute("SELECT count(*) FROM region").fetchone()
    w = connect(p)
    with w:
        w.execute("UPDATE region SET name = 'ОШИБКА' WHERE region_id = 1")
        w.execute("INSERT INTO region(name) VALUES ('ПОСЛЕ')")
    w.close()
    reader.close()  # сайт остановлен; читатель закрылся последним
    assert p.with_name("biobank.db-wal").stat().st_size > 0

    result = restore_database(p, backup)
    assert region_names(p) == ["до 1", "до 2"]
    assert wal_files(p) == []
    conn = connect(p)
    try:
        assert count(conn, "PRAGMA integrity_check") == "ok"
        assert count(conn, "PRAGMA journal_mode") == "wal"
    finally:
        conn.close()
    # состояние до восстановления (с ошибочной правкой) сохранено как pre-op
    assert result.backup.parent.name == "pre-op"
    assert region_names(result.backup) == ["ОШИБКА", "ПОСЛЕ", "до 2"]
    assert names(backup.parent) == [backup.name]  # у копии не появилось -wal/-shm
    assert not list(tmp_path.glob("*.restore*"))


def test_restore_refuses_db_open_elsewhere(path, tmp_path):
    backup = backup_database(path, tmp_path / "copies", kind="daily", now=NOW)
    other = connect_read_only(path)
    other.execute("SELECT 1 FROM specimen LIMIT 1").fetchall()
    before = sha(path)
    try:
        with pytest.raises(DatabaseBusyError):
            restore_database(path, backup)
    finally:
        other.close()
    assert sha(path) == before
    assert not list(path.parent.glob("*.restore*"))
    assert names(path.parent / "backups" / "pre-op") == []  # замены не было — копия удалена


def test_restore_into_missing_db(empty, tmp_path):
    backup = backup_database(empty, kind="daily", now=NOW)
    target = tmp_path / "new" / "biobank.db"
    result = restore_database(target, backup)
    assert result.backup is None and not result.live
    assert journal_mode(target) == "wal"


def test_restore_carries_live_mark(empty, tmp_path):
    set_live(empty, True)
    backup = backup_database(empty, kind="daily", now=NOW)
    set_live(empty, False)
    assert restore_database(empty, backup).live
    assert is_live(empty)


@pytest.mark.parametrize("problem", ["not_db", "not_biobank", "same_file"])
def test_restore_refuses_bad_source(empty, tmp_path, problem):
    source = tmp_path / "copy.db"
    if problem == "not_db":
        source.write_text("это не база")
    elif problem == "not_biobank":
        conn = sqlite3.connect(source)
        conn.execute("CREATE TABLE t (x)")
        conn.close()
    else:
        source = empty
    before = sha(empty)
    with pytest.raises(ValueError):
        restore_database(empty, source)
    assert sha(empty) == before
    assert not (empty.parent / "backups").exists()


def test_restore_refuses_when_current_db_cannot_be_saved(empty, tmp_path):
    backup = backup_database(empty, tmp_path / "copies", kind="daily", now=NOW)
    broken = tmp_path / "broken" / "biobank.db"
    broken.parent.mkdir()
    broken.write_text("испорченная база")
    with pytest.raises(BackupError, match="перенесите её файл"):
        restore_database(broken, backup)
    assert broken.read_text() == "испорченная база"
    assert not list(broken.parent.glob("*.restore*"))


def test_cli_restore(empty, monkeypatch, capsys):
    set_live(empty, True)
    backup = backup_database(empty, kind="daily", now=NOW)
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    assert main(["--db", str(empty), "restore", str(backup)]) == 1
    assert "остановите сайт" in capsys.readouterr().out
    assert main(["--db", str(empty), "restore", str(backup), "-y"]) == 0
    out = capsys.readouterr().out
    assert "восстановлена из копии" in out
    assert "Восстановленная база — рабочая" in out
    assert "backups/pre-op/biobank.db_pre-op_" in out


def test_cli_restore_bad_source(empty, tmp_path, capsys):
    assert main(["--db", str(empty), "restore", str(tmp_path / "nope.db"), "-y"]) == 1
    assert "База не изменена" in capsys.readouterr().err


def test_clock_was_ahead_then_fixed(empty):
    """t3: 8 дней часы спешили на год, потом их исправили. Новые копии не удаляются,
    еженедельные продолжаются, о копиях «из будущего» — предупреждение."""
    ahead = NOW.replace(year=2027)
    for day in range(8):
        run_backup(empty, now=ahead + dt.timedelta(days=day))
    for day in range(8, 20):
        report = run_backup(empty, now=NOW + dt.timedelta(days=day))
        assert all(p.exists() for p in report.created)  # новые копии не удаляются
        assert all("_2026" in p.name for p in report.deleted)  # будущие — тоже
        assert len(report.future) == 7 + 2  # 7 ежедневных и 2 еженедельных из 2027 года
    daily = names(empty.parent / "backups" / "daily")
    assert len([n for n in daily if "_2026" in n]) == 7  # правило работает для настоящих
    assert len([n for n in daily if "_2027" in n]) == 7  # будущие не удалены
    weekly_2026 = [n for n in names(empty.parent / "backups" / "weekly") if "_2026" in n]
    assert weekly_2026 == [
        "biobank.db_weekly_20261027_030000.db",  # первый день с верными часами
        "biobank.db_weekly_20261103_030000.db",
    ]


def test_clock_jumps_ahead_and_back(empty):
    """Часы прыгнули на год вперёд: старые копии «состарились», но 7 последних остаются;
    вернулись назад — копии, сделанные с неверными часами, не мешают новым."""
    for day in range(7):
        run_backup(empty, now=NOW + dt.timedelta(days=day))
    jump = run_backup(empty, now=NOW + dt.timedelta(days=372))
    assert len(jump.deleted) == 1  # 7 старых + 1 новая → 7: удалена самая старая
    assert [p.parent.name for p in jump.created] == ["daily", "weekly"]
    back = run_backup(empty, now=NOW + dt.timedelta(days=7))
    assert [p.parent.name for p in back.created] == ["daily", "weekly"]  # неделя прошла
    assert all(p.exists() for p in back.created)
    assert back.deleted == []  # 6 настоящих + новая = 7; будущие не в счёт
    assert [p.name for p in back.future] == [
        "biobank.db_daily_20271026_030000.db",
        "biobank.db_weekly_20271026_030000.db",
    ]


def test_cli_backup_warns_about_future_copies(empty, capsys):
    touch_backups(empty.parent / "backups" / "daily", "biobank.db", "daily", [-400])
    assert main(["--db", str(empty), "backup"]) == 2  # копии сделаны, но таймер заметит
    err = capsys.readouterr().err
    assert "копии из будущего" in err and "проверьте часы" in err


def test_two_databases_in_one_folder_keep_own_backups(tmp_path):
    """t5: biobank.db и biobank.sqlite рядом — очистка одной не трогает копии другой."""
    a = create_database(tmp_path / "biobank.db")
    b = create_database(tmp_path / "biobank.sqlite")
    for day in range(7):
        backup_database(b, kind="daily", now=NOW + dt.timedelta(days=day, hours=1))
    only_a = backup_database(a, kind="daily", now=NOW)
    assert prune_backups(a, "daily", now=NOW + dt.timedelta(days=7)) == []
    assert only_a.exists()
    assert len(names(tmp_path / "backups" / "daily")) == 8


@pytest.mark.parametrize("command", ["go-live", "undo-go-live"])
def test_go_live_on_busy_db_has_clear_message(empty, monkeypatch, capsys, command):
    def busy(_):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("biobank.__main__.is_live", busy)
    assert main(["--db", str(empty), command]) == 1
    err = capsys.readouterr().err
    assert "не удалось прочитать базу" in err and "Traceback" not in err


# ---------------------------------------------------------------------------
# 5. Повторное ревью D1
# ---------------------------------------------------------------------------


def test_restore_from_file_being_written_takes_committed_snapshot(empty, tmp_path):
    """Источник пишут (как ~/biobank_test.db под сайтом): restore берёт цельный снимок
    сохранённого — с изменениями из его -wal, без незавершённой транзакции; проверяется
    именно перенесённый файл. Служебные файлы источника (они уже были) не трогаются."""
    source = create_database(tmp_path / "test" / "biobank_test.db")
    writer = connect(source)
    with writer:
        writer.execute("INSERT INTO region(name) VALUES ('сохранено')")  # лежит в -wal
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO region(name) VALUES ('не сохранено')")
    try:
        prepared = restore_database(empty, source)
        assert wal_files(source)  # были до restore — остались
    finally:
        writer.rollback()
        writer.close()
    assert region_names(empty) == ["сохранено"]
    assert not prepared.new_path.exists()


def test_restore_leaves_no_side_files_next_to_backup(empty):
    """У копии не было -wal/-shm — после restore их тоже нет, файл копии не изменился."""
    backup = backup_database(empty, kind="daily", now=NOW)
    before = sha(backup)
    restore_database(empty, backup)
    assert names(backup.parent) == [backup.name]
    assert sha(backup) == before


def test_prepare_restore_checks_and_discards(empty, tmp_path):
    backup = backup_database(empty, kind="daily", now=NOW)
    before = sha(empty)
    with prepare_restore(empty, backup) as prepared:
        assert prepared.new_path.exists() and prepared.exists
        assert not prepared.current_live and not prepared.source_live
    assert not prepared.new_path.exists()
    assert sha(empty) == before


def test_restore_rereads_live_mark_before_replace(empty):
    """go-live выполнили, пока restore ждал ответа «да/нет»: пометка не теряется."""
    backup = backup_database(empty, kind="daily", now=NOW)
    with prepare_restore(empty, backup) as prepared:
        assert not prepared.current_live
        set_live(empty, True)
        prepared.replace_database()
    assert is_live(empty)


def test_restore_keeps_live_mark_of_current_db(empty, monkeypatch, capsys):
    """Копия сделана до go-live, текущая база рабочая: restore -y пометку не снимает."""
    backup = backup_database(empty, kind="daily", now=NOW)
    set_live(empty, True)
    assert main(["--db", str(empty), "restore", str(backup), "-y"]) == 0
    out = capsys.readouterr().out
    assert "Пометка «рабочая база» сохранена" in out
    assert is_live(empty)
    assert not is_live(backup)  # файл копии не менялся


def test_restore_api_keeps_live_mark(empty):
    backup = backup_database(empty, kind="daily", now=NOW)
    set_live(empty, True)
    result = restore_database(empty, backup)
    assert result.live and is_live(empty)


def test_slightly_future_copy_is_not_future(empty):
    """Копия на 23 часа «впереди» (летнее время, небольшой сбой) — обычная копия."""
    folder = empty.parent / "backups" / "daily"
    folder.mkdir(parents=True)
    stamp = (NOW + dt.timedelta(hours=23)).strftime("%Y%m%d_%H%M%S")
    (folder / f"biobank.db_daily_{stamp}.db").write_text("копия")
    assert run_backup(empty, now=NOW).future == []


def touch_future(folder, count_):
    """count_ копий «из будущего» (через год) с разным временем файла (st_mtime)."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count_):
        stamp = (NOW + dt.timedelta(days=365 + i)).strftime("%Y%m%d_%H%M%S")
        path = folder / f"biobank.db_{folder.name}_{stamp}.db"
        path.write_text("копия")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
        paths.append(path)
    return paths


def test_future_copies_limited_when_enough_real_ones(empty):
    folder = empty.parent / "backups" / "daily"
    future = touch_future(folder, 10)
    touch_backups(folder, "biobank.db", "daily", range(1, 8))  # 7 настоящих
    deleted = prune_backups(empty, "daily", now=NOW)
    assert sorted(deleted) == future[:3]  # самые старые по времени файла
    assert all(p.exists() for p in future[3:])


def test_future_copies_kept_when_few_real_ones(empty):
    folder = empty.parent / "backups" / "daily"
    future = touch_future(folder, 10)
    touch_backups(folder, "biobank.db", "daily", range(1, 7))  # 6 настоящих
    assert prune_backups(empty, "daily", now=NOW) == []
    assert all(p.exists() for p in future)


def test_pre_op_future_copies_do_not_grow_and_edit_warns(path, capsys):
    """Долгий сбой часов: pre-op перед каждой правкой не копятся без конца; правка
    предупреждает (CLI — в stderr, функция — в notices)."""
    folder = path.parent / "backups" / "pre-op"
    touch_future(folder, 25)
    touch_backups(folder, "biobank.db", "pre-op", range(1, 21))  # 20 настоящих
    assert main(["--db", str(path), "link-trawl", "5 pc", "3"]) == 0
    assert "копии из будущего" in capsys.readouterr().err
    future = [n for n in names(folder) if "_2027" in n]
    assert len(future) == 20  # не больше keep_last
    result = edit.apply_edit(path, lambda c: edit.resolve_issue(c, 1, "сверено"))
    assert any("проверьте часы" in n for n in result.notices)


def test_edit_without_future_copies_has_no_notices(path):
    result = edit.apply_edit(path, lambda c: edit.link_trawl(c, "5 pc", 3))
    assert result.notices == []
