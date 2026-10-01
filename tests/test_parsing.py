"""Тесты парсеров на реальных значениях из data/source.xlsx.

@pytest.mark.parametrize запускает один и тот же тест много раз —
по разу на каждую строку таблицы примеров «вход → ожидаемый результат».
"""

import datetime as dt

import pytest

from biobank.parsing import (
    clean_comment,
    is_blank,
    is_catch_comment,
    is_catch_note,
    is_dead_text,
    is_text_number,
    is_whole_specimen,
    parse_catch_counts,
    parse_catch_note,
    parse_date,
    parse_gear,
    parse_histology,
    parse_label,
    parse_label_range,
    parse_labs,
    parse_number,
    parse_sex,
    parse_shrimp_samples,
    parse_species,
    parse_time,
    parse_trawl_point,
    smear_looks_like_histology,
)

# ---------------------------------------------------------------------------
# Числа
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10,103733.", 10.103733),
        ("105, 96480", 105.9648),
        ("10, 80573 °", 10.80573),
        ("29,3.", 29.3),
        ("8, 4", 8.4),
        ("14.", 14.0),
        ("20 min", 20.0),
        (28.1, 28.1),
        (7, 7.0),
        ("9.5187059", 9.5187059),
    ],
)
def test_parse_number(raw, expected):
    result = parse_number(raw)
    assert result.value == pytest.approx(expected)
    assert result.warning is None


@pytest.mark.parametrize("raw", [None, "-", "—", "  "])
def test_parse_number_blank(raw):
    assert parse_number(raw) == parse_number(None)
    assert parse_number(raw).value is None


def test_parse_number_excel_date():
    # SL у 155 pc: Excel превратил 29,9 в дату 29.01.1900 21:36
    result = parse_number(dt.datetime(1900, 1, 29, 21, 36))
    assert result.value == pytest.approx(29.9)
    assert "дату" in result.warning


@pytest.mark.parametrize("raw", ["целая рыба", "N", "Заспиртовали полностью"])
def test_parse_number_not_a_number(raw):
    result = parse_number(raw)
    assert result.value is None
    assert result.warning is not None


def test_is_text_number():
    assert is_text_number("29,3.")
    assert is_text_number("20 min")
    assert not is_text_number(29.3)
    assert not is_text_number("-")
    assert not is_text_number("целая рыба")


# ---------------------------------------------------------------------------
# Время и даты
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (dt.time(9, 30), "09:30"),
        (dt.datetime(2025, 9, 25, 14, 5), "14:05"),
        ("9:30", "09:30"),
        ("9.30", "09:30"),
    ],
)
def test_parse_time(raw, expected):
    result = parse_time(raw)
    assert result.value == expected
    assert result.warning is None


@pytest.mark.parametrize("raw, expected", [(10.48, "10:48"), (11.08, "11:08")])
def test_parse_time_float_warns(raw, expected):
    result = parse_time(raw)
    assert result.value == expected
    assert "числом" in result.warning


def test_parse_time_invalid():
    assert parse_time(10.75).value is None
    assert parse_time(None).value is None


@pytest.mark.parametrize(
    "header, expected",
    [
        ("25.09.25 пр.Донтхап, г. Хонг-нга (Hong-Ngu)", "2025-09-25"),
        ("22. 10. 25 пр. Виньлонг, г. Бенче", "2025-10-22"),
        ("30.10.25. пр. Виньлонг, г. Кантхо", "2025-10-30"),
        ("11.08.2026 пр. Кантхо (г.Thot not)", "2026-08-11"),
        ("18.08.2026  пр.Кантхо, г. Шокчанг", "2026-08-18"),
        (None, None),
        ("Рынок", None),
    ],
)
def test_parse_date(header, expected):
    assert parse_date(header) == expected


# ---------------------------------------------------------------------------
# Комментарии
# ---------------------------------------------------------------------------


