#!/usr/bin/env python3
"""
Импорт полевой таблицы уловов (Вьетнам 2025–2026) в базу SQLite.

Запуск:
    python3 import_excel.py source.xlsx vietnam_fish.db

Скрипт:
  * создаёт базу по schema.sql (старый файл базы перезаписывается);
  * разбирает все листы выездов: особи, образцы, траления, замеры воды,
    заметки об улове и комментарии к ячейкам;
  * приводит числа к одному формату ('10,103733.' -> 10.103733);
  * приводит названия видов к одному написанию;
  * всё спорное пишет в таблицу data_issue, ничего не исправляя молча.
"""
import datetime as dt
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict

import openpyxl

SRC = sys.argv[1] if len(sys.argv) > 1 else "source.xlsx"
DB = sys.argv[2] if len(sys.argv) > 2 else "vietnam_fish.db"
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# ----------------------------------------------------------------------------
# Справочник видов: исходное написание -> (латинское название, уверенность)
# ----------------------------------------------------------------------------
TAXA = {
    # name: (genus, code, group)
    "Pangasius sp.": ("Pangasius", "sp", "рыба"),
    "Pangasius elongatus": ("Pangasius", "e", "рыба"),
    "Pangasius macronema": ("Pangasius", "mc", "рыба"),
    "Pangasius conchophilus": ("Pangasius", None, "рыба"),
    "Pangasianodon hypophthalmus": ("Pangasianodon", "ph", "рыба"),
    "Plotosus canius": ("Plotosus", "pc", "рыба"),
    "Bagarius sp.": ("Bagarius", "Bg", "рыба"),
    "Macrobrachium rosenbergii": ("Macrobrachium", "mr", "ракообразное"),
}


def parse_taxon(raw):
    """Возвращает (название из справочника или None, уверенность, примечание)."""
    if raw is None:
        return None, "не определён", None
    s = " ".join(str(raw).split())
    low = s.lower()
    if s in ("?",) or "вид неизвестен" in low:
        return None, "не определён", s
    if "не elongatus" in low:
        return "Pangasius sp.", "не определён", s
    conf = "точно"
    if low.startswith(("пред.", "предп.")):
        conf = "предп."
    if "elongatus" in low:
        return "Pangasius elongatus", conf, None
    if "macronema" in low:
        return "Pangasius macronema", conf, None
    if "conchophilus" in low:
        return "Pangasius conchophilus", conf, None
    if "hypophthalmus" in low:
        note = "в таблице написано «Pangasiodon»" if "pangasiodon" in low else None
        return "Pangasianodon hypophthalmus", conf, note
    if low.startswith("pangasius sp"):
        return "Pangasius sp.", "до рода", None
    if "plotosus" in low:
        return "Plotosus canius", conf, None
    if "bagarius" in low:
        return "Bagarius sp.", conf, None
    if "macrobra" in low or "rozenberg" in low or "rosenberg" in low:
        return "Macrobrachium rosenbergii", conf, "в таблице написано «macrobrahius rozenbergii»"
    return None, "не определён", s


# ----------------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------------
issues = []


def issue(category, sheet, cell, obj, raw, desc):
    issues.append((category, sheet, cell, obj, None if raw is None else str(raw), desc))


text_number_count = defaultdict(int)   # сколько чисел на листе записано текстом


