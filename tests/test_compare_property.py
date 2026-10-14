"""Свойства защиты импорта (importer.compare_databases) на случайных правках.

Гарантия: compare_databases(old, new) == [] тогда и только тогда, когда базы совпадают
с точностью до номеров записей (изоморфны: есть взаимно однозначное соответствие
записей каждой таблицы, при котором совпадают значения полей и ссылки ведут на
соответствующие записи).

1. «Любая правка находится»: 200 случайных правок свежей копии импорта (половина — после
   сдвига номеров shift_all), для каждой compare_databases(...) != [].
2. «Совпадающие базы дают пусто»: копия с перемешанными номерами всех записей и другим
   порядком вставки строк — compare_databases(...) == [].

Случайность воспроизводима: random.Random(SEED * 1000 + номер правки). В сообщении
падения — seed, номер правки и что именно изменено.

Как отличить настоящую правку от «пустой» (новое значение = старому, обмен двух
одинаковых значений, обмен названиями двух лабораторий с одинаковыми отправками):
у каждой базы считается «отпечаток» (invariant) — для каждой записи цвет из её полей,
цветов родителей (по ссылкам) и цветов детей; цвета уточняются проходами вверх и вниз по
ссылкам (это вариант известного теста изоморфизма графов Вейсфейлера — Лемана). У
изоморфных баз отпечатки всегда одинаковы, поэтому если отпечатки разные — базы точно
различаются, и защита обязана это заметить. Если отпечатки совпали — правка, скорее всего,
ничего не изменила по смыслу; её пропускаем (не проверяем) и берём другую. Ложного
падения так не бывает, а пропуски считаются и ограничены.

Отпечаток нужен только перепривязке и обмену (NEEDS_INVARIANT): у правки поля другое
значение одной записи меняет набор значений таблицы (сравниваем то, что SQLite записал
на самом деле), удаление и копия меняют число строк — такие базы различаются заведомо.
"""

import random
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

import pytest
from test_compare import shift_all

from biobank.db import SCHEMA_PATH, connect_read_only
from biobank.importer import compare_databases

SEED = 20261014
EDITS = 200  # проверенных правок (пропущенные «пустые» не в счёт)
KINDS = ("поле", "удаление", "копия", "ссылка", "обмен")
# правки, после которых база может остаться той же: их проверяем отпечатком
NEEDS_INVARIANT = ("ссылка", "обмен")
ATTEMPTS = 60  # попыток на одну правку: правка нарушила CHECK/UNIQUE/FK — берём другую
# ссылки без внешнего ключа: таблица → (колонка с именем таблицы, колонка с номером)
POLYMORPHIC = {"data_issue": ("table_name", "record_id")}


# ---- структура базы (читаем сами из PRAGMA, не из importer: тест не опирается на код,
# который проверяет) ----


@dataclass(frozen=True)
class Link:
    """Внешний ключ: колонки этой таблицы → колонки родителя (составной — одним ключом)."""

    parent: str
    columns: tuple[str, ...]
    parent_columns: tuple[str, ...]


def primary_key(conn, table):
    rows = conn.execute("SELECT name, pk FROM pragma_table_info(?)", (table,)).fetchall()
    return [r[0] for r in sorted(rows, key=lambda r: r[1]) if r[1]]


