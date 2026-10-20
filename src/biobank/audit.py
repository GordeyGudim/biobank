"""Транзакция записи «от имени пользователя» и журнал изменений (change_log).

Как это устроено:
- write_transaction(conn, user) открывает транзакцию BEGIN IMMEDIATE, заново читает
  пользователя из базы (роли, активен ли — по свежим данным, в той же транзакции) и
  запоминает его как «текущего» до конца транзакции;
- все изменения данных идут через insert / update / delete отсюда. Они сами:
  проверяют права текущего пользователя на эту запись (permissions.require),
  заполняют created_by / created_at при создании и пишут строки change_log.
  Так ни одна функция записи не может «забыть» проверку прав или журнал;
- user=None — СИСТЕМНЫЙ режим: команды CLI на сервере и импорт. Права не проверяются
  (у того, кто запускает команды на сервере, и так есть доступ к файлу базы — проверка
  была бы видимостью защиты), журнал пишется с user_id = NULL. На сайте системный режим
  запрещён: сайт ставит флаг web_request() на время запроса, и запись без user —
  PermissionDenied (решение пользователя 2026-10-20);
- пользователь с must_change_password = 1 (временный пароль) данные не меняет, пока не
  сменит пароль.

«Текущий пользователь» хранится в ContextVar — переменной, у которой своё значение в
каждом потоке (у каждого запроса сайта — своё). Её не нужно передавать параметром через
все вспомогательные функции; write_transaction ставит значение и обязательно снимает его.

Что пишется в change_log:
- создание — одна строка, new_value = вся запись в JSON;
- изменение — по строке на каждое изменённое поле: column_name, old_value → new_value;
- удаление — одна строка, old_value = вся запись в JSON (запись удалена, журнал остаётся).
Хеш пароля (app_user.password_hash) не попадает в журнал никогда: ни в JSON, ни как
значение поля — вместо него «(скрыто)».

Журнал НЕ пишут: импорт Excel (created_by NULL уже значит «из Excel») и автоматические
записи проверок в data_issue (checks.run_checks: у них source = 'проверка' и своя пометка
в resolution) — иначе новая база из Excel всегда отличалась бы от старой временем записей.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from biobank.db import now_utc
from biobank.errors import EditError, NotFoundError, PermissionDenied
from biobank.permissions import (
    ADMIN,
    CREATE,
    DELETE,
    UPDATE,
    User,
    can_edit_data,
    denial_message,
    load_user,
    require,
)

# Действия в change_log.action (как в CHECK схемы)
LOG_CREATE = "создание"
LOG_UPDATE = "изменение"
LOG_DELETE = "удаление"

# Что пользователь собирается делать в транзакции — грубая проверка сразу после BEGIN
DATA = "данные"  # менять данные: нужна роль исследователь или куратор
USERS = "пользователи"  # менять учётные записи и роли: нужна роль админ
SELF = "свой пароль"  # любой активный пользователь (сменить свой пароль)

# Колонки, значения которых в журнал не пишутся
SECRET_COLUMNS = {"app_user": ("password_hash",)}
HIDDEN = "(скрыто)"

UserRef = User | int | None  # пользователь, его номер или None (системный режим)

_current: ContextVar[User | None] = ContextVar("biobank_current_user", default=None)
# Идёт ли сейчас запрос сайта. Сайт ставит флаг на время каждого запроса (web_request);
# пока он стоит, системный режим (user=None) запрещён: с сайта пишет только вошедший.
_web: ContextVar[bool] = ContextVar("biobank_web_request", default=False)
LOGIN_MARK = "отметка входа"  # scope users.authenticate: время входа пишется и с сайта


def current_user() -> User | None:
    """Кто пишет в текущей транзакции; None — системный режим (CLI, импорт)."""
    return _current.get()


def begin_web_request() -> Token:
    """Отметить: начался запрос сайта (Flask before_request). Вернуть «жетон» для end_."""
    return _web.set(True)


def end_web_request(token: Token) -> None:
    """Запрос сайта закончился (Flask teardown_request) — снять отметку."""
    _web.reset(token)


@contextmanager
def web_request() -> Iterator[None]:
    """То же одним блоком: `with web_request(): …` — внутри запись без user запрещена."""
    token = begin_web_request()
    try:
        yield
    finally:
        end_web_request(token)


def in_web_request() -> bool:
    return _web.get()


def _resolve_user(conn: sqlite3.Connection, user: UserRef, scope: str) -> User | None:
    """Свежие данные пользователя из базы и грубая проверка прав на scope."""
    if user is None:
        if _web.get() and scope != LOGIN_MARK:
            raise PermissionDenied(
                "Запись с сайта без вошедшего пользователя запрещена — войдите заново."
            )
        return None
    user_id = user.user_id if isinstance(user, User) else user
    try:
        actor = load_user(conn, user_id)
    except NotFoundError:
        raise PermissionDenied("Пользователь не найден — войдите заново.") from None
    if not actor.is_active:
        raise PermissionDenied(denial_message(actor, CREATE))
    if scope == DATA and not can_edit_data(actor):
        raise PermissionDenied(denial_message(actor, CREATE))
    if scope == DATA and actor.must_change_password:
        raise PermissionDenied(
            "Сначала смените временный пароль — до этого изменять данные нельзя."
        )
    if scope == USERS and not actor.has(ADMIN):
        raise PermissionDenied("Пользователей и роли меняет только админ.")
    return actor


@contextmanager
def write_transaction(
    conn: sqlite3.Connection, user: UserRef = None, *, scope: str = DATA
) -> Iterator[User | None]:
    """Транзакция, которая сразу берёт блокировку записи: BEGIN IMMEDIATE.

    Обычный `with conn:` начинает транзакцию только на первом UPDATE/INSERT,
    а проверки (SELECT) до него идут вне транзакции. Если два человека правят
    одновременно, второй мог бы записать поверх того, что проверил до правки первого.
    С BEGIN IMMEDIATE второй ждёт, пока первый закончит, и проверяет уже новые данные.

    Внутри уже открытой транзакции так нельзя: SQLite не умеет вкладывать BEGIN в BEGIN,
    а COMMIT здесь сохранил бы и чужие, ещё не проверенные изменения вызывающего кода.
    Поэтому в этом случае — EditError, база не меняется.

    user — кто пишет (User или user_id; None — системный режим, см. описание модуля).
    Нет прав на scope, вход закрыт, пользователя нет — PermissionDenied, база не меняется.
    Внутри блока `with … as actor` actor — свежие данные пользователя (или None).
    """
    if conn.in_transaction:
        raise EditError(
            "Подключение уже в транзакции — правку нельзя выполнить внутри чужой транзакции. "
            "Завершите её (commit или rollback) и повторите."
        )
    conn.execute("BEGIN IMMEDIATE")
    token = None
    try:
        actor = _resolve_user(conn, user, scope)
        token = _current.set(actor)
        yield actor
        conn.commit()  # и сбой самого COMMIT (диск полон, база занята) — под откатом
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if token is not None:
            _current.reset(token)


# ---------------------------------------------------------------------------
# Сведения о таблицах (имена — из кода и из самой базы, не от пользователя)
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))]


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute("SELECT name, pk FROM pragma_table_info(?)", (table,)).fetchall()
    return [r[0] for r in sorted(rows, key=lambda r: r[1]) if r[1]]


def record_key(key: dict) -> str:
    """Ключ записи текстом для change_log.record_key.

    >>> record_key({"specimen_id": 12}), record_key({"sample_id": 812, "lab_id": 1})
    ('12', 'sample_id=812, lab_id=1')
    """
    if len(key) == 1:
        return str(next(iter(key.values())))
    return ", ".join(f"{column}={value}" for column, value in key.items())


def _select(conn: sqlite3.Connection, table: str, key: dict) -> sqlite3.Row | None:
    where = " AND ".join(f"{column} = ?" for column in key)
    return conn.execute(f"SELECT * FROM {table} WHERE {where}", tuple(key.values())).fetchone()


def _public(table: str, row: sqlite3.Row | dict) -> dict:
    """Запись без секретных колонок (хеша пароля) — для JSON в журнале."""
    hidden = SECRET_COLUMNS.get(table, ())
    return {k: row[k] for k in row.keys() if k not in hidden}


def _scalar(conn: sqlite3.Connection, sql: str, *params):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def record_label(conn: sqlite3.Connection, table: str, row) -> str | None:
    """Понятное имя записи для журнала: «26 ph», «26 ph a (гистология) особи 26 ph»…"""
    get = row.__getitem__

    def specimen(specimen_id):
        return _scalar(conn, "SELECT label FROM specimen WHERE specimen_id = ?", specimen_id)

    def event(event_id):
        r = conn.execute(
            "SELECT source_sheet, event_date FROM sampling_event WHERE event_id = ?", (event_id,)
        ).fetchone()
        if r is None:
            return f"выезд №{event_id}"
        return f"{r['source_sheet'] or f'выезд №{event_id}'} ({r['event_date']})"

    if table == "specimen":
        return get("label")
    if table == "sample":
        return f"{get('label')} ({get('sample_type')}) особи {specimen(get('specimen_id'))}"
    if table == "species_identification":
        return f"определение вида особи {specimen(get('specimen_id'))}"
    if table == "sample_shipment":
        sample = _scalar(conn, "SELECT label FROM sample WHERE sample_id = ?", get("sample_id"))
        lab = _scalar(conn, "SELECT name FROM lab WHERE lab_id = ?", get("lab_id"))
        return f"образец {sample} → {lab}"
    if table == "analysis":
        sample = _scalar(conn, "SELECT label FROM sample WHERE sample_id = ?", get("sample_id"))
        return f"{get('analysis_type')}, образец {sample}"
    if table == "sampling_event":
        return event(get("event_id"))
    if table == "trawling":
        return f"{event(get('event_id'))} / траление {get('trawl_no')}"
    if table == "water_measurement":
        return f"{event(get('event_id'))} / вода, {get('point_type')}"
    if table == "catch_record":
        return f"{event(get('event_id'))} / прилов «{get('raw_text')}»"
    if table == "data_issue":
        return f"проблема №{get('issue_id')}: {get('description')}"
    if table == "app_user":
        return f"{get('display_name')} ({get('login')})"
    if table == "user_role":
        login = _scalar(conn, "SELECT login FROM app_user WHERE user_id = ?", get("user_id"))
        return f"{login}: {get('role')}"
    if table == "expedition_member":
        name = _scalar(conn, "SELECT full_name FROM person WHERE person_id = ?", get("person_id"))
        expedition = _scalar(
            conn, "SELECT name FROM expedition WHERE expedition_id = ?", get("expedition_id")
        )
        return f"{name} — {expedition}"
    for column in ("name", "scientific_name", "full_name"):
        if column in row.keys():
            return get(column)
    if table == "gear":
        return f"{get('gear_type')} {get('mesh_cm') or '?'} / {get('size_m') or '?'}"
    return None


def _log(
    conn: sqlite3.Connection, action: str, table: str, key: dict, label: str | None,
    column: str | None = None, old=None, new=None,
) -> None:  # fmt: skip
    actor = current_user()
    conn.execute(
        "INSERT INTO change_log (changed_at, user_id, action, table_name, record_key, "
        "record_label, column_name, old_value, new_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_utc(), actor.user_id if actor else None, action, table, record_key(key), label,
         column, old, new),
    )  # fmt: skip


def _json(table: str, row) -> str:
    return json.dumps(_public(table, row), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Изменение данных
# ---------------------------------------------------------------------------


def insert(conn: sqlite3.Connection, table: str, **values) -> int:
    """INSERT одной строки от имени текущего пользователя; вернуть её id (lastrowid).

    Права: создание в таблице table. Сами заполняются created_at (время, UTC) и
    created_by (текущий пользователь; в системном режиме — пусто), если такие колонки есть.
    В журнал — строка «создание» с записью целиком (JSON).
    Имена таблицы и колонок — из кода (не от пользователя), значения — через «?».
    """
    actor = current_user()
    if actor is not None:
        require(actor, CREATE, table)
    columns = _columns(conn, table)
    if "created_at" in columns and "created_at" not in values:
        values["created_at"] = now_utc()
    if "created_by" in columns and "created_by" not in values:
        values["created_by"] = actor.user_id if actor else None
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values()))
    pk = _primary_key(conn, table)
    if len(pk) == 1 and pk[0] not in values:
        key = {pk[0]: cursor.lastrowid}
    else:
        key = {column: values[column] for column in pk}
    row = _select(conn, table, key)
    _log(conn, LOG_CREATE, table, key, record_label(conn, table, row), new=_json(table, row))
    return cursor.lastrowid


def update(
    conn: sqlite3.Connection, table: str, key: dict, new: dict, *, derived: bool = False
) -> dict:
    """UPDATE только тех колонок, значение которых правда меняется; вернуть их {колонка: новое}.

    Права: изменение ЭТОЙ записи (исследователь — только своей). derived=True — изменение —
    следствие другого, уже разрешённого действия (текущий вид особи после нового определения,
    даты экспедиции после нового выезда, пометка в журнале проблем об удалённой записи):
    права на саму запись не проверяются, но в журнал изменение попадает.
    В журнал — по строке на каждое изменённое поле (было → стало).
    """
    row = _select(conn, table, key)
    if row is None:
        raise NotFoundError(f"Записи {record_key(key)} в таблице {table} нет.")
    actor = current_user()
    if actor is not None and not derived:
        require(actor, UPDATE, table, row)
    changed = {column: value for column, value in new.items() if row[column] != value}
    if not changed:
        return changed
    assignments = ", ".join(f"{column} = ?" for column in changed)
    where = " AND ".join(f"{column} = ?" for column in key)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {where}", (*changed.values(), *key.values())
    )
    label = record_label(conn, table, _select(conn, table, key))
    hidden = SECRET_COLUMNS.get(table, ())
    for column, value in changed.items():
        if column in hidden:
            _log(conn, LOG_UPDATE, table, key, label, column, HIDDEN, HIDDEN)
        else:
            _log(conn, LOG_UPDATE, table, key, label, column, row[column], value)
    return changed


def delete(conn: sqlite3.Connection, table: str, key: dict, *, derived: bool = False) -> None:
    """DELETE одной записи; в журнал — строка «удаление» с записью целиком (JSON).

    Права: удаление ЭТОЙ записи (исследователь — только своей); derived — как у update.
    """
    row = _select(conn, table, key)
    if row is None:
        raise NotFoundError(f"Записи {record_key(key)} в таблице {table} нет.")
    actor = current_user()
    if actor is not None and not derived:
        require(actor, DELETE, table, row)
    label = record_label(conn, table, row)
    where = " AND ".join(f"{column} = ?" for column in key)
    conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(key.values()))
    _log(conn, LOG_DELETE, table, key, label, old=_json(table, row))