def test_clean_comment_removes_header():
    raw = (
        "======\nID#AAACHv6r0RQ\nadmin    (2026-09-29 14:21:50)\n"
        "Сеть:\nразмер ячеи 5х5 см.,\nсама сеть 15х20 м."
    )
    assert clean_comment(raw) == "Сеть: размер ячеи 5х5 см., сама сеть 15х20 м."


def test_clean_comment_other_author():
    raw = "======\nID#AAACHv6r0Ls\nInfinix    (2026-09-29 14:21:50)\nтрал пустой"
    assert clean_comment(raw) == "трал пустой"


def test_clean_comment_empty():
    # реальный комментарий у 87 ph на листе «Рынок.Бенче»
    assert clean_comment("======\nID#AAACHv6r0OA\nadmin    (2026-09-29 14:21:50)\nadmin:") is None
    assert clean_comment(None) is None
    assert clean_comment("") is None


# ---------------------------------------------------------------------------
# Номера проб
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, label, series",
    [
        (107.0, "107", "Pangasius"),
        ("107.0", "107", "Pangasius"),
        ("155  pc", "155 pc", "pc"),
        ("3 p.c.", "3 pc", "pc"),
        ("2pc", "2 pc", "pc"),
        ("1е", "1 e", "Pangasius"),  # кириллическая «е»
        ("1 e", "1 e", "Pangasius"),
        ("40 рс", "40 pc", "pc"),  # кириллические «р», «с»
        ("2 р.с.", "2 pc", "pc"),
        ("15sp", "15 sp", "Pangasius"),
        ("26 ph", "26 ph", "Pangasius"),
        ("66 mc", "66 mc", "Pangasius"),
        ("1 Bg.", "1 Bg", "Bg"),
        ("3 mr", "3 mr", "mr"),
    ],
)
def test_parse_label(raw, label, series):
    parsed = parse_label(raw)
    assert parsed.label == label
    assert parsed.series == series


@pytest.mark.parametrize(
    "raw",
    [None, "ХХ", "-", "26 ph б", "2pc - печень", "17 pc, целиком в пробирку", "m рыбы 2417", 0.5],
)
def test_parse_label_rejects(raw):
    assert parse_label(raw) is None


def test_parse_label_number():
    parsed = parse_label("155  pc")
    assert (parsed.number, parsed.suffix) == (155, "pc")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2 рыбак (138-142)", (138, 142)),
        ("1 рыбак (118)", (118, 118)),
        ("Координаты 2 (163-178)", (163, 178)),
        ("старт 1", None),
        (None, None),
    ],
)
def test_parse_label_range(raw, expected):
    assert parse_label_range(raw) == expected


# ---------------------------------------------------------------------------
# Виды
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, name, confidence",
    [
        ("Plotosus canius ", "Plotosus canius", "точно"),
        ("plotosus canius", "Plotosus canius", "точно"),
        ("Pangasius elongatus", "Pangasius elongatus", "точно"),
        ("пред. Pangasius elongatus", "Pangasius elongatus", "предп."),
        ("предп. Pangasius elongatus", "Pangasius elongatus", "предп."),
        ("Pangasius sp", "Pangasius sp.", "до рода"),
        ("pangasius sp.", "Pangasius sp.", "до рода"),
        ("Pangasius conchophilus", "Pangasius conchophilus", "точно"),
        ("Pangasius macronema", "Pangasius macronema", "точно"),
        ("Pangasiodon hypophthalmus", "Pangasianodon hypophthalmus", "точно"),
        ("Bagarius sp.", "Bagarius sp.", "точно"),
        ("macrobrahius rozenbergii ", "Macrobrachium rosenbergii", "точно"),
        ("?", None, "не определён"),
        ("вид неизвестен, заспиртовали на ДНК", None, "не определён"),
        ("скорее всего не elongatus", "Pangasius sp.", "до рода"),
        (None, None, "не определён"),
    ],
)
def test_parse_species(raw, name, confidence):
    parsed = parse_species(raw)
    assert (parsed.name, parsed.confidence) == (name, confidence)


