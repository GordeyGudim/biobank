"""Тесты этапа 7: правки link-trawl, identify, resolve-issue и резервные копии.

Каждый тест работает с КОПИЕЙ импортированной базы (фикстура edit_db),
чтобы правки не влияли на другие тесты.
"""

import shutil

import pytest
from conftest import SOURCE, count

from biobank.__main__ import main
from biobank.db import connect
from biobank.edit import EditError, backup_database, identify, link_trawl, resolve_issue

pytestmark = pytest.mark.skipif(not SOURCE.exists(), reason="нет data/source.xlsx")


@pytest.fixture
def edit_path(db_path, tmp_path):
    path = tmp_path / "output" / "biobank.db"
    path.parent.mkdir()
    shutil.copy(db_path, path)
    return path


@pytest.fixture
def edit_db(edit_path):
    conn = connect(edit_path)
    yield conn
    conn.close()


def trawl_of(conn, label):
    return count(
        conn,
        "SELECT t.trawl_no FROM specimen s LEFT JOIN trawling t USING (trawling_id) "
        "WHERE s.label = ?",
        label,
    )


# ---------------------------------------------------------------------------
# link-trawl
# ---------------------------------------------------------------------------


def test_link_trawl(edit_db):
    result = link_trawl(edit_db, "5 pc", 3)
    assert trawl_of(edit_db, "5 pc") == 3
    assert "привязана к тралению 3" in result.message


def test_link_trawl_label_normalized(edit_db):
    link_trawl(edit_db, "5pc", 2)  # без пробела — как в Excel
    assert trawl_of(edit_db, "5 pc") == 2


def test_relink_reports_change(edit_db):
    link_trawl(edit_db, "5 pc", 3)
    assert "траление 3 → 1" in link_trawl(edit_db, "5 pc", 1).message


@pytest.mark.parametrize(
    "label, trawl_no, error",
    [
        ("5 pc", 9, "нет траления 9. Есть: 1, 2, 3, 4"),  # Точка 4: 4 траления
        ("26 ph", 1, "нет тралений"),  # аквахозяйство
        ("138 pc", 1, "получена не тралением"),  # пойман рыбаком
        ("9999 pc", 1, "не найдена"),
    ],
)
def test_link_trawl_errors(edit_db, label, trawl_no, error):
    before = count(edit_db, "SELECT count(*) FROM specimen WHERE trawling_id IS NOT NULL")
    with pytest.raises(EditError, match=error):
        link_trawl(edit_db, label, trawl_no)
    after = count(edit_db, "SELECT count(*) FROM specimen WHERE trawling_id IS NOT NULL")
    assert before == after == 0


def test_linking_all_closes_issue(edit_db):
    """Точка 5 (Донг-хап): одна особь. Привязали — проблема «не привязаны» закрылась сама."""

    def open_issue():
        return count(
            edit_db,
            "SELECT count(*) FROM data_issue WHERE sheet_name = 'Точка 5' "
            "AND description LIKE 'Особи не привязаны%' AND resolved = 0",
        )

    assert open_issue() == 1
    label = count(
        edit_db,
        "SELECT s.label FROM specimen s JOIN sampling_event e USING (event_id) "
        "WHERE e.source_sheet = 'Точка 5'",
    )
    result = link_trawl(edit_db, label, 6)
    assert result.checks.closed == 1
    assert open_issue() == 0


def test_partial_linking_keeps_one_issue(edit_db):
    link_trawl(edit_db, "5 pc", 3)
    link_trawl(edit_db, "6 pc", 3)
    assert (
        count(
            edit_db,
            "SELECT count(*) FROM data_issue WHERE sheet_name = 'Точка 4' "
            "AND description LIKE 'Особи не привязаны%'",
        )
        == 1
    )  # одна запись, без дублей от каждой привязки


# ---------------------------------------------------------------------------
# identify
# ---------------------------------------------------------------------------


def test_identify_adds_history(edit_db):
    result = identify(
        edit_db, "1 e", "Pangasius elongatus", "ДНК", "точно",
        identified_by="Иванов", identified_on="2026-09-15", notes="GenBank XX",
    )  # fmt: skip
    assert "→ Pangasius elongatus" in result.message
    history = edit_db.execute(
        "SELECT method, confidence, identified_by, identified_on FROM species_identification "
        "JOIN specimen USING (specimen_id) WHERE label = '1 e' ORDER BY identification_id"
    ).fetchall()
    assert [tuple(r) for r in history] == [
        ("морфология", "точно", None, "2025-09-27"),
        ("ДНК", "точно", "Иванов", "2026-09-15"),
    ]
    assert (
        count(
            edit_db,
            "SELECT sp.scientific_name FROM specimen s JOIN species sp USING (species_id) "
            "WHERE s.label = '1 e'",
        )
        == "Pangasius elongatus"
    )


def test_identify_closes_suffix_issue(edit_db):
    """5–8 e записаны как Pangasius sp.; ДНК подтвердила elongatus → несоответствия больше нет."""
    for label in ("5 e", "6 e", "7 e", "8 e"):
        result = identify(edit_db, label, "P. elongatus", "ДНК", "точно")
    assert result.checks.closed == 1
    assert (
        count(
            edit_db,
            "SELECT count(*) FROM data_issue WHERE object_label LIKE '%5 e%' AND resolved = 0",
        )
        == 0
    )


def test_identify_genus_abbreviation(edit_db):
    identify(edit_db, "1 e", "P. sp", "морфология", "до рода")
    assert (
        count(
            edit_db,
            "SELECT sp.scientific_name FROM specimen s JOIN species sp USING (species_id) "
            "WHERE s.label = '1 e'",
        )
        == "Pangasius sp."
    )


