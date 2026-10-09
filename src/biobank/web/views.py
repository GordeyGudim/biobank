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
    "trawling_id": ("трал", "траление"),
    "issue_id": ("id",),
}


@dataclass
class SectionView:
    """Раздел (CardSection) + где в нём ссылки.

    links — по словарю на строку: номер колонки → адрес ссылки;
    anchors — по строке: id HTML-строки (для ссылок вида «…#trawling-5») или None.
    """

    section: CardSection
    links: list[dict[int, str]] = field(default_factory=list)
    anchors: list[str | None] = field(default_factory=list)


def issue_url(issue_id: int, category: str | None = None, resolved: bool = False) -> str:
    """Адрес проблемы в журнале: её категория и статус + якорь #issue-N на строку.

    Якорь (часть адреса после «#») — браузер прокручивает страницу к элементу с таким id.
    """
    args = {"status": "resolved" if resolved else "open"}
    if category:
        args["category"] = category
    return url_for("issues", _anchor=f"issue-{issue_id}", **args)


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
        # отдельной страницы траления нет — строка в таблице «Траления» карточки выезда
        if event_id is None:
            return None
        return url_for("event", event_id=event_id, _anchor=f"trawling-{value}")
    if key == "issue_id":
        status = str(row.get("статус") or "")
        return issue_url(value, row.get("категория"), status.startswith("решено"))
    return None


def section_views(
    sections: list[CardSection],
    *,
    own_key: str | None = None,
    event_id: int | None = None,
    id_column: str | None = None,
) -> list[SectionView]:
    """Ссылки для всех разделов страницы.

    own_key — id самой страницы (в карточке выезда — «event_id»): на себя не ссылаемся;
    event_id — выезд, к которому относятся траления на странице (ссылка на его карточку);
    id_column — что лежит в колонке «id» отчёта без row_ids («issue_id», «site_id»).
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
    return None
