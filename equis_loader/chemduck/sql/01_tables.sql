-- ChemDuck Schema: Base Tables
-- Phase 1.1-1.7: Core data tables

-- ============================================================================
-- 1.0 project - Project-level metadata
-- ============================================================================
CREATE TABLE IF NOT EXISTS project (
    project_name VARCHAR PRIMARY KEY,
    crs VARCHAR,  -- e.g. 'EPSG:2263'
    -- The non-detect policy for this project's locations (nd_policies);
    -- NULL = the dataset's (chemduck_settings), then chemduck_default.
    nd_policy VARCHAR,
    nd_policy_version INTEGER
);
ALTER TABLE project ADD COLUMN IF NOT EXISTS nd_policy VARCHAR;
ALTER TABLE project ADD COLUMN IF NOT EXISTS nd_policy_version INTEGER;

-- ============================================================================
-- chemduck_settings - Dataset-level settings, written at load or publish
-- ============================================================================
-- nd_policy / nd_policy_version: the dataset's non-detect policy. The loader
-- records the version it resolved, so a dataset keeps the numbers it was
-- loaded with even after a newer version of the policy ships.
CREATE TABLE IF NOT EXISTS chemduck_settings (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- ============================================================================
-- nd_policies / nd_policy_rules - How a project values non-detects
-- ============================================================================
-- docs/shared-rules-decisions-response.md, D7. What a non-detect IS is
-- fixed (results.detected, from the flag); how it is valued is a declared,
-- versioned policy: a header, and one rule per stage at which a non-detect
-- meets other results.
--
--   reruns         analyses of one sample                 (rank_reruns)
--   same_day       samples of one point on one date       (rank_across)
--   over_time      events of one point, for a statistic   (rank_across)
--   across_points  points of one location, for a rollup   (rank_across)
--   sum            analytes of one sum within one sample
--
-- estimator: select (one real result, by the stage's order) | pool (do not
-- collapse here; hand the rows to the next stage) | mean | median | max |
-- min | km_mean | km_median (Kaplan-Meier) | sum | refuse | combine (D2's
-- recipe: all detects -> their mean; detects with non-detects -> the
-- highest detect; all non-detects -> the highest limit; never a detect
-- averaged with a substituted limit).
-- nd_factor: a non-detect enters mean/median/max/min/sum as factor x limit.
-- km_floor: where Kaplan-Meier places the share below the lowest detect.
-- all_nd: a group with no detect: 'estimator' (the rule as usual, over the
-- substitutes) or factor x the max/min/median limit.
-- detected_rule: a computed value is detected when all / any input was.
-- Selection ranks non-detects by their limits, never by substitutes, so no
-- factor changes which result a stage selects.

CREATE TABLE IF NOT EXISTS nd_policies (
    name VARCHAR NOT NULL,
    version INTEGER NOT NULL,
    description VARCHAR,
    limit_basis VARCHAR NOT NULL
        CHECK (limit_basis IN ('source_chain', 'reporting', 'method_detection', 'quantitation')),
    display_factor DOUBLE NOT NULL CHECK (display_factor >= 0),
    across_nd_limit VARCHAR NOT NULL CHECK (across_nd_limit IN ('higher', 'lower')),
    reruns_nd_limit VARCHAR NOT NULL CHECK (reruns_nd_limit IN ('higher', 'lower')),
    -- what one event is: a sample at its time ('timestamp': samples of one
    -- point merge only when their times coincide, or their dates when the
    -- source has no time), or a whole day ('date': D1d's explicit recipe)
    event_grain VARCHAR NOT NULL DEFAULT 'timestamp'
        CHECK (event_grain IN ('timestamp', 'date')),
    PRIMARY KEY (name, version)
);
ALTER TABLE nd_policies ADD COLUMN IF NOT EXISTS event_grain VARCHAR DEFAULT 'timestamp';

CREATE TABLE IF NOT EXISTS nd_policy_rules (
    policy VARCHAR NOT NULL,
    version INTEGER NOT NULL,
    stage VARCHAR NOT NULL
        CHECK (stage IN ('reruns', 'same_day', 'over_time', 'across_points', 'sum')),
    estimator VARCHAR NOT NULL
        CHECK (estimator IN ('select', 'pool', 'mean', 'median', 'max', 'min',
                             'km_mean', 'km_median', 'sum', 'refuse', 'combine')),
    nd_factor DOUBLE CHECK (nd_factor >= 0),
    km_floor VARCHAR CHECK (km_floor IN ('lowest_detect', 'half_lowest_detect', 'zero')),
    all_nd VARCHAR NOT NULL
        CHECK (all_nd IN ('estimator', 'max_limit', 'min_limit', 'median_limit')),
    detected_rule VARCHAR NOT NULL CHECK (detected_rule IN ('all', 'any')),
    PRIMARY KEY (policy, version, stage),
    -- pool hands rows on, so only a stage with a later stage may pool
    CHECK (estimator <> 'pool' OR stage IN ('reruns', 'same_day')),
    -- combine (D2's recipe) joins results of one measurement, not events
    CHECK (estimator <> 'combine' OR stage IN ('reruns', 'same_day', 'across_points')),
    -- Kaplan-Meier is a statistic over events or points, not over reruns
    CHECK (estimator NOT IN ('km_mean', 'km_median') OR stage IN ('over_time', 'across_points')),
    CHECK (estimator NOT IN ('km_mean', 'km_median') OR km_floor IS NOT NULL),
    -- with no detect, Kaplan-Meier has nothing to estimate from: name a limit
    CHECK (estimator NOT IN ('km_mean', 'km_median') OR all_nd <> 'estimator'),
    CHECK (estimator <> 'sum' OR stage = 'sum'),
    CHECK (stage <> 'sum' OR estimator IN ('sum', 'refuse')),
    CHECK (estimator NOT IN ('mean', 'median', 'max', 'min', 'sum') OR nd_factor IS NOT NULL)
);

-- The seeded policies (docs/shared-rules-decisions-response.md, D7). A
-- project picks one, or declares its own; these are starting points.
INSERT OR REPLACE INTO nd_policies (name, version, description, limit_basis, display_factor,
                                     across_nd_limit, reruns_nd_limit, event_grain) VALUES
    ('chemduck_default', 1, 'Real results on maps; Kaplan-Meier for statistics',
     'source_chain', 1.0, 'higher', 'lower', 'timestamp'),
    ('half_dl_average', 1, 'Half-limit substitutes averaged with the detects',
     'source_chain', 0.5, 'higher', 'lower', 'timestamp'),
    ('km_lowest_detect', 1, 'Kaplan-Meier, the share below the lowest detect placed at it',
     'source_chain', 1.0, 'higher', 'lower', 'timestamp'),
    ('chemduck_0_1', 1, 'ChemDuck 0.1: duplicates and events averaged by day, non-detects at half the limit',
     'source_chain', 0.5, 'higher', 'lower', 'date'),
    ('combine_same_day', 1, 'chemduck_default, but a day''s samples of one point combined by D2''s recipe',
     'source_chain', 1.0, 'higher', 'lower', 'timestamp');

INSERT OR REPLACE INTO nd_policy_rules VALUES
    ('chemduck_default', 1, 'reruns',        'select',  NULL, NULL,            'estimator', 'all'),
    ('chemduck_default', 1, 'same_day',      'select',  NULL, NULL,            'estimator', 'all'),
    ('chemduck_default', 1, 'over_time',     'km_mean', NULL, 'lowest_detect', 'max_limit', 'all'),
    ('chemduck_default', 1, 'across_points', 'select',  NULL, NULL,            'estimator', 'all'),
    ('chemduck_default', 1, 'sum',           'refuse',  NULL, NULL,            'estimator', 'all'),
    ('half_dl_average',  1, 'reruns',        'pool',    NULL, NULL,            'estimator', 'any'),
    ('half_dl_average',  1, 'same_day',      'mean',    0.5,  NULL,            'estimator', 'any'),
    ('half_dl_average',  1, 'over_time',     'mean',    0.5,  NULL,            'estimator', 'any'),
    ('half_dl_average',  1, 'across_points', 'select',  NULL, NULL,            'estimator', 'all'),
    ('half_dl_average',  1, 'sum',           'sum',     0.5,  NULL,            'estimator', 'any'),
    ('km_lowest_detect', 1, 'reruns',        'select',  NULL, NULL,            'estimator', 'all'),
    ('km_lowest_detect', 1, 'same_day',      'select',  NULL, NULL,            'estimator', 'all'),
    ('km_lowest_detect', 1, 'over_time',     'km_mean', NULL, 'lowest_detect', 'max_limit', 'all'),
    ('km_lowest_detect', 1, 'across_points', 'select',  NULL, NULL,            'estimator', 'all'),
    ('km_lowest_detect', 1, 'sum',           'refuse',  NULL, NULL,            'estimator', 'all'),
    ('combine_same_day', 1, 'reruns',        'select',  NULL, NULL,            'estimator', 'all'),
    ('combine_same_day', 1, 'same_day',      'combine', NULL, NULL,            'estimator', 'all'),
    ('combine_same_day', 1, 'over_time',     'km_mean', NULL, 'lowest_detect', 'max_limit', 'all'),
    ('combine_same_day', 1, 'across_points', 'select',  NULL, NULL,            'estimator', 'all'),
    ('combine_same_day', 1, 'sum',           'refuse',  NULL, NULL,            'estimator', 'all'),
    ('chemduck_0_1',     1, 'reruns',        'pool',    NULL, NULL,            'estimator', 'any'),
    ('chemduck_0_1',     1, 'same_day',      'mean',    0.5,  NULL,            'estimator', 'any'),
    ('chemduck_0_1',     1, 'over_time',     'mean',    0.5,  NULL,            'estimator', 'any'),
    ('chemduck_0_1',     1, 'across_points', 'select',  NULL, NULL,            'estimator', 'all'),
    ('chemduck_0_1',     1, 'sum',           'refuse',  NULL, NULL,            'estimator', 'all');

-- ============================================================================
-- 1.1 locations - Spatial reference for sampling points
-- ============================================================================
CREATE TABLE IF NOT EXISTS locations (
    location_id VARCHAR PRIMARY KEY,
    x DOUBLE,
    y DOUBLE,
    loc_type VARCHAR,
    loc_desc VARCHAR,
    aquifer VARCHAR,
    region VARCHAR,
    dataset VARCHAR,
    project VARCHAR,
    study VARCHAR,
    study_description VARCHAR,
    elev_ground DOUBLE,     -- ground surface elevation (ft or m above datum)
    elev_measuring DOUBLE   -- top-of-casing / measuring point elevation
);

-- ============================================================================
-- 1.2 samples - Individual sampling events
-- ============================================================================
CREATE TABLE IF NOT EXISTS samples (
    sample_id VARCHAR PRIMARY KEY,
    location_id VARCHAR NOT NULL REFERENCES locations(location_id),
    sample_date DATE NOT NULL,
    matrix VARCHAR NOT NULL,
    top_depth DOUBLE,
    bottom_depth DOUBLE,
    depth DOUBLE,  -- computed or reported depth
    depth_units VARCHAR,
    sample_type VARCHAR,
    composite_type VARCHAR,
    event VARCHAR,
    sample_material VARCHAR,  -- specific material (e.g. OutdoorAir, Soil, Sediment)
    parent_sample_id VARCHAR,  -- parent sample for QA/QC chains
    -- the sampling time, when the source records one (NULL: date only)
    sample_datetime TIMESTAMP,
    -- vertical position by label (surface, mid, bottom, a source's own
    -- vocabulary), set by a mapping's position_rules; NULL = unknown, its
    -- own value, never merged with a known position (shared rules D1)
    position VARCHAR
);
ALTER TABLE samples ADD COLUMN IF NOT EXISTS sample_datetime TIMESTAMP;
ALTER TABLE samples ADD COLUMN IF NOT EXISTS position VARCHAR;

-- ============================================================================
-- load_report - What a load's declared rules did, as data
-- ============================================================================
-- One row per (category, item): how many samples a position or QC rule
-- matched, retyped or skipped, and what no rule matched. Written at every
-- load and publish, so a rule that silently stops matching, or starts
-- matching too much, is visible (shared rules, "load-time data-quality
-- report").
CREATE TABLE IF NOT EXISTS load_report (
    category VARCHAR NOT NULL,
    item VARCHAR NOT NULL,
    n BIGINT,
    detail VARCHAR,
    PRIMARY KEY (category, item)
);

-- Indexes will be created after data load for better performance

-- ============================================================================
-- 1.3 results - Analytical measurements (core fact table)
-- ============================================================================
CREATE TABLE IF NOT EXISTS results (
    result_id INTEGER PRIMARY KEY,
    sample_id VARCHAR NOT NULL REFERENCES samples(sample_id),
    analyte VARCHAR NOT NULL,
    anl_sm_mb VARCHAR,  -- analyte name with sample matrix basis
    analyte_group VARCHAR,
    cas_number VARCHAR,
    result DOUBLE,
    units VARCHAR,
    std_units VARCHAR,
    method_detection_limit DOUBLE,
    reporting_limit DOUBLE,
    quantitation_limit DOUBLE,
    upper_quantitation_limit DOUBLE,  -- upper calibration/countable range limit
    dilution_factor DOUBLE,
    modeled_relative_uncertainty DOUBLE,  -- statistical uncertainty as relative fraction
    detected BOOLEAN,
    estimated BOOLEAN,
    tic BOOLEAN,  -- tentatively identified compound
    qualifier VARCHAR,
    lab_name VARCHAR,
    lab_sample_id VARCHAR,  -- lab's internal sample identifier
    method_code VARCHAR,
    date_analyzed DATE,
    lab_package_id VARCHAR,
    validation_level VARCHAR,
    lab_flags VARCHAR,
    validator_flags VARCHAR,
    meas_basis VARCHAR,  -- reference state: dry weight, wet weight, as-received, etc.
    fraction VARCHAR,    -- Total, Dissolved, etc. (aqueous metals filtration)
    -- What is measured (method_classes), set at load: two results in
    -- different classes are different variables, even for one analyte; an
    -- uncurated method code is its own class (shared rules D3)
    method_class VARCHAR,
    -- the source's own reportable designation (EQuIS REPORTABLE_RESULT)
    reportable BOOLEAN,
    -- the analysis's test type (EQuIS TEST_TYPE: INITIAL, REEXTRACT, ...)
    -- and its rank by the mapping's declared precedence (0 first)
    test_type VARCHAR,
    test_rank INTEGER,
    -- the limit the reporting system used for this result (EQuIS
    -- REPORT_RESULT_LIMIT), and the limit that stands for a non-detect,
    -- chosen at load by the mapping's limit_chain (shared rules D4):
    -- limit_source names the link ('report', 'reporting', 'method_detection',
    -- 'quantitation'), 'none' when no link had one
    report_limit DOUBLE,
    nd_limit DOUBLE,
    limit_source VARCHAR,
    -- Units, converted once at load (shared rules D5): result, units and
    -- every limit are in the variable's canonical unit where a factor was
    -- known; reported_units is the unit the source gave, unit_factor the
    -- factor applied (NULL: none), and unit_source how the row got its
    -- unit - 'as_reported', 'source' (a factor the mapping declares from
    -- the source system), 'chemduck' (unit_conversions), 'unconverted' (no
    -- factor known) or 'contradicts_matrix' (never converted)
    reported_units VARCHAR,
    unit_factor DOUBLE,
    unit_source VARCHAR
);
ALTER TABLE results ADD COLUMN IF NOT EXISTS reported_units VARCHAR;
ALTER TABLE results ADD COLUMN IF NOT EXISTS unit_factor DOUBLE;
ALTER TABLE results ADD COLUMN IF NOT EXISTS unit_source VARCHAR;
ALTER TABLE results ADD COLUMN IF NOT EXISTS report_limit DOUBLE;
ALTER TABLE results ADD COLUMN IF NOT EXISTS nd_limit DOUBLE;
ALTER TABLE results ADD COLUMN IF NOT EXISTS limit_source VARCHAR;
ALTER TABLE results ADD COLUMN IF NOT EXISTS method_class VARCHAR;
ALTER TABLE results ADD COLUMN IF NOT EXISTS reportable BOOLEAN;
ALTER TABLE results ADD COLUMN IF NOT EXISTS test_type VARCHAR;
ALTER TABLE results ADD COLUMN IF NOT EXISTS test_rank INTEGER;

-- ============================================================================
-- method_classes - What a method code measures (shared rules D3)
-- ============================================================================
-- A method class says what is measured: an in-situ field reading ('field'),
-- a laboratory analysis ('lab'), or a named leaching or extraction procedure
-- ('leach:TCLP', 'extract:NBR10006', ...). Within one analyte, methods of one
-- class are alternatives, selected among (never averaged by default); methods
-- of different classes are different variables.
--
-- pattern is a regular expression matched against the whole method code,
-- upper-cased and trimmed; the lowest priority wins. This seed covers only
-- families whose class is well known, so a laboratory moving from 6010 to
-- 6020 does not split a series. A source's mapping adds its own
-- (method_classes: in the mapping, priority 0); a code nothing matches is its
-- own class and goes on the load's curation list (load_report).
CREATE TABLE IF NOT EXISTS method_classes (
    pattern VARCHAR PRIMARY KEY,
    method_class VARCHAR NOT NULL,
    priority INTEGER NOT NULL,
    note VARCHAR
);

-- Separators vary by facility ('EPA 300.0', 'EPA_300.0', 'SW-6010B'):
-- every family allows a space, an underscore or a hyphen, or none.
INSERT OR REPLACE INTO method_classes VALUES
    ('FIELD([ _-]?(MEASURE|MEASUREMENT|METER|PARAMETER|TEST)S?)?|FLD|FIELD[ _-].*', 'field', 10,
     'in-situ readings taken at sampling'),
    -- named leaching and extraction procedures: each its own measurand
    ('((SW|EPA|E)[ _-]?)?1311[A-Z]?|TCLP', 'leach:TCLP', 20, 'toxicity characteristic leaching'),
    ('((SW|EPA|E)[ _-]?)?1312[A-Z]?|SPLP', 'leach:SPLP', 20, 'synthetic precipitation leaching'),
    ('((SW|EPA|E)[ _-]?)?1313[A-Z]?', 'leach:LEAF1313', 20, 'LEAF pH dependence'),
    ('((SW|EPA|E)[ _-]?)?1314[A-Z]?', 'leach:LEAF1314', 20, 'LEAF percolation column'),
    ('((SW|EPA|E)[ _-]?)?1315[A-Z]?', 'leach:LEAF1315', 20, 'LEAF mass transfer'),
    ('((SW|EPA|E)[ _-]?)?1316[A-Z]?', 'leach:LEAF1316', 20, 'LEAF liquid-solid ratio'),
    ('NBR[ _-]?10005.*', 'leach:NBR10005', 20, 'Brazilian leaching test (lixiviacao)'),
    ('NBR[ _-]?10006.*', 'extract:NBR10006', 20, 'Brazilian solubilisation test (solubilizacao)'),
    ('(ASTM[ _-]?)?D[ _-]?3987.*', 'leach:ASTM_D3987', 20, 'shake extraction of solid waste with water'),
    -- laboratory analyses of the sample itself
    ('((SW|EPA|E)[ _-]?)?[0-9]{4}[A-Z]?(-[A-Z0-9]+)?', 'lab', 50, 'SW-846 methods'),
    ('((EPA|E)[ _-]?)?[0-9]{3}\.[0-9]{1,2}[A-Z]?', 'lab', 50, 'EPA water methods (200.7, 300.0, 180.1, ...)'),
    ('SM[ _-]?[0-9]{4}.*', 'lab', 50, 'Standard Methods'),
    ('ASTM[ _-]?[A-Z][ _-]?[0-9]+.*', 'lab', 50, 'ASTM methods'),
    ('ISO[ _-]?[0-9]+.*', 'lab', 50, 'ISO methods');

-- Indexes will be created after data load for better performance

-- ============================================================================
-- 1.4 screening_levels - Regulatory/comparison thresholds
-- ============================================================================
CREATE TABLE IF NOT EXISTS screening_levels (
    screening_id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    analyte VARCHAR NOT NULL,
    matrix VARCHAR NOT NULL,
    level_type VARCHAR,  -- MCL, RSL, PRG, etc.
    value DOUBLE NOT NULL,
    units VARCHAR NOT NULL,
    comparison VARCHAR DEFAULT '>',  -- '>' (upper limit) or '<' (lower limit)
    source VARCHAR
);

-- Index will be created after data load for better performance

-- ============================================================================
-- 1.5 unit_conversions - Conversion factors between measurement units
-- ============================================================================
CREATE TABLE IF NOT EXISTS unit_conversions (
    from_units VARCHAR NOT NULL,
    to_units VARCHAR NOT NULL,
    multiplier DOUBLE NOT NULL,
    analyte_group VARCHAR DEFAULT '',  -- empty string for general conversions
    matrix_class VARCHAR DEFAULT '',   -- '' = any matrix; else a matrix_classes class
    PRIMARY KEY (from_units, to_units, analyte_group)
);
-- Databases created before matrix_class existed
ALTER TABLE unit_conversions ADD COLUMN IF NOT EXISTS matrix_class VARCHAR DEFAULT '';

-- Seed common conversions (use INSERT OR REPLACE for idempotency).
-- Mass/volume <-> mass/mass is never a conversion (it needs a density), so no
-- row maps mg/kg to mg/L. ppm/ppb are mass ratios: exactly mg/kg and ug/kg in
-- any matrix, but mg/L and ug/L only in dilute water, so those rows are
-- scoped to the 'aqueous' matrix class.
INSERT OR REPLACE INTO unit_conversions (from_units, to_units, multiplier, analyte_group, matrix_class) VALUES
    ('mg/L', 'ug/L', 1000.0, '', ''),
    ('ug/L', 'mg/L', 0.001, '', ''),
    ('mg/kg', 'ug/kg', 1000.0, '', ''),
    ('ug/kg', 'mg/kg', 0.001, '', ''),
    ('ug/g', 'mg/kg', 1.0, '', ''),
    ('mg/kg', 'ug/g', 1.0, '', ''),
    ('ppm', 'mg/kg', 1.0, '', ''),
    ('ppb', 'ug/kg', 1.0, '', ''),
    ('mg/kg', 'ppm', 1.0, '', ''),
    ('ug/kg', 'ppb', 1.0, '', ''),
    ('ppm', 'mg/L', 1.0, '', 'aqueous'),
    ('ppb', 'ug/L', 1.0, '', 'aqueous'),
    ('mg/L', 'ppm', 1.0, '', 'aqueous'),
    ('ug/L', 'ppb', 1.0, '', 'aqueous'),
    ('ng/L', 'ug/L', 0.001, '', ''),
    ('g/L', 'mg/L', 1000.0, '', ''),
    ('ng/kg', 'ug/kg', 0.001, '', ''),
    ('ng/g', 'ug/kg', 1.0, '', ''),
    ('mg/g', 'mg/kg', 1000.0, '', ''),
    -- a percentage of a solid is a mass fraction; of a water it is not
    -- known to be one
    ('percent', 'mg/kg', 10000.0, '', 'solid'),
    ('%', 'mg/kg', 10000.0, '', 'solid');

-- ============================================================================
-- 1.5b matrix_classes - Which matrix codes share a unit convention
-- ============================================================================
-- Matrix codes differ by source system (EQuIS WG/WS, IDA 'Groundwater', ...).
-- Conversions scoped to a class apply only to matrices listed here; a matrix
-- with no row gets only the unscoped ('' class) conversions. Extend per site.
CREATE TABLE IF NOT EXISTS matrix_classes (
    matrix VARCHAR PRIMARY KEY,
    matrix_class VARCHAR NOT NULL
);

INSERT OR REPLACE INTO matrix_classes (matrix, matrix_class) VALUES
    ('Groundwater', 'aqueous'),
    ('Surface Water', 'aqueous'),
    ('Water', 'aqueous'),
    ('GW', 'aqueous'),
    ('SW', 'aqueous'),
    ('WG', 'aqueous'),   -- EQuIS groundwater
    ('WS', 'aqueous'),   -- EQuIS surface water
    ('WW', 'aqueous'),   -- EQuIS wastewater
    ('WP', 'aqueous'),   -- EQuIS drinking water
    ('WQ', 'aqueous'),   -- EQuIS QC water
    ('Soil', 'solid'),
    ('Sediment', 'solid'),
    ('SO', 'solid'),     -- EQuIS soil
    ('SE', 'solid'),     -- EQuIS sediment
    ('TAILS', 'solid');  -- tailings

-- ============================================================================
-- unit_dimensions - What a concentration unit is per (shared rules D5)
-- ============================================================================
-- A unit that contradicts its matrix (a per-kilogram unit on a water sample)
-- is a coding error: it is never converted, and the load lists it. Only
-- units whose dimension is unambiguous are listed: ppm, percent or mg/dm3
-- (per volume of soil, in some soil-science methods) are not.
-- matrix_class_dimensions: the dimension a class's concentrations are in.
-- A leaching or extraction result (method class leach:* / extract:*) is a
-- concentration in its extract, so it is exempt.
CREATE TABLE IF NOT EXISTS unit_dimensions (
    units VARCHAR PRIMARY KEY,
    dimension VARCHAR NOT NULL   -- 'mass/mass' or 'mass/volume'
);

INSERT OR REPLACE INTO unit_dimensions (units, dimension) VALUES
    ('mg/kg', 'mass/mass'), ('ug/kg', 'mass/mass'), ('ng/kg', 'mass/mass'),
    ('ug/g', 'mass/mass'), ('ng/g', 'mass/mass'), ('mg/g', 'mass/mass'),
    ('mg/L', 'mass/volume'), ('ug/L', 'mass/volume'), ('ng/L', 'mass/volume'),
    ('g/L', 'mass/volume');

CREATE TABLE IF NOT EXISTS matrix_class_dimensions (
    matrix_class VARCHAR PRIMARY KEY,
    dimension VARCHAR NOT NULL
);

INSERT OR REPLACE INTO matrix_class_dimensions (matrix_class, dimension) VALUES
    ('aqueous', 'mass/volume'),
    ('solid', 'mass/mass');

-- ============================================================================
-- 1.6 location_attributes - Extended location metadata (EAV pattern)
-- ============================================================================
CREATE TABLE IF NOT EXISTS location_attributes (
    location_id VARCHAR NOT NULL REFERENCES locations(location_id),
    field_name VARCHAR NOT NULL,
    field_value VARCHAR,
    PRIMARY KEY (location_id, field_name)
);

-- Index will be created after data load for better performance

-- ============================================================================
-- 1.7 sample_attributes - Extended sample metadata (EAV pattern)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sample_attributes (
    sample_id VARCHAR NOT NULL REFERENCES samples(sample_id),
    field_name VARCHAR NOT NULL,
    field_value VARCHAR,
    PRIMARY KEY (sample_id, field_name)
);

-- Index will be created after data load for better performance

-- ============================================================================
-- 1.8 downhole_survey - Borehole deviation survey data (optional)
-- ============================================================================
CREATE TABLE IF NOT EXISTS downhole_survey (
    location_id VARCHAR NOT NULL REFERENCES locations(location_id),
    depth DOUBLE NOT NULL,          -- measured depth along borehole
    dip DOUBLE NOT NULL,            -- inclination (negative = downward)
    azimuth DOUBLE NOT NULL,        -- bearing relative to grid (x,y) north
    PRIMARY KEY (location_id, depth)
);

-- ============================================================================
-- 1.8b desurveyed_stations_fine - empty stub for materialized desurvey grid
-- ============================================================================
-- The real contents are built by sql/09_desurvey.sql (or tools/desurvey.py).
-- Declared here so views in sql/03_views.sql can reference it at schema-load
-- time, before refresh_desurvey() has populated it. Re-running 09_desurvey.sql
-- replaces the table via CREATE OR REPLACE TABLE.
CREATE TABLE IF NOT EXISTS desurveyed_stations_fine (
    location_id VARCHAR,
    md DOUBLE,
    x DOUBLE,
    y DOUBLE,
    z DOUBLE,
    tx DOUBLE,
    ty DOUBLE,
    tz DOUBLE,
    extrapolated BOOLEAN,
    collar_x DOUBLE,
    collar_y DOUBLE,
    collar_z DOUBLE
);

-- ============================================================================
-- 1.9 result_attributes - Extended result metadata (EAV pattern)
-- ============================================================================
CREATE TABLE IF NOT EXISTS result_attributes (
    result_id INTEGER NOT NULL REFERENCES results(result_id),
    field_name VARCHAR NOT NULL,
    field_value VARCHAR,
    PRIMARY KEY (result_id, field_name)
);

-- Index will be created after data load for better performance
