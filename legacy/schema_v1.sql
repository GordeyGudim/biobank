-- =====================================================================
--  База данных уловов, Вьетнам 2025–2026
--  СУБД: SQLite (схема почти без изменений переносится в PostgreSQL)
--
--  Порядок связей:
--    expedition 1 → N event 1 → N haul 1 → N haul_point
--                          event 1 → N specimen 1 → N sample
--                          haul  1 → N specimen   (если известно)
--                          taxon 1 → N specimen
--                          event/haul 1 → N catch_record
--  data_issue — журнал спорных значений, найденных при импорте
-- =====================================================================

PRAGMA foreign_keys = ON;

-- Экспедиция: Донг-хап, Виньлонг, Кантхо
CREATE TABLE expedition (
    expedition_id   INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,        -- как на листе-разделителе
    year            INTEGER,
    date_start      TEXT,                        -- ГГГГ-ММ-ДД
    date_end        TEXT,
    net_mesh_cm     TEXT,                        -- размер ячеи сети, напр. '5x5'
    net_size_m      TEXT,                        -- размер сети, напр. '15x20'
    notes           TEXT
);

-- Выезд: один лист Excel = один выезд
CREATE TABLE event (
    event_id        INTEGER PRIMARY KEY,
    expedition_id   INTEGER NOT NULL REFERENCES expedition(expedition_id),
    sheet_name      TEXT NOT NULL,               -- имя исходного листа
    event_date      TEXT,                        -- ГГГГ-ММ-ДД
    event_type      TEXT NOT NULL CHECK (event_type IN ('трал','рынок','рыбак','аквахозяйство')),
    locality        TEXT,                        -- строка-заголовок листа как есть
    lat             REAL,                        -- координаты для рынка/рыбака/фермы
    lon             REAL,
    notes           TEXT
);

-- Траление (или точка лова рыбака с замером воды)
CREATE TABLE haul (
    haul_id              INTEGER PRIMARY KEY,
    event_id             INTEGER NOT NULL REFERENCES event(event_id),
    haul_no              INTEGER,                -- номер траления на выезде
    kind                 TEXT NOT NULL DEFAULT 'трал' CHECK (kind IN ('трал','точка рыбака')),
    time_start           TEXT,                   -- ЧЧ:ММ
    time_end             TEXT,
    duration_min         REAL,
    speed_kmh            REAL,
    intermediate_depth_m REAL,                   -- только 2026
    catch_note           TEXT,                   -- что попало в трал (из комментариев)
    notes                TEXT,                   -- берег, заводы, драги и т.п.
    UNIQUE (event_id, kind, haul_no)
);

-- Замер на точке: старт и финиш траления
CREATE TABLE haul_point (
    point_id        INTEGER PRIMARY KEY,
    haul_id         INTEGER NOT NULL REFERENCES haul(haul_id),
    point_type      TEXT NOT NULL CHECK (point_type IN ('старт','финиш','точка')),
    garmin_wp       TEXT,                        -- номер точки на Garmin, как записан
    lat             REAL,
    lon             REAL,
    coord_source    TEXT,                        -- 'Garmin' или 'Google Maps'
    depth_m         REAL,
    o2_pct          REAL,
    ppm             REAL,                        -- минерализация, мг/л
    ms              REAL,                        -- электропроводность, мСм/см
    us              REAL,                        -- электропроводность, мкСм/см
    ph              REAL,
    water_temp_c    REAL,
    notes           TEXT,
    UNIQUE (haul_id, point_type)
);

-- Справочник видов
CREATE TABLE taxon (
    taxon_id        INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,        -- латинское название в одном написании
    genus           TEXT,
    code            TEXT,                        -- суффикс в номере пробы: pc, ph, e, mc, Bg, mr
    grp             TEXT                         -- рыба / ракообразное
);

-- Особь: одна строка = одна пойманная особь
CREATE TABLE specimen (
    specimen_id     INTEGER PRIMARY KEY,
    label           TEXT NOT NULL UNIQUE,        -- номер пробы, приведённый к виду '26 ph'
    label_raw       TEXT,                        -- как записано в Excel
    series          TEXT,                        -- сквозная нумерация: Pangasius, pc, Bg, mr
    series_no       INTEGER,                     -- номер внутри серии
    event_id        INTEGER NOT NULL REFERENCES event(event_id),
    haul_id         INTEGER REFERENCES haul(haul_id),   -- если известно
    taxon_id        INTEGER REFERENCES taxon(taxon_id),
    id_confidence   TEXT CHECK (id_confidence IN ('точно','предп.','до рода','не определён')),
    taxon_raw       TEXT,                        -- вид как записан в Excel
    origin          TEXT CHECK (origin IN ('трал','рыбак','рынок','аквахозяйство')),
    tl_cm           REAL,                        -- полная длина
    sl_cm           REAL,                        -- длина без хвоста
    mass_g          REAL,                        -- полная масса
    mass_gutted_g   REAL,                        -- масса без внутренних органов
    whole_fixed     INTEGER NOT NULL DEFAULT 0,  -- 1 = особь зафиксирована целиком
    is_dead         INTEGER NOT NULL DEFAULT 0,  -- 1 = особь была мёртвой при разборе
    capture_lat     REAL,                        -- если место поимки своё (рыбак)
    capture_lon     REAL,
    source_row      INTEGER,                     -- строка на исходном листе
    notes           TEXT
);

