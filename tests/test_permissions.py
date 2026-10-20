"""Права по ролям и журнал изменений (change_log) в функциях записи entry / records / edit.

База — копия импортированной книги с пятью пользователями (по одному на роль и второй
исследователь). Шаблон с пользователями собирается один раз на модуль (хеш пароля
считается медленно — так задумано), каждый тест получает свою копию.
"""

import json
import shutil

import pytest
from conftest import SOURCE, count

from biobank import browse, entry, records
from biobank.audit import write_transaction
from biobank.db import connect
from biobank.edit import identify, link_trawl, merge_people, resolve_issue
from biobank.errors import EditError, PermissionDenied
from biobank.permissions import (
    ADMIN,
    CREATE,
    CURATOR,
    DELETE,
    MANAGE_USERS,
    READER,
    RESEARCHER,
    RESOLVE,
    UPDATE,
    VIEW,
    can,
)
from biobank.users import create_user, deactivate, get_user

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")

PASSWORD = "очень-длинный-пароль"
LOGINS = {
    "reader": [READER],
    "res": [RESEARCHER],
    "res2": [RESEARCHER],
    "cur": [CURATOR],
    "admin": [ADMIN],
}


@pytest.fixture(scope="module")
def template(db_path, tmp_path_factory):
    path = tmp_path_factory.mktemp("perm") / "users.db"
    shutil.copy(db_path, path)
    conn = connect(path)
    for login, roles in LOGINS.items():
        create_user(conn, login, PASSWORD, login.title(), roles, must_change_password=False)
    conn.close()
    return path


@pytest.fixture
def conn(template, tmp_path):
    path = tmp_path / "work.db"
    shutil.copy(template, path)
    c = connect(path)
    yield c
    c.close()


@pytest.fixture
def u(conn):
    """Пользователи по логину: u["res"] — исследователь и т.д."""
    return {login: get_user(conn, login) for login in LOGINS}


@pytest.fixture
def event(conn):
    """Выезд-траление, введённый исследователем res (его запись)."""
    res = get_user(conn, "res")
    expedition = entry.create_expedition(conn, "Кантхо 2027", user=res).record_id
    site = count(conn, "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'")
    event_id = entry.create_event(conn, expedition, site, "2026-09-01", "траление", user=res)
    entry.add_trawling(conn, event_id.record_id, 1, user=res)
    return event_id.record_id


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}


def denied(conn, match, func, *args, **kwargs):
    """func(conn, …) — PermissionDenied с текстом match; база (и журнал) не изменились."""
    before = snapshot(conn)
    with pytest.raises(PermissionDenied, match=match):
        func(conn, *args, **kwargs)
    assert not conn.in_transaction
    assert snapshot(conn) == before


def specimen_id(conn, label):
    return count(conn, "SELECT specimen_id FROM specimen WHERE label = ?", label)


def log(conn, **where):
    sql = "SELECT * FROM change_log"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return [dict(r) for r in conn.execute(sql + " ORDER BY change_id", tuple(where.values()))]


# ---------------------------------------------------------------------------
# can(): чистые правила
# ---------------------------------------------------------------------------


def test_can_matrix(u):
    own = {"created_by": u["res"].user_id}
    other = {"created_by": u["res2"].user_id}
    excel = {"created_by": None}
    expected = {
        # (действие, таблица, запись): читатель, исследователь, куратор, админ
        (VIEW, "specimen", None): (True, True, True, True),
        (CREATE, "specimen", None): (False, True, True, False),
        (CREATE, "species", None): (False, True, True, False),  # справочник — тоже
        (UPDATE, "specimen", "own"): (False, True, True, False),
        (UPDATE, "specimen", "other"): (False, False, True, False),
        (UPDATE, "specimen", "excel"): (False, False, True, False),
        (DELETE, "sample", "own"): (False, True, True, False),
        (DELETE, "sample", "excel"): (False, False, True, False),
        (RESOLVE, "data_issue", None): (False, False, True, False),
        (MANAGE_USERS, None, None): (False, False, False, True),
        (UPDATE, "app_user", None): (False, False, False, True),
        (CREATE, "change_log", None): (False, False, False, False),
    }
    records_ = {"own": own, "other": other, "excel": excel, None: None}
    for (action, table, rec), answers in expected.items():
        got = tuple(
            can(u[login], action, table, records_[rec])
            for login in ("reader", "res", "cur", "admin")
        )
        assert got == answers, (action, table, rec)
    assert not can(None, VIEW, "specimen")