def test_identify_undetermined(edit_db):
    identify(edit_db, "1 e", None, "морфология", "не определён")
    assert count(edit_db, "SELECT species_id FROM specimen WHERE label = '1 e'") is None


@pytest.mark.parametrize(
    "species, method, confidence, date, error",
    [
        ("Pangasius kremfi", "ДНК", "точно", None, "нет в справочнике"),
        ("X. elongatus", "ДНК", "точно", None, "нет в справочнике"),
        ("Pangasius sp.", "ПЦР", "точно", None, "Метод"),
        ("Pangasius sp.", "ДНК", "наверное", None, "Уверенность"),
        ("Pangasius sp.", "ДНК", "точно", "15.09.2026", "ГГГГ-ММ-ДД"),
        (None, "ДНК", "точно", None, "только уверенность «не определён»"),
    ],
)
def test_identify_errors(edit_db, species, method, confidence, date, error):
    before = count(edit_db, "SELECT count(*) FROM species_identification")
    with pytest.raises(EditError, match=error):
        identify(edit_db, "1 e", species, method, confidence, identified_on=date)
    assert count(edit_db, "SELECT count(*) FROM species_identification") == before


# ---------------------------------------------------------------------------
# resolve-issue
# ---------------------------------------------------------------------------


def test_resolve_issue(edit_db):
    issue_id = count(edit_db, "SELECT min(issue_id) FROM data_issue WHERE category = 'дата'")
    resolve_issue(edit_db, issue_id, "Дату подтвердил коллега по журналу")
    row = edit_db.execute(
        "SELECT resolved, resolution FROM data_issue WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    assert tuple(row) == (1, "Дату подтвердил коллега по журналу")
    with pytest.raises(EditError, match="уже решена"):
        resolve_issue(edit_db, issue_id, "ещё раз")


@pytest.mark.parametrize(
    "issue_id, text, error", [(99999, "x", "нет в журнале"), (1, " ", "пустой")]
)
def test_resolve_issue_errors(edit_db, issue_id, text, error):
    with pytest.raises(EditError, match=error):
        resolve_issue(edit_db, issue_id, text)


def test_resolved_check_issue_survives_recheck(edit_db):
    """Решённая вручную проблема проверки не открывается снова при следующей правке."""
    issue_id = count(edit_db, "SELECT issue_id FROM data_issue WHERE object_label = '262'")
    resolve_issue(edit_db, issue_id, "Сверено с журналом: так и есть")
    link_trawl(edit_db, "5 pc", 3)  # любая правка перезапускает проверки
    assert count(edit_db, "SELECT resolved FROM data_issue WHERE issue_id = ?", issue_id) == 1


# ---------------------------------------------------------------------------
# Резервные копии и CLI
# ---------------------------------------------------------------------------


def test_backup_is_full_copy(edit_path):
    backup = backup_database(edit_path)
    assert backup.parent.name == "backups"
    assert backup.name.startswith("biobank_")
    conn = connect(backup)
    try:
        assert count(conn, "SELECT count(*) FROM specimen") == 462
    finally:
        conn.close()


def test_two_backups_same_second_do_not_overwrite(edit_path):
    assert backup_database(edit_path) != backup_database(edit_path)


def test_cli_edit_makes_backup(edit_path, capsys):
    assert main(["--db", str(edit_path), "link-trawl", "5 pc", "3"]) == 0
    out = capsys.readouterr().out
    assert "Резервная копия до правки" in out
    assert len(list((edit_path.parent / "backups").iterdir())) == 1


def test_cli_failed_edit_leaves_no_backup(edit_path, capsys):
    assert main(["--db", str(edit_path), "link-trawl", "5 pc", "99"]) == 1
    assert "База не изменена" in capsys.readouterr().err
    assert list((edit_path.parent / "backups").iterdir()) == []


def test_cli_identify_question_mark(edit_path):
    args = ["identify", "1 e", "?", "--method", "морфология", "--confidence", "не определён"]
    assert main(["--db", str(edit_path), *args]) == 0


# ---------------------------------------------------------------------------
# Повторный импорт поверх ручных правок
# ---------------------------------------------------------------------------


def test_reimport_asks_when_edits_exist(edit_path, monkeypatch, capsys):
    assert main(["--db", str(edit_path), "link-trawl", "5 pc", "3"]) == 0
    monkeypatch.setattr("builtins.input", lambda _: "n")  # пользователь отвечает «нет»
    assert main(["--db", str(edit_path), "import", str(SOURCE)]) == 1
    out = capsys.readouterr().out
    assert "особей привязано к тралениям: 1" in out
    conn = connect(edit_path)
    try:
        assert trawl_of(conn, "5 pc") == 3  # правка на месте
    finally:
        conn.close()


def test_reimport_with_yes_makes_backup(edit_path, capsys):
    assert main(["--db", str(edit_path), "link-trawl", "5 pc", "3"]) == 0
    assert main(["--db", str(edit_path), "import", str(SOURCE), "--yes"]) == 0
    assert "Резервная копия текущей базы" in capsys.readouterr().out
    backups = sorted((edit_path.parent / "backups").iterdir())
    assert len(backups) == 2  # до правки и до импорта
    conn = connect(backups[-1])
    try:
        assert trawl_of(conn, "5 pc") == 3  # в копии правка сохранилась
    finally:
        conn.close()


def test_fresh_import_has_no_manual_edits(edit_db):
    from biobank.edit import manual_edits

    assert manual_edits(edit_db) == {}


def test_morphology_reidentification_counts_as_edit(edit_db):
    from biobank.edit import manual_edits

    identify(edit_db, "1 e", "Pangasius sp.", "морфология", "до рода")
    assert manual_edits(edit_db) == {"определений вида добавлено": 1}