class Schema:
    """Таблицы, номера записей, внешние ключи, обычные колонки и UNIQUE каждой таблицы."""

    def __init__(self, conn):
        self.tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        self.columns, self.own_id, self.links, self.plain = {}, {}, {}, {}
        self.notnull, self.unique = {}, {}
        for table in self.tables:
            info = conn.execute(
                'SELECT name, "notnull" FROM pragma_table_info(?) ORDER BY cid', (table,)
            ).fetchall()
            self.columns[table] = [r[0] for r in info]
            self.notnull[table] = {r[0] for r in info if r[1]}
            key = primary_key(conn, table)
            self.own_id[table] = key[0] if len(key) == 1 else None
            groups = defaultdict(list)
            for r in conn.execute(
                'SELECT id, seq, "table", "from", "to" FROM pragma_foreign_key_list(?)', (table,)
            ):
                groups[r[0]].append(r)
            links = []
            for rows in groups.values():
                rows.sort(key=lambda r: r[1])
                parent = rows[0][2]
                to = tuple(r[4] for r in rows)
                if None in to:  # REFERENCES parent без колонок — первичный ключ родителя
                    to = tuple(primary_key(conn, parent))
                links.append(Link(parent, tuple(r[3] for r in rows), to))
            self.links[table] = sorted(links, key=lambda k: (k.parent, k.columns))
            refs = {c for k in links for c in k.columns}
            if table in POLYMORPHIC:
                refs.add(POLYMORPHIC[table][1])
            self.plain[table] = [
                c for c in self.columns[table] if c != self.own_id[table] and c not in refs
            ]
            unique = []
            for index in conn.execute('SELECT name, "unique" FROM pragma_index_list(?)', (table,)):
                if index[1]:
                    names = tuple(
                        r[0]
                        for r in conn.execute("SELECT name FROM pragma_index_info(?)", (index[0],))
                    )
                    if self.own_id[table] not in names:  # UNIQUE (trawling_id, event_id) — нет
                        unique.append(names)
            self.unique[table] = sorted(unique)
        self.order = self._order()

    def _order(self):
        """Таблицы по порядку: родители раньше детей, таблица с POLYMORPHIC — в конце."""
        parents = {t: {k.parent for k in self.links[t]} for t in self.tables}
        for table in POLYMORPHIC:
            parents[table] = set(self.tables) - {table}
        order = []
        while len(order) < len(self.tables):
            order += sorted(t for t in self.tables if t not in order and parents[t] <= set(order))
        return order

    def link_of(self, table, column):
        """Внешний ключ, в который входит колонка (первый по порядку), или None."""
        return next((k for k in self.links[table] if column in k.columns), None)


# ---- отпечаток базы: одинаков у баз, совпадающих с точностью до номеров ----


def invariant(conn, schema):
    """{таблица: Counter(цвет записи)} — см. пояснение в начале файла.

    Ссылка на запись — ребро графа; NULL, ссылка data_issue на таблицу без своего номера
    (sample_shipment) и ссылка на удалённую запись сравниваются как значения — так же,
    как их сравнивает compare_databases.
    """
    nodes = []  # (таблица, rowid, поля, родители)
    lookups = {}
    for table in schema.order:
        rows = conn.execute(f"SELECT rowid AS _r, * FROM {table}").fetchall()
        lookups[table] = rows
    index = {}  # (таблица, колонки) → {значения: rowid}

    def find(table, columns):
        if (table, columns) not in index:
            index[table, columns] = {tuple(r[c] for c in columns): r[0] for r in lookups[table]}
        return index[table, columns]

    for table in schema.order:
        for row in lookups[table]:
            parents = []
            for link in schema.links[table]:
                values = tuple(row[c] for c in link.columns)
                if None in values:  # нет ссылки; у (NULL, event_id) номер выезда не в счёт
                    parents.append(("значение", None))
                    continue
                found = find(link.parent, link.parent_columns).get(values)
                parents.append((link.parent, found) if found else ("значение", values))
            if table in POLYMORPHIC:
                name_column, id_column = POLYMORPHIC[table]
                target, record_id = row[name_column], row[id_column]
                own = schema.own_id.get(target)
                found = find(target, (own,)).get((record_id,)) if own and record_id else None
                parents.append((target, found) if found else ("значение", record_id))
            plain = (table, tuple(row[c] for c in schema.plain[table]))
            nodes.append(((table, row[0]), plain, parents))

    children = defaultdict(list)  # узел → [(слот ссылки, узел-ребёнок)]
    for key, _, parents in nodes:
        for slot, parent in enumerate(parents):
            if parent[0] != "значение":
                children[parent].append(((key[0], slot), key))
    colour = {key: hash(plain) for key, plain, _ in nodes}

    def parent_colour(parent):
        return hash(parent) if parent[0] == "значение" else colour[parent]

    for _ in range(2):  # проходы: вверх (предки), вниз (потомки) — дважды
        for key, _, parents in nodes:  # родители стоят раньше детей
            colour[key] = hash((colour[key], tuple(parent_colour(p) for p in parents)))
        for key, _, _ in reversed(nodes):  # дети — раньше родителей
            below = sorted((slot, colour[child]) for slot, child in children[key])
            colour[key] = hash((colour[key], tuple(below)))
    result = defaultdict(Counter)
    for key, _, _ in nodes:
        result[key[0]][colour[key]] += 1
    return dict(result)


