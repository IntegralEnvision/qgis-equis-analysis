-- ChemDuck Schema: per-sample results for multivariate views
--
-- A scatter, a ternary or a correlation plots SAMPLES: every point is one
-- sample, with each analyte's value read from that sample. Per-location
-- summaries (pivot_results_by_analyte, analyte_pair_data) average across a
-- location's samples, and the maxima of two analytes at one location come
-- from different samples more often than not (174 of 216 multi-sample water
-- stations for manganese and lead at the test facility), so a per-location
-- scatter plots combinations no sample ever had. These macros keep the
-- sample.
--
-- A COLUMN is (analyte, fraction, meas_basis, method_class): dissolved and
-- total, dry and wet weight, or a field and a lab reading (shared rules D3),
-- are never one column. Its CELL in a sample is ONE result,
-- chosen among the analyses of that one sample by rank_reruns, the order
-- aggregate_points uses for a sample's re-runs: a detect over a non-detect
-- over an unknown status; among detects the higher result, among
-- non-detects the lower limit (the more sensitive analysis), or the higher
-- if the project's non-detect policy says so; then the later analysis, then
-- result_id. n_results says how many results the cell was chosen from -
-- never hidden. nd_policy names the policy the order came from.
--
-- Values are as reported: result, detected, qualifier and detection_limit
-- each in its own field. Substituting a value for a non-detect is the
-- chart's decision, made with the flag in hand.
--
-- Units (shared rules D5) are converted at load. They are checked per
-- VARIABLE, as aggregate_points checks them: (matrix, fraction, meas_basis,
-- analyte, method_class) across the call, the matrix being the class's
-- when p_matrices combines. A variable still in more than one unit, or in a
-- unit that contradicts the matrix, is left out (p_units := 'refuse', the
-- default) - one mixed analyte does not cost the others - and
-- sample_results_refused, called with the same parameters, lists it: its
-- units with their counts, and why. p_units := 'flag' keeps it, with
-- units_conflict TRUE: such a column mixes units and must not be plotted as
-- one axis.
--
-- Parameters (all but the first by name, all optional):
--   analyte_list    VARCHAR[] - analytes to include; NULL = every analyte
--   p_matrix        VARCHAR   - one matrix; NULL = all
--   p_matrices      VARCHAR[] - matrices of one curated class, checked for
--                               units as one (shared rules D6); each row
--                               keeps its sample's own matrix. Not with
--                               p_matrix; the refusals are aggregate_points'
--   p_fraction      VARCHAR   - one fraction; NULL = each its own column
--   p_method_class  VARCHAR   - one method class; NULL = each its own column
--   p_sample_types  VARCHAR[] - sample-type scope: NULL (the default) = normal
--                               samples (in_normal_scope); [] = every type;
--                               a list = those types (in_sample_scope)
--   p_start_date, p_end_date  DATE - inclusive bounds on sample_date
--   p_locations     VARCHAR[] - locations to include; NULL = all
--   p_units         'refuse' (default) or 'flag'
--   p_display_factor DOUBLE   - NULL (default): every value as reported. Else
--                               a non-detect's result is this factor x its
--                               detection_limit, and display_factor says so
--   p_reportable_only BOOLEAN - leave out results not reportable; NULL = the
--                               dataset's choice (aggregate_points)
--
-- A filter on a column the source lacks matches everything; every row's
-- filters_ignored names it (as aggregate_points).

CREATE OR REPLACE MACRO cell_key(analyte, fraction, meas_basis, method_class) AS (
    analyte || '|' || COALESCE(fraction, '') || '|' || COALESCE(meas_basis, '')
        || '|' || COALESCE(method_class, '')
);

-- ============================================================================
-- sample_scope - the rows a per-sample call reads, each with its variable's
-- unit check (internal: sample_results and sample_results_refused share it)
-- ============================================================================