def test_parse_species_typo_noted():
    assert "Pangasiodon" in parse_species("Pangasiodon hypophthalmus").note


# ---------------------------------------------------------------------------
# Состояние особи
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["целая рыба", "Заспиртовали полностью", "заспиртовали целиком", "Целая особь",
     "зафиксирована целиком", "17 pc, целиком в пробирку", "целиком в пробирке"],
)  # fmt: skip
def test_is_whole_specimen(raw):
    assert is_whole_specimen(raw)


@pytest.mark.parametrize("raw", [None, "-", "26 ph", 12.5])
def test_is_not_whole_specimen(raw):
    assert not is_whole_specimen(raw)


def test_is_dead_text():
    assert is_dead_text("- , особь мертвая")
    assert is_dead_text("особи мертвые")
    assert is_dead_text("Рыба мертвая")
    assert is_dead_text("из погибшей особи")
    assert not is_dead_text("нет, т.к. рыба маленькая")
    assert not is_dead_text(None)


def test_parse_sex():
    assert parse_sex("самка") == "самка"
    assert parse_sex("самец") == "самец"
    assert parse_sex("с икрой") is None


def test_is_blank():
    assert is_blank(None) and is_blank("-") and is_blank(" — ")
    assert not is_blank(0) and not is_blank("1 pc")


# ---------------------------------------------------------------------------
# Гистология
# ---------------------------------------------------------------------------


def samples_of(text, specimen_label):
    """Короткая запись результата для сравнения: [(тип, орган, метка), ...]."""
    return [
        (s.sample_type, s.organ, s.label) for s in parse_histology(text, specimen_label).samples
    ]


@pytest.mark.parametrize(
    "text, specimen, expected",
    [
        (
            "11 pc a - печень, b - почки, с - икра",
            "11 pc",
            [("гистология", "печень", "11 pc a"), ("гистология", "почки", "11 pc b"),
             ("гистология", "икра", "11 pc с")],
        ),
        (
            "15sp а - печень 15sp б - почки",  # без запятой
            "15 sp",
            [("гистология", "печень", "15 sp а"), ("гистология", "почки", "15 sp б")],
        ),
        ("13а - печень, почек нет", "13", [("гистология", "печень", "13 а")]),
        ("14б - печень, почек нет", "14", [("гистология", "печень", "14 б")]),
        (
            "кишечник+гистология в пакете",
            "241",
            [("кишечник", "кишечник", "241"), ("гистология", None, "241")],
        ),
        (
            "26 ph a - печень, 26 ph б - почки",
            "26 ph",
            [("гистология", "печень", "26 ph a"), ("гистология", "почки", "26 ph б")],
        ),
        (
            "102 pc а - печень, в - почки",
            "102 pc",
            [("гистология", "печень", "102 pc а"), ("гистология", "почки", "102 pc в")],
        ),
        (
            "18 pc a-печень, pc b-почки ",
            "18 pc",
            [("гистология", "печень", "18 pc a"), ("гистология", "почки", "18 pc b")],
        ),
        (
            "1а -почки, 1б - печень",
            "1",
            [("гистология", "почки", "1 а"), ("гистология", "печень", "1 б")],
        ),
        ("10е - печень", "10 e", [("гистология", "печень", "10 e")]),
        ("16sp - печень", "16 sp", [("гистология", "печень", "16 sp")]),
        ("241 (печень)", "241", [("гистология", "печень", "241")]),
        ("277 a - печень", "277", [("гистология", "печень", "277 a")]),
        (
            "129 e a - печень, b-почки",
            "129 e",
            [("гистология", "печень", "129 e a"), ("гистология", "почки", "129 e b")],
        ),
        (
            "1 Bg a - печень, b - почки",
            "1 Bg",
            [("гистология", "печень", "1 Bg a"), ("гистология", "почки", "1 Bg b")],
        ),
        ("108, + кишечник", "108", [("гистология", None, "108"), ("кишечник", "кишечник", "108")]),
        ("1 pc - печень", "1 pc", [("гистология", "печень", "1 pc")]),
        ("1 pc", "1 pc", [("гистология", None, "1 pc")]),
    ],
)  # fmt: skip
def test_parse_histology(text, specimen, expected):
    assert samples_of(text, specimen) == expected


