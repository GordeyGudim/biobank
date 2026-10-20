"""Тесты пользователей сайта (users.py): пароли, вход, роли, последний админ, CLI,
журнал изменений без хеша пароля; restore копии другой версии схемы.

Почти все тесты — на пустой базе по схеме (create_database во временной папке):
данные экспедиций для учётных записей не нужны.
"""

import re
import shutil
import sqlite3

import pytest
from conftest import count

from biobank import users
from biobank.__main__ import main
from biobank.db import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    backup_database,
    connect,
    create_database,
    prepare_restore,
)
from biobank.errors import EditError, NotFoundError, PermissionDenied
from biobank.permissions import ADMIN, CURATOR, READER, RESEARCHER
from biobank.users import (
    LOGIN_FAILED,
    activate,
    authenticate,
    change_own_password,
    create_user,
    deactivate,
    get_user,
    hash_password,
    list_users,
    needs_rehash,
    reset_password,
    set_password,
    set_roles,
    verify_password,
)

PASSWORD = "очень-длинный-пароль"


@pytest.fixture
def conn(tmp_path):
    c = connect(create_database(tmp_path / "users.db"))
    yield c
    c.close()


@pytest.fixture
def admin(conn):
    """Первый админ — создан командой CLI (системный режим, user=None)."""
    return create_user(conn, "admin", PASSWORD, "Админ", [ADMIN], must_change_password=False)


def log_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM change_log ORDER BY change_id")]


# ---------------------------------------------------------------------------
# Хеш пароля
# ---------------------------------------------------------------------------


def test_hash_has_parameters_and_random_salt():
    h1, h2 = hash_password(PASSWORD), hash_password(PASSWORD)
    assert h1 != h2  # разная соль
    assert re.fullmatch(r"scrypt\$32768\$8\$1\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+", h1)
    assert verify_password(PASSWORD, h1) and verify_password(PASSWORD, h2)
    assert not verify_password(PASSWORD + "x", h1)
    assert not verify_password(PASSWORD, "md5$что-то")  # чужой формат — не совпадает


def test_old_cost_hash_is_upgraded_on_login(conn, admin):
    """Хеш с прежними (дешёвыми) параметрами проверяется и пересчитывается при входе."""
    cheap = hash_password(PASSWORD, n=2**10)
    assert needs_rehash(cheap)
    conn.execute("UPDATE app_user SET password_hash = ? WHERE login = 'admin'", (cheap,))
    conn.commit()
    assert authenticate(conn, "admin", PASSWORD) is not None
    stored = count(conn, "SELECT password_hash FROM app_user WHERE login = 'admin'")
    assert not needs_rehash(stored) and verify_password(PASSWORD, stored)


# ---------------------------------------------------------------------------
# Создание пользователя
# ---------------------------------------------------------------------------


def test_create_user(conn, admin):
    user = create_user(conn, "Ivanova", PASSWORD, " Иванова  А. ", [RESEARCHER, CURATOR],
                       user=admin)  # fmt: skip
    assert (user.login, user.display_name) == ("Ivanova", "Иванова А.")
    assert user.roles == {RESEARCHER, CURATOR}
    assert user.is_active and user.must_change_password
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", user.created_at)
    assert get_user(conn, "ivanova") == user  # логин без учёта регистра
    assert get_user(conn, user.user_id) == user
    assert [u.login for u in list_users(conn)] == ["admin", "Ivanova"]


@pytest.mark.parametrize(
    "login, password, name, roles, match",
    [
        ("ADMIN", PASSWORD, "Другой", [READER], "уже занят"),
        ("иванова", PASSWORD, "Иванова", [READER], "Логин «иванова» не подходит"),
        ("x", PASSWORD, "Икс", [READER], "не подходит"),
        ("ivanova", "короткий", "Иванова", [READER], "не меньше 10 символов"),
        ("ivanova1234", "ivanova1234", "Иванова", [READER], "не должен совпадать с логином"),
        ("ivanova", PASSWORD, "  ", [READER], "имя для подписи"),
        ("ivanova", PASSWORD, "Иванова", ["бог"], "Роли «бог» нет"),
        ("ivanova", PASSWORD, "Иванова", [], "хотя бы одну роль"),
    ],
)
def test_create_user_errors(conn, admin, login, password, name, roles, match):
    with pytest.raises(EditError, match=match):
        create_user(conn, login, password, name, roles, user=admin)
    assert count(conn, "SELECT count(*) FROM app_user") == 1