def test_roles_combine():
    from biobank.permissions import User

    both = User(9, "g", "Г", None, True, False, frozenset({CURATOR, ADMIN}))
    assert can(both, MANAGE_USERS) and can(both, UPDATE, "specimen", {"created_by": None})
    inactive = User(9, "g", "Г", None, False, False, frozenset({CURATOR, ADMIN}))
    assert not can(inactive, VIEW, "specimen") and not can(inactive, MANAGE_USERS)


# ---------------------------------------------------------------------------
# Создание
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("login", "match"), [("reader", "нужна роль"), ("admin", "нужна роль")])
def test_reader_and_admin_cannot_create(conn, u, login, match):
    denied(conn, match, entry.create_expedition, "Новая", user=u[login])
    denied(conn, match, entry.add_sample, "1 pc", "ДНК", sample_label="1 pc x", user=u[login])
    denied(conn, match, identify, "1 pc", "Plotosus canius", "ДНК", "точно", user=u[login])


def test_create_fills_created_by_and_logs(conn, u, event):
    result = entry.create_specimen(conn, event, "Plotosus canius", trawl_no=1, user=u["res"])
    row = conn.execute("SELECT * FROM specimen WHERE specimen_id = ?", (result.record_id,))
    row = row.fetchone()
    assert row["created_by"] == u["res"].user_id and row["created_at"]
    rows = log(conn, table_name="specimen", record_key=str(result.record_id))
    assert [(r["action"], r["user_id"], r["record_label"]) for r in rows] == [
        ("создание", u["res"].user_id, "179 pc")
    ]
    record = json.loads(rows[0]["new_value"])
    assert record["label"] == "179 pc" and record["created_by"] == u["res"].user_id
    ident = log(conn, table_name="species_identification", action="создание")
    assert ident[-1]["record_label"] == "определение вида особи 179 pc"


def test_user_as_id_and_inactive(conn, u, event):
    entry.create_specimen(conn, event, "Plotosus canius", user=u["res"].user_id)  # номер — тоже
    deactivate(conn, "res", user=u["admin"])
    denied(conn, "закрыт", entry.create_specimen, event, "Plotosus canius", user=u["res"])


def test_system_mode_logs_without_user(conn):
    entry.create_expedition(conn, "Из CLI")
    rows = log(conn, table_name="expedition")
    assert rows[-1]["user_id"] is None and rows[-1]["action"] == "создание"
    created = count(conn, "SELECT created_by FROM expedition WHERE name = 'Из CLI'")
    assert created is None


# ---------------------------------------------------------------------------
# Правка: своё / чужое / из Excel
# ---------------------------------------------------------------------------


def test_researcher_edits_only_own(conn, u, event):
    own = entry.create_specimen(conn, event, "Plotosus canius", tl_cm=20, user=u["res"])
    records.update_specimen(conn, own.record_id, notes="своя", user=u["res"])
    entry.update_measurements(conn, own.record_id, tl_cm=21, user=u["res"])
    denied(conn, "Чужие записи", records.update_specimen, own.record_id, notes="x",
           user=u["res2"])  # fmt: skip
    denied(conn, "из Excel", entry.update_measurements, "1 pc", tl_cm=10, user=u["res"])
    denied(conn, "из Excel", records.update_specimen, specimen_id(conn, "1 pc"), notes="x",
           user=u["res"])  # fmt: skip
    denied(conn, "из Excel", link_trawl, "5 pc", 1, user=u["res"])
    # куратор правит и чужое, и из Excel
    records.update_specimen(conn, own.record_id, notes="поправил куратор", user=u["cur"])
    entry.update_measurements(conn, "1 pc", tl_cm=10, user=u["cur"])
    link_trawl(conn, "5 pc", 1, user=u["cur"])


