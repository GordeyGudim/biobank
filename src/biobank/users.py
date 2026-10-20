"""Пользователи сайта: учётные записи, пароли, роли, вход.

Пароль в базе не хранится — только его хеш (hash_password). Хеш — результат функции
scrypt: по паролю его посчитать легко, а по хешу пароль не восстановить; scrypt специально
медленная и требует много памяти, чтобы подбор паролей перебором был дорогим.
Соль — случайные байты, свои у каждого пароля: одинаковые пароли дают разные хеши.
Строка хеша хранит и параметры scrypt ('scrypt$16384$8$1$соль$хеш'), поэтому стоимость
можно будет поднять позже: старые хеши проверяются по своим параметрам, а при следующем
входе пересчитываются по новым (needs_rehash).

Отзыв сессий: у учётной записи есть session_version. Сайт кладёт в сессию
(user_id, session_version) и на каждом запросе сверяет с базой (session_is_valid). Выход
(logout), смена и сброс пароля, закрытие входа увеличивают версию на 1 — все прежние
сессии этого человека (на всех устройствах, и скопированная cookie) недействительны.

Кто что может (permissions.py): пользователями и ролями управляет только админ
(user=None — системный режим команд CLI: так создаётся первый админ). Свой пароль меняет
каждый (change_own_password). В системе всегда остаётся хотя бы один активный админ.
Все изменения — в журнале изменений (change_log), хеш пароля туда не попадает.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Iterable

from biobank import audit
from biobank.audit import LOGIN_MARK, SELF, USERS, UserRef, write_transaction
from biobank.db import now_utc
from biobank.errors import EditError, NotFoundError, PermissionDenied
from biobank.permissions import ADMIN, ROLES, User, load_user, normalize_role

# Параметры scrypt: n — стоимость (степень двойки), r — размер блока, p — параллельность.
# Память ≈ 128·n·r байт = 32 МБ; время на сервере — порядка 0,1 с на одну проверку.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SCRYPT_MAXMEM = 128 * 1024 * 1024  # запас, чтобы n можно было поднять
SALT_BYTES = 16

MIN_PASSWORD = 10
MAX_PASSWORD = 256  # очень длинная строка — не пароль, а ошибка или атака
LOGIN_RE = re.compile(r"[A-Za-z0-9._-]{2,64}")
LOGIN_FAILED = "Неверный логин или пароль."
# в временном пароле нет похожих символов (0/O, 1/l/I)
TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


# ---------------------------------------------------------------------------
# Хеш пароля
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """Хеш пароля со случайной солью: 'scrypt$n$r$p$соль$хеш' (base64).

    >>> h = hash_password("правильный пароль")
    >>> h.startswith("scrypt$32768$8$1$"), verify_password("правильный пароль", h)
    (True, True)
    >>> verify_password("неправильный", h), h == hash_password("правильный пароль")
    (False, False)
    """
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )  # fmt: skip
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def _parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        return n, r, p, base64.b64decode(parts[4]), base64.b64decode(parts[5])
    except ValueError:
        return None


def verify_password(password: str, stored: str) -> bool:
    """Совпадает ли пароль с хешем. Сравнение — hmac.compare_digest: время сравнения
    не зависит от того, в каком байте первое отличие (иначе по времени можно подбирать)."""
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
        maxmem=SCRYPT_MAXMEM,
    )  # fmt: skip
    return hmac.compare_digest(digest, expected)


def needs_rehash(stored: str) -> bool:
    """Хеш посчитан с другими (старыми) параметрами — пересчитать при следующем входе.

    >>> needs_rehash(hash_password("x" * 10)), needs_rehash("scrypt$1024$8$1$AA==$AA==")
    (False, True)
    """
    parsed = _parse_hash(stored)
    return parsed is None or parsed[:3] != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


# Хеш-«пустышка»: для несуществующего логина authenticate проверяет пароль с ним,
# чтобы ответ шёл столько же времени, сколько для существующего (по времени ответа
# нельзя было узнать, есть ли такой логин). Считается при загрузке модуля — тогда и самый
# первый вход после запуска сайта не медленнее остальных.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def check_password_rules(password: str, login: str | None = None) -> None:
    """Правила пароля: не короче MIN_PASSWORD символов, не длиннее MAX_PASSWORD, не равен
    логину. Нарушение — EditError.

    >>> check_password_rules("длинный-пароль-1")
    >>> check_password_rules("короткий")
    Traceback (most recent call last):
    ...
    biobank.errors.EditError: Пароль слишком короткий: нужно не меньше 10 символов (сейчас 8).
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        size = len(password) if isinstance(password, str) else 0
        raise EditError(
            f"Пароль слишком короткий: нужно не меньше {MIN_PASSWORD} символов (сейчас {size})."
        )
    if len(password) > MAX_PASSWORD:
        raise EditError(f"Пароль слишком длинный: не больше {MAX_PASSWORD} символов.")
    if login is not None and password.strip().lower() == login.strip().lower():
        raise EditError("Пароль не должен совпадать с логином.")