CREATE OR REPLACE MACRO sample_scope(
    analyte_list,
    p_matrix := NULL,
    p_fraction := NULL,
    p_sample_types := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_locations := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_display_factor := NULL,
    p_reportable_only := NULL,
    p_caller := 'sample_results'
) AS TABLE
WITH params AS (
    SELECT CASE
        WHEN p_units IN ('refuse', 'flag') THEN p_units
        ELSE error(p_caller || ': p_units must be refuse or flag, not '
                   || COALESCE(CAST(p_units AS VARCHAR), 'NULL'))
    END AS um,
    matrix_combine_class(p_caller, p_matrix, p_matrices) AS combine_class,
    display_factor_check(p_caller, p_display_factor) AS dfac_call,
    reportable_scope(p_reportable_only) AS rep_only,
    source_lacks('fraction') AS no_fraction,
    source_lacks('sample_type') AS no_sample_type,
    source_lacks('method_code') AS no_method,
    source_lacks('matrix') AS no_matrix,
    filters_ignored(p_matrix, p_fraction, p_sample_types, p_method_class,
                    reportable_scope(p_reportable_only)) AS ignored
),
-- The scan's filters read only columns and constants, so they reach the
-- source (a parquet scan in the hybrid consumer reads only the analytes
-- asked for); a join to params here kept the analyte list from reaching it
-- and read ten times the rows (2,614,538 for 245,879, the test facility's
-- water Mn + Pb). What depends on the dataset's settings applies after.
filtered AS (
    SELECT
        v.sample_id, v.location_id, v.x, v.y, v.matrix, v.sample_date, v.sample_datetime,
        v.top_depth, v.bottom_depth, v.position, v.sample_type,
        v.analyte, v.fraction, v.meas_basis,
        v.result, v.detected, v.qualifier, v.detection_limit, v.units, v.result_id,
        v.date_analyzed, v.method_class, v.method_code, v.reportable, v.test_rank,
        v.unit_source,
        nd_policy_choice(p_nd_policy, p_nd_policy_version, pr.nd_policy, pr.nd_policy_version,
                         ds.ds_policy, ds.ds_version) AS pol
    FROM v_results_denormalized v
    LEFT JOIN project pr ON pr.project_name = v.project
    CROSS JOIN v_nd_dataset_policy ds
    WHERE (analyte_list IS NULL
           OR v.analyte IN (SELECT unnest(CAST(analyte_list AS VARCHAR[]))))
      AND (p_matrix IS NULL OR v.matrix = p_matrix OR v.matrix IS NULL)
      AND (p_matrices IS NULL OR list_contains(CAST(p_matrices AS VARCHAR[]), v.matrix))
      AND (p_fraction IS NULL OR v.fraction = p_fraction OR v.fraction IS NULL)
      AND (p_start_date IS NULL OR v.sample_date >= p_start_date)
      AND (p_end_date IS NULL OR v.sample_date <= p_end_date)
      AND (p_locations IS NULL
           OR v.location_id IN (SELECT unnest(CAST(p_locations AS VARCHAR[]))))
),
scoped AS (
    SELECT f.*, params.um, params.dfac_call, params.ignored AS filters_ignored,
           COALESCE(params.combine_class, f.matrix) AS var_matrix
    FROM filtered f
    CROSS JOIN params
    WHERE (p_matrix IS NULL OR f.matrix IS NOT NULL OR params.no_matrix)
      AND (p_fraction IS NULL OR f.fraction IS NOT NULL OR params.no_fraction)
      AND (p_method_class IS NULL OR f.method_class = p_method_class OR params.no_method)
      AND (params.no_sample_type
           OR CASE WHEN p_sample_types IS NULL THEN in_normal_scope(f.sample_type)
                   ELSE in_sample_scope(f.sample_type, p_sample_types) END)
      AND (f.reportable IS DISTINCT FROM FALSE OR NOT params.rep_only)
)
SELECT f.*,
       count(DISTINCT f.units) OVER var > 1 AS units_mixed,
       COALESCE(bool_or(f.unit_source = 'contradicts_matrix') OVER var, FALSE) AS units_contradict
FROM scoped f
WINDOW var AS (PARTITION BY f.var_matrix, f.fraction, f.meas_basis, f.analyte, f.method_class);

-- ============================================================================
-- sample_results - one row per (sample, column): the long form
-- ============================================================================