@pytest.mark.parametrize("text", [None, "-", "целая рыба", "Заспиртовали полностью"])
def test_parse_histology_empty(text):
    result = parse_histology(text, "1 pc")
    assert result.samples == [] and result.warnings == []


def test_parse_histology_number_cell():
    assert samples_of(241.0, "241") == [("гистология", None, "241")]


def test_parse_histology_bag_note():
    result = parse_histology("кишечник+гистология в пакете", "241")
    assert all(s.notes == "в пакете" for s in result.samples)


def test_parse_histology_copy_paste_warns():
    # Аквахозяйство: у всех особей «26 ph б - почки»
    result = parse_histology("27 ph a - печень, 26 ph б - почки", "27 ph")
    assert len(result.samples) == 2
    assert any("копипаст" in w for w in result.warnings)


def test_parse_histology_copy_paste_3pc():
    result = parse_histology("3 p.c. a -печень, 2 р.с. b - почки", "3 pc")
    assert [s.label for s in result.samples] == ["3 pc a", "2 pc b"]
    assert any("копипаст" in w for w in result.warnings)


def test_parse_histology_own_number_no_warning():
    assert parse_histology("11 pc a - печень, b - почки", "11 pc").warnings == []


def test_smear_column_swap_detected():
    # Точка 2: в колонке «мазок крови» — '1 pc - печень', в «гистологии» — '1 pc'
    assert smear_looks_like_histology("1 pc - печень")
    assert smear_looks_like_histology("2pc - печень")
    assert not smear_looks_like_histology("1 pc")
    assert not smear_looks_like_histology("- , особь мертвая")


def test_parse_shrimp_samples():
    samples = parse_shrimp_samples(
        "Кусок мышц, кусок жабр в 10% form.",
        "Кишечник hepatopancreas и кусок каропакса заглазнчиной обл. - заморозили в пакете",
        "1 mr",
    )
    assert [(s.sample_type, s.organ, s.label, s.preservative) for s in samples] == [
        ("гистология", "мышцы", "1 mr", "формалин 10%"),
        ("гистология", "жабры", "1 mr", "формалин 10%"),
        ("другое", "кишечник, гепатопанкреас, карапакс", "1 mr", "заморозка"),
    ]


# ---------------------------------------------------------------------------
# Лаборатории
# ---------------------------------------------------------------------------


def test_parse_labs_dna():
    text = "1 образец - в Россию 1 образец - в тропцентре 1 образец - в Кантхо на секвенировании"
    assert parse_labs(text) == ["Россия", "Тропцентр", "Кантхо (секвенирование)"]


def test_parse_labs_smear():
    assert parse_labs("1 - в Россию 1 - в тропцентр") == ["Россия", "Тропцентр"]


def test_parse_labs_vietnam_company():
    # в таблице опечатка «Вьетамскую»
    assert parse_labs("Отправили на ген.анализ во Вьетамскую компанию") == [
        "Вьетнамская компания (ген. анализ)"
    ]


def test_parse_labs_unrelated():
    assert parse_labs("кровь брали шприцом из сердца") == []


# ---------------------------------------------------------------------------
# Траления и сеть
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("старт 1", (1, "старт")),
        ("финиш 8", (8, "финиш")),
        ("1 старт", (1, "старт")),
        ("4 финиш", (4, "финиш")),
        ("точка лова рыбака", (None, "точка")),
        ("№ траления", None),
        ("2 рыбак (138-142)", None),
        (None, None),
    ],
)
def test_parse_trawl_point(raw, expected):
    assert parse_trawl_point(raw) == expected