def num(v, sheet=None, cell=None, obj=None):
    """Число из ячейки. Текст вида '10,103733.' / '105, 96480' / '29,3.' тоже понимает."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dt.datetime):
        # Excel превратил число в дату (например 29.9 -> 29 января 1900)
        serial = (v - dt.datetime(1899, 12, 31)).total_seconds() / 86400
        serial = round(serial, 4)
        issue("формат", sheet, cell, obj, v,
              f"Число превратилось в дату; восстановлено как {serial:g}. Проверьте.")
        return serial
    s = str(v).strip()
    if s in ("", "-", "—", "–"):
        return None
    t = s.replace("°", "").replace(" ", "").rstrip(".")
    t = t.replace(",", ".")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(min|мин)?", t)
    if m:
        if sheet and re.search(r"[, °]|\.$", s):
            text_number_count[sheet] += 1
        return float(m.group(1))
    return None


def fmt_time(v, sheet=None, cell=None, obj=None):
    if v is None:
        return None
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, dt.datetime):
        return v.strftime("%H:%M")
    if isinstance(v, (int, float)):
        h = int(v)
        mnt = int(round((v - h) * 100))
        issue("формат", sheet, cell, obj, v, f"Время записано числом; понято как {h:02d}:{mnt:02d}")
        return f"{h:02d}:{mnt:02d}"
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def clean_comment(c):
    if c is None:
        return None
    t = c.text or ""
    t = re.sub(r"^=+\s*\nID#\S+\s*\n[^\n]*\(\d{4}-\d\d-\d\d[^)]*\)\s*\n", "", t)
    t = re.sub(r"-{5,}.*$", "", t, flags=re.S)
    t = " ".join(t.split())
    return t or None


def is_blank(v):
    return v is None or (isinstance(v, str) and v.strip() in ("", "-", "—", "–"))


WHOLE_WORDS = ("целая рыба", "целиком", "полностью", "целая особь")


def is_whole(v):
    return isinstance(v, str) and any(w in v.lower() for w in WHOLE_WORDS)


def parse_date(text):
    if not text:
        return None
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{2,4})", str(text))
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    return f"{y:04d}-{mo:02d}-{d:02d}"


def dist_km(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371 * math.hypot(x, y)


ORGANS = [("печен", "печень"), ("почк", "почки"), ("икр", "икра"), ("кишечник", "кишечник"),
          ("жабр", "жабры"), ("мышц", "мышцы"), ("hepatopancreas", "гепатопанкреас")]


def organ_of(text):
    low = text.lower()
    for key, name in ORGANS:
        if key in low:
            return name
    return None


SUFFIX_MAP = {"е": "e", "e": "e", "sp": "sp", "ph": "ph", "mc": "mc", "pc": "pc", "рс": "pc",
              "bg": "Bg", "mr": "mr", "": ""}


def parse_label(v):
    """'26 ph' / '107.0' / '3 p.c.' / '155  pc' -> (label, series, number, suffix) или None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return str(n), "Pangasius", n, ""
    s = str(v).strip()
    m = re.fullmatch(r"(\d+)(?:\.0)?\s*([A-Za-zА-Яа-яЁё. ]*)", s)
    if not m:
        return None
    n = int(m.group(1))
    suf = m.group(2).replace(".", "").replace(" ", "").lower()
    if suf not in SUFFIX_MAP:
        return None
    suf = SUFFIX_MAP[suf]
    series = {"pc": "Plotosus (pc)", "Bg": "Bagarius (Bg)", "mr": "Macrobrachium (mr)"}.get(suf, "Pangasius")
    label = f"{n} {suf}".strip()
    return label, series, n, suf


# ----------------------------------------------------------------------------
# Разбор заголовков
# ----------------------------------------------------------------------------
def spec_columns(ws, hr):
    cols = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(hr, c).value
        if not isinstance(v, str):
            continue
        low = " ".join(v.lower().split())
        if low.startswith("№ пробы"):
            cols["label"] = c
        elif low == "вид":
            cols["species"] = c
        elif low == "dna":
            cols["dna"] = c
        elif low == "кишечник":
            cols["gut"] = c
        elif low == "гистология":
            cols["histo"] = c
        elif low.startswith("мазок"):
            cols["smear"] = c
        elif low.startswith("tl"):
            cols["tl"] = c
        elif low.startswith("sl"):
            cols["sl"] = c
        elif low.startswith("m (г"):
            cols["mass"] = c
        elif low.startswith("m (без порки") or low.startswith("m (порка"):
            cols["gutted"] = c
    return cols


def haul_columns(ws):
    """Ищет строку с '№ траления' и возвращает (номер строки, словарь колонок)."""
    for r in range(1, min(ws.max_row, 8) + 1):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and v.strip().lower() == "№ траления":
                cols = {"haul": c}
                for cc in range(c + 1, ws.max_column + 1):
                    h = ws.cell(r, cc).value
                    if not isinstance(h, str):
                        continue
                    low = " ".join(h.lower().split())
                    if "garmin" in low and "результата" in low:
                        cols["wp"] = cc
                    elif low.startswith("o2"):
                        cols["o2"] = cc
                    elif low == "ppm":
                        cols["ppm"] = cc
                    elif low == "ms":
                        cols["ms"] = cc
                    elif low == "us":
                        cols["us"] = cc
                    elif low == "ph":
                        cols["ph"] = cc
                    elif low.startswith("n ("):
                        cols["lat"] = cc
                        cols["coord_source"] = "Google Maps" if "google" in low else "Garmin"
                    elif low.startswith("e ("):
                        cols["lon"] = cc
                    elif low.startswith("промежуточная"):
                        cols["idepth"] = cc
                    elif low.startswith("глубина"):
                        cols["depth"] = cc
                    elif low.startswith("t старта"):
                        cols["ts"] = cc
                    elif low.startswith("t финиша"):
                        cols["tf"] = cc
                    elif low.startswith("t траления"):
                        cols["dur"] = cc
                    elif low.startswith("t воды"):
                        cols["wt"] = cc
                    elif low.startswith("v"):
                        cols["speed"] = cc
                return r, cols
    return None, {}