CREATE OR REPLACE MACRO sample_results(
    analyte_list,
    p_matrix := NULL,
    p_fraction := NULL,
    p_sample_types := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_locations := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_display_factor := NULL,
    p_reportable_only := NULL
) AS TABLE
WITH ranked AS (
    SELECT
        f.*,
        f.units_mixed OR f.units_contradict AS units_conflict,
        p.name || '@' || p.version AS nd_policy,
        row_number() OVER (
            PARTITION BY f.sample_id, f.analyte, f.fraction, f.meas_basis, f.method_class
            ORDER BY CASE WHEN f.reportable THEN 0 WHEN f.reportable IS NULL THEN 1 ELSE 2 END,
                     COALESCE(f.test_rank, 0),
                     rank_reruns_key(f.detected, f.result, f.detection_limit, f.date_analyzed,
                                     f.result_id, p.reruns_nd_limit)
        ) AS rk,
        count(*) OVER (PARTITION BY f.sample_id, f.analyte, f.fraction, f.meas_basis,
                                    f.method_class) AS n_results
    FROM sample_scope(analyte_list, p_matrix := p_matrix, p_fraction := p_fraction,
                      p_sample_types := p_sample_types, p_start_date := p_start_date,
                      p_end_date := p_end_date, p_locations := p_locations,
                      p_nd_policy := p_nd_policy, p_nd_policy_version := p_nd_policy_version,
                      p_units := p_units, p_method_class := p_method_class,
                      p_matrices := p_matrices, p_display_factor := p_display_factor,
                      p_reportable_only := p_reportable_only) f
    LEFT JOIN v_nd_policies p ON p.name = f.pol.name AND p.version = COALESCE(f.pol.version, p.latest_version)
    WHERE CASE WHEN p.name IS NULL THEN CAST(error('sample_results: no non-detect policy '
                   || f.pol.name || COALESCE(' version ' || f.pol.version, '')
                   || ' in nd_policies') AS BOOLEAN)
               ELSE TRUE END
)
SELECT
    sample_id, location_id, x, y, matrix, sample_date, sample_datetime,
    top_depth, bottom_depth, position, sample_type,
    analyte, fraction, meas_basis, method_class, method_code,
    cell_key(analyte, fraction, meas_basis, method_class) AS cell_key,
    CASE WHEN dfac_call IS NOT NULL AND detected = FALSE THEN dfac_call * detection_limit
         ELSE result END AS result,
    detected, qualifier, detection_limit, units,
    n_results, units_conflict, nd_policy,
    CASE WHEN detected = FALSE THEN dfac_call END AS display_factor,
    filters_ignored
FROM ranked
WHERE rk = 1 AND (um = 'flag' OR NOT units_conflict);

-- ============================================================================
-- sample_results_refused - what sample_results leaves out, and why
-- ============================================================================
-- Same parameters as sample_results; one row per variable that call leaves
-- out under p_units := 'refuse' (none under 'flag'): its identity (matrix
-- the class's when p_matrices combines), reason ('mixed units' or
-- 'contradicts matrix'), units ('mg/L: 1, ug/L: 1': each unit and its
-- results) and n_rows, the results left out.

CREATE OR REPLACE MACRO sample_results_refused(
    analyte_list,
    p_matrix := NULL,
    p_fraction := NULL,
    p_sample_types := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_locations := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_reportable_only := NULL
) AS TABLE
SELECT analyte, var_matrix AS matrix, fraction, meas_basis, method_class,
       CASE WHEN bool_or(contra) THEN 'contradicts matrix' ELSE 'mixed units' END AS reason,
       string_agg(COALESCE(units, 'no unit') || ': ' || n, ', ' ORDER BY units) AS units,
       sum(n) AS n_rows
FROM (
    SELECT var_matrix, fraction, meas_basis, analyte, method_class, units,
           count(*) AS n, bool_or(unit_source = 'contradicts_matrix') AS contra
    FROM sample_scope(analyte_list, p_matrix := p_matrix, p_fraction := p_fraction,
                      p_sample_types := p_sample_types, p_start_date := p_start_date,
                      p_end_date := p_end_date, p_locations := p_locations,
                      p_nd_policy := p_nd_policy, p_nd_policy_version := p_nd_policy_version,
                      p_units := p_units, p_method_class := p_method_class,
                      p_matrices := p_matrices, p_reportable_only := p_reportable_only,
                      p_caller := 'sample_results_refused')
    WHERE um = 'refuse' AND (units_mixed OR units_contradict)
    GROUP BY ALL
)
GROUP BY var_matrix, fraction, meas_basis, analyte, method_class
ORDER BY analyte, matrix, fraction, meas_basis, method_class;

