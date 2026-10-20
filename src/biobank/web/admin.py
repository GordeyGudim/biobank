"""Страницы админа: пользователи сайта и их роли (только роль «админ»).

Всё делают функции users.py (create_user, set_roles, reset_password, deactivate,
activate) — они сами проверяют, что действует админ, пишут журнал изменений и не дают
остаться без активного админа (тогда — EditError с объяснением, его видно у формы).

Временный пароль показывается ОДИН раз — прямо в ответе на POST (страница результата,
Cache-Control: no-store, без перенаправления). Нигде больше он не хранится: ни в cookie
(сообщения flash лежат в cookie, её можно прочитать), ни в памяти сервера — поэтому так
работает и при нескольких процессах сервера. Обычное правило «после записи —
перенаправление» здесь нарушено нарочно: иначе пароль пришлось бы где-то сохранить до
следующего запроса. F5 на этой странице браузер переспросит; повтор создания откажет
(«логин занят»), повтор сброса выдаст новый пароль — прежний перестанет действовать.
"""

from __future__ import annotations

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from biobank import users
from biobank.errors import EditError, NotFoundError, PermissionDenied
from biobank.permissions import MANAGE_USERS, ROLES
from biobank.web.auth import current_user, need, start_session, write_db

ROLE_HINTS = {
    "читатель": "только смотрит",
    "исследователь": "вносит записи, правит и удаляет свои",
    "куратор": "правит любые записи, закрывает проблемы, объединяет людей",
    "админ": "только пользователи и роли (данные не правит)",
}


def register_admin_routes(app: Flask, pages) -> None:
    """Маршруты /admin/users… pages — модуль app.py (read_db)."""

    def users_page(form=None, error=None, status=200, note=None):
        return render_template(
            "admin_users.html",
            users=users.list_users(pages.read_db()),
            roles=ROLES,
            role_hints=ROLE_HINTS,
            form=form or {"roles": []},
            error=error,
            note=note,
        ), status

    def user_page(user_id: int, errors=None, status=200, note=None):
        target = users.get_user(pages.read_db(), user_id)  # нет такого — 404
        return render_template(
            "admin_user.html",
            target=target,
            roles=ROLES,
            role_hints=ROLE_HINTS,
            errors=errors or {},
            note=note,
        ), status

    def run(action):
        """users.*(conn, …, user=админ) на подключении на запись; нет прав — 403."""
        try:
            with write_db() as conn:
                return action(conn, current_user())
        except PermissionDenied as error:
            abort(403, description=str(error))

    @app.get("/admin/users")
    def admin_users():
        need(MANAGE_USERS)
        return users_page()

    @app.post("/admin/users/new")
    def admin_user_new():
        need(MANAGE_USERS)
        form = {
            "login": " ".join(request.form.get("login", "").split()),
            "display_name": " ".join(request.form.get("display_name", "").split()),
            "roles": request.form.getlist("roles"),
            "password_mode": request.form.get("password_mode", "generate"),
        }
        if form["password_mode"] == "manual":
            password = request.form.get("password", "")
        else:
            password = users.generate_temporary_password()
        try:
            created = run(
                lambda conn, admin: users.create_user(
                    conn, form["login"], password, form["display_name"], form["roles"],
                    must_change_password=True, user=admin,
                )
            )  # fmt: skip
        except EditError as error:  # логин занят, пароль короткий, роль не выбрана…
            return users_page(form, str(error), 400)
        note = {"login": created.login, "name": created.display_name, "created": True}
        if form["password_mode"] != "manual":
            note["password"] = password
        # ответ на POST — сразу страница с паролем (см. описание модуля)
        return users_page(note=note)

    @app.get("/admin/users/<int:user_id>")
    def admin_user(user_id: int):
        need(MANAGE_USERS)
        return user_page(user_id)

    @app.post("/admin/users/<int:user_id>/roles")
    def admin_user_roles(user_id: int):
        need(MANAGE_USERS)
        roles = request.form.getlist("roles")
        try:
            changed = run(lambda conn, admin: users.set_roles(conn, user_id, roles, user=admin))
        except NotFoundError:
            raise
        except EditError as error:  # последний админ, ни одной роли…
            return user_page(user_id, {"roles": str(error)}, 400)
        flash(f"Роли {changed.login}: {', '.join(sorted(changed.roles))}.", "ok")
        return redirect(url_for("admin_user", user_id=user_id), code=303)

    @app.post("/admin/users/<int:user_id>/reset-password")
    def admin_user_reset(user_id: int):
        need(MANAGE_USERS)
        try:
            password = run(lambda conn, admin: users.reset_password(conn, user_id, user=admin))
        except NotFoundError:
            raise
        except EditError as error:
            return user_page(user_id, {"password": str(error)}, 400)
        target = users.get_user(pages.read_db(), user_id)
        note = {"login": target.login, "name": target.display_name, "password": password}
        refresh_own_session(target)
        return user_page(user_id, note=note)  # пароль — прямо в ответе (см. описание модуля)

    def refresh_own_session(target):
        """Админ отозвал сессии самому себе — его текущая сессия получает новую версию,
        чтобы он не вышел посреди работы (остальные его устройства выйдут)."""
        if target.user_id == current_user().user_id:
            start_session(target)

    @app.post("/admin/users/<int:user_id>/sessions")
    def admin_user_sessions(user_id: int):
        """«Завершить все сессии»: пользователь выходит на всех устройствах."""
        need(MANAGE_USERS)
        try:
            changed = run(lambda conn, admin: users.bump_session_version(conn, user_id, user=admin))
        except NotFoundError:
            raise
        except EditError as error:
            return user_page(user_id, {"sessions": str(error)}, 400)
        refresh_own_session(changed)
        flash(f"Все сессии {changed.login} завершены: войти нужно заново.", "ok")
        return redirect(url_for("admin_user", user_id=user_id), code=303)

    @app.post("/admin/users/<int:user_id>/active")
    def admin_user_active(user_id: int):
        need(MANAGE_USERS)
        open_login = request.form.get("active") == "1"
        function = users.activate if open_login else users.deactivate
        try:
            changed = run(lambda conn, admin: function(conn, user_id, user=admin))
        except NotFoundError:
            raise
        except EditError as error:  # последний активный админ
            return user_page(user_id, {"active": str(error)}, 400)
        state = "открыт" if open_login else "закрыт (сессии перестали действовать)"
        flash(f"Вход для {changed.login} {state}.", "ok")
        return redirect(url_for("admin_user", user_id=user_id), code=303)