# ---- копии базы в памяти ----


def memory_copy(data: bytes) -> sqlite3.Connection:
    """Свежая копия базы в оперативной памяти (deserialize — быстрее копии файла)."""
    conn = sqlite3.connect(":memory:", isolation_level=None)  # транзакции — вручную
    conn.deserialize(data)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture(scope="module")
def fresh(db_path):
    """Свежий импорт только на чтение — «новая база из Excel» для всех сравнений."""
    conn = connect_read_only(db_path)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def schema(fresh):
    return Schema(fresh)


@pytest.fixture(scope="module")
def images(fresh):
    """Содержимое базы байтами: как есть и со сдвинутыми номерами (shift_all)."""
    data = fresh.serialize()
    conn = sqlite3.connect(":memory:")
    conn.deserialize(data)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    shift_all(conn)
    shifted = conn.serialize()
    conn.close()
    return {False: data, True: shifted}


# ---- случайные правки: каждая возвращает описание или None («не вышло, другую») ----


def row_ids(conn, table):
    return [r[0] for r in conn.execute(f"SELECT rowid FROM {table} ORDER BY rowid")]


def pick_row(conn, rng, tables):
    """Случайная запись случайной непустой таблицы: (таблица, строка) или None."""
    tables = [t for t in tables if row_ids(conn, t)]
    if not tables:
        return None
    table = rng.choice(tables)
    rowid = rng.choice(row_ids(conn, table))
    return table, conn.execute(
        f"SELECT rowid AS _r, * FROM {table} WHERE rowid = ?", (rowid,)
    ).fetchone()


def mutate(value, rng):
    """Другое значение того же типа (число +, текст с припиской, NULL → что-то)."""
    if value is None:
        return rng.choice(["правка", 1])
    if isinstance(value, int):
        return value + rng.randint(1, 5)
    if isinstance(value, float):
        return round(value + rng.choice([0.5, 1.25, 10.0]), 4)
    return f"{value} (правка {rng.randint(1, 999)})"


def other_parent(conn, rng, link, current=None, same=None):
    """Значения parent_columns случайной записи родителя, кроме current.

    same — {колонка родителя: значение}: выбирать только среди таких записей родителя
    (траление того же выезда).
    """
    columns = ", ".join(link.parent_columns)
    rows = [tuple(r) for r in conn.execute(f"SELECT {columns} FROM {link.parent} ORDER BY rowid")]
    if same:
        position = {c: i for i, c in enumerate(link.parent_columns)}
        rows = [r for r in rows if all(r[position[c]] == v for c, v in same.items())]
    rows = [r for r in rows if r != current]
    return rng.choice(rows) if rows else None


def edit_field(conn, schema, rng):
    """Изменить одно обычное поле одной записи: другое значение колонки, изменённое, NULL."""
    picked = pick_row(conn, rng, [t for t in schema.tables if schema.plain[t]])
    if not picked:
        return None
    table, row = picked
    column = rng.choice(schema.plain[table])
    old = row[column]
    seen = []
    for (value,) in conn.execute(f"SELECT {column} FROM {table} ORDER BY rowid"):
        if value not in seen and value != old:
            seen.append(value)
    candidates = [mutate(old, rng)]
    if seen:
        candidates.append(rng.choice(seen))
    if old is not None:
        candidates.append(None)
    new = rng.choice(candidates)
    conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (new, row["_r"]))
    # что записалось на самом деле: SQLite может привести тип ('5' → 5 в INTEGER-колонке)
    (stored,) = conn.execute(
        f"SELECT {column} FROM {table} WHERE rowid = ?", (row["_r"],)
    ).fetchone()
    if stored == old:  # та же база — не правка (try_edit откатит)
        return None
    return f"поле {table}.{column} (rowid {row['_r']}): {old!r} → {stored!r}"


def dependents(conn, schema, table, rowid):
    """Записи, которые ссылаются на запись (и на них — рекурсивно): [(таблица, rowid)]."""
    found, queue = [], [(table, rowid)]
    while queue:
        parent, parent_rowid = queue.pop()
        for child in schema.tables:
            for link in schema.links[child]:
                if link.parent != parent:
                    continue
                columns = ", ".join(link.parent_columns)
                values = conn.execute(
                    f"SELECT {columns} FROM {parent} WHERE rowid = ?", (parent_rowid,)
                ).fetchone()
                where = " AND ".join(f"{c} = ?" for c in link.columns)
                for (child_rowid,) in conn.execute(
                    f"SELECT rowid FROM {child} WHERE {where}", tuple(values)
                ):
                    if (child, child_rowid) not in found:
                        found.append((child, child_rowid))
                        queue.append((child, child_rowid))
    return found


