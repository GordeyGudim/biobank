"""Пользователь сайта и права: кто что может делать с данными.

Роли (у человека их может быть несколько — права складываются):
- читатель — только просмотр;
- исследователь — просмотр; создание любых записей данных, включая справочники (виды,
  лаборатории, сети, города, люди); правка и удаление ТОЛЬКО своих записей
  (created_by = он);
- куратор — всё с данными: правка и удаление любых записей (в том числе из Excel —
  у них created_by пустой), закрытие проблем журнала, слияние людей в справочнике;
  исследователь закрывает только проблемы, которые добавил сам (data_issue.source =
  'пользователь' и created_by = он); проблемы импорта и проверок — только куратор
  (решение пользователя 2026-10-20);
- админ — только пользователи и роли; данные не правит (просмотр — как у всех).
Неактивный пользователь (вход закрыт) не может ничего.

can() — чистая функция без базы: веб по ней решает, какие кнопки показывать.
Функции записи (entry, records, edit, users) проверяют права сами, внутри своей
транзакции, по свежим данным пользователя из базы (load_user): сайт — не единственная защита.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from biobank.errors import EditError, NotFoundError, PermissionDenied

READER = "читатель"
RESEARCHER = "исследователь"
CURATOR = "куратор"
ADMIN = "админ"
ROLES = (READER, RESEARCHER, CURATOR, ADMIN)  # как в CHECK схемы (user_role.role)
# Английские синонимы — для удобства в командной строке; в базе и на сайте роль русская
ROLE_ALIASES = {"reader": READER, "researcher": RESEARCHER, "curator": CURATOR, "admin": ADMIN}


def roles_help() -> str:
    """Роли с английскими синонимами — для справки и сообщений.

    >>> roles_help()
    'читатель (reader), исследователь (researcher), куратор (curator), админ (admin)'
    """
    english = {role: alias for alias, role in ROLE_ALIASES.items()}
    return ", ".join(f"{role} ({english[role]})" for role in ROLES)


def normalize_role(text: str) -> str:
    """Роль по-русски или по-английски, в любом регистре → русское имя роли (как в базе).

    Неизвестная роль — EditError со списком возможных.

    >>> normalize_role(" Админ "), normalize_role("CURATOR"), normalize_role("исследователь")
    ('админ', 'куратор', 'исследователь')
    >>> normalize_role("бог")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ...
    biobank.errors.EditError: Роли «бог» нет. Возможные: читатель (reader), ...
    """
    key = " ".join(str(text).split()).lower()
    if key in ROLES:
        return key
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    raise EditError(f"Роли «{' '.join(str(text).split())}» нет. Возможные: {roles_help()}.")


# Действия
VIEW = "просмотр"
CREATE = "создание"
UPDATE = "изменение"
DELETE = "удаление"
RESOLVE = "закрытие проблемы"  # data_issue: отметить решённой
MERGE_PEOPLE = "слияние людей"  # справочник person: два человека → один
MANAGE_USERS = "пользователи"  # учётные записи и роли
ACTIONS = (VIEW, CREATE, UPDATE, DELETE, RESOLVE, MERGE_PEOPLE, MANAGE_USERS)

USER_TABLES = ("app_user", "user_role")  # их меняет только админ
SYSTEM_TABLES = ("change_log",)  # пишет только программа, руками — никто


@dataclass(frozen=True)
class User:
    """Пользователь сайта (без хеша пароля: он нужен только users.authenticate)."""

    user_id: int
    login: str
    display_name: str
    person_id: int | None
    is_active: bool
    must_change_password: bool
    roles: frozenset[str]
    created_at: str | None = None
    last_login_at: str | None = None
    # версия сессий: сайт хранит её в сессии и сверяет на каждом запросе; +1 при выходе,
    # смене/сбросе пароля, закрытии входа — все прежние сессии недействительны
    session_version: int = 0

    def has(self, role: str) -> bool:
        return role in self.roles


def load_user(conn: sqlite3.Connection, user_id: int) -> User:
    """Пользователь по номеру — свежие данные из базы (роли, активен ли). Нет — NotFoundError."""
    row = conn.execute(
        "SELECT user_id, login, display_name, person_id, is_active, must_change_password, "
        "created_at, last_login_at, session_version FROM app_user WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Пользователя №{user_id} нет.")
    roles = frozenset(
        r[0] for r in conn.execute("SELECT role FROM user_role WHERE user_id = ?", (user_id,))
    )
    return User(
        row["user_id"], row["login"], row["display_name"], row["person_id"],
        bool(row["is_active"]), bool(row["must_change_password"]), roles,
        row["created_at"], row["last_login_at"], row["session_version"],
    )  # fmt: skip


def _field(record: Mapping | sqlite3.Row | None, column: str):
    """Значение колонки записи; нет записи или колонки — None."""
    if record is None:
        return None
    try:
        return record[column]
    except (KeyError, IndexError):
        return None


def _owner(record: Mapping | sqlite3.Row | None) -> int | None:
    """created_by записи (кто внёс); нет колонки или записи — None (= «из Excel»)."""
    return _field(record, "created_by")


def can(
    user: User | None,
    action: str,
    table: str | None = None,
    record: Mapping | sqlite3.Row | None = None,
) -> bool:
    """Можно ли пользователю действие action с таблицей table (и записью record).

    record нужен для изменения и удаления: исследователь правит только записи,
    у которых created_by = его user_id. user=None (не вошёл) — ничего нельзя.

    >>> r = User(7, "ivanova", "Иванова А.", None, True, False, frozenset({RESEARCHER}))
    >>> can(r, CREATE, "specimen"), can(r, UPDATE, "specimen", {"created_by": 7})
    (True, True)
    >>> can(r, UPDATE, "specimen", {"created_by": None}), can(r, RESOLVE, "data_issue")
    (False, False)
    >>> can(r, RESOLVE, "data_issue", {"source": "пользователь", "created_by": 7})
    True
    >>> a = User(1, "admin", "Админ", None, True, False, frozenset({ADMIN}))
    >>> can(a, CREATE, "specimen"), can(a, MANAGE_USERS), can(a, VIEW, "specimen")
    (False, True, True)
    """
    if user is None or not user.is_active:
        return False
    if action == VIEW:
        return True  # видимость данных одинакова для всех вошедших
    if action == MANAGE_USERS or table in USER_TABLES:
        return user.has(ADMIN)
    if table in SYSTEM_TABLES:
        return False
    if action == RESOLVE:
        if user.has(CURATOR):
            return True
        return (
            user.has(RESEARCHER)
            and _field(record, "source") == "пользователь"
            and _owner(record) is not None
            and _owner(record) == user.user_id
        )
    if action == MERGE_PEOPLE:
        return user.has(CURATOR)
    if action == CREATE:
        return user.has(CURATOR) or user.has(RESEARCHER)
    if action in (UPDATE, DELETE):
        if user.has(CURATOR):
            return True
        owner = _owner(record)
        return user.has(RESEARCHER) and owner is not None and owner == user.user_id
    raise ValueError(f"Неизвестное действие «{action}». Возможные: {', '.join(ACTIONS)}.")


def can_edit_data(user: User | None) -> bool:
    """Может ли пользователь вообще что-то менять в данных (исследователь или куратор)."""
    return can(user, CREATE, "specimen")


def denial_message(user: User, action: str, table: str | None = None, record=None) -> str:
    """Понятный текст отказа (для PermissionDenied)."""
    if not user.is_active:
        return "Вход для этого пользователя закрыт — изменения недоступны."
    if action == MANAGE_USERS or table in USER_TABLES:
        return "Пользователей и роли меняет только админ."
    if action == RESOLVE:
        if user.has(RESEARCHER):
            return (
                "Исследователь закрывает только проблемы, которые добавил сам; проблемы "
                "импорта, проверок и других пользователей закрывает куратор."
            )
        return "Закрывать проблемы журнала может только куратор."
    if action == MERGE_PEOPLE:
        return "Объединять людей в справочнике может только куратор."
    if action in (UPDATE, DELETE) and user.has(RESEARCHER):
        verb = "править" if action == UPDATE else "удалять"
        if _owner(record) is None:
            return (
                f"Записи, перенесённые из Excel, может {verb} только куратор. "
                "Если нашли ошибку — напишите куратору или добавьте проблему в журнал."
            )
        return f"Чужие записи может {verb} только куратор (эту внёс другой пользователь)."
    return "Нет прав на изменение данных: нужна роль «исследователь» или «куратор»."


def require(user: User, action: str, table: str | None = None, record=None) -> None:
    """can() или PermissionDenied с понятным текстом."""
    if not can(user, action, table, record):
        raise PermissionDenied(denial_message(user, action, table, record))
