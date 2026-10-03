"""Тесты карточки особи (команда show)."""

import shutil

import pytest
from conftest import SOURCE

from biobank.__main__ import main
from biobank.card import render_card, specimen_card
from biobank.db import connect
from biobank.edit import EditError, link_trawl
from biobank.reports import connect_read_only

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture
def ro(db_path):
    conn = connect_read_only(db_path)
    yield conn
    conn.close()


def sections_of(conn, label):
    """Карточка как словарь: заголовок раздела (без пояснений) → раздел."""
    return {sec.title.split(":")[0]: sec for sec in specimen_card(conn, label)}


def fields_of(conn, label):
    return dict(sections_of(conn, label)["Особь"].rows)


def test_main_fields(ro):
    f = fields_of(ro, "155 pc")
    assert f["вид (текущий)"] == "Plotosus canius"
    assert f["выезд (лист)"] == "Рыбак (самостоятельно 22.08)"
    assert f["SL"] == "29.9 см"  # восстановлено из «даты»
    assert f["траление"] == "неизвестно"


def test_label_is_normalized(ro):
    assert fields_of(ro, "155pc")["номер пробы"] == "155 pc"


def test_samples_and_labs(ro):
    sec = sections_of(ro, "155 pc")["Образцы"]
    by_type = {}
    for row in sec.rows:
        by_type.setdefault(row[0], []).append(dict(zip(sec.columns, row, strict=True)))
    assert {s["орган"] for s in by_type["гистология"]} == {"печень", "почки"}
    assert by_type["ДНК"][0]["отправлен"] == "Россия; Тропцентр; Кантхо (секвенирование)"
    assert len(sec.rows) == 5  # отправка в 3 лаборатории не размножает строки


def test_issues_of_specimen(ro):
    sec = sections_of(ro, "155 pc")["Проблемы в журнале (особь и её выезд)"]
    assert any("дату" in row[5] for row in sec.rows)


def test_issue_found_in_collapsed_list(ro):
    """Свёрнутая запись хранит '5 e, 6 e, 7 e, 8 e': 6 e её находит, 9 e (тот же лист) — нет."""
    title = "Проблемы в журнале (особь и её выезд)"
    assert any("Суффикс" in row[5] for row in sections_of(ro, "6 e")[title].rows)
    assert not any("Суффикс" in row[5] for row in sections_of(ro, "9 e")[title].rows)


def test_water_of_whole_event_when_trawl_unknown(ro):
    water = sections_of(ro, "10 pc")["Вода"]
    assert len(water.rows) == 8  # 4 траления × старт/финиш
    assert "неизвестно" in water.note
    # в 2025 электропроводность не мерили — колонка есть, но пустая
    assert all(row[water.columns.index("mS")] is None for row in water.rows)
    assert "mS" in render_card([water])


def test_water_note_for_fisher_specimen(ro):
    water = sections_of(ro, "140 pc")["Вода"]
    assert "рыбак" in water.note
    assert fields_of(ro, "140 pc")["способ получения"] == "рыбак (выезд — траление)"


def test_water_of_linked_trawl(db_path, tmp_path):
    path = tmp_path / "copy.db"
    shutil.copy(db_path, path)
    conn = connect(path)
    try:
        link_trawl(conn, "10 pc", 2)
        water = sections_of(conn, "10 pc")["Вода"]
    finally:
        conn.close()
    assert water.title == "Вода: траление 2"
    assert [row[1] for row in water.rows] == ["старт", "финиш"]
    assert water.note is None


def test_unknown_specimen(ro):
    with pytest.raises(EditError, match="не найдена"):
        specimen_card(ro, "999 pc")


def test_render(ro):
    text = render_card(specimen_card(ro, "26 ph"))
    assert text.startswith("Особь\n┌")
    assert "Результаты анализов (строк: 0)\n  (пока нет)" in text


def test_render_fits_screen(ro):
    text = render_card(specimen_card(ro, "155 pc"), total_width=80)
    assert max(len(line) for line in text.splitlines()) <= 80
    # в окне пошире слова не режутся: перенесено на новую строку целиком
    assert "(секвенирование)" in render_card(specimen_card(ro, "155 pc"), total_width=110)


def test_cli_show(db_path, capsys):
    assert main(["--db", str(db_path), "show", "155 pc"]) == 0
    assert "Plotosus canius" in capsys.readouterr().out
    assert main(["--db", str(db_path), "show", "999 pc"]) == 1
    assert "не найдена" in capsys.readouterr().err