def edit_delete(conn, schema, rng):
    """Удалить запись: только лист (на неё никто не ссылается) или вместе с детьми."""
    picked = pick_row(conn, rng, schema.tables)
    if not picked:
        return None
    table, row = picked
    cascade = rng.random() < 0.5
    doomed = [(table, row["_r"])] + dependents(conn, schema, table, row["_r"])
    if not cascade and len(doomed) > 1:
        return None
    issues = 0
    if rng.random() < 0.5:  # и записи журнала о них (иначе ссылки журнала станут «битыми»)
        for t, r in list(doomed):
            own = schema.own_id[t]
            if not own or t == "data_issue":
                continue
            (own_value,) = conn.execute(f"SELECT {own} FROM {t} WHERE rowid = ?", (r,)).fetchone()
            for (issue,) in conn.execute(
                "SELECT rowid FROM data_issue WHERE table_name = ? AND record_id = ?",
                (t, own_value),
            ):
                if ("data_issue", issue) not in doomed:
                    doomed.append(("data_issue", issue))
                    issues += 1
    for t, r in doomed:
        conn.execute(f"DELETE FROM {t} WHERE rowid = ?", (r,))
    kind = "с детьми" if len(doomed) > 1 else "лист"
    return f"удаление {table} rowid {row['_r']} ({kind}: всего {len(doomed)}, журнал {issues})"


def edit_copy(conn, schema, rng):
    """Добавить копию записи; в каждом UNIQUE изменить одну колонку (без UNIQUE — точная копия)."""
    picked = pick_row(conn, rng, schema.tables)
    if not picked:
        return None
    table, row = picked
    values = {c: row[c] for c in schema.columns[table] if c != schema.own_id[table]}
    changed = []
    for unique in schema.unique[table]:
        column = rng.choice(unique)
        link = schema.link_of(table, column)
        if link:  # колонка-ссылка: другая запись родителя (составной ключ — целиком)
            parent = other_parent(conn, rng, link, tuple(values[c] for c in link.columns))
            if parent is None:
                return None
            values.update(zip(link.columns, parent, strict=True))
        else:
            values[column] = mutate(values[column], rng)
        changed.append(column)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))
    what = f"изменены {', '.join(changed)}" if changed else "точная копия"
    return f"копия {table} rowid {row['_r']} ({what})"


def edit_relink(conn, schema, rng):
    """Перепривязать ссылку: на другую запись родителя, NULL → значение, значение → NULL."""
    tables = [t for t in schema.tables if schema.links[t] or t in POLYMORPHIC]
    picked = pick_row(conn, rng, tables)
    if not picked:
        return None
    table, row = picked
    choices = list(schema.links[table]) + (["журнал"] if table in POLYMORPHIC else [])
    link = rng.choice(choices)
    if link == "журнал":  # data_issue.record_id — на другую запись той же таблицы или NULL
        name_column, id_column = POLYMORPHIC[table]
        target, old = row[name_column], row[id_column]
        own = schema.own_id.get(target)
        ids = (
            [r[0] for r in conn.execute(f"SELECT {own} FROM {target} ORDER BY rowid")]
            if own
            else []
        )
        options = [i for i in ids if i != old] + ([None] if old is not None else [])
        if not options:
            return None
        new = rng.choice(options)
        conn.execute(f"UPDATE {table} SET {id_column} = ? WHERE rowid = ?", (new, row["_r"]))
        return f"ссылка {table}.{id_column} (rowid {row['_r']}, {target}): {old!r} → {new!r}"
    old = tuple(row[c] for c in link.columns)
    nullable = [c for c in link.columns if c not in schema.notnull[table]]
    if None not in old and nullable and rng.random() < 0.3:  # значение → NULL
        new = {c: None for c in nullable}
    else:
        same = None
        if len(link.columns) > 1 and rng.random() < 0.5:  # траление того же выезда
            shared = [
                (c, p) for c, p in zip(link.columns, link.parent_columns, strict=True)
                if c in schema.notnull[table]
            ]  # fmt: skip
            same = {p: row[c] for c, p in shared}
        parent = other_parent(conn, rng, link, old, same)
        if parent is None:
            return None
        new = dict(zip(link.columns, parent, strict=True))
    sets = ", ".join(f"{c} = ?" for c in new)
    conn.execute(f"UPDATE {table} SET {sets} WHERE rowid = ?", (*new.values(), row["_r"]))
    return f"ссылка {table}.{link.columns} (rowid {row['_r']}): {old} → {tuple(new.values())}"