def test_update_logs_each_field(conn, u, event):
    own = entry.create_specimen(conn, event, "Plotosus canius", tl_cm=20, user=u["res"])
    entry.update_measurements(conn, own.record_id, tl_cm=21, mass_g=150, user=u["res"])
    rows = log(conn, table_name="specimen", action="изменение")
    assert {(r["column_name"], r["old_value"], r["new_value"]) for r in rows} == {
        ("tl_cm", 20.0, 21.0),
        ("mass_g", None, 150.0),
    }
    assert {r["user_id"] for r in rows} == {u["res"].user_id}
    # без изменений — в журнале ничего
    entry.update_measurements(conn, own.record_id, tl_cm=21, user=u["res"])
    assert len(log(conn, table_name="specimen", action="изменение")) == 2


def test_identify_excel_specimen_by_researcher(conn, u):
    """Добавить определение вида можно и к особи из Excel: текущий вид — следствие."""
    identify(conn, "1 pc", "Plotosus canius", "ДНК", "точно", user=u["res"])
    rows = log(conn, table_name="species_identification", action="создание")
    assert rows[-1]["user_id"] == u["res"].user_id


# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------


def test_delete_own_and_logged_as_json(conn, u, event):
    own = entry.create_specimen(conn, event, "Plotosus canius", tl_cm=20, user=u["res"])
    denied(conn, "Чужие записи", records.delete_specimen, own.record_id, user=u["res2"])
    records.delete_specimen(conn, own.record_id, user=u["res"])
    rows = log(conn, action="удаление")
    assert [r["table_name"] for r in rows] == ["species_identification", "specimen"]
    deleted = json.loads(rows[-1]["old_value"])
    assert deleted["label"] == "179 pc" and deleted["tl_cm"] == 20
    assert rows[-1]["record_label"] == "179 pc" and rows[-1]["user_id"] == u["res"].user_id
    # история удалённой записи остаётся в журнале
    history = browse.record_history(conn, "specimen", own.record_id)
    assert [h["action"] for h in history] == ["создание", "удаление"]
    assert history[0]["user_name"] == "Res"


def test_delete_blocked_by_other_users_child(conn, u, event):
    """Исследователь не удалит свою особь, если куратор добавил ей определение вида."""
    own = entry.create_specimen(conn, event, "Plotosus canius", user=u["res"])
    identify(conn, "179 pc", "Plotosus canius", "ДНК", "точно", user=u["cur"])
    denied(conn, "Чужие записи", records.delete_specimen, own.record_id, user=u["res"])
    records.delete_specimen(conn, own.record_id, user=u["cur"])


def test_delete_excel_record_only_curator(conn, u):
    catch = count(conn, "SELECT min(catch_id) FROM catch_record")
    denied(conn, "из Excel", records.delete_catch_record, catch, user=u["res"])
    denied(conn, "нужна роль", records.delete_catch_record, catch, user=u["reader"])
    records.delete_catch_record(conn, catch, user=u["cur"])


def test_delete_sample_logs_shipments(conn, u):
    sample = entry.add_sample(conn, "1 pc", "ДНК", sample_label="1 pc x", labs=["Россия"],
                              user=u["res"]).record_id  # fmt: skip
    records.delete_sample(conn, sample, user=u["res"])
    rows = log(conn, action="удаление")
    assert [(r["table_name"], r["record_key"]) for r in rows][0][0] == "sample_shipment"
    assert rows[0]["record_key"].startswith(f"sample_id={sample}, lab_id=")


# ---------------------------------------------------------------------------
# Журнал проблем: закрывает куратор
# ---------------------------------------------------------------------------


