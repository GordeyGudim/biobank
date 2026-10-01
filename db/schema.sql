-- =====================================================================
--  Схема v2: информационный банк по биоте Вьетнама
--  С таблицей мест (sampling_site) — под повторные экспедиции на те же точки.
--  Объединяет наши решения и предложения из документа коллеги (Этап 2).
--  Синтаксис SQLite; для PostgreSQL заменить INTEGER PRIMARY KEY на
--  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY.
-- =====================================================================
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- 1. ГЕОГРАФИЯ И МЕСТА  (где)
-- ---------------------------------------------------------------------
CREATE TABLE region (                         -- провинция
    region_id     INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- 'Донг-хап', 'Виньлонг', 'Кантхо'
    country       TEXT NOT NULL DEFAULT 'Вьетнам'
);

CREATE TABLE city (
    city_id       INTEGER PRIMARY KEY,
    region_id     INTEGER NOT NULL REFERENCES region(region_id),
    name          TEXT NOT NULL,              -- 'Хонг-нгу', 'Бенче', 'Кантхо'
    name_latin    TEXT,                       -- 'Hong Ngu' — чтобы не путаться в транслитерации
    UNIQUE (region_id, name)
);

CREATE TABLE sampling_site (                  -- МЕСТО: не меняется между экспедициями
    site_id       INTEGER PRIMARY KEY,
    city_id       INTEGER REFERENCES city(city_id),
    name          TEXT NOT NULL,              -- 'Участок у Хонг-нгу', 'Рынок Бенче', 'Точка рыбака ниже Кантхо'
    site_type     TEXT NOT NULL CHECK (site_type IN ('участок реки','рынок','аквахозяйство','точка рыбака')),
    lat           REAL,                       -- ориентировочный центр места
    lon           REAL,
    description   TEXT
);

-- ---------------------------------------------------------------------
-- 2. СПРАВОЧНИКИ
-- ---------------------------------------------------------------------
CREATE TABLE capture_method (                 -- способ получения (из документа коллеги)
    capture_method_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE        -- 'траление', 'рыбак', 'рынок', 'аквахозяйство'
);

CREATE TABLE gear (                           -- орудие лова
    gear_id       INTEGER PRIMARY KEY,
    gear_type     TEXT NOT NULL,              -- 'трал'
    mesh_cm       TEXT,                       -- '5x5', '3,5x3,5'
    size_m        TEXT,                       -- '15x20', '6,5x12'
    UNIQUE (gear_type, mesh_cm, size_m)
);

CREATE TABLE species (
    species_id      INTEGER PRIMARY KEY,
    scientific_name TEXT NOT NULL UNIQUE,     -- 'Plotosus canius'
    family          TEXT,                     -- 'Plotosidae'
    genus           TEXT,
    code            TEXT                      -- суффикс в номере пробы: pc, ph, e, mc, Bg, mr
);

CREATE TABLE lab (                            -- куда отправляют образцы
    lab_id        INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE        -- 'Россия', 'Тропцентр', 'Кантхо (секвенирование)'
);

-- ---------------------------------------------------------------------
-- 3. ЭКСПЕДИЦИИ И ВЫЕЗДЫ  (когда)
-- ---------------------------------------------------------------------
CREATE TABLE expedition (
    expedition_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- 'Донг-хап 2025'
    date_start    TEXT,
    date_end      TEXT,
    notes         TEXT
);

CREATE TABLE sampling_event (                 -- ВЫЕЗД: конкретный день в конкретном месте
    event_id          INTEGER PRIMARY KEY,
    expedition_id     INTEGER NOT NULL REFERENCES expedition(expedition_id),
    site_id           INTEGER NOT NULL REFERENCES sampling_site(site_id),
    event_date        TEXT NOT NULL,          -- дата хранится ЗДЕСЬ, один раз
    capture_method_id INTEGER NOT NULL REFERENCES capture_method(capture_method_id),
    gear_id           INTEGER REFERENCES gear(gear_id),   -- только для тралений
    source_sheet      TEXT,                   -- лист Excel, откуда импортировано
    notes             TEXT
);