def test_only_admin_manages_users(conn, admin):
    curator = create_user(conn, "cur", PASSWORD, "Куратор", [CURATOR, RESEARCHER], user=admin)
    for action in (
        lambda: create_user(conn, "new", PASSWORD, "Новый", [READER], user=curator),
        lambda: set_roles(conn, "admin", [READER], user=curator),
        lambda: deactivate(conn, "admin", user=curator),
        lambda: reset_password(conn, "admin", user=curator.user_id),
    ):
        with pytest.raises(PermissionDenied, match="только админ"):
            action()
    assert not conn.in_transaction


def test_inactive_admin_cannot_manage(conn, admin):
    other = create_user(conn, "admin2", PASSWORD, "Админ 2", [ADMIN], user=admin)
    deactivate(conn, other, user=admin)
    with pytest.raises(PermissionDenied, match="закрыт"):
        create_user(conn, "new", PASSWORD, "Новый", [READER], user=other)


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


def test_authenticate_ok_records_login(conn, admin):
    assert admin.last_login_at is None
    user = authenticate(conn, " admin ", PASSWORD)
    assert user.user_id == admin.user_id and user.last_login_at is not None
    assert log_rows(conn)[-1]["action"] == "создание"  # вход — не правка, в журнал не пишется


@pytest.mark.parametrize(
    "login, password",
    [("admin", "неверный-пароль"), ("nobody", PASSWORD), ("", ""), ("admin", None)],
    ids=["wrong_password", "no_such_login", "empty", "no_password"],
)
def test_authenticate_refuses(conn, admin, login, password):
    assert authenticate(conn, login, password) is None
    assert LOGIN_FAILED == "Неверный логин или пароль."


def test_authenticate_inactive_user(conn, admin):
    user = create_user(conn, "reader", PASSWORD, "Читатель", [READER], user=admin)
    deactivate(conn, user, user=admin)
    assert authenticate(conn, "reader", PASSWORD) is None
    activate(conn, "reader", user=admin)
    assert authenticate(conn, "reader", PASSWORD) is not None


def test_authenticate_checks_hash_even_without_login(conn, admin, monkeypatch):
    """Несуществующий логин: хеш всё равно проверяется (время ответа то же)."""
    calls = []
    real = users.verify_password

    def spy(password, stored):
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(users, "verify_password", spy)
    assert authenticate(conn, "nobody", PASSWORD) is None
    assert len(calls) == 1 and calls[0].startswith("scrypt$32768$")


# ---------------------------------------------------------------------------
# Пароли: сброс, смена своего
# ---------------------------------------------------------------------------


def test_reset_and_change_own_password(conn, admin):
    create_user(conn, "reader", PASSWORD, "Читатель", [READER], user=admin)
    temporary = reset_password(conn, "reader", user=admin)
    assert re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", temporary)
    assert authenticate(conn, "reader", PASSWORD) is None  # прежний больше не подходит
    user = authenticate(conn, "reader", temporary)
    assert user.must_change_password

    with pytest.raises(EditError, match="Прежний пароль неверный"):
        change_own_password(conn, user, "не-тот-пароль", "новый-пароль-123")
    with pytest.raises(EditError, match="совпадает с прежним"):
        change_own_password(conn, user, temporary, temporary)
    with pytest.raises(EditError, match="не меньше 10"):
        change_own_password(conn, user, temporary, "коротко")
    changed = change_own_password(conn, user, temporary, "новый-пароль-123")
    assert not changed.must_change_password
    assert authenticate(conn, "reader", "новый-пароль-123") is not None