CATCH_WORDS = ("рыб", "улов", "поймал", "трал", "pangas", "plotos", "пангас", "плотос",
               "креветок", "карпов", "parambasis", "polynemus", "piaractus", "экземпляр")


def is_catch_comment(t):
    low = t.lower()
    if parse_catch_counts(t):
        return True
    if any(w in low for w in ("берег", "завод", "сказал", "перерыв")):
        return False
    return any(w in low for w in CATCH_WORDS)


def parse_catch_counts(text):
    """'2 plotosus, 3 pangasius' -> [('Plotosus', 2), ('Pangasius', 3)]"""
    out = []
    for seg in re.split(r";", text.lower()):
        if any(w in seg for w in ("купил", "взял", "у рыбак", "у другого")):
            continue          # куплено или взято у рыбаков, а не поймано тралом
        for m in re.finditer(r"(\d+)\s+(plotosus|pangasius|плотосус\w*|пангасиус\w*)", seg):
            name = "Plotosus" if m.group(2).startswith(("plot", "плот")) else "Pangasius"
            out.append((name, int(m.group(1))))
    return out


def parse_catch_note(text):
    """Строка-заметка внизу листа -> (taxon_text, count, mass_g, haul_no)."""
    t = " ".join(str(text).split())
    haul_no = None
    m = re.search(r"\((\d+)\s*траление\)", t)
    if m:
        haul_no = int(m.group(1))
    count = mass = None
    low = t.lower()
    m = re.search(r"(\d+)\s*шт", low)
    if m:
        count = int(m.group(1))
    if low.startswith("m ") or low.startswith("m ") or "вес" in low or low.startswith("m "):
        nums = re.findall(r"(\d+(?:[.,]\d+)?)", t)
        if nums:
            mass = float(nums[-1].replace(",", "."))
    taxon = re.split(r"\s[-–:]\s?|:\s|\s-|-\d|\(", t)[0].strip(" -:")
    taxon = re.sub(r"\s+\d+(?:[.,]\d+)?\s*г?\.?$", "", taxon)
    return taxon or None, count, mass, haul_no