-- ---------------------------------------------------------------------
-- 4. ТРАЛЕНИЯ И ПАРАМЕТРЫ ВОДЫ
-- ---------------------------------------------------------------------
CREATE TABLE trawling (
    trawling_id    INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES sampling_event(event_id),
    trawl_no       INTEGER NOT NULL,          -- номер траления в этот день
    time_start     TEXT,
    time_end       TEXT,
    duration_min   REAL,
    speed_kmh      REAL,
    intermediate_depth_m REAL,
    catch_note     TEXT,                      -- «2 plotosus», «трал пустой»
    notes          TEXT,                      -- «на правом берегу драги»
    UNIQUE (event_id, trawl_no),
    UNIQUE (trawling_id, event_id)            -- нужно для составного внешнего ключа ниже
);

CREATE TABLE water_measurement (              -- замер на старте / финише траления
    measurement_id INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL,
    trawling_id    INTEGER,                   -- NULL: замер не при тралении (точка рыбака)
    point_type     TEXT NOT NULL CHECK (point_type IN ('старт','финиш','точка')),
    measured_time  TEXT,
    lat            REAL,
    lon            REAL,
    coord_source   TEXT,                      -- 'Garmin' / 'Google Maps'
    garmin_wp      TEXT,
    depth_m        REAL,
    o2_pct         REAL,
    ppm            REAL,
    conductivity_ms REAL,
    conductivity_us REAL,
    ph             REAL,
    water_temp_c   REAL,
    notes          TEXT,
    FOREIGN KEY (event_id) REFERENCES sampling_event(event_id),
    FOREIGN KEY (trawling_id, event_id) REFERENCES trawling(trawling_id, event_id),
    UNIQUE (trawling_id, point_type)
);

-- ---------------------------------------------------------------------
-- 5. ОСОБИ, ОПРЕДЕЛЕНИЕ ВИДА
-- ---------------------------------------------------------------------
CREATE TABLE specimen (                       -- ОСОБЬ: одна пойманная рыба
    specimen_id       INTEGER PRIMARY KEY,
    label             TEXT NOT NULL UNIQUE,   -- '26 ph' (нормализованный номер пробы)
    label_raw         TEXT,                   -- как записано в Excel: '26 ph', '107.0', '155  pc'
    series            TEXT,                   -- сквозная нумерация: 'Pangasius', 'pc', 'Bg', 'mr'
    series_no         INTEGER,                -- номер внутри серии
    event_id          INTEGER NOT NULL,
    trawling_id       INTEGER,                -- если известно
    species_id        INTEGER REFERENCES species(species_id),   -- текущее (принятое) определение
    capture_method_id INTEGER REFERENCES capture_method(capture_method_id), -- если отличается от выезда
    capture_site_id   INTEGER REFERENCES sampling_site(site_id),            -- если отличается от выезда
    capture_date      TEXT,                   -- если отличается от даты выезда
    tl_cm             REAL CHECK (tl_cm > 0),
    sl_cm             REAL CHECK (sl_cm > 0),
    mass_g            REAL CHECK (mass_g >= 0),
    mass_gutted_g     REAL CHECK (mass_gutted_g >= 0),
    sex               TEXT CHECK (sex IN ('самка','самец')),
    is_dead           INTEGER NOT NULL DEFAULT 0,
    notes             TEXT,
    source_row        INTEGER,
    FOREIGN KEY (event_id) REFERENCES sampling_event(event_id),
    FOREIGN KEY (trawling_id, event_id) REFERENCES trawling(trawling_id, event_id)
);

CREATE TABLE species_identification (         -- история определений (из документа коллеги)
    identification_id INTEGER PRIMARY KEY,
    specimen_id   INTEGER NOT NULL REFERENCES specimen(specimen_id),
    species_id    INTEGER REFERENCES species(species_id),   -- NULL = не определён
    method        TEXT NOT NULL CHECK (method IN ('морфология','ДНК','другое')),
    confidence    TEXT NOT NULL CHECK (confidence IN ('точно','предп.','до рода','не определён')),
    identified_by TEXT,
    identified_on TEXT,
    raw_text      TEXT,                       -- как записано: 'пред. Pangasius elongatus'
    notes         TEXT
);