def edit_swap(conn, schema, rng):
    """Обменять поле (или ссылку целиком) между двумя записями одной таблицы.

    Обмен одинаковых значений — не правка (None). Колонки UNIQUE меняются через
    временное значение: две записи на миг не должны совпасть.
    """
    tables = [t for t in schema.tables if len(row_ids(conn, t)) >= 2]
    if not tables:
        return None
    table = rng.choice(tables)
    groups = [(c,) for c in schema.plain[table]] + [k.columns for k in schema.links[table]]
    if table in POLYMORPHIC:
        groups.append((POLYMORPHIC[table][1],))
    columns = rng.choice(groups)
    first, second = rng.sample(row_ids(conn, table), 2)
    select = ", ".join(columns)
    a = tuple(conn.execute(f"SELECT {select} FROM {table} WHERE rowid = ?", (first,)).fetchone())
    b = tuple(conn.execute(f"SELECT {select} FROM {table} WHERE rowid = ?", (second,)).fetchone())
    if a == b:  # обмен одинаковых — база та же
        return None
    sets = ", ".join(f"{c} = ?" for c in columns)
    in_unique = {c for u in schema.unique[table] for c in u} & set(columns)
    if in_unique:
        temp = [
            ("__обмен__" if isinstance(v, str) else -987_654) if c in in_unique else v
            for c, v in zip(columns, a, strict=True)
        ]
        conn.execute(f"UPDATE {table} SET {sets} WHERE rowid = ?", (*temp, first))
    conn.execute(f"UPDATE {table} SET {sets} WHERE rowid = ?", (*a, second))
    conn.execute(f"UPDATE {table} SET {sets} WHERE rowid = ?", (*b, first))
    return f"обмен {table}.{columns} между rowid {first} и {second}: {a} ↔ {b}"


EDITORS = dict(
    zip(KINDS, (edit_field, edit_delete, edit_copy, edit_relink, edit_swap), strict=True)
)


def try_edit(conn, schema, kind, rng):
    """Правка в транзакции: нарушено ограничение (CHECK, NOT NULL, UNIQUE, FK) — откат, None.

    PRAGMA defer_foreign_keys откладывает проверку внешних ключей до COMMIT: удалять
    родителя раньше детей и менять ссылки по очереди можно, к концу всё должно сойтись.
    """
    conn.execute("BEGIN")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    try:
        description = EDITORS[kind](conn, schema, rng)
        if description is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    return description


# ---- 1. любая правка находится ----


def test_oracle_sees_swapped_labs_as_same(fresh, schema, images):
    """Проверка самого «отпечатка»: обмен названиями «Россия» ↔ «Тропцентр» (у них одни
    и те же отправки, см. test_compare) — та же база; правка TL — другая."""
    expected = invariant(fresh, schema)
    conn = memory_copy(images[False])
    with_temp = [("временно", "Россия"), ("Россия", "Тропцентр"), ("Тропцентр", "временно")]
    for new, old in with_temp:
        conn.execute("UPDATE lab SET name = ? WHERE name = ?", (new, old))
    assert invariant(conn, schema) == expected
    conn.execute("UPDATE specimen SET tl_cm = tl_cm + 1 WHERE label = '3'")
    assert invariant(conn, schema) != expected
    conn.close()