-- ============================================================================
-- sample_cell_maps - one row per sample, its cells in a MAP
-- ============================================================================
-- cells: MAP(cell_key -> STRUCT(value, detected, qualifier, detection_limit,
-- units, n_results, units_conflict)). Read a column out by key; a sample
-- without that cell gives NULL, which is what an absent cell is:
--
--   CREATE TEMP TABLE m AS
--       SELECT * FROM sample_cell_maps(NULL, p_matrix := 'TAILS',
--                                      p_sample_types := ['N']);
--   SELECT sample_id,
--          cells[cell_key('Manganese', 'Dissolved', NULL, 'lab')].value AS mn,
--          cells[cell_key('Lead', 'Dissolved', NULL, 'lab')].value      AS pb
--   FROM m;
--
-- (The key's fraction is as loaded: the EQuIS mappings turn D into
-- 'Dissolved'. map_keys(cells) lists what a sample has.)
--
-- MATERIALISE IT FIRST, in a statement of its own, as above. Measured in the
-- QGIS plugin (2026-10-10): a map per sample read out by key took 0.26 s for
-- every analyte in tailings, where max(...) FILTER (WHERE ...) per analyte
-- ran past five minutes and 18 GB, and read-outs over maps built in a
-- subquery took 89 s, the optimiser treating each map reference as its own
-- expression. Here, on the 3,452,146-row test facility (2026-10-22): every
-- analyte in tailings, maps 0.36 s and all 644 columns read out 0.25 s;
-- every analyte in water, 11.2 s and 6.2 s (231 columns x 79,282 samples);
-- water Mn + Pb, 0.64 s, then 0.03 s for the 19,015 dissolved pairs.

CREATE OR REPLACE MACRO sample_cell_maps(
    analyte_list,
    p_matrix := NULL,
    p_fraction := NULL,
    p_sample_types := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_locations := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_display_factor := NULL,
    p_reportable_only := NULL
) AS TABLE
SELECT
    sample_id,
    -- location, date, depths and type are the samples row's: constant here
    any_value(location_id)  AS location_id,
    any_value(x)            AS x,
    any_value(y)            AS y,
    any_value(matrix)       AS matrix,
    any_value(sample_date)  AS sample_date,
    any_value(top_depth)    AS top_depth,
    any_value(bottom_depth) AS bottom_depth,
    any_value(position)     AS position,
    any_value(sample_datetime) AS sample_datetime,
    any_value(sample_type)  AS sample_type,
    count(*)                AS n_cells,
    any_value(nd_policy)    AS nd_policy,
    any_value(filters_ignored) AS filters_ignored,
    map_from_entries(list((
        cell_key,
        struct_pack(value := result, detected := detected, qualifier := qualifier,
                    detection_limit := detection_limit, units := units,
                    n_results := n_results, units_conflict := units_conflict)
    ))) AS cells
FROM sample_results(
    analyte_list,
    p_matrix := p_matrix,
    p_fraction := p_fraction,
    p_sample_types := p_sample_types,
    p_start_date := p_start_date,
    p_end_date := p_end_date,
    p_locations := p_locations,
    p_nd_policy := p_nd_policy,
    p_nd_policy_version := p_nd_policy_version,
    p_units := p_units,
    p_method_class := p_method_class,
    p_matrices := p_matrices,
    p_display_factor := p_display_factor,
    p_reportable_only := p_reportable_only)
GROUP BY sample_id;