def test_change_own_password_needs_login(conn):
    with pytest.raises(PermissionDenied):
        change_own_password(conn, None, PASSWORD, "новый-пароль-123")


def test_set_password_by_admin_requires_change(conn, admin):
    create_user(conn, "reader", PASSWORD, "Читатель", [READER], must_change_password=False,
                user=admin)  # fmt: skip
    user = set_password(conn, "reader", "задал-админ-123", user=admin)
    assert user.must_change_password


# ---------------------------------------------------------------------------
# Роли и последний админ
# ---------------------------------------------------------------------------


def test_set_roles(conn, admin):
    create_user(conn, "ivanova", PASSWORD, "Иванова", [READER], user=admin)
    user = set_roles(conn, "ivanova", [RESEARCHER, CURATOR], user=admin)
    assert user.roles == {RESEARCHER, CURATOR}
    with pytest.raises(EditError, match="Роли «бог» нет"):
        set_roles(conn, "ivanova", ["бог"], user=admin)
    with pytest.raises(NotFoundError):
        set_roles(conn, "nobody", [READER], user=admin)


def test_last_admin_keeps_role_and_login(conn, admin):
    with pytest.raises(EditError, match="последний активный админ"):
        set_roles(conn, admin, [CURATOR], user=admin)
    with pytest.raises(EditError, match="последний активный админ"):
        deactivate(conn, admin, user=admin)
    with pytest.raises(EditError, match="последний активный админ"):
        set_roles(conn, "admin", [READER])  # и в системном режиме (CLI)
    assert get_user(conn, "admin").roles == {ADMIN}

    # когда есть второй активный админ — можно
    create_user(conn, "admin2", PASSWORD, "Админ 2", [ADMIN], user=admin)
    assert set_roles(conn, admin, [CURATOR, ADMIN], user=admin).roles == {CURATOR, ADMIN}
    assert not deactivate(conn, admin, user=admin).is_active
    with pytest.raises(EditError, match="последний активный админ"):
        set_roles(conn, "admin2", [READER])


# ---------------------------------------------------------------------------
# Журнал изменений: пользователи — да, хеш пароля — никогда
# ---------------------------------------------------------------------------


def test_user_changes_logged_without_password_hash(conn, admin):
    user = create_user(conn, "ivanova", PASSWORD, "Иванова", [READER], user=admin)
    set_password(conn, "ivanova", "другой-пароль-1", user=admin)
    temporary = reset_password(conn, "ivanova", user=admin)
    change_own_password(conn, user, temporary, "свой-пароль-123")
    set_roles(conn, "ivanova", [RESEARCHER], user=admin)
    deactivate(conn, "ivanova", user=admin)

    rows = log_rows(conn)
    hashes = [r[0] for r in conn.execute("SELECT password_hash FROM app_user")]
    for row in rows:
        for value in (row["old_value"], row["new_value"]):
            text = str(value)
            assert "scrypt$" not in text and "password_hash" not in text
            assert all(h not in text for h in hashes)
    created = next(
        r for r in rows
        if r["table_name"] == "app_user" and r["action"] == "создание"
        and r["record_key"] == str(user.user_id)
    )  # fmt: skip
    assert created["user_id"] == admin.user_id and "Иванова" in created["new_value"]
    password_rows = [r for r in rows if r["column_name"] == "password_hash"]
    assert len(password_rows) == 3  # задал админ, сброс, смена своего
    assert {(r["old_value"], r["new_value"]) for r in password_rows} == {("(скрыто)", "(скрыто)")}
    own = password_rows[-1]
    assert own["user_id"] == user.user_id  # свой пароль — от своего имени
    roles = [(r["action"], r["record_key"]) for r in rows if r["table_name"] == "user_role"]
    assert ("удаление", f"user_id={user.user_id}, role=читатель") in roles
    assert ("создание", f"user_id={user.user_id}, role=исследователь") in roles


