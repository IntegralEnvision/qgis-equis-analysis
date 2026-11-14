-- ChemDuck Schema: Catalogs
-- Phase 7.1: Runtime-queryable catalogs of domain-specific enumerations
--
-- These tables drive UI dropdowns in consumer apps (e.g. chemrooms) so that
-- adding or renaming an aggregation rule, non-detect method, etc. in the
-- schema automatically surfaces in the UI with no client-side code changes.
-- Consumers should load the catalog at startup and bind dropdowns to the
-- `label` column with the `name` column as the value.

-- ============================================================================
-- 7.1 aggregation_rules
-- ============================================================================
-- Canonical names of aggregation methods consumer apps can pass to the
-- aggregate_results table macro. Split into categories:
--   event_agg  - collapse multiple events (dates) at a single physical point
--   dup_agg    - collapse duplicate measurements at a single sample event
--   nd_method  - non-detect substitution strategy
--
-- Every aggregate_results rule works within one measurement identity
-- (location, matrix, depth interval, fraction, meas_basis, analyte): it never
-- combines dissolved with total, or dry with wet weight.

CREATE TABLE IF NOT EXISTS aggregation_rules (
    category VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    label VARCHAR NOT NULL,
    description VARCHAR,
    display_order INTEGER DEFAULT 0,
    PRIMARY KEY (category, name)
);

-- Clear and repopulate on every schema reload so the catalog can evolve
-- with the schema without needing a migration step.
DELETE FROM aggregation_rules;

INSERT INTO aggregation_rules (category, name, label, description, display_order) VALUES
-- ---------- event_agg: collapse multiple events (dates) at a single point ----
('event_agg', 'most_recent', 'Most recent',
 'Return the adjusted result from the most recent sample event, separately '
 'for each fraction and measurement basis. '
 'If that row is a non-detect, the result is reported as non-detect.',
 10),
('event_agg', 'maximum', 'Maximum value',
 'Return the maximum adjusted result across all events, separately for each '
 'fraction and measurement basis. '
 'If any events are detects, the winner is the highest detect. '
 'If all events are non-detects, the winner is the one with the highest '
 'detection limit (conservative non-detect).',
 20),
('event_agg', 'mean', 'Mean',
 'Arithmetic mean of adjusted results across all events, separately for each '
 'fraction and measurement basis. Units are not converted: when '
 'units_conflict is true the mean mixes units. '
 'Detect status is preserved as true if any contributing event was a detect.',
 30),

-- ---------- dup_agg: collapse duplicate measurements within a sample ---------
('dup_agg', 'avg', 'Average',
 'Arithmetic mean of duplicate measurements.',
 10),
('dup_agg', 'max', 'Maximum',
 'Highest value among duplicate measurements.',
 20),
('dup_agg', 'min', 'Minimum',
 'Lowest value among duplicate measurements.',
 30),
('dup_agg', 'first', 'First',
 'First duplicate measurement in source-file order.',
 40),
('dup_agg', 'last', 'Last',
 'Last duplicate measurement in source-file order.',
 50),

-- ---------- nd_method: non-detect substitution strategy ---------------------
('nd_method', 'raw', 'Reported value',
 'Use the reported value as-is without substitution.',
 10),
('nd_method', 'half_dl', 'Half detection limit',
 'Substitute 0.5 times the detection limit for non-detects.',
 20),
('nd_method', 'dl', 'At detection limit',
 'Substitute the detection limit itself for non-detects.',
 30),
('nd_method', 'zero', 'Zero',
 'Substitute zero for non-detects.',
 40),
('nd_method', 'exclude', 'Exclude',
 'Drop non-detects from the dataset entirely.',
 50);