def test_resolve_issue_only_curator(conn, u):
    issue = count(conn, "SELECT min(issue_id) FROM data_issue WHERE resolved = 0")
    for login in ("reader", "res", "admin"):
        denied(conn, "куратор|нужна роль", resolve_issue, issue, "сверено", user=u[login])
    resolve_issue(conn, issue, "сверено", user=u["cur"])
    row = conn.execute("SELECT * FROM data_issue WHERE issue_id = ?", (issue,)).fetchone()
    assert row["resolved"] == 1 and row["resolved_by"] == u["cur"].user_id and row["resolved_at"]
    columns = {r["column_name"] for r in log(conn, table_name="data_issue")}
    assert {"resolved", "resolution", "resolved_at", "resolved_by"} <= columns


# ---------------------------------------------------------------------------
# Справочник людей: слияние
# ---------------------------------------------------------------------------


def test_merge_people(conn, u):
    identify(conn, "1 pc", "Plotosus canius", "ДНК", "точно", identified_by="Иванов А.",
             user=u["res"])  # fmt: skip
    identify(conn, "2 pc", "Plotosus canius", "ДНК", "точно", identified_by="Иванов А.А.",
             user=u["res"])  # fmt: skip
    keep = count(conn, "SELECT person_id FROM person WHERE full_name = 'Иванов А.А.'")
    remove = count(conn, "SELECT person_id FROM person WHERE full_name = 'Иванов А.'")
    denied(conn, "только куратор", merge_people, keep, remove, user=u["res"])
    with pytest.raises(EditError, match="тот же"):
        merge_people(conn, keep, keep, user=u["cur"])
    result = merge_people(conn, keep, remove, user=u["cur"])
    assert "перенесено определений вида: 1" in result.message
    assert count(conn, "SELECT count(*) FROM person") == 1
    assert count(conn, "SELECT count(*) FROM species_identification WHERE identified_by = ?",
                 keep) == 2  # fmt: skip
    deleted = log(conn, table_name="person", action="удаление")
    assert json.loads(deleted[0]["old_value"])["full_name"] == "Иванов А."


# ---------------------------------------------------------------------------
# Чтение журнала для сайта
# ---------------------------------------------------------------------------


def test_list_changes_filters(conn, u, event):
    entry.create_specimen(conn, event, "Plotosus canius", user=u["res"])
    entry.update_measurements(conn, "1 pc", tl_cm=10, user=u["cur"])
    by_cur = browse.list_changes(conn, user_id=u["cur"].user_id)
    assert by_cur.total == 1 and by_cur.items[0]["user_name"] == "Cur"
    specimens = browse.list_changes(conn, table="specimen")
    assert specimens.total == 2 and specimens.items[0]["action"] == "изменение"  # новые первыми
    today = specimens.items[0]["changed_at"][:10]
    # без viewer (как у не-админа) — без изменений учётных записей и ролей
    assert browse.list_changes(conn, date_from=today, date_to=today).total == count(
        conn,
        "SELECT count(*) FROM change_log WHERE table_name NOT IN ('app_user', 'user_role')",
    )
    assert browse.list_changes(conn, date_to="2000-01-01").total == 0
    page = browse.list_changes(conn, per_page=2, page=2)
    assert page.page == 2 and len(page.items) == 2
    assert "specimen" in browse.change_tables(conn)


def test_write_transaction_rejects_unknown_user(conn):
    with pytest.raises(PermissionDenied, match="не найден"), write_transaction(conn, 99999):
        pass
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# Даты экспедиции — по её выездам (опечатка в годе не портит их навсегда)
# ---------------------------------------------------------------------------


def expedition_dates(conn, expedition_id):
    row = conn.execute(
        "SELECT date_start, date_end FROM expedition WHERE expedition_id = ?", (expedition_id,)
    ).fetchone()
    return tuple(row)