-- Образец: одна пробирка / одно стекло / одна фиксированная особь
CREATE TABLE sample (
    sample_id       INTEGER PRIMARY KEY,
    specimen_id     INTEGER NOT NULL REFERENCES specimen(specimen_id),
    sample_type     TEXT NOT NULL CHECK (sample_type IN
                      ('ДНК','гистология','мазок крови','кишечник','целая особь','другое')),
    organ           TEXT,                        -- печень, почки, икра, жабры, мышцы...
    label           TEXT,                        -- метка на пробирке, напр. '26 ph a'
    preservative    TEXT,                        -- этанол 96%, формалин 10%, заморозка...
    storage         TEXT,                        -- куда отправлены копии: Россия; тропцентр; Кантхо
    raw_value       TEXT,                        -- исходный текст ячейки
    notes           TEXT
);

-- Улов и прилов: заметки внизу листов и комментарии к тралениям
CREATE TABLE catch_record (
    catch_id        INTEGER PRIMARY KEY,
    event_id        INTEGER NOT NULL REFERENCES event(event_id),
    haul_id         INTEGER REFERENCES haul(haul_id),
    taxon_text      TEXT,                        -- кто попался, как записано
    count_n         INTEGER,
    mass_g          REAL,
    raw_text        TEXT NOT NULL
);

-- Спорные значения, найденные при импорте: сверить с полевыми журналами
CREATE TABLE data_issue (
    issue_id        INTEGER PRIMARY KEY,
    category        TEXT NOT NULL,               -- промеры / номера / формат / координаты / вид / связь
    sheet_name      TEXT,
    cell            TEXT,                        -- адрес ячейки в Excel
    object          TEXT,                        -- номер пробы или траление
    raw_value       TEXT,
    description     TEXT NOT NULL,
    resolved        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX ix_event_exp      ON event(expedition_id);
CREATE INDEX ix_haul_event     ON haul(event_id);
CREATE INDEX ix_point_haul     ON haul_point(haul_id);
CREATE INDEX ix_spec_event     ON specimen(event_id);
CREATE INDEX ix_spec_haul      ON specimen(haul_id);
CREATE INDEX ix_spec_taxon     ON specimen(taxon_id);
CREATE INDEX ix_sample_spec    ON sample(specimen_id);

-- Удобное представление: особь со всем контекстом одной строкой
CREATE VIEW v_specimen AS
SELECT s.label            AS проба,
       t.name             AS вид,
       s.id_confidence    AS определение,
       x.name             AS экспедиция,
       e.sheet_name       AS лист,
       e.event_date       AS дата,
       e.event_type       AS тип_выезда,
       h.haul_no          AS траление,
       s.origin           AS откуда,
       s.tl_cm, s.sl_cm, s.mass_g, s.mass_gutted_g,
       s.whole_fixed      AS целиком,
       s.is_dead          AS мёртвая,
       (SELECT group_concat(sample_type || COALESCE(' (' || organ || ')', ''), '; ')
          FROM sample p WHERE p.specimen_id = s.specimen_id) AS образцы,
       s.notes            AS заметки
FROM specimen s
JOIN event e      ON e.event_id = s.event_id
JOIN expedition x ON x.expedition_id = e.expedition_id
LEFT JOIN haul h  ON h.haul_id = s.haul_id
LEFT JOIN taxon t ON t.taxon_id = s.taxon_id;

-- Средние параметры воды по траления (старт и финиш)
CREATE VIEW v_haul_water AS
SELECT h.haul_id, x.name AS экспедиция, e.sheet_name AS лист, e.event_date AS дата,
       h.haul_no AS траление, h.kind,
       round(avg(p.depth_m),1)      AS глубина_м,
       round(avg(p.water_temp_c),1) AS t_воды,
       round(avg(p.o2_pct),1)       AS o2_pct,
       round(avg(p.ph),2)           AS ph,
       round(avg(p.ppm),1)          AS ppm,
       h.catch_note                 AS улов
FROM haul h
JOIN event e      ON e.event_id = h.event_id
JOIN expedition x ON x.expedition_id = e.expedition_id
LEFT JOIN haul_point p ON p.haul_id = h.haul_id
GROUP BY h.haul_id;