# ----------------------------------------------------------------------------
# Основной разбор
# ----------------------------------------------------------------------------
def main():
    wb = openpyxl.load_workbook(SRC)
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(open(SCHEMA, encoding="utf-8").read())
    cur = con.cursor()

    taxon_id = {}
    for name, (genus, code, grp) in TAXA.items():
        cur.execute("INSERT INTO taxon(name, genus, code, grp) VALUES (?,?,?,?)", (name, genus, code, grp))
        taxon_id[name] = cur.lastrowid

    expedition = None
    exp_ids = {}
    exp_net = {}
    all_labels = {}                  # label -> (sheet, row)
    series_numbers = defaultdict(list)

    for ws in wb.worksheets:
        name = ws.title
        if name.startswith("Условные"):
            continue
        # Пустой лист-разделитель = экспедиция
        if ws.max_row <= 1 and ws.max_column <= 1:
            expedition = name
            cur.execute("INSERT INTO expedition(name) VALUES (?)", (name,))
            exp_ids[name] = cur.lastrowid
            continue

        low_name = name.lower()
        etype = ("рынок" if "рынок" in low_name else
                 "рыбак" if "рыбак" in low_name else
                 "аквахозяйство" if "аквахоз" in low_name else "трал")
        origin_default = {"трал": "трал", "рынок": "рынок", "рыбак": "рыбак",
                          "аквахозяйство": "аквахозяйство"}[etype]

        # Строка заголовка особей
        hr = None
        for r in range(1, 5):
            v = ws.cell(r, 1).value
            if isinstance(v, str) and v.strip().lower().startswith("№ пробы"):
                hr = r
                break
        if hr is None:
            issue("формат", name, None, None, None, "Не найдена строка «№ пробы» — лист пропущен")
            continue
        header_text = ws.cell(1, 1).value if hr > 1 else None
        event_date = parse_date(header_text)
        if event_date is None:
            issue("формат", name, "A1", None, header_text, "На листе не указана дата выезда")

        event_notes = []
        cur.execute("INSERT INTO event(expedition_id, sheet_name, event_date, event_type, locality) "
                    "VALUES (?,?,?,?,?)",
                    (exp_ids[expedition], name, event_date, etype,
                     " ".join(str(header_text).split()) if header_text else None))
        event_id = cur.lastrowid

        sc = spec_columns(ws, hr)
        hhr, hc = haul_columns(ws)
        haul_first_col = hc.get("haul", 10 ** 6)

        # --- комментарии в шапке: сеть, способ взятия проб, куда отправлены пробы
        dna_storage = smear_storage = None
        for r in range(1, hr + 2 if hhr is None else max(hr, hhr) + 1):
            for c in range(1, ws.max_column + 1):
                cm = clean_comment(ws.cell(r, c).comment)
                if not cm:
                    continue
                v = ws.cell(r, c).value
                vlow = str(v).lower() if v is not None else ""
                if r not in (hr, hhr):
                    continue
                if "ячеи" in cm:
                    mesh = re.search(r"ячеи\s*([\d,]+\s*[хx]\s*[\d,]+)", cm)
                    size = re.search(r"сеть\s*([\d,]+\s*[хx]\s*[\d,]+)", cm)
                    exp_net.setdefault(expedition, (mesh.group(1).replace(" ", "") if mesh else None,
                                                    size.group(1).replace(" ", "") if size else None))
                elif vlow == "dna" and "росси" in cm.lower():
                    dna_storage = "Россия; тропцентр; Кантхо (секвенирование)"
                elif vlow.startswith("мазок") and "росси" in cm.lower():
                    smear_storage = "Россия; тропцентр"
                elif vlow.startswith("мазок") or "шприц" in cm:
                    pass   # методика мазка — одинаковая, описана в README
                else:
                    event_notes.append(f"{v}: {cm}")

        # --- координаты для рынка/рыбака/фермы и точки рыбаков внутри «точек»
        fisher_ranges = []   # (lo, hi, lat, lon, label)
        for r in range(1, ws.max_row + 1):
            for c in range(1, ws.max_column + 1):
                v = ws.cell(r, c).value
                if not isinstance(v, str):
                    continue
                vl = v.lower()
                if not (vl.startswith("координаты") or "рыбак (" in vl):
                    continue
                a, b = ws.cell(r, c + 1).value, ws.cell(r, c + 2).value
                if a == "N" and b == "E":           # значения строкой ниже (Аквахозяйство)
                    a, b = ws.cell(r + 1, c + 1).value, ws.cell(r + 1, c + 2).value
                lat, lon = num(a), num(b)
                if lat is None or lon is None:
                    continue
                rng = re.search(r"\((\d+)\s*(?:-\s*(\d+))?\)", v)
                if rng:
                    lo = int(rng.group(1))
                    hi = int(rng.group(2) or lo)
                    fisher_ranges.append((lo, hi, lat, lon, " ".join(v.split())))
                else:
                    cur.execute("UPDATE event SET lat=?, lon=? WHERE event_id=?", (lat, lon, event_id))

        # --- траления
        hauls = {}              # (kind, no) -> dict
        if hhr:
            for r in range(hhr + 1, ws.max_row + 1):
                hv = ws.cell(r, hc["haul"]).value
                if not isinstance(hv, str):
                    continue
                hs = " ".join(hv.lower().split())
                m = re.fullmatch(r"(?:(\d+)\s*(старт|финиш)|(старт|финиш)\s*(\d+))", hs)
                if m:
                    no = int(m.group(1) or m.group(4))
                    ptype = m.group(2) or m.group(3)
                    kind = "трал"
                elif "точка лова рыбака" in hs:
                    no, ptype, kind = 1, "точка", "точка рыбака"
                else:
                    continue
                h = hauls.setdefault((kind, no), {"points": {}, "notes": [], "catch": [], "row": r})

                def g(key):
                    return ws.cell(r, hc[key]).value if key in hc else None

                def cell_ref(key):
                    return ws.cell(r, hc[key]).coordinate if key in hc else None

                obj = f"траление {no} {ptype}"
                point = {
                    "wp": None if g("wp") is None else str(g("wp")).strip(),
                    "lat": num(g("lat"), name, cell_ref("lat"), obj),
                    "lon": num(g("lon"), name, cell_ref("lon"), obj),
                    "depth": num(g("depth"), name, cell_ref("depth"), obj),
                    "o2": num(g("o2"), name, cell_ref("o2"), obj),
                    "ppm": num(g("ppm"), name, cell_ref("ppm"), obj),
                    "ms": num(g("ms"), name, cell_ref("ms"), obj),
                    "us": num(g("us"), name, cell_ref("us"), obj),
                    "ph": num(g("ph"), name, cell_ref("ph"), obj),
                    "wt": num(g("wt"), name, cell_ref("wt"), obj),
                    "notes": [],
                }
                if ptype == "старт":
                    h["ts"] = fmt_time(g("ts"), name, cell_ref("ts"), obj)
                    h["idepth"] = num(g("idepth"), name, cell_ref("idepth"), obj)
                if ptype == "финиш":
                    h["tf"] = fmt_time(g("tf"), name, cell_ref("tf"), obj)
                d = num(g("dur"), name, cell_ref("dur"), obj)
                if d is not None:
                    h["dur"] = d
                sp = num(g("speed"), name, cell_ref("speed"), obj)
                if sp is not None:
                    h.setdefault("speeds", []).append(sp)
                for key in ("lat", "lon"):
                    val = point[key]
                    if val is not None and not ((8 <= val <= 12) if key == "lat" else (104 <= val <= 108)):
                        issue("координаты", name, cell_ref(key), obj, g(key), "Координата вне района работ")
                # комментарии к ячейкам траления
                for c in range(hc["haul"], ws.max_column + 1):
                    cm = clean_comment(ws.cell(r, c).comment)
                    if not cm:
                        continue
                    if c == hc["haul"]:
                        if is_catch_comment(cm):
                            h["catch"].append(cm)
                        else:
                            h["notes"].append(cm)
                    else:
                        head = ws.cell(hhr, c).value
                        head = " ".join(str(head).split()) if head else ws.cell(r, c).coordinate
                        if c in (hc.get("ts"), hc.get("tf"), hc.get("dur")):
                            h["notes"].append(cm)
                        else:
                            point["notes"].append(f"{head}: {cm}")
                h["points"][ptype] = point

        haul_ids = {}
        for (kind, no), h in sorted(hauls.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            speeds = h.get("speeds") or []
            cur.execute("INSERT INTO haul(event_id, haul_no, kind, time_start, time_end, duration_min, "
                        "speed_kmh, intermediate_depth_m, catch_note, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (event_id, no, kind, h.get("ts"), h.get("tf"), h.get("dur"),
                         round(sum(speeds) / len(speeds), 2) if speeds else None, h.get("idepth"),
                         "; ".join(h["catch"]) or None, "; ".join(h["notes"]) or None))
            hid = cur.lastrowid
            haul_ids[(kind, no)] = hid
            for ptype, p in h["points"].items():
                cur.execute("INSERT INTO haul_point(haul_id, point_type, garmin_wp, lat, lon, coord_source, "
                            "depth_m, o2_pct, ppm, ms, us, ph, water_temp_c, notes) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (hid, ptype, p["wp"], p["lat"], p["lon"], hc.get("coord_source"),
                             p["depth"], p["o2"], p["ppm"], p["ms"], p["us"], p["ph"], p["wt"],
                             "; ".join(p["notes"]) or None))
            for cm in h["catch"]:
                for taxon_text, cnt in parse_catch_counts(cm):
                    cur.execute("INSERT INTO catch_record(event_id, haul_id, taxon_text, count_n, raw_text) "
                                "VALUES (?,?,?,?,?)", (event_id, hid, taxon_text, cnt, cm))
            if kind == "трал" and ("старт" not in h["points"] or "финиш" not in h["points"]):
                issue("связь", name, None, f"траление {no}", None, "У траления нет строки старта или финиша")

        # Проверка скачков координат внутри выезда
        pts = [(p["lat"], p["lon"], no, pt) for (k, no), h in hauls.items()
               for pt, p in h["points"].items() if p["lat"] and p["lon"]]
        if len(pts) >= 4:
            mlat = sorted(p[0] for p in pts)[len(pts) // 2]
            mlon = sorted(p[1] for p in pts)[len(pts) // 2]
            for lat, lon, no, pt in pts:
                d = dist_km((lat, lon), (mlat, mlon))
                if d > 30:
                    issue("координаты", name, None, f"траление {no} {pt}", f"{lat}, {lon}",
                          f"Точка в {d:.0f} км от остальных точек этого выезда")

        # --- особи и заметки
        fisher_from_here = None     # после комментария «далее рыба пойманная ... рыбаком»
        spec_rows = []
        for r in range(hr + 1, ws.max_row + 1):
            a = ws.cell(r, sc["label"]).value
            b = ws.cell(r, sc["species"]).value if "species" in sc else None
            parsed = parse_label(a) if b is not None else None
            if a is not None and str(a).strip() == "ХХ":
                parsed = (f"ХХ ({name})", None, None, "")
            if parsed:
                spec_rows.append((r, parsed))
                continue
            # не особь: заметка / улов
            texts = [ws.cell(r, c).value for c in range(1, haul_first_col)]
            texts = [t for t in texts if isinstance(t, str) and t.strip() not in ("", "-")]
            if not texts:
                continue
            t = " ".join(texts[0].split())
            tl = t.lower()
            if re.search(r"шт|m\s|вес|\bm\b|креветк|краб|угорь|рыба-жаба|рыбы sp", tl) and not tl.startswith("пробы "):
                taxon_text, cnt, mass, hno = parse_catch_note(t)
                hid = haul_ids.get(("трал", hno)) if hno else None
                cur.execute("INSERT INTO catch_record(event_id, haul_id, taxon_text, count_n, mass_g, raw_text) "
                            "VALUES (?,?,?,?,?,?)", (event_id, hid, taxon_text, cnt, mass, t))
            else:
                event_notes.append(t)
                if re.search(r"\d+\s*-\s*\d+.*только гистология", tl):
                    issue("номера", name, ws.cell(r, 1).coordinate, t, t,
                          "Для этих номеров нет отдельных строк особей — промеры и вид не записаны")

        for r, (label, series, sno, suf) in spec_rows:
            def v(key):
                return ws.cell(r, sc[key]).value if key in sc else None

            def ref(key):
                return ws.cell(r, sc[key]).coordinate if key in sc else None

            label_raw = v("label")
            obj = label
            if label in all_labels:
                issue("номера", name, ref("label"), label, label_raw,
                      f"Номер уже встречался на листе «{all_labels[label][0]}»")
                label = f"{label} ({name})"
            all_labels[label] = (name, r)
            if sno is not None:
                series_numbers[series].append((sno, name))

            tname, conf, tnote = parse_taxon(v("species"))
            notes = []
            if tnote:
                notes.append(f"вид: {tnote}")

            # комментарии к ячейкам особи
            comments = {}
            for key, c in sc.items():
                cm = clean_comment(ws.cell(r, c).comment)
                if cm:
                    comments[key] = cm
            is_dead = 0
            for key, cm in comments.items():
                lowc = cm.lower()
                if "мертв" in lowc or "погиб" in lowc:
                    is_dead = 1
                if key == "smear" and ("шприц" in lowc):
                    continue
                if any(cm in n for n in notes):
                    continue
                notes.append(cm if key in ("label", "species") else f"{key}: {cm}")
                if key == "label" and "рыбаком" in lowc and "далее" in lowc:
                    fisher_from_here = series
            for key in ("dna", "histo", "smear", "gut", "tl"):
                val = v(key)
                if isinstance(val, str) and ("мертв" in val.lower()):
                    is_dead = 1

            origin = origin_default
            haul_ref = None
            cap_lat = cap_lon = None
            if fisher_from_here is not None and fisher_from_here == series:
                origin = "рыбак"
                haul_ref = haul_ids.get(("точка рыбака", 1))
            for lo, hi, lat, lon, flabel in fisher_ranges:
                if sno is not None and lo <= sno <= hi:
                    cap_lat, cap_lon = lat, lon
                    origin = "рыбак"
                    notes.append(f"место поимки: {flabel}")
            for key, cm in comments.items():
                m = re.search(r"(\d+)\s*[-–]\s*(\d+).*аквафер", cm.lower())
                if m:
                    pass
            # диапазон «129 - 132 e - из аквафермы» из комментария к первой особи
            for rr, (lab2, ser2, sno2, _) in spec_rows:
                cm = clean_comment(ws.cell(rr, sc["label"]).comment)
                if cm and "аквафер" in cm.lower():
                    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", cm)
                    if m and ser2 == series and sno is not None and int(m.group(1)) <= sno <= int(m.group(2)):
                        origin = "аквахозяйство"

            tl = num(v("tl"), name, ref("tl"), obj)
            sl = num(v("sl"), name, ref("sl"), obj)
            mass = num(v("mass"), name, ref("mass"), obj)
            gut_m = num(v("gutted"), name, ref("gutted"), obj)
            whole = int(any(is_whole(v(k)) for k in ("dna", "histo", "tl", "mass")) or
                        (isinstance(v("dna"), str) and "в пробирку" in v("dna")))

            cur.execute("INSERT INTO specimen(label, label_raw, series, series_no, event_id, haul_id, taxon_id, "
                        "id_confidence, taxon_raw, origin, tl_cm, sl_cm, mass_g, mass_gutted_g, whole_fixed, "
                        "is_dead, capture_lat, capture_lon, source_row, notes) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (label, None if label_raw is None else str(label_raw), series, sno, event_id, haul_ref,
                         taxon_id.get(tname), conf, None if v("species") is None else " ".join(str(v("species")).split()),
                         origin, tl, sl, mass, gut_m, whole, is_dead, cap_lat, cap_lon, r,
                         "; ".join(notes) or None))
            spec_id = cur.lastrowid

            def add_sample(stype, organ=None, lab=None, preservative=None, storage=None, raw=None, note=None):
                cur.execute("INSERT INTO sample(specimen_id, sample_type, organ, label, preservative, storage, "
                            "raw_value, notes) VALUES (?,?,?,?,?,?,?,?)",
                            (spec_id, stype, organ, lab, preservative, storage,
                             None if raw is None else str(raw), note))

            # ---------------- образцы ----------------
            if whole:
                add_sample("целая особь", lab=label, preservative="этанол",
                           raw=v("dna") if is_whole(v("dna")) else None)

            if series == "Macrobrachium (mr)":
                # у креветок колонки использованы по-другому: мышцы и жабры, внутренности
                d_val, e_val = v("histo"), v("smear")
                if isinstance(d_val, str):
                    for part in d_val.split(","):
                        add_sample("гистология", organ_of(part), label, "формалин 10%", raw=d_val)
                if isinstance(e_val, str):
                    add_sample("другое", "кишечник, гепатопанкреас, карапакс", label, "заморозка", raw=e_val)
            else:
                dna = v("dna")
                if not is_blank(dna) and not is_whole(dna) and "в пробирку" not in str(dna):
                    add_sample("ДНК", lab=parse_label(dna)[0] if parse_label(dna) else str(dna),
                               storage=dna_storage, raw=dna)

                gut = v("gut")
                if not is_blank(gut) and not is_whole(gut):
                    add_sample("кишечник", "кишечник",
                               parse_label(gut)[0] if parse_label(gut) else str(gut).strip(), raw=gut)

                histo, smear = v("histo"), v("smear")
                smear_has_organ = isinstance(smear, str) and organ_of(smear) is not None
                if smear_has_organ:
                    issue("формат", name, ref("smear"), label, smear,
                          "В колонке «мазок крови» записан орган — похоже, значения гистологии и мазка "
                          "перепутаны местами; импортировано как гистология + мазок")
                    histo, smear = smear, histo
                if isinstance(histo, str) and not is_blank(histo) and not is_whole(histo):
                    htext = " ".join(histo.split())
                    nums_in = {int(x) for x in re.findall(r"\d+", htext)}
                    if sno is not None and nums_in and nums_in != {sno}:
                        issue("номера", name, ref("histo"), label, htext,
                              "В метке гистологии стоит другой номер пробы (вероятно, копипаст)")
                    parts = [p.strip() for p in re.split(r",|\+|\s(?=\d+\s?[a-zа-я]*\s?[a-zа-яё]\s?-)", htext) if p.strip()]
                    made = False
                    for part in parts:
                        pl = part.lower()
                        pres = "в пакете" if "пакет" in htext.lower() else None
                        if "гистология" in pl:
                            add_sample("гистология", None, label, pres, raw=htext)
                            made = True
                            continue
                        organ = organ_of(part)
                        if organ is None and ("нет" in pl or made):
                            continue
                        if "почек нет" in pl or "не брали" in pl:
                            continue
                        head = re.split(r"\s[-–]\s?|-|\(", part)[0].strip()
                        if organ and organ in head.lower():
                            head = ""
                        if re.fullmatch(r"[a-zа-я]", head.lower()):
                            lab = f"{label} {head}"
                        elif head and re.match(r"\d", head):
                            lab = " ".join(head.split())
                            pl2 = parse_label(lab)
                            if pl2:
                                lab = pl2[0]
                        else:
                            lab = label
                        stype = "кишечник" if organ == "кишечник" else "гистология"
                        add_sample(stype, organ, lab, pres, raw=htext)
                        made = True
                    if not made:
                        add_sample("гистология", None, htext, raw=htext)
                elif isinstance(histo, (int, float)):
                    add_sample("гистология", None, str(int(histo)), raw=histo)

                if not is_blank(smear) and not is_whole(smear):
                    sl_ = str(smear).lower()
                    if "мертв" in sl_ or sl_.startswith("нет"):
                        pass
                    else:
                        note = comments.get("smear") if comments.get("smear") and "шприц" not in comments["smear"] else None
                        add_sample("мазок крови", lab=parse_label(smear)[0] if parse_label(smear) else str(smear),
                                   preservative="этанол 96% (фиксация)", storage=smear_storage, raw=smear,
                                   note=note)

            # ---------------- проверки промеров ----------------
            if tl and sl and sl > tl:
                issue("промеры", name, ref("sl"), label, f"TL {tl:g}, SL {sl:g}", "SL больше TL")
            if tl and sl and sl < 0.6 * tl:
                issue("промеры", name, ref("sl"), label, f"TL {tl:g}, SL {sl:g}", "SL слишком мала для такой TL")
            if mass and gut_m and gut_m > mass:
                issue("промеры", name, ref("gutted"), label, f"m {mass:g}, m без внутр. {gut_m:g}",
                      "Масса без внутренностей больше полной массы")
            if tl and mass:
                k = 100 * mass / tl ** 3
                if k < 0.2 or k > 5:
                    issue("промеры", name, ref("tl"), label, f"TL {tl:g} см, m {mass:g} г",
                          f"Длина и масса не согласуются (коэф. упитанности {k:.2f})")
            if tl and sl and mass and gut_m and mass == tl and gut_m == sl:
                issue("промеры", name, ref("mass"), label, f"m {mass:g}, {gut_m:g}",
                      "Массы совпадают с длинами — вероятно, скопированы")
            code = TAXA.get(tname, (None, None, None))[1] if tname else None
            if suf in ("e", "ph", "mc") and code != suf:
                issue("вид", name, ref("label"), label, v("species"),
                      f"Суффикс «{suf}» в номере не соответствует виду {tname or '(не определён)'}")

        cur.execute("UPDATE event SET notes=? WHERE event_id=?", ("; ".join(event_notes) or None, event_id))
        if etype == "трал" and hauls and spec_rows:
            issue("связь", name, None, None, None,
                  "Особи не привязаны к конкретным тралениям — восстановить по полевому журналу")
        if text_number_count[name]:
            issue("формат", name, None, None, None,
                  f"{text_number_count[name]} чисел записаны текстом (запятая, пробел или точка в конце); "
                  "при импорте прочитаны как числа")

    # --- экспедиции: даты и сеть
    for exp, eid in exp_ids.items():
        d = cur.execute("SELECT min(event_date), max(event_date) FROM event WHERE expedition_id=?", (eid,)).fetchone()
        mesh, size = exp_net.get(exp, (None, None))
        year = int(d[0][:4]) if d[0] else None
        cur.execute("UPDATE expedition SET year=?, date_start=?, date_end=?, net_mesh_cm=?, net_size_m=? "
                    "WHERE expedition_id=?", (year, d[0], d[1], mesh, size, eid))

    # --- пропуски и повторы в сквозной нумерации
    for series, lst in series_numbers.items():
        nums = [n for n, _ in lst]
        seen = defaultdict(list)
        for n, sh in lst:
            seen[n].append(sh)
        dups = sorted(n for n, shs in seen.items() if len(shs) > 1)
        if dups:
            issue("номера", None, None, series, ", ".join(map(str, dups)),
                  "Номера повторяются в сквозной нумерации серии (разные суффиксы, разные листы)")
        missing = [n for n in range(1, max(nums) + 1) if n not in seen]
        if missing:
            ranges, start = [], missing[0]
            for a, b in zip(missing, missing[1:] + [None]):
                if b != a + 1:
                    ranges.append(f"{start}–{a}" if start != a else str(a))
                    start = b
            issue("номера", None, None, series, ", ".join(ranges), "Номера пропущены в сквозной нумерации")

    grouped = defaultdict(list)
    for it in issues:
        grouped[(it[0], it[1], it[5])].append(it)
    final = []
    for (cat, sh, desc), items in grouped.items():
        if len(items) >= 4 and all(i[3] for i in items):
            objs = ", ".join(i[3] for i in items)
            cells = f"{items[0][2]}…{items[-1][2]}" if items[0][2] else None
            final.append((cat, sh, cells, objs, items[0][4], f"{desc} ({len(items)} шт.)"))
        else:
            final.extend(items)
    issues[:] = final
    cur.executemany("INSERT INTO data_issue(category, sheet_name, cell, object, raw_value, description) "
                    "VALUES (?,?,?,?,?,?)", issues)
    con.commit()
    con.close()
    print(f"Готово: {DB}")


if __name__ == "__main__":
    main()