-- ---------------------------------------------------------------------
-- 6. ОБРАЗЦЫ (материал) И АНАЛИЗЫ (результаты)
-- ---------------------------------------------------------------------
CREATE TABLE sample (                         -- ОБРАЗЕЦ: пробирка / стекло / зафиксированная особь
    sample_id     INTEGER PRIMARY KEY,
    specimen_id   INTEGER NOT NULL REFERENCES specimen(specimen_id),
    sample_type   TEXT NOT NULL CHECK (sample_type IN
                    ('ДНК','гистология','мазок крови','кишечник','целая особь','другое')),
    organ         TEXT,                       -- печень, почки, икра, жабры, мышцы
    label         TEXT,                       -- '26 ph a'
    preservative  TEXT,                       -- этанол 96%, формалин 10%, заморозка
    raw_value     TEXT,                       -- исходный текст ячейки Excel
    notes         TEXT
);

CREATE TABLE sample_shipment (                -- образец ↔ лаборатория (многие ко многим)
    sample_id     INTEGER NOT NULL REFERENCES sample(sample_id),
    lab_id        INTEGER NOT NULL REFERENCES lab(lab_id),
    sent_on       TEXT,
    PRIMARY KEY (sample_id, lab_id)
);

CREATE TABLE analysis (                       -- РЕЗУЛЬТАТ исследования образца
    analysis_id   INTEGER PRIMARY KEY,
    sample_id     INTEGER NOT NULL REFERENCES sample(sample_id),
    lab_id        INTEGER REFERENCES lab(lab_id),
    analysis_type TEXT NOT NULL,              -- 'секвенирование COI', 'гистология', 'мазок'
    analysed_on   TEXT,
    method        TEXT,
    result        TEXT,
    accession     TEXT,                       -- номер в GenBank и т.п.
    notes         TEXT
);

-- ---------------------------------------------------------------------
-- 7. ПРИЛОВ И КОНТРОЛЬ КАЧЕСТВА
-- ---------------------------------------------------------------------
CREATE TABLE catch_record (                   -- что ещё попало в трал / общий вес
    catch_id      INTEGER PRIMARY KEY,
    event_id      INTEGER NOT NULL,
    trawling_id   INTEGER,
    taxon_text    TEXT,
    count_n       INTEGER,
    mass_g        REAL,
    raw_text      TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES sampling_event(event_id),
    FOREIGN KEY (trawling_id, event_id) REFERENCES trawling(trawling_id, event_id)
);

CREATE TABLE data_issue (                     -- спорные значения: сверить с полевыми журналами
    issue_id      INTEGER PRIMARY KEY,
    category      TEXT NOT NULL,              -- промеры / номера / формат / координаты / вид / связь
    table_name    TEXT,                       -- к какой таблице относится
    record_id     INTEGER,                    -- id записи в этой таблице (если есть)
    sheet_name    TEXT,                       -- лист Excel
    cell          TEXT,                       -- адрес ячейки, напр. 'H3'
    object_label  TEXT,                       -- номер пробы / траление, напр. '155 pc'
    raw_value     TEXT,                       -- исходное значение
    description   TEXT NOT NULL,
    resolved      INTEGER NOT NULL DEFAULT 0,
    resolution    TEXT                        -- как решили (заполняет пользователь)
);

-- ---------------------------------------------------------------------
-- ИНДЕКСЫ на колонки-ссылки
-- ---------------------------------------------------------------------
CREATE INDEX ix_city_region     ON city(region_id);
CREATE INDEX ix_site_city       ON sampling_site(city_id);
CREATE INDEX ix_event_exp       ON sampling_event(expedition_id);
CREATE INDEX ix_event_site      ON sampling_event(site_id);
CREATE INDEX ix_event_date      ON sampling_event(event_date);
CREATE INDEX ix_trawl_event     ON trawling(event_id);
CREATE INDEX ix_water_trawl     ON water_measurement(trawling_id);
CREATE INDEX ix_water_event     ON water_measurement(event_id);
CREATE INDEX ix_spec_event      ON specimen(event_id);
CREATE INDEX ix_spec_trawl      ON specimen(trawling_id);
CREATE INDEX ix_spec_species    ON specimen(species_id);
CREATE INDEX ix_ident_spec      ON species_identification(specimen_id);
CREATE INDEX ix_sample_spec     ON sample(specimen_id);
CREATE INDEX ix_analysis_sample ON analysis(sample_id);
CREATE INDEX ix_catch_event     ON catch_record(event_id);
