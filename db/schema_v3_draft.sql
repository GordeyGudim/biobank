-- =====================================================================
--  Схема v3 (ПРОЕКТ, 2026-10-19): информационный банк по биоте Вьетнама
--  С таблицей мест (sampling_site) — под повторные экспедиции на те же точки.
--  Объединяет наши решения и предложения из документа коллеги (Этап 2).
--  v3 = v2 + пользователи и роли, журнал изменений, справочник людей,
--  «кто и когда внёс», защита типов (STRICT, форматы дат и времени),
--  уникальные метки образцов, источник записей журнала проблем.
--  Синтаксис SQLite (нужна версия ≥ 3.37 из-за STRICT); для PostgreSQL заменить
--  INTEGER PRIMARY KEY на INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY.
--  Время (…_at) — 'ГГГГ-ММ-ДД ЧЧ:ММ:СС' в UTC; даты — 'ГГГГ-ММ-ДД'; время суток — 'ЧЧ:ММ'.
-- =====================================================================
PRAGMA foreign_keys = ON;
PRAGMA user_version = 3;                      -- версия схемы (для будущих миграций)

-- ---------------------------------------------------------------------
-- 0. ЛЮДИ, ПОЛЬЗОВАТЕЛИ И РОЛИ  (новое в v3)
-- ---------------------------------------------------------------------
CREATE TABLE person (                         -- справочник людей: не обязательно пользователи сайта
    person_id     INTEGER PRIMARY KEY,
    full_name     TEXT NOT NULL,              -- 'Слынько Е.Е.'
    name_latin    TEXT,                       -- 'Slynko E.E.' — для статей и GBIF
    organization  TEXT,
    notes         TEXT,
    UNIQUE (full_name, organization)
) STRICT;

CREATE TABLE app_user (                       -- учётная запись для входа на сайт
    user_id       INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,              -- только хеш (scrypt), сам пароль не хранится
    display_name  TEXT NOT NULL,              -- как подписывать в карточках: 'Иванова А.'
    person_id     INTEGER REFERENCES person(person_id),     -- этот же человек в справочнике (если есть)
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),  -- 0 = вход закрыт, история остаётся
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    created_at    TEXT NOT NULL CHECK (datetime(created_at) IS created_at),
    last_login_at TEXT CHECK (last_login_at IS NULL OR datetime(last_login_at) IS last_login_at)
) STRICT;

CREATE TABLE user_role (                      -- у одного пользователя может быть несколько ролей
    user_id       INTEGER NOT NULL REFERENCES app_user(user_id),
    role          TEXT NOT NULL CHECK (role IN ('читатель','исследователь','куратор','админ')),
    PRIMARY KEY (user_id, role)
) STRICT;