def test_typo_event_deleted_restores_excel_expedition_dates(conn, u):
    first = conn.execute(
        "SELECT e.expedition_id, e.site_id, m.name FROM sampling_event e "
        "JOIN capture_method m USING (capture_method_id) ORDER BY e.event_id LIMIT 1"
    ).fetchone()
    before = expedition_dates(conn, first["expedition_id"])
    result = entry.create_event(
        conn, first["expedition_id"], first["site_id"], "1925-09-25", first["name"], user=u["res"]
    )
    assert expedition_dates(conn, first["expedition_id"]) == ("1925-09-25", before[1])
    assert any("вне дат экспедиции" in w for w in result.warnings)
    records.delete_event(conn, result.record_id, user=u["res"])
    assert expedition_dates(conn, first["expedition_id"]) == before
    # и исправление даты (вместо удаления) тоже возвращает даты
    event = entry.create_event(
        conn, first["expedition_id"], first["site_id"], "1925-09-25", first["name"], user=u["res"]
    ).record_id
    # дата внутри прежних дат экспедиции (на том же месте в этот день выезда нет)
    assert before[0] < "2025-10-01" < before[1]
    records.update_event(conn, event, event_date="2025-10-01", user=u["res"])
    assert expedition_dates(conn, first["expedition_id"]) == before
    # изменение дат — в журнале, от имени исследователя (следствие его правки)
    rows = log(conn, table_name="expedition", action="изменение")
    assert rows and {r["user_id"] for r in rows} == {u["res"].user_id}


def test_move_event_recomputes_both_expeditions(conn, u):
    a = entry.create_expedition(conn, "Экспедиция А", user=u["res"]).record_id
    b = entry.create_expedition(conn, "Экспедиция Б", user=u["res"]).record_id
    site = count(conn, "SELECT site_id FROM sampling_site WHERE name = 'Участок у Кантхо'")
    entry.create_event(conn, a, site, "2026-09-01", "траление", user=u["res"])
    moved = entry.create_event(conn, a, site, "2026-09-03", "траление", user=u["res"]).record_id
    assert expedition_dates(conn, a) == ("2026-09-01", "2026-09-03")
    records.update_event(conn, moved, expedition_id=b, user=u["res"])
    assert expedition_dates(conn, a) == ("2026-09-01", "2026-09-01")
    assert expedition_dates(conn, b) == ("2026-09-03", "2026-09-03")


# ---------------------------------------------------------------------------
# Сайт: запись без вошедшего пользователя запрещена
# ---------------------------------------------------------------------------


def test_web_request_forbids_system_mode(conn, u):
    from biobank.audit import web_request
    from biobank.users import authenticate

    before = count(conn, "SELECT count(*) FROM change_log")
    with web_request():
        denied(conn, "без вошедшего пользователя", entry.create_expedition, "С сайта")
        denied(conn, "без вошедшего пользователя", resolve_issue, 1, "сверено")
        # с пользователем — можно; вход (отметка времени) — тоже
        entry.create_expedition(conn, "С сайта", user=u["res"])
        assert authenticate(conn, "res", PASSWORD) is not None
    assert count(conn, "SELECT count(*) FROM change_log") == before + 1
    entry.create_expedition(conn, "Из CLI")  # вне запроса сайта — системный режим работает


# ---------------------------------------------------------------------------
# COMMIT тоже под откатом
# ---------------------------------------------------------------------------


class FailingCommit(__import__("sqlite3").Connection):
    """Подключение, у которого COMMIT падает (как при «диск полон» или «база занята»)."""

    def commit(self):
        raise __import__("sqlite3").OperationalError("сбой COMMIT")


def test_commit_failure_rolls_back(conn, tmp_path):
    import sqlite3

    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    bad = sqlite3.connect(path, factory=FailingCommit)
    bad.row_factory = sqlite3.Row
    bad.execute("PRAGMA foreign_keys = ON")
    before = count(conn, "SELECT count(*) FROM change_log")
    with pytest.raises(sqlite3.OperationalError, match="сбой COMMIT"):
        entry.create_expedition(bad, "Не сохранится")
    assert not bad.in_transaction
    bad.close()
    assert count(conn, "SELECT count(*) FROM expedition WHERE name = 'Не сохранится'") == 0
    assert count(conn, "SELECT count(*) FROM change_log") == before


# ---------------------------------------------------------------------------
# Временный пароль: данные менять нельзя, пока не сменён
# ---------------------------------------------------------------------------