# ---------------------------------------------------------------------------
# Команды CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path):
    return create_database(tmp_path / "cli.db")


def feed_passwords(monkeypatch, *answers):
    answers = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))


def test_cli_users(db_file, monkeypatch, capsys):
    feed_passwords(monkeypatch, PASSWORD, PASSWORD)
    argv = ["--db", str(db_file), "create-user", "admin", "--role", "админ", "--role", "куратор",
            "--name", "Гордей"]  # fmt: skip
    assert main(argv) == 0
    assert "Создан пользователь admin «Гордей»: куратор, админ." in capsys.readouterr().out

    feed_passwords(monkeypatch, PASSWORD, "другой-пароль")
    argv = ["--db", str(db_file), "create-user", "x2", "--role", "читатель", "--name", "Х"]
    assert main(argv) == 1
    assert "не совпадают" in capsys.readouterr().err

    feed_passwords(monkeypatch, PASSWORD, PASSWORD)
    argv = ["--db", str(db_file), "create-user", "ivanova", "--role", "читатель",
            "--name", "Иванова", "--must-change"]  # fmt: skip
    assert main(argv) == 0
    assert "попросит сменить пароль" in capsys.readouterr().out

    assert main(["--db", str(db_file), "reset-password", "ivanova"]) == 0
    out = capsys.readouterr().out
    temporary = re.search(r"ivanova: (\S+)", out).group(1)
    conn = connect(db_file)
    assert authenticate(conn, "ivanova", temporary) is not None
    conn.close()

    assert main(["--db", str(db_file), "set-roles", "ivanova", "--role", "исследователь"]) == 0
    assert "Роли ivanova: исследователь." in capsys.readouterr().out
    assert main(["--db", str(db_file), "deactivate-user", "ivanova"]) == 0
    assert main(["--db", str(db_file), "deactivate-user", "admin"]) == 1
    assert "последний активный админ" in capsys.readouterr().err
    assert main(["--db", str(db_file), "activate-user", "ivanova"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db_file), "list-users"]) == 0
    out = capsys.readouterr().out
    assert "admin" in out and "ivanova" in out and "исследователь" in out
    assert "scrypt" not in out

    conn = connect(db_file)
    # команды CLI — системный режим: в журнале изменений без пользователя
    assert count(conn, "SELECT count(*) FROM change_log WHERE user_id IS NOT NULL") == 0
    assert count(conn, "SELECT count(*) FROM change_log") > 0
    conn.close()


def test_cli_password_not_an_argument(db_file, capsys):
    """Пароля среди аргументов create-user нет — только ввод с клавиатуры."""
    with pytest.raises(SystemExit):
        main(["--db", str(db_file), "create-user", "a1", "--role", "админ", "--name", "А",
              "--password", PASSWORD])  # fmt: skip


def test_cli_list_users_empty(db_file, capsys):
    assert main(["--db", str(db_file), "list-users"]) == 0
    assert "Пользователей нет" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Версия схемы: restore не восстанавливает копию другой версии
# ---------------------------------------------------------------------------


def test_schema_version_constant_matches_schema_sql():
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    assert f"PRAGMA user_version = {SCHEMA_VERSION};" in text


@pytest.mark.parametrize(
    ("version", "match"),
    [(0, "сначала её нужно перевести"), (2, "миграция"), (4, "обновите программу")],
)
def test_restore_refuses_other_schema_version(tmp_path, version, match):
    current = create_database(tmp_path / "base" / "biobank.db")
    old = create_database(tmp_path / "old" / "biobank.db")
    with sqlite3.connect(old) as c:
        c.execute(f"PRAGMA user_version = {int(version)}")  # число из параметров теста
    c.close()
    backup = backup_database(old, tmp_path / "copies", kind="daily")
    before = current.read_bytes()
    with pytest.raises(ValueError, match=match):
        prepare_restore(current, backup)
    assert current.read_bytes() == before
    assert not (current.parent / "biobank.db.restore").exists()


