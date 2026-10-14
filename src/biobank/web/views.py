"""Помощники отображения для сайта: ссылки по id записей и разбор фильтров из адреса.

Здесь нет SQL и нет правил предметной области — только «как показать»:
- какая ячейка таблицы становится ссылкой и куда она ведёт (по CardSection.row_ids);
- как прочитать фильтры из адреса страницы (GET-параметры) и собрать их обратно
  в адрес для ссылок «следующая страница».

GET-параметры — часть адреса после «?»: /specimens?expedition=1&page=2. Фильтры живут
в адресе, поэтому ссылкой на отфильтрованный список можно поделиться.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from flask import url_for

from biobank.tables import CardSection

# ---------------------------------------------------------------------------
# Ссылки в таблицах карточек и отчётов
# ---------------------------------------------------------------------------

# В какой колонке раздела-списка ставить ссылку для каждого вида id из row_ids.
# Первая найденная колонка из перечня; нет такой колонки — ссылки нет.
LINK_COLUMNS = {
    "specimen_id": ("№ пробы",),
    "species_id": ("вид",),
    "event_id": ("выезд",),
    "expedition_id": ("экспедиция",),
    "trawling_id": ("трал", "траление", "№"),
    "issue_id": ("id",),
}


@dataclass
class SectionView:
    """Раздел (CardSection) + где в нём ссылки и кнопки.

    links — по словарю на строку: номер колонки → адрес ссылки;
    anchors — по строке: id HTML-строки (для ссылок вида «…#trawling-5») или None;
    actions — по строке: кнопки [(текст, адрес)…] — «Изменить», «Удалить» (пусто — нет).
    """

    section: CardSection
    links: list[dict[int, str]] = field(default_factory=list)
    anchors: list[str | None] = field(default_factory=list)
    actions: list[list[tuple[str, str]]] = field(default_factory=list)

    @property
    def has_actions(self) -> bool:
        """Нужна ли колонка «действия» (хоть у одной строки есть кнопки)."""
        return any(self.actions)


def issue_url(issue_id: int) -> str:
    """Адрес страницы одной проблемы журнала (/issues/N)."""
    return url_for("issue", issue_id=issue_id)


# Какой записи принадлежит строка таблицы — по id в row_ids, в порядке «от частного к общему»:
# у строки образца есть и sample_id, у строки замера — и trawling_id (ссылка на траление),
# но кнопки «Изменить»/«Удалить» относятся к самому образцу или замеру.
# Значение — вид записи в адресах /edit/<вид>/<id> и /delete/<вид>/<id> (record_forms.py).
ROW_KINDS = (
    ("identification_id", "identification"),
    ("sample_id", "sample"),
    ("measurement_id", "water"),
    ("catch_id", "catch"),
    ("specimen_id", "specimen"),
    ("trawling_id", "trawling"),
)
# Строки с такими id — не записи, которые здесь правят (проблема журнала, результат анализа)
NO_ACTIONS = ("issue_id", "analysis_id")
# Записи, которые можно только удалить (правки нет: вид меняют новым определением)
DELETE_ONLY = ("identification",)


def local_path(value: str | None) -> str | None:
    r"""Адрес «куда вернуться» (параметр next), если это путь на этом же сайте; иначе None.

    Защита от «открытого перенаправления» (open redirect): чужая ссылка вида
    /edit/…?next=https://evil.example не должна уводить пользователя после сохранения
    на посторонний сайт. Принимаем только путь: один «/» в начале, без схемы (https:)
    и хоста. «//evil» и «/\evil» браузер понимает как адрес другого сайта, а табуляции
    и переводы строк он выбрасывает («/<Tab>/evil» → «//evil») — такие адреса отклоняем.

    >>> local_path("/events/3"), local_path("/specimens?event=3&page=2")
    ('/events/3', '/specimens?event=3&page=2')
    >>> local_path("https://evil"), local_path("//evil"), local_path("/\\evil"), local_path("")
    (None, None, None, None)
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def row_actions(ids: Mapping, here: str | None = None) -> list[tuple[str, str]]:
    """Кнопки строки таблицы: «Изменить» и «Удалить» для записи этой строки.

    here — адрес текущей страницы: передаётся в ссылки параметром next, чтобы после
    сохранения или удаления вернуться сюда же (None — на страницу самой записи).
    """
    if any(key in ids for key in NO_ACTIONS):
        return []
    back = {"next": here} if here else {}
    for key, kind in ROW_KINDS:
        record_id = ids.get(key)
        if record_id is None:
            continue
        buttons = []
        if kind not in DELETE_ONLY:
            buttons.append(
                ("Изменить", url_for("record_edit", kind_name=kind, record_id=record_id, **back))
            )
        buttons.append(
            ("Удалить", url_for("record_delete", kind_name=kind, record_id=record_id, **back))
        )
        return buttons
    return []


def _url(key: str, value, row: Mapping, event_id: int | None) -> str | None:
    """Адрес записи по виду id (key) и значению; None — ссылки нет."""
    if key == "species_id":
        # вид не определён — тоже ссылка: список особей без вида
        return url_for("specimens", species=value if value is not None else "none")
    if value is None:
        return None
    if key == "specimen_id":
        label = row.get("№ пробы") or row.get("номер пробы")
        return url_for("specimen", label=str(label)) if label else None
    if key == "event_id":
        return url_for("event", event_id=value)
    if key == "expedition_id":
        return url_for("expedition", expedition_id=value)
    if key == "site_id":
        return url_for("site", site_id=value)
    if key == "trawling_id":
        return url_for("trawling", trawling_id=value)
    if key == "issue_id":
        return issue_url(value)
    return None


def section_views(
    sections: list[CardSection],
    *,
    own_key: str | None = None,
    event_id: int | None = None,
    id_column: str | None = None,
    actions: bool = False,
    here: str | None = None,
) -> list[SectionView]:
    """Ссылки для всех разделов страницы.

    own_key — id самой страницы (в карточке выезда — «event_id»): на себя не ссылаемся;
    event_id — выезд страницы (оставлен для совместимости, ссылки от него не зависят);
    id_column — что лежит в колонке «id» отчёта без row_ids («issue_id», «site_id»);
    actions — у строк-записей кнопки «Изменить»/«Удалить» (карточки; в отчётах — нет);
    here — адрес страницы: туда кнопки вернут после сохранения/удаления (параметр next).
    Строки таблицы «Траления» (только trawling_id) получают якорь trawling-N.
    """
    views = []
    for sec in sections:
        view = SectionView(sec)
        for i, row in enumerate(sec.rows):
            ids = dict(sec.row_ids[i]) if i < len(sec.row_ids) else {}
            links: dict[int, str] = {}
            if sec.is_fields:
                name, value = row
                for key, record_id in ids.items():
                    if key != own_key:
                        url = _url(key, record_id, {"номер пробы": value}, event_id)
                        if url:
                            links[1] = url
            else:
                named = dict(zip(sec.columns, row, strict=True))
                if id_column == "issue_id" and "id" in sec.columns:
                    ids.setdefault("issue_id", named["id"])
                for key, record_id in ids.items():
                    for column in LINK_COLUMNS.get(key, ()):
                        if column not in sec.columns:
                            continue
                        url = _url(key, record_id, named, event_id)
                        if url:
                            links[sec.columns.index(column)] = url
                        break
                if id_column == "site_id" and "id" in sec.columns:
                    links[sec.columns.index("id")] = _url("site_id", named["id"], named, None)
            view.links.append(links)
            view.actions.append(row_actions(ids, here) if actions and not sec.is_fields else [])
            only_trawl = set(ids) == {"trawling_id"} and ids["trawling_id"] is not None
            view.anchors.append(f"trawling-{ids['trawling_id']}" if only_trawl else None)
        views.append(view)
    return views


def field_ids(section: CardSection) -> dict[str, int | None]:
    """Все id из раздела «поле — значение» одним словарём: {'event_id': 3, 'site_id': 5…}."""
    merged: dict[str, int | None] = {}
    for ids in section.row_ids:
        merged.update(ids)
    return merged


# ---------------------------------------------------------------------------
# Фильтры из адреса страницы
# ---------------------------------------------------------------------------


@dataclass
class Query:
    """Разобранные GET-параметры списка.

    args — непустые параметры как в адресе (без page): из них строятся ссылки
    «следующая страница» и т.п.; kwargs — аргументы для функции browse;
    warnings — что не удалось понять (показываются над списком).
    """

    args: dict[str, str] = field(default_factory=dict)
    kwargs: dict = field(default_factory=dict)
    page: int = 1
    warnings: list[str] = field(default_factory=list)

    def text(self, name: str) -> str:
        """Непустое значение параметра (уже без лишних пробелов) или ''."""
        return self.args.get(name, "")

    def number(self, name: str, title: str) -> int | None:
        """Целое число из параметра name; не число — предупреждение, фильтр не учтён."""
        value = self.text(name)
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            self.warnings.append(f"Фильтр «{title}»: «{value}» — не номер, фильтр не учтён.")
            del self.args[name]
            return None


def read_query(params: Mapping[str, str], names: tuple[str, ...]) -> Query:
    """Взять из адреса параметры names (пустые отбросить) и номер страницы."""
    query = Query()
    for name in names:
        value = " ".join(str(params.get(name, "")).split())
        if value:
            query.args[name] = value
    try:
        query.page = max(int(params.get("page", "1")), 1)
    except ValueError:
        query.page = 1
    return query


SPECIMEN_PARAMS = ("expedition", "event", "site", "species", "method", "label", "issues", "order")
SPECIMEN_ORDERS = {"label": "по номеру", "date": "по дате выезда"}


def specimen_query(params: Mapping[str, str]) -> Query:
    """Фильтры списка особей → аргументы browse.list_specimens.

    species=none — «вид не определён»; issues=1 — «есть открытые проблемы»;
    order — label (по номеру) или date (по дате).
    """
    q = read_query(params, SPECIMEN_PARAMS)
    kw = q.kwargs
    kw["expedition_id"] = q.number("expedition", "экспедиция")
    kw["event_id"] = q.number("event", "выезд")
    kw["site_id"] = q.number("site", "место")
    if q.text("species") == "none":
        kw["unidentified"] = True
    else:
        kw["species_id"] = q.number("species", "вид")
    kw["capture_method"] = q.text("method") or None
    kw["label_prefix"] = q.text("label") or None
    if q.text("issues"):
        q.args["issues"] = "1"
        kw["has_open_issues"] = True
    order = q.text("order") or "label"
    if order not in SPECIMEN_ORDERS:
        q.warnings.append(f"Сортировка «{order}» не подходит — показано по номеру.")
        order = "label"
    if order == "label":
        q.args.pop("order", None)  # по номеру — порядок по умолчанию, в адрес не пишем
    kw["order"] = order
    kw["page"] = q.page
    return q


ISSUE_STATUSES = {"open": "открытые", "resolved": "решённые", "all": "все"}


def issue_query(params: Mapping[str, str]) -> Query:
    """Фильтры журнала проблем → аргументы browse.list_issues (status: open/resolved/all)."""
    q = read_query(params, ("category", "status"))
    status = q.text("status") or "open"
    if status not in ISSUE_STATUSES:
        q.warnings.append(f"Статус «{status}» не подходит — показаны открытые.")
        status = "open"
    q.args["status"] = status
    q.kwargs = {
        "category": q.text("category") or None,
        "resolved": {"open": False, "resolved": True, "all": None}[status],
        "page": q.page,
    }
    return q


# Как называть таблицы базы в журнале проблем
TABLE_TITLES = {
    "specimen": "особь",
    "sampling_event": "выезд",
    "sampling_site": "место",
    "trawling": "траление",
    "water_measurement": "замер воды",
    "sample": "образец",
    "sample_shipment": "отправка",
    "species_identification": "определение вида",
    "catch_record": "прилов",
}


def issue_object_url(issue: Mapping) -> str | None:
    """Ссылка на объект проблемы журнала, если её можно построить без запросов к базе."""
    record_id = issue.get("record_id")
    if record_id is None:
        return None
    table = issue.get("table_name")
    if table == "specimen" and issue.get("object_label"):
        return url_for("specimen", label=issue["object_label"])
    if table == "sampling_event":
        return url_for("event", event_id=record_id)
    if table == "sampling_site":
        return url_for("site", site_id=record_id)
    if table == "trawling":
        return url_for("trawling", trawling_id=record_id)
    return None