def generate_temporary_password() -> str:
    """Временный пароль: 3 группы по 4 символа, без похожих букв и цифр ('k7mf-2xqa-p9td').

    >>> p = generate_temporary_password(); len(p), p.count("-")
    (14, 2)
    """
    groups = ["".join(secrets.choice(TEMP_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


def _find_id(conn: sqlite3.Connection, target: int | str | User) -> int:
    """user_id по номеру, логину (без учёта регистра) или объекту User; нет — NotFoundError."""
    if isinstance(target, User):
        target = target.user_id
    if isinstance(target, int) and not isinstance(target, bool):
        row = conn.execute("SELECT user_id FROM app_user WHERE user_id = ?", (target,)).fetchone()
    else:
        row = conn.execute(
            "SELECT user_id FROM app_user WHERE login = ?", (str(target).strip(),)
        ).fetchone()
    if row is None:
        raise NotFoundError(f"Пользователя «{target}» нет.")
    return row[0]


def get_user(conn: sqlite3.Connection, target: int | str) -> User:
    """Пользователь по номеру (int) или логину (str). Нет — NotFoundError. Хеша в User нет."""
    return load_user(conn, _find_id(conn, target))


def list_users(conn: sqlite3.Connection) -> list[User]:
    """Все пользователи по логину (и закрытые — у них is_active False)."""
    ids = [r[0] for r in conn.execute("SELECT user_id FROM app_user ORDER BY login")]
    return [load_user(conn, user_id) for user_id in ids]


def _active_admins(conn: sqlite3.Connection) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT u.user_id FROM app_user u JOIN user_role r USING (user_id) "
            "WHERE u.is_active = 1 AND r.role = ?",
            (ADMIN,),
        )
    }


def _keep_an_admin(conn: sqlite3.Connection, user_id: int) -> None:
    """Отказ, если user_id — последний активный админ (после правки админов бы не осталось)."""
    if _active_admins(conn) == {user_id}:
        raise EditError(
            "Это последний активный админ: без него никто не сможет управлять пользователями. "
            "Сначала назначьте админом другого пользователя."
        )


def _clean_roles(roles: Iterable[str]) -> list[str]:
    if isinstance(roles, str):
        roles = [roles]
    # по-русски или по-английски, в любом регистре; неизвестная — EditError со списком
    cleaned = list(dict.fromkeys(normalize_role(r) for r in roles))
    if not cleaned:
        raise EditError(f"Укажите хотя бы одну роль: {', '.join(ROLES)}.")
    return cleaned


# ---------------------------------------------------------------------------
# Изменение (только админ или системный режим)
# ---------------------------------------------------------------------------


def create_user(
    conn: sqlite3.Connection,
    login: str,
    password: str,
    display_name: str,
    roles: Iterable[str],
    person_id: int | None = None,
    *,
    must_change_password: bool = True,
    user: UserRef = None,
) -> User:
    """Новая учётная запись. Возвращает созданного пользователя.

    login — латиница, цифры, «.», «_», «-» (2–64 символа), без учёта регистра уникален;
    display_name — как подписывать в карточках («Иванова А.»); person_id — этот же человек
    в справочнике людей (если есть). must_change_password — при первом входе сайт попросит
    сменить пароль (так, если пароль придумал админ).
    """
    login = (login or "").strip()
    if not LOGIN_RE.fullmatch(login):
        raise EditError(
            f"Логин «{login}» не подходит: 2–64 символа — латинские буквы, цифры, «.», «_», «-»."
        )
    display_name = " ".join((display_name or "").split())
    if not display_name:
        raise EditError("Укажите имя для подписи (например «Иванова А.»).")
    check_password_rules(password, login)
    roles = _clean_roles(roles)
    with write_transaction(conn, user, scope=USERS):
        if conn.execute("SELECT 1 FROM app_user WHERE login = ?", (login,)).fetchone():
            raise EditError(f"Логин «{login}» уже занят.")
        if (
            person_id is not None
            and not conn.execute(
                "SELECT 1 FROM person WHERE person_id = ?", (person_id,)
            ).fetchone()
        ):
            raise NotFoundError(f"Человека №{person_id} нет в справочнике людей.")
        user_id = audit.insert(
            conn, "app_user", login=login, password_hash=hash_password(password),
            display_name=display_name, person_id=person_id, is_active=1,
            must_change_password=int(bool(must_change_password)),
        )  # fmt: skip
        for role in roles:
            audit.insert(conn, "user_role", user_id=user_id, role=role)
        return load_user(conn, user_id)


def set_roles(
    conn: sqlite3.Connection, target: int | str, roles: Iterable[str], *, user: UserRef = None
) -> User:
    """Заменить роли пользователя набором roles (хотя бы одна).

    Снять роль «админ» с последнего активного админа нельзя — EditError.
    """
    roles = _clean_roles(roles)
    with write_transaction(conn, user, scope=USERS):
        user_id = _find_id(conn, target)
        current = {r[0] for r in conn.execute("SELECT role FROM user_role WHERE user_id = ?",
                                              (user_id,))}  # fmt: skip
        if ADMIN in current and ADMIN not in roles:
            _keep_an_admin(conn, user_id)
        for role in sorted(current - set(roles)):
            audit.delete(conn, "user_role", {"user_id": user_id, "role": role})
        for role in roles:
            if role not in current:
                audit.insert(conn, "user_role", user_id=user_id, role=role)
        return load_user(conn, user_id)


def deactivate(conn: sqlite3.Connection, target: int | str, *, user: UserRef = None) -> User:
    """Закрыть вход (is_active = 0). Записи и история пользователя остаются.

    Сессия на сайте перестаёт действовать сразу: права проверяются по свежим данным из базы.
    Закрыть вход последнему активному админу нельзя — EditError.
    """
    with write_transaction(conn, user, scope=USERS):
        user_id = _find_id(conn, target)
        _keep_an_admin(conn, user_id)
        audit.update(
            conn, "app_user", {"user_id": user_id},
            {"is_active": 0, "session_version": _next_version(conn, user_id)},
        )  # fmt: skip
        return load_user(conn, user_id)


def activate(conn: sqlite3.Connection, target: int | str, *, user: UserRef = None) -> User:
    """Открыть вход снова (is_active = 1)."""
    with write_transaction(conn, user, scope=USERS):
        user_id = _find_id(conn, target)
        audit.update(conn, "app_user", {"user_id": user_id}, {"is_active": 1})
        return load_user(conn, user_id)


def set_password(
    conn: sqlite3.Connection, target: int | str, password: str, *, user: UserRef = None
) -> User:
    """Админ задаёт пользователю пароль; при входе сайт попросит его сменить."""
    with write_transaction(conn, user, scope=USERS):
        user_id = _find_id(conn, target)
        login = conn.execute("SELECT login FROM app_user WHERE user_id = ?", (user_id,)).fetchone()
        check_password_rules(password, login[0])
        audit.update(
            conn, "app_user", {"user_id": user_id},
            {"password_hash": hash_password(password), "must_change_password": 1,
             "session_version": _next_version(conn, user_id)},
        )  # fmt: skip
        return load_user(conn, user_id)


def reset_password(conn: sqlite3.Connection, target: int | str, *, user: UserRef = None) -> str:
    """Новый временный пароль (вернуть, чтобы показать ОДИН раз); при входе — сменить."""
    temporary = generate_temporary_password()
    set_password(conn, target, temporary, user=user)
    return temporary


def change_own_password(
    conn: sqlite3.Connection, user: User | int, old_password: str, new_password: str
) -> User:
    """Пользователь меняет свой пароль: нужен прежний. После — must_change_password = 0.

    Версия сессий +1: прежние сессии (и текущая) недействительны — сайт после смены
    пароля заново кладёт в сессию новую версию (из возвращённого User).

    Неверный прежний пароль — EditError; новый — по правилам (check_password_rules) и
    не равен прежнему.
    """
    if user is None:
        raise PermissionDenied("Сменить свой пароль может только вошедший пользователь.")
    if new_password == old_password:
        raise EditError("Новый пароль совпадает с прежним — придумайте другой.")
    with write_transaction(conn, user, scope=SELF) as actor:
        stored = conn.execute(
            "SELECT password_hash FROM app_user WHERE user_id = ?", (actor.user_id,)
        ).fetchone()[0]
        if not verify_password(old_password, stored):
            raise EditError("Прежний пароль неверный.")
        check_password_rules(new_password, actor.login)
        audit.update(  # своя учётная запись: права админа не нужны
            conn, "app_user", {"user_id": actor.user_id},
            {"password_hash": hash_password(new_password), "must_change_password": 0,
             "session_version": _next_version(conn, actor.user_id)},
            derived=True,
        )  # fmt: skip
        return load_user(conn, actor.user_id)


def _next_version(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute("SELECT session_version FROM app_user WHERE user_id = ?", (user_id,))
    return row.fetchone()[0] + 1


def bump_session_version(
    conn: sqlite3.Connection, target: int | str, *, user: UserRef = None
) -> User:
    """«Выйти на всех устройствах»: session_version + 1, все сессии target недействительны.

    Свою — любой вошедший (и с временным паролем), чужую — только админ (user=None —
    системный режим CLI). В журнал изменений — как изменение поля session_version.
    """
    own = user is not None and _find_id(conn, target) == (
        user.user_id if isinstance(user, User) else user
    )
    with write_transaction(conn, user, scope=SELF if own else USERS):
        user_id = _find_id(conn, target)
        audit.update(  # своя учётная запись — права админа не нужны
            conn, "app_user", {"user_id": user_id},
            {"session_version": _next_version(conn, user_id)}, derived=own,
        )  # fmt: skip
        return load_user(conn, user_id)


def logout(conn: sqlite3.Connection, user: User | int) -> User:
    """Выход с сайта: то же, что bump_session_version для себя — «Выйти» выкидывает все
    устройства этого человека (и скопированную раньше cookie). Сессию очищает сайт."""
    if user is None:
        raise PermissionDenied("Выйти может только вошедший пользователь.")
    return bump_session_version(conn, user.user_id if isinstance(user, User) else user, user=user)


def session_is_valid(user: User | None, session_version: int | None) -> bool:
    """Сессия ещё действует: пользователь есть, вход открыт и версия совпадает.

    user — свежие данные из базы (get_user по user_id из сессии), session_version — из сессии.

    >>> u = User(1, "a", "А", None, True, False, frozenset(), session_version=2)
    >>> session_is_valid(u, 2), session_is_valid(u, 1), session_is_valid(None, 2)
    (True, False, False)
    """
    return (
        user is not None
        and user.is_active
        and session_version is not None
        and user.session_version == session_version
    )


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


def authenticate(
    conn: sqlite3.Connection, login: str, password: str, *, record_login: bool = True
) -> User | None:
    """Проверить логин и пароль: пользователь — или None (отказ).

    Отказ одинаковый (None; текст для человека — LOGIN_FAILED) при любой причине:
    нет такого логина, неверный пароль, вход закрыт (is_active = 0) — по ответу нельзя
    узнать, какие логины существуют. Хеш проверяется всегда, даже если логина нет
    (с хешем-пустышкой), чтобы и время ответа не отличалось.
    record_login — записать время входа (last_login_at); подключение тогда — на запись
    (connect, не connect_read_only: тот хеш пароля не отдаёт) и БЕЗ открытой транзакции —
    внутри чужой транзакции вызывать нельзя (write_transaction откажет EditError).
    Время входа и пересчёт хеша (needs_rehash) в журнал изменений не пишутся — это не правка.
    Ограничение числа попыток (защита от подбора) — на стороне сайта.
    """
    login = (login or "").strip()
    password = password if isinstance(password, str) else ""
    row = conn.execute(
        "SELECT user_id, password_hash, is_active FROM app_user WHERE login = ?", (login,)
    ).fetchone()
    stored = row["password_hash"] if row else _DUMMY_HASH
    ok = len(password) <= MAX_PASSWORD and verify_password(password, stored)
    if row is None or not ok or not row["is_active"]:
        return None
    if record_login:
        with write_transaction(conn, scope=LOGIN_MARK):  # системная отметка, не правка
            values = [now_utc()]
            sql = "UPDATE app_user SET last_login_at = ?"
            if needs_rehash(stored):
                sql += ", password_hash = ?"
                values.append(hash_password(password))
            conn.execute(sql + " WHERE user_id = ?", (*values, row["user_id"]))
    return load_user(conn, row["user_id"])