def test_restore_same_version_ok(tmp_path):
    current = create_database(tmp_path / "base" / "biobank.db")
    backup = backup_database(current, tmp_path / "copies", kind="daily")
    with prepare_restore(current, backup) as prepared:
        assert prepared.new_path.exists()
    shutil.rmtree(tmp_path / "copies")


# ---------------------------------------------------------------------------
# Хеш пароля не виден через подключение на чтение (команда sql, сайт, отчёты)
# ---------------------------------------------------------------------------


def test_read_only_connection_hides_password_hash(db_file, capsys):
    from biobank.db import connect_read_only

    conn = connect(db_file)
    create_user(conn, "admin", PASSWORD, "Админ", [ADMIN])
    conn.close()
    ro = connect_read_only(db_file)
    assert ro.execute("SELECT password_hash FROM app_user").fetchone()[0] is None
    row = ro.execute("SELECT * FROM app_user").fetchone()
    assert row["password_hash"] is None and row["login"] == "admin"
    assert ro.execute("SELECT count(*) FROM app_user WHERE password_hash LIKE 'scrypt%'"
                      ).fetchone()[0] == 0  # fmt: skip
    ro.close()
    for query in ("SELECT password_hash FROM app_user", "SELECT * FROM app_user"):
        assert main(["--db", str(db_file), "sql", query]) == 0
        out = capsys.readouterr().out
        assert "scrypt" not in out
    # вход читает хеш через обычное подключение — работает
    conn = connect(db_file)
    assert authenticate(conn, "admin", PASSWORD) is not None
    conn.close()


def test_rehash_from_previous_cost(conn, admin):
    """Хеш с прежней стоимостью (2**14, до 2026-10-20) пересчитывается при входе на 2**15."""
    old = hash_password(PASSWORD, n=2**14)
    conn.execute("UPDATE app_user SET password_hash = ? WHERE login = 'admin'", (old,))
    conn.commit()
    assert needs_rehash(old)
    assert authenticate(conn, "admin", PASSWORD) is not None
    stored = count(conn, "SELECT password_hash FROM app_user WHERE login = 'admin'")
    assert stored.startswith("scrypt$32768$8$1$") and verify_password(PASSWORD, stored)


def test_authenticate_inside_transaction_refused(conn, admin):
    conn.execute("BEGIN")
    with pytest.raises(EditError, match="уже в транзакции"):
        authenticate(conn, "admin", PASSWORD)
    conn.rollback()


# ---------------------------------------------------------------------------
# Версия сессий: выход, смена/сброс пароля, закрытие входа отзывают все сессии
# ---------------------------------------------------------------------------


def version(conn, login):
    return get_user(conn, login).session_version


def test_session_version_bumped_by_every_case(conn, admin):
    from biobank.users import bump_session_version, logout, session_is_valid

    user = create_user(conn, "ivanova", PASSWORD, "Иванова", [READER], user=admin)
    assert user.session_version == 0 and version(conn, "ivanova") == 0
    steps = [
        lambda: logout(conn, get_user(conn, "ivanova")),
        lambda: bump_session_version(conn, "ivanova", user=get_user(conn, "ivanova")),
        lambda: bump_session_version(conn, "ivanova", user=admin),
        lambda: set_password(conn, "ivanova", "задал-админ-123", user=admin),
        lambda: reset_password(conn, "ivanova", user=admin),
        lambda: deactivate(conn, "ivanova", user=admin),
    ]
    for expected, step in enumerate(steps, start=1):
        step()
        assert version(conn, "ivanova") == expected
    activate(conn, "ivanova", user=admin)  # открыть вход — сессии не возвращает
    assert version(conn, "ivanova") == len(steps)

    temporary = reset_password(conn, "ivanova", user=admin)
    before = version(conn, "ivanova")
    changed = change_own_password(conn, get_user(conn, "ivanova"), temporary, "свой-пароль-12")
    assert changed.session_version == before + 1
    old_session = before
    assert not session_is_valid(get_user(conn, "ivanova"), old_session)
    assert session_is_valid(get_user(conn, "ivanova"), changed.session_version)