def test_every_edit_is_found(fresh, schema, images):
    """200 правок (по 40 каждого вида, половина — после сдвига номеров): каждую видно."""
    expected = invariant(fresh, schema)
    missed, skipped, checked = [], Counter(), Counter()
    for number in range(EDITS):
        kind = KINDS[number % len(KINDS)]
        shifted = (number // len(KINDS)) % 2 == 1
        rng = random.Random(SEED * 1000 + number)
        conn = memory_copy(images[shifted])
        for _ in range(ATTEMPTS):
            description = try_edit(conn, schema, kind, rng)
            if description is None:
                continue  # правка не прошла ограничения — база не тронута, другую
            if kind in NEEDS_INVARIANT and invariant(conn, schema) == expected:
                skipped[kind] += 1  # та же база с точностью до номеров — не правка
                conn.close()
                conn = memory_copy(images[shifted])
                continue
            checked[kind, shifted] += 1
            if compare_databases(conn, fresh) == []:
                where = f"seed {SEED}, правка {number} ({kind}, сдвиг номеров: {shifted})"
                missed.append(f"{where}: {description}")
            break
        conn.close()
    assert missed == [], "защита не заметила правки:\n" + "\n".join(missed)
    # каждого вида проверено по 20 с номерами как есть и после сдвига
    assert checked == Counter({(k, s): EDITS // len(KINDS) // 2 for k in KINDS for s in (0, 1)})
    assert sum(skipped.values()) < EDITS // 4, skipped  # «пустых» правок немного


# ---- 2. совпадающие базы дают пусто ----


def permuted_copy(source, schema, rng) -> sqlite3.Connection:
    """Та же база с перемешанными номерами всех записей и случайным порядком вставки.

    Номер каждой записи с собственным номером заменяется случайным (без повторов), ссылки
    переводятся; у sample_shipment своего номера нет — меняется только порядок строк
    (а значит, и скрытый rowid).
    """
    ids = {}
    for table in schema.tables:
        own = schema.own_id[table]
        if own:
            old = [r[0] for r in source.execute(f"SELECT {own} FROM {table} ORDER BY rowid")]
            ids[table] = dict(
                zip(old, rng.sample(range(1, 10 * len(old) + 10), len(old)), strict=True)
            )
    remap = {}  # (таблица, колонка) → таблица, по номерам которой переводить
    for table in schema.tables:
        for link in schema.links[table]:
            for column, parent_column in zip(link.columns, link.parent_columns, strict=True):
                if parent_column == schema.own_id[link.parent]:
                    remap[table, column] = link.parent
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = OFF")  # таблицы — в случайном порядке, детей раньше
    conn.execute("BEGIN")
    for table in rng.sample(schema.tables, len(schema.tables)):
        rows = source.execute(f"SELECT * FROM {table}").fetchall()
        rng.shuffle(rows)
        own = schema.own_id[table]
        for row in rows:
            values = dict(zip(row.keys(), tuple(row), strict=True))
            if own:
                values[own] = ids[table][values[own]]
            for column in values:
                parent = remap.get((table, column))
                if parent and values[column] is not None:
                    values[column] = ids[parent][values[column]]
            if table in POLYMORPHIC:
                name_column, id_column = POLYMORPHIC[table]
                target_ids = ids.get(values[name_column], {})
                values[id_column] = target_ids.get(values[id_column], values[id_column])
            marks = ", ".join("?" for _ in values)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})",
                tuple(values.values()),
            )
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    return conn


def test_shifted_ids_give_no_differences(fresh, images):
    """shift_all (как в test_compare) — в обе стороны пусто."""
    conn = memory_copy(images[True])
    assert compare_databases(conn, fresh) == []
    assert compare_databases(fresh, conn) == []
    conn.close()


@pytest.mark.parametrize("seed", range(5))
def test_permuted_ids_and_row_order_give_no_differences(fresh, schema, seed):
    """Случайные номера всех записей + другой порядок строк — в обе стороны пусто."""
    rng = random.Random(SEED * 1000 + 900 + seed)
    conn = permuted_copy(fresh, schema, rng)
    # номера правда другие (а не совпали случайно)
    assert [
        r[0] for r in conn.execute("SELECT specimen_id FROM specimen ORDER BY rowid LIMIT 5")
    ] != [r[0] for r in fresh.execute("SELECT specimen_id FROM specimen ORDER BY rowid LIMIT 5")]
    assert invariant(conn, schema) == invariant(fresh, schema)  # «отпечаток» не зависит от номеров
    assert compare_databases(conn, fresh) == [], f"seed {SEED * 1000 + 900 + seed}"
    assert compare_databases(fresh, conn) == [], f"seed {SEED * 1000 + 900 + seed}"
    conn.close()