-- ---------------------------------------------------------------------
-- 1. ГЕОГРАФИЯ И МЕСТА  (где)
-- ---------------------------------------------------------------------
CREATE TABLE region (                         -- провинция
    region_id     INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- 'Донг-хап', 'Виньлонг', 'Кантхо'
    country       TEXT NOT NULL DEFAULT 'Вьетнам',
    created_by    INTEGER REFERENCES app_user(user_id),   -- кто добавил; NULL = импорт из Excel
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

CREATE TABLE city (
    city_id       INTEGER PRIMARY KEY,
    region_id     INTEGER NOT NULL REFERENCES region(region_id),
    name          TEXT NOT NULL,              -- 'Хонг-нгу', 'Бенче', 'Кантхо'
    name_latin    TEXT,                       -- 'Hong Ngu' — чтобы не путаться в транслитерации,
    created_by    INTEGER REFERENCES app_user(user_id),   -- кто добавил; NULL = импорт из Excel
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    UNIQUE (region_id, name)
) STRICT;

CREATE TABLE sampling_site (                  -- МЕСТО: не меняется между экспедициями
    site_id       INTEGER PRIMARY KEY,
    city_id       INTEGER REFERENCES city(city_id),
    name          TEXT NOT NULL,              -- 'Участок у Хонг-нгу', 'Рынок Бенче', 'Точка рыбака ниже Кантхо'
    site_type     TEXT NOT NULL CHECK (site_type IN ('участок реки','рынок','аквахозяйство','точка рыбака')),
    lat           REAL,                       -- ориентировочный центр места
    lon           REAL,
    description   TEXT,
    created_by    INTEGER REFERENCES app_user(user_id),   -- кто внёс; NULL = импорт из Excel
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

-- ---------------------------------------------------------------------
-- 2. СПРАВОЧНИКИ
-- ---------------------------------------------------------------------
CREATE TABLE capture_method (                 -- способ получения (из документа коллеги)
    capture_method_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE        -- 'траление', 'рыбак', 'рынок', 'аквахозяйство'
) STRICT;

CREATE TABLE gear (                           -- орудие лова
    gear_id       INTEGER PRIMARY KEY,
    gear_type     TEXT NOT NULL,              -- 'трал'
    mesh_cm       TEXT,                       -- '5x5', '3,5x3,5'
    size_m        TEXT,                       -- '15x20', '6,5x12',
    created_by    INTEGER REFERENCES app_user(user_id),   -- кто добавил; NULL = импорт из Excel
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    UNIQUE (gear_type, mesh_cm, size_m)
) STRICT;

CREATE TABLE species (
    species_id      INTEGER PRIMARY KEY,
    scientific_name TEXT NOT NULL UNIQUE,     -- 'Plotosus canius'
    family          TEXT,                     -- 'Plotosidae'
    genus           TEXT,
    code            TEXT,                     -- суффикс в номере пробы: pc, ph, e, mc, Bg, mr
    created_by      INTEGER REFERENCES app_user(user_id),   -- кто добавил; NULL = импорт из Excel
    created_at      TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

CREATE TABLE lab (                            -- куда отправляют образцы
    lab_id        INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- 'Россия', 'Тропцентр', 'Кантхо (секвенирование)'
    created_by    INTEGER REFERENCES app_user(user_id),   -- кто добавил; NULL = импорт из Excel
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

-- ---------------------------------------------------------------------
-- 3. ЭКСПЕДИЦИИ И ВЫЕЗДЫ  (когда)
-- ---------------------------------------------------------------------
CREATE TABLE expedition (
    expedition_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- 'Донг-хап 2025'
    date_start    TEXT CHECK (date_start IS NULL OR date(date_start) IS date_start),
    date_end      TEXT CHECK (date_end IS NULL OR date(date_end) IS date_end),
    notes         TEXT,
    created_by    INTEGER REFERENCES app_user(user_id),
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

CREATE TABLE expedition_member (              -- участники экспедиции (новое в v3)
    expedition_id INTEGER NOT NULL REFERENCES expedition(expedition_id),
    person_id     INTEGER NOT NULL REFERENCES person(person_id),
    role_text     TEXT,                       -- 'руководитель', 'ихтиолог' — свободный текст (решение 2026-10-19)
    PRIMARY KEY (expedition_id, person_id)
) STRICT;

CREATE TABLE sampling_event (                 -- ВЫЕЗД: конкретный день в конкретном месте
    event_id          INTEGER PRIMARY KEY,
    expedition_id     INTEGER NOT NULL REFERENCES expedition(expedition_id),
    site_id           INTEGER NOT NULL REFERENCES sampling_site(site_id),
    event_date        TEXT NOT NULL CHECK (date(event_date) IS event_date),  -- дата хранится ЗДЕСЬ, один раз
    capture_method_id INTEGER NOT NULL REFERENCES capture_method(capture_method_id),
    gear_id           INTEGER REFERENCES gear(gear_id),   -- только для тралений
    source_sheet      TEXT,                   -- лист Excel, откуда импортировано
    notes             TEXT,
    created_by        INTEGER REFERENCES app_user(user_id),
    created_at        TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

-- ---------------------------------------------------------------------
-- 4. ТРАЛЕНИЯ И ПАРАМЕТРЫ ВОДЫ
-- ---------------------------------------------------------------------
CREATE TABLE trawling (
    trawling_id    INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES sampling_event(event_id),
    trawl_no       INTEGER NOT NULL,          -- номер траления в этот день
    time_start     TEXT CHECK (time_start IS NULL OR (time_start GLOB '[0-2][0-9]:[0-5][0-9]' AND time_start < '24')),
    time_end       TEXT CHECK (time_end IS NULL OR (time_end GLOB '[0-2][0-9]:[0-5][0-9]' AND time_end < '24')),
    duration_min   REAL,
    speed_kmh      REAL,
    intermediate_depth_m REAL,
    catch_note     TEXT,                      -- «2 plotosus», «трал пустой»
    notes          TEXT,                      -- «на правом берегу драги»
    created_by     INTEGER REFERENCES app_user(user_id),
    created_at     TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    UNIQUE (event_id, trawl_no),
    UNIQUE (trawling_id, event_id)            -- нужно для составного внешнего ключа ниже
) STRICT;

CREATE TABLE water_measurement (              -- замер на старте / финише траления
    measurement_id INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL,
    trawling_id    INTEGER,                   -- NULL: замер не при тралении (точка рыбака)
    point_type     TEXT NOT NULL CHECK (point_type IN ('старт','финиш','точка')),
    measured_time  TEXT CHECK (measured_time IS NULL OR (measured_time GLOB '[0-2][0-9]:[0-5][0-9]' AND measured_time < '24')),
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
    created_by     INTEGER REFERENCES app_user(user_id),
    created_at     TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    FOREIGN KEY (event_id) REFERENCES sampling_event(event_id),
    FOREIGN KEY (trawling_id, event_id) REFERENCES trawling(trawling_id, event_id),
    UNIQUE (trawling_id, point_type)
) STRICT;

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
    capture_date      TEXT CHECK (capture_date IS NULL OR date(capture_date) IS capture_date),  -- если отличается от даты выезда
    tl_cm             REAL CHECK (tl_cm > 0),
    sl_cm             REAL CHECK (sl_cm > 0),
    mass_g            REAL CHECK (mass_g >= 0),
    mass_gutted_g     REAL CHECK (mass_gutted_g >= 0),
    sex               TEXT CHECK (sex IN ('самка','самец')),
    is_dead           INTEGER NOT NULL DEFAULT 0 CHECK (is_dead IN (0, 1)),
    notes             TEXT,
    source_row        INTEGER,
    created_by        INTEGER REFERENCES app_user(user_id),
    created_at        TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    FOREIGN KEY (event_id) REFERENCES sampling_event(event_id),
    FOREIGN KEY (trawling_id, event_id) REFERENCES trawling(trawling_id, event_id)
) STRICT;

CREATE TABLE species_identification (         -- история определений (из документа коллеги)
    identification_id INTEGER PRIMARY KEY,
    specimen_id   INTEGER NOT NULL REFERENCES specimen(specimen_id),
    species_id    INTEGER REFERENCES species(species_id),   -- NULL = не определён
    method        TEXT NOT NULL CHECK (method IN ('морфология','ДНК','другое')),
    confidence    TEXT NOT NULL CHECK (confidence IN ('точно','предп.','до рода','не определён')),
    identified_by INTEGER REFERENCES person(person_id),     -- кто определил (было TEXT; в данных пусто)
    identified_on TEXT CHECK (identified_on IS NULL OR date(identified_on) IS identified_on),
    raw_text      TEXT,                       -- как записано: 'пред. Pangasius elongatus'
    notes         TEXT,
    created_by    INTEGER REFERENCES app_user(user_id),     -- кто ВНЁС запись (≠ кто определил)
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

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
    notes         TEXT,
    created_by    INTEGER REFERENCES app_user(user_id),
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

CREATE TABLE sample_shipment (                -- образец ↔ лаборатория (многие ко многим)
    sample_id     INTEGER NOT NULL REFERENCES sample(sample_id),
    lab_id        INTEGER NOT NULL REFERENCES lab(lab_id),
    sent_on       TEXT CHECK (sent_on IS NULL OR date(sent_on) IS sent_on),
    created_by    INTEGER REFERENCES app_user(user_id),
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    PRIMARY KEY (sample_id, lab_id)
) STRICT;

CREATE TABLE analysis (                       -- РЕЗУЛЬТАТ исследования образца
    analysis_id   INTEGER PRIMARY KEY,
    sample_id     INTEGER NOT NULL REFERENCES sample(sample_id),
    lab_id        INTEGER REFERENCES lab(lab_id),
    analysis_type TEXT NOT NULL,              -- 'секвенирование COI', 'гистология', 'мазок'
    analysed_on   TEXT CHECK (analysed_on IS NULL OR date(analysed_on) IS analysed_on),
    method        TEXT,
    result        TEXT,
    accession     TEXT,                       -- номер в GenBank и т.п.
    notes         TEXT,
    created_by    INTEGER REFERENCES app_user(user_id),
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

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
    created_by    INTEGER REFERENCES app_user(user_id),
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at),
    FOREIGN KEY (event_id) REFERENCES sampling_event(event_id),
    FOREIGN KEY (trawling_id, event_id) REFERENCES trawling(trawling_id, event_id)
) STRICT;

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
    source        TEXT NOT NULL DEFAULT 'пользователь'
                  CHECK (source IN ('импорт','проверка','пользователь')),  -- откуда запись (новое в v3)
    resolved      INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolution    TEXT,                       -- как решили (заполняет пользователь)
    resolved_by   INTEGER REFERENCES app_user(user_id),     -- кто закрыл (новое в v3)
    resolved_at   TEXT CHECK (resolved_at IS NULL OR datetime(resolved_at) IS resolved_at),
    created_by    INTEGER REFERENCES app_user(user_id),
    created_at    TEXT CHECK (created_at IS NULL OR datetime(created_at) IS created_at)
) STRICT;

CREATE TABLE change_log (                     -- журнал изменений: кто, когда, было → стало (новое в v3)
    change_id     INTEGER PRIMARY KEY,
    changed_at    TEXT NOT NULL CHECK (datetime(changed_at) IS changed_at),
    user_id       INTEGER REFERENCES app_user(user_id),     -- NULL = команда CLI / импорт
    action        TEXT NOT NULL CHECK (action IN ('создание','изменение','удаление')),
    table_name    TEXT NOT NULL,
    record_key    TEXT NOT NULL,              -- id записи; у составного ключа 'sample_id=812, lab_id=1'
    record_label  TEXT,                       -- понятное имя: '26 ph', '26 ph a', 'Точка 3 / траление 2'
    column_name   TEXT,                       -- NULL при создании / удалении целой записи
    old_value     ANY,                        -- было (при удалении — вся запись в JSON)
    new_value     ANY                         -- стало (при создании — вся запись в JSON)
) STRICT;

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
-- новое в v3
CREATE INDEX ix_ident_person    ON species_identification(identified_by);
CREATE INDEX ix_member_person   ON expedition_member(person_id);
CREATE INDEX ix_log_record      ON change_log(table_name, record_key);
CREATE INDEX ix_log_time        ON change_log(changed_at);
CREATE INDEX ix_log_user        ON change_log(user_id);

-- Метка образца уникальна в пределах особи, типа образца и органа. Просто (specimen_id, label)
-- нельзя: ДНК, мазок и кишечник одной особи по традиции Excel носят одну метку '26 ph'
-- (301 такая группа). ifnull: в SQLite два NULL в UNIQUE считаются разными, а орган бывает пустым.
CREATE UNIQUE INDEX ux_sample_label ON sample(specimen_id, sample_type, ifnull(organ, ''), label);