def test_session_bump_rights(conn, admin):
    from biobank.users import bump_session_version, logout

    a = create_user(conn, "aaa", PASSWORD, "А", [CURATOR], user=admin)
    create_user(conn, "bbb", PASSWORD, "Б", [READER], user=admin)
    with pytest.raises(PermissionDenied, match="только админ"):
        bump_session_version(conn, "bbb", user=a)  # чужую — только админ
    assert version(conn, "bbb") == 0
    with pytest.raises(PermissionDenied):
        logout(conn, None)
    bump_session_version(conn, "bbb")  # системный режим (CLI)
    assert version(conn, "bbb") == 1


def test_session_version_logged_without_hash(conn, admin):
    from biobank.users import logout

    user = create_user(conn, "ivanova", PASSWORD, "Иванова", [READER], user=admin)
    logout(conn, user)
    set_password(conn, "ivanova", "задал-админ-123", user=admin)
    rows = [r for r in log_rows(conn) if r["column_name"] == "session_version"]
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(0, 1), (1, 2)]
    assert rows[0]["user_id"] == user.user_id and rows[1]["user_id"] == admin.user_id
    for r in log_rows(conn):
        assert "scrypt$" not in f"{r['old_value']}{r['new_value']}"


def test_session_version_not_exported(conn, admin, tmp_path):
    from biobank.export import EXPORT_COLUMNS

    assert "session_version" not in EXPORT_COLUMNS["app_user"]


def test_session_version_must_be_non_negative(conn, admin):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE app_user SET session_version = -1")
    conn.rollback()


# ---------------------------------------------------------------------------
# Английские синонимы ролей (для командной строки; в базе — русские)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "role"),
    [("reader", READER), ("Researcher", RESEARCHER), ("CURATOR", CURATOR), (" admin ", ADMIN),
     ("Админ", ADMIN), ("куратор", CURATOR)],
)  # fmt: skip
def test_normalize_role(text, role):
    from biobank.permissions import normalize_role

    assert normalize_role(text) == role


def test_normalize_role_unknown():
    from biobank.permissions import normalize_role

    with pytest.raises(EditError, match=r"Роли «god» нет\. Возможные: читатель \(reader\)"):
        normalize_role("god")


def test_users_functions_accept_english_roles(conn, admin):
    user = create_user(conn, "ivanova", PASSWORD, "Иванова", ["Researcher", "исследователь"],
                       user=admin)  # fmt: skip
    assert user.roles == {RESEARCHER}  # повтор (по-русски и по-английски) — одна роль
    assert set_roles(conn, "ivanova", ["curator", "READER"], user=admin).roles == {CURATOR, READER}
    stored = {r[0] for r in conn.execute("SELECT role FROM user_role")}
    assert stored <= {READER, RESEARCHER, CURATOR, ADMIN}  # в базе — русские


def test_cli_english_roles(db_file, monkeypatch, capsys):
    feed_passwords(monkeypatch, PASSWORD, PASSWORD)
    argv = ["--db", str(db_file), "create-user", "admin", "--role", "ADMIN", "--role", "curator",
            "--name", "Гордей"]  # fmt: skip
    assert main(argv) == 0
    assert "куратор, админ" in capsys.readouterr().out
    assert main(["--db", str(db_file), "set-roles", "admin", "--role", "Admin",
                 "--role", "исследователь"]) == 0  # fmt: skip
    assert "исследователь, админ" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--db", str(db_file), "set-roles", "admin", "--role", "god"])
    assert "Роли «god» нет" in capsys.readouterr().err


def test_cli_role_help_shows_both(capsys):
    with pytest.raises(SystemExit):
        main(["create-user", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "админ (admin)" in out and "читатель (reader)" in out