-- ============================================================================
-- km_quartiles_of - Kaplan-Meier quartiles over rows the caller hands in
-- ============================================================================
-- Shared rules D2 ("mean, median, quartiles"): a box over censored data is
-- drawn from Kaplan-Meier, never from substituted values. This is the one
-- copy of that arithmetic; km_quartiles calls it. The arithmetic is the QGIS
-- plugin's (web/src/censored.js), held there and here to an independent
-- product-limit estimate:
--
--   F(x) = P(X <= x) = product over detected x_k > x of (1 - d_k / n_k)
--
-- d_k the detects AT x_k, n_k the samples known to be <= x_k (a non-detect
-- at L is below L: at risk at L, not an event there). below_lowest is F0,
-- the share the estimate puts under the lowest detect without saying where.
-- A quartile p is the smallest detect x with F(x) >= p - the midpoint with
-- the next detect when F is exactly p (R's type 2 when nothing is
-- censored) - and UNDETERMINED, NULL with <q>_below TRUE, when F0 >= p. A
-- group with no detect has no quartiles; its counts and max_limit stand.
--
-- p_rows names a table, a view or a CTE of the caller (query_table): a
-- chart's brushed samples, say, which no scope parameter can state. Its
-- columns:
--   grp       any     - the group; one output row per grp
--   value     DOUBLE  - a detect's result, a non-detect's limit
--   detected  BOOLEAN - NULL when the detect status is unknown
-- A row with a NULL value or detected is counted in n_left_out, never
-- placed. A CTE named like a table shadows it, so a caller's CTE needs a
-- name of its own. Internal names start kmq_ for the same reason.