def test_must_change_password_blocks_data(conn, u):
    from biobank.users import change_own_password

    temp = create_user(conn, "new", PASSWORD, "Новый", [RESEARCHER], user=u["admin"])
    assert temp.must_change_password
    denied(conn, "смените временный пароль", entry.create_expedition, "Х", user=temp)
    change_own_password(conn, temp, PASSWORD, "мой-новый-пароль")
    entry.create_expedition(conn, "Х", user=temp)


# ---------------------------------------------------------------------------
# Свои проблемы журнала исследователь закрывает сам (решение 2026-10-20)
# ---------------------------------------------------------------------------


def add_issue(conn, user, source="пользователь"):
    """Своя проблема — через edit.add_issue; «чужой» источник — напрямую (так не бывает)."""
    from biobank import audit
    from biobank.edit import add_issue as add

    if source == "пользователь":
        return add(conn, category="промеры", description="Сверить TL", user=user).record_id
    with write_transaction(conn, user):
        return audit.insert(
            conn, "data_issue", category="промеры", description="Сверить TL", source=source
        )


def test_researcher_resolves_own_user_issue(conn, u):
    own = add_issue(conn, u["res"])
    assert count(conn, "SELECT created_by FROM data_issue WHERE issue_id = ?", own) == (
        u["res"].user_id
    )
    denied(conn, "добавил сам", resolve_issue, own, "сверено", user=u["res2"])
    resolve_issue(conn, own, "сверено", user=u["res"])
    assert count(conn, "SELECT resolved_by FROM data_issue WHERE issue_id = ?", own) == (
        u["res"].user_id
    )
    # проблема импорта и запись «проверки», даже с его created_by, — только куратор
    imported = count(conn, "SELECT min(issue_id) FROM data_issue WHERE source = 'импорт'")
    denied(conn, "добавил сам", resolve_issue, imported, "сверено", user=u["res"])
    check_like = add_issue(conn, u["res"], source="проверка")
    denied(conn, "добавил сам", resolve_issue, check_like, "сверено", user=u["res"])
    resolve_issue(conn, check_like, "сверено", user=u["cur"])


def test_add_issue(conn, u):
    from biobank.edit import add_issue

    sp = specimen_id(conn, "155 pc")
    result = add_issue(conn, category=" промеры ", description="Сверить  TL с журналом",
                       table_name="specimen", record_id=sp, object_label="155 pc",
                       user=u["res"])  # fmt: skip
    row = conn.execute("SELECT * FROM data_issue WHERE issue_id = ?", (result.record_id,))
    row = row.fetchone()
    assert (row["source"], row["created_by"], row["category"], row["description"]) == (
        "пользователь",
        u["res"].user_id,
        "промеры",
        "Сверить TL с журналом",
    )
    assert row["created_at"] and row["resolved"] == 0
    logged = log(conn, table_name="data_issue", action="создание")
    assert logged[-1]["record_key"] == str(result.record_id)
    assert logged[-1]["user_id"] == u["res"].user_id
    # сайт видит автора и может решить, показывать ли «Решено»
    issue = browse.get_issue(conn, result.record_id)
    assert issue["created_by_name"] == "Res" and issue["resolved_by_name"] is None
    assert can(u["res"], RESOLVE, "data_issue", issue)
    assert not can(u["res2"], RESOLVE, "data_issue", issue)
    resolve_issue(conn, result.record_id, "сверено", user=u["res"])
    issue = browse.get_issue(conn, result.record_id)
    assert issue["resolved_by_name"] == "Res"
    listed = browse.list_issues(conn, table_name="specimen", resolved=True)
    assert any(i["issue_id"] == result.record_id and i["created_by_name"] == "Res"
               for i in listed.items)  # fmt: skip


@pytest.mark.parametrize(
    ("kwargs", "match", "error"),
    [
        ({"category": " ", "description": "x"}, "категорию", EditError),
        ({"category": "промеры", "description": ""}, "Опишите", EditError),
        ({"category": "промеры", "description": "x", "table_name": "specimen"}, "вместе",
         EditError),
        ({"category": "промеры", "description": "x", "table_name": "нет_такой",
          "record_id": 1}, "Таблицы", EditError),
        ({"category": "промеры", "description": "x", "table_name": "specimen",
          "record_id": 999999}, "нет", EditError),
    ],
)  # fmt: skip
def test_add_issue_errors(conn, u, kwargs, match, error):
    from biobank.edit import add_issue

    before = snapshot(conn)
    with pytest.raises(error, match=match):
        add_issue(conn, **kwargs, user=u["res"])
    assert snapshot(conn) == before