def test_parse_gear():
    assert parse_gear("Сеть: размер ячеи 5х5 см., сама сеть 15х20 м.") == ("5x5", "15x20")
    assert parse_gear("Сеть: размер ячеи 3,5х3,5 см., сама сеть 6,5х12 м.") == ("3,5x3,5", "6,5x12")
    assert parse_gear(None) == (None, None)


# ---------------------------------------------------------------------------
# Улов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2 plotosus", [("Plotosus", 2)]),
        ("10 plotosus", [("Plotosus", 10)]),
        ("2 плотосуса, 2 пангасиуса, 1 стоматопода",
         [("Plotosus", 2), ("Pangasius", 2), ("Stomatopoda", 1)]),
        ("Рыбы мало, 3 Pangasius", [("Pangasius", 3)]),
        ("4 pangasius sp.", [("Pangasius", 4)]),
        ("5 пангасиусов, внешне похож на helicophagus leptorhynchus", [("Pangasius", 5)]),
        ("5 pangasius sp, scaptophagus; купили у другого рыбака 2 плотосуса", [("Pangasius", 5)]),
        ("7 plotosus; у другого рыбакаа взяли 1 plotosus", [("Plotosus", 7)]),
        ("1 piaractus; взяли у другого рыбака 2 мертвых плотосусов)", [("Piaractus", 1)]),
        ("Купили 5 Plotosus у рыбаков", []),
        ("рыбы 0, креветок 0", []),
        ("трал пустой", []),
    ],
)  # fmt: skip
def test_parse_catch_counts(text, expected):
    assert parse_catch_counts(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("трал пустой, рыбы нет", True),
        ("Поймали Plotosus canius", True),
        ("Не поймали рыбу, т.к. порвалась сеть", True),
        ("2 plotosus", True),
        ("есть plotosus canius", True),
        ("На правом берегу драги для добычи песка", False),
        ("рядом завод", False),
        ("Рыбак сказал, что среда хорошая для пангасиусов", False),
        ("судоходная часть реки, большая глубина", False),
        ("Купили 5 Plotosus у рыбаков", False),
    ],
)
def test_is_catch_comment(text, expected):
    assert is_catch_comment(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("arius maculatus - 39 шт", True),
        ("arius sp.-4шт.", True),
        ("m креветок: 1754 г.", True),
        ("Общий вес рыбы: 1737г.", True),
        ("краб (3 траление) в DNA пробирке 30кр", True),
        ("угорь - 4 шт", True),
        ("52 - 65 (вкл) - только гистология", False),
        ("пробы 18-23 пойманы в районе Тань-фу; 24-26 - в районе Ба-Ти", False),
    ],
)
def test_is_catch_note(text, expected):
    assert is_catch_note(text) is expected


@pytest.mark.parametrize(
    "text, taxon, count, mass, trawl",
    [
        ("arius maculatus - 39 шт", "arius maculatus", 39, None, None),
        ("arius sp.-4шт.", "arius sp.", 4, None, None),
        ("polynemus melanochir -  4 шт", "polynemus melanochir", 4, None, None),
        ("рыба-жаба - 1 шт", "рыба-жаба", 1, None, None),
        ("сем. Карповые - 1 шт.", "сем. Карповые", 1, None, None),
        ("m креветок: 1754 г.", "креветок", None, 1754.0, None),
        ("m  остальной рыбы: 2102 г.", "остальной рыбы", None, 2102.0, None),
        ("m креветок 414", "креветок", None, 414.0, None),
        ("Общий вес рыбы: 1737г.", "рыбы", None, 1737.0, None),
        ("краб (3 траление) в DNA пробирке 30кр", "краб", None, None, 3),
        ("2 креветки (4 траление) в DNA пробирке 30кв", "креветки", 2, None, 4),
    ],
)
def test_parse_catch_note(text, taxon, count, mass, trawl):
    note = parse_catch_note(text)
    assert (note.taxon_text, note.count, note.mass_g, note.trawl_no) == (taxon, count, mass, trawl)
