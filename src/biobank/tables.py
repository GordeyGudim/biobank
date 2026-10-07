"""Таблицы как данные (CardSection) и их печать в терминале.

Общий модуль для карточки особи (card.py) и отчётов (reports.py):
функции возвращают список разделов CardSection — заголовок, колонки, строки, —
а как их показать, решает тот, кто вызывает: терминал (render_sections)
или будущий веб-интерфейс (HTML-таблица из тех же данных).
"""

from __future__ import annotations

import sqlite3
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class CardSection:
    """Раздел карточки или отчёта: таблица с заголовком."""

    title: str
    columns: list[str]
    rows: list[tuple]
    empty_text: str = "(нет)"
    note: str | None = None  # пояснение под заголовком
    is_fields: bool = False  # таблица «поле — значение» (одна запись), а не список строк


def query_section(
    conn: sqlite3.Connection,
    title: str,
    sql: str,
    params: Sequence | dict = (),
    empty_text: str = "(нет)",
) -> CardSection:
    """Выполнить запрос и вернуть результат как раздел."""
    columns, rows = run_query(conn, sql, params)
    return CardSection(title, columns, [tuple(r) for r in rows], empty_text)


def _cell_text(value, max_width: int) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.10g}"  # 10 значащих цифр: координаты не обрезаются
    else:
        text = " ".join(str(value).split())
    return text if len(text) <= max_width else text[: max_width - 1] + "…"


MIN_COLUMN_WIDTH = 8  # уже этого текстовые колонки не сжимаем


def _fit_widths(
    widths: list[int], numeric: list, total_width: int, longest_words: list[int]
) -> list[int]:
    """Сжать текстовые колонки, чтобы таблица поместилась в total_width символов.

    Каждый раз отнимаем по символу у самой широкой текстовой колонки.
    Сначала — не уже самого длинного слова в колонке (слова не режутся),
    если не хватило — до MIN_COLUMN_WIDTH. Числа не сжимаем.
    Если не помещается даже так — печатаем как есть.
    """
    widths = list(widths)
    frame = 3 * len(widths) + 1  # «│ » в начале, « │ » между колонками, « │» в конце
    for limits in (longest_words, [0] * len(widths)):
        while sum(widths) + frame > total_width:
            shrinkable = [
                i
                for i, w in enumerate(widths)
                if not numeric[i] and w > max(MIN_COLUMN_WIDTH, limits[i])
            ]
            if not shrinkable:
                break
            widest = max(shrinkable, key=lambda i: widths[i])
            widths[widest] -= 1
    return widths


def format_table(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    max_width: int = 50,
    total_width: int | None = None,
) -> str:
    """Таблица с рамкой из символов, как `sqlite3 -box`.

    total_width — ширина экрана: если таблица шире, текстовые колонки сужаются,
    а длинный текст переносится на следующую строку внутри ячейки.

    >>> print(format_table(["вид", "n"], [("Plotosus canius", 173), ("?", 2)]))
    ┌─────────────────┬─────┐
    │ вид             │   n │
    ├─────────────────┼─────┤
    │ Plotosus canius │ 173 │
    │ ?               │   2 │
    └─────────────────┴─────┘
    >>> print(format_table(["вид", "n"], [("Plotosus canius", 173)], total_width=18))
    ┌──────────┬─────┐
    │ вид      │   n │
    ├──────────┼─────┤
    │ Plotosus │ 173 │
    │ canius   │     │
    └──────────┴─────┘
    """
    texts = [[_cell_text(v, max_width) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in texts:
        widths = [max(w, len(t)) for w, t in zip(widths, row, strict=True)]
    # числа выравниваем вправо, текст — влево
    numeric = [
        all(isinstance(row[i], (int, float)) or row[i] is None for row in rows) and rows
        for i in range(len(columns))
    ]
    if total_width:
        longest_words = [
            max(
                len(word)
                for text in [col, *(row[i] for row in texts)]
                for word in [*text.split(), ""]
            )
            for i, col in enumerate(columns)
        ]
        widths = _fit_widths(widths, numeric, total_width, longest_words)

    def line(cells):
        # textwrap.wrap режет текст на куски не длиннее w (по пробелам, если можно);
        # ячейка становится многострочной, высота строки — по самой высокой ячейке
        wrapped = [
            textwrap.wrap(c, w) or [""] if len(c) > w else [c]
            for c, w in zip(cells, widths, strict=True)
        ]
        height = max(len(parts) for parts in wrapped)
        out = []
        for k in range(height):
            parts = [
                (p[k] if k < len(p) else "").rjust(w) if num
                else (p[k] if k < len(p) else "").ljust(w)
                for p, w, num in zip(wrapped, widths, numeric, strict=True)
            ]  # fmt: skip
            out.append("│ " + " │ ".join(parts) + " │")
        return "\n".join(out)

    def border(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [border("┌", "┬", "┐"), line(columns), border("├", "┼", "┤")]
    out += [line(row) for row in texts]
    out.append(border("└", "┴", "┘"))
    return "\n".join(out)


def run_query(
    conn: sqlite3.Connection, sql: str, params: Sequence | dict = ()
) -> tuple[list, list]:
    """Выполнить запрос; вернуть (имена колонок, строки)."""
    cursor = conn.execute(sql, params)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    return columns, cursor.fetchall()


def render_sections(
    sections: list[CardSection], max_width: int = 50, total_width: int | None = None
) -> str:
    """Текст разделов для печати в терминале.

    total_width — ширина экрана; длинный текст переносится внутри ячеек.

    >>> print(render_sections([CardSection("Виды", ["вид"], [], "(пусто)")]))
    Виды (строк: 0)
      (пусто)
    """
    parts = []
    for sec in sections:
        lines = [sec.title if sec.is_fields else f"{sec.title} (строк: {len(sec.rows)})"]
        if sec.note:
            lines.append(f"  {sec.note}")
        if sec.rows:
            lines.append(format_table(sec.columns, sec.rows, max_width, total_width))
        else:
            lines.append(f"  {sec.empty_text}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