CREATE OR REPLACE MACRO km_quartiles_of(p_rows) AS TABLE
WITH kmq_rows AS (
    SELECT grp, CAST(value AS DOUBLE) AS v, CAST(detected AS BOOLEAN) AS d
    FROM query_table(p_rows)
),
kmq_src AS (
    SELECT * FROM kmq_rows WHERE d IS NOT NULL AND v IS NOT NULL
),
kmq_steps AS (
    SELECT grp, v, sum(CAST(d AS INTEGER)) AS dk, count(*) AS c FROM kmq_src GROUP BY grp, v
),
kmq_km AS (
    SELECT grp, v, dk,
           CASE WHEN dk > 0
                THEN 1 - CAST(dk AS DOUBLE) / sum(c) OVER (PARTITION BY grp ORDER BY v
                                                           ROWS UNBOUNDED PRECEDING)
                ELSE 1.0 END AS f
    FROM kmq_steps
),
kmq_cdf AS (
    SELECT grp, v, dk,
           COALESCE(product(f) OVER (PARTITION BY grp ORDER BY v DESC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 1.0) AS F,
           product(f) OVER (PARTITION BY grp) AS F0
    FROM kmq_km
),
kmq_det AS (
    SELECT grp, v, F, F0, lead(v) OVER (PARTITION BY grp ORDER BY v) AS nxt
    FROM kmq_cdf WHERE dk > 0
),
kmq_quart AS (
    -- 1e-9: the midpoint rule's equality on a product of doubles
    -- (0.75 * (2/3) is 0.49999999999999994)
    SELECT grp, F0,
           CASE WHEN F0 >= 0.25 - 1e-9 THEN NULL
                ELSE arg_min(CASE WHEN abs(F - 0.25) < 1e-9 AND nxt IS NOT NULL
                                  THEN (v + nxt) / 2 ELSE v END, v)
                     FILTER (WHERE F >= 0.25 - 1e-9) END AS q1,
           F0 >= 0.25 - 1e-9 AS q1_below,
           CASE WHEN F0 >= 0.5 - 1e-9 THEN NULL
                ELSE arg_min(CASE WHEN abs(F - 0.5) < 1e-9 AND nxt IS NOT NULL
                                  THEN (v + nxt) / 2 ELSE v END, v)
                     FILTER (WHERE F >= 0.5 - 1e-9) END AS med,
           F0 >= 0.5 - 1e-9 AS med_below,
           CASE WHEN F0 >= 0.75 - 1e-9 THEN NULL
                ELSE arg_min(CASE WHEN abs(F - 0.75) < 1e-9 AND nxt IS NOT NULL
                                  THEN (v + nxt) / 2 ELSE v END, v)
                     FILTER (WHERE F >= 0.75 - 1e-9) END AS q3,
           F0 >= 0.75 - 1e-9 AS q3_below
    FROM kmq_det GROUP BY grp, F0
),
kmq_counts AS (
    SELECT grp,
           count(*) FILTER (WHERE d IS NOT NULL AND v IS NOT NULL) AS n,
           count(*) FILTER (WHERE d AND v IS NOT NULL) AS detects,
           min(v) FILTER (WHERE d) AS min_detect,
           max(v) FILTER (WHERE d) AS max_detect,
           max(v) FILTER (WHERE NOT d) AS max_limit,
           count(*) FILTER (WHERE d IS NULL OR v IS NULL) AS n_left_out
    FROM kmq_rows GROUP BY grp
)
SELECT c.grp, c.n, c.detects, c.n - c.detects AS nondetects,
       c.min_detect, c.max_detect, c.max_limit,
       q.F0 AS below_lowest, q.q1, q.q1_below, q.med, q.med_below, q.q3, q.q3_below,
       c.n_left_out
FROM kmq_counts c
LEFT JOIN kmq_quart q ON q.grp IS NOT DISTINCT FROM c.grp
ORDER BY c.grp;

-- ============================================================================
-- km_quartiles - Kaplan-Meier quartiles per group, for box plots
-- ============================================================================
-- km_quartiles_of (above) over the samples sample_results gives for the same
-- scope: one result per sample, its re-runs resolved, mixed units refused. A
-- non-detect stands at its detection_limit. One row per variable and group.
--
-- p_group: 'none' (default; grp 'All samples'), 'location', 'year'.
-- n_left_out counts the samples that could not be placed: an unknown detect
-- status, or a non-detect without a limit. The other parameters are
-- sample_results'.

CREATE OR REPLACE MACRO km_quartiles(
    analyte_list,
    p_group := 'none',
    p_matrix := NULL,
    p_fraction := NULL,
    p_sample_types := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_locations := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_reportable_only := NULL
) AS TABLE
WITH cells AS (
    SELECT COALESCE(matrix_combine_class('km_quartiles', p_matrix, p_matrices), s.matrix) AS matrix,
           s.fraction, s.meas_basis, s.analyte, s.method_class, s.units,
           CASE p_group
               WHEN 'none' THEN 'All samples'
               WHEN 'location' THEN COALESCE(s.location_id, '(no location)')
               WHEN 'year' THEN COALESCE(CAST(year(s.sample_date) AS VARCHAR), '(undated)')
               ELSE error('km_quartiles: p_group must be none, location or year, not '
                          || COALESCE(CAST(p_group AS VARCHAR), 'NULL'))
           END AS g,
           CAST(CASE WHEN s.detected THEN s.result ELSE s.detection_limit END AS DOUBLE) AS v,
           s.detected AS d
    FROM sample_results(analyte_list, p_matrix := p_matrix, p_fraction := p_fraction,
                        p_sample_types := p_sample_types, p_start_date := p_start_date,
                        p_end_date := p_end_date, p_locations := p_locations,
                        p_nd_policy := p_nd_policy, p_nd_policy_version := p_nd_policy_version,
                        p_units := p_units, p_method_class := p_method_class,
                        p_matrices := p_matrices, p_reportable_only := p_reportable_only) s
),
km_cells AS (
    -- a name of its own: query_table reads a CTE before a table of that name
    SELECT hash(matrix, fraction, meas_basis, analyte, method_class, g) AS grp,
           v AS value, d AS detected
    FROM cells
),
labels AS (
    SELECT hash(matrix, fraction, meas_basis, analyte, method_class, g) AS grp,
           any_value(matrix) AS matrix, any_value(fraction) AS fraction,
           any_value(meas_basis) AS meas_basis, any_value(analyte) AS analyte,
           any_value(method_class) AS method_class, any_value(g) AS g,
           string_agg(DISTINCT units, ', ' ORDER BY units) AS units
    FROM cells GROUP BY ALL
)
SELECT l.analyte, l.matrix, l.fraction, l.meas_basis, l.method_class, l.g AS grp, l.units,
       k.n, k.detects, k.nondetects, k.min_detect, k.max_detect, k.max_limit,
       k.below_lowest, k.q1, k.q1_below, k.med, k.med_below, k.q3, k.q3_below,
       k.n_left_out
FROM labels l
JOIN km_quartiles_of('km_cells') k USING (grp)
ORDER BY l.analyte, l.matrix, l.fraction, l.meas_basis, l.method_class, l.g;