@pytest.mark.parametrize("login", ["reader", "admin"])
def test_add_issue_needs_researcher_or_curator(conn, u, login):
    from biobank.edit import add_issue

    denied(conn, "нужна роль", add_issue, category="промеры", description="x", user=u[login])
    add_issue(conn, category="промеры", description="x", user=u["cur"])


# ---------------------------------------------------------------------------
# Журнал изменений: учётные записи и роли видит только админ
# ---------------------------------------------------------------------------


def test_change_log_hides_user_tables_from_non_admins(conn, u):
    from biobank.users import set_roles

    set_roles(conn, "reader", [READER, RESEARCHER], user=u["admin"])  # правка ролей админом
    entry.create_expedition(conn, "Видна всем", user=u["res"])
    reader_id = u["reader"].user_id
    private = ("app_user", "user_role")

    for login in ("reader", "res", "cur"):
        viewer = get_user(conn, login)
        page = browse.list_changes(conn, viewer=viewer, per_page=500)
        assert page.items and not {i["table_name"] for i in page.items} & set(private)
        assert all("user_login" not in i for i in page.items)  # логин автора скрыт
        assert any(i["user_name"] == "Res" for i in page.items)
        assert browse.list_changes(conn, table="user_role", viewer=viewer).total == 0
        assert browse.record_history(conn, "app_user", reader_id, viewer=viewer) == []
        key = f"user_id={reader_id}, role={RESEARCHER}"
        assert browse.record_history(conn, "user_role", key, viewer=viewer) == []
        assert not set(browse.change_tables(conn, viewer=viewer)) & set(private)
        authors = browse.change_authors(conn, viewer=viewer)
        # админ менял только роли — в фильтре «Кто» его нет; логинов нет ни у кого
        assert [a["display_name"] for a in authors] == ["Res"]
        assert all("login" not in a for a in authors)
    assert browse.list_changes(conn).total == browse.list_changes(conn, viewer=u["cur"]).total

    admin = u["admin"]
    assert set(private) <= set(browse.change_tables(conn, viewer=admin))
    roles = browse.list_changes(conn, table="user_role", user_id=admin.user_id, viewer=admin)
    assert roles.total == 1 and roles.items[0]["user_login"] == "admin"
    key = f"user_id={reader_id}, role={RESEARCHER}"
    assert browse.record_history(conn, "user_role", key, viewer=admin)[0]["action"] == "создание"
    assert browse.record_history(conn, "app_user", reader_id, viewer=admin)
    authors = {a["login"] for a in browse.change_authors(conn, viewer=admin)}
    assert authors == {"admin", "res"}


def test_list_shipments_has_author(conn, u):
    sample = entry.add_sample(conn, "1 pc", "ДНК", sample_label="1 pc x", labs=["Россия"],
                              user=u["res"]).record_id  # fmt: skip
    (shipment,) = browse.list_shipments(conn, sample)
    assert shipment["created_by"] == u["res"].user_id and shipment["created_at"]
    assert shipment["created_by_name"] == "Res"
    assert can(u["res"], DELETE, "sample_shipment", shipment)
    assert not can(u["res2"], DELETE, "sample_shipment", shipment)
    records.delete_shipment(conn, sample, shipment["lab_id"], user=u["res"])
    assert browse.list_shipments(conn, sample) == []
    # отправки из Excel — без автора: удалить может только куратор
    excel = count(conn, "SELECT min(sample_id) FROM sample_shipment")
    old = browse.list_shipments(conn, excel)[0]
    assert old["created_by"] is None and old["created_by_name"] is None
    assert not can(u["res"], DELETE, "sample_shipment", old)
