-- ChemDuck Schema: aggregate_points, the non-detect policy engine
--
-- One value per measurement point, with every non-detect choice taken from
-- the project's non-detect policy (nd_policies, nd_policy_rules;
-- docs/shared-rules-decisions-response.md, D7) and every row carrying its
-- provenance. aggregate_results (08_aggregate_results.sql) calls this when
-- it is given neither dup_agg nor nd_method.
--
-- Each stage hands the next one VALUES of one shape, a struct
--   v = {detected, result, lim, computed, lim_source, lim_type, method_code,
--        sample_id, depth, units, std_units, rerun_by}
-- (the analyte's own attributes - anl_sm_mb, analyte_group, cas_number -
-- are read once per measurement identity, not carried through the stages)
-- and is ONE hash aggregate over its groups: the winner of a selection by
-- arg_min over the stage's rank key (rank_reruns_key / rank_across_key),
-- and, only for a computed rule, the group's items {inp, v} in one list
-- (inp: the item's value under the stage's substitution factor).
--
-- Why this shape, measured: ranking by window functions sorted every row
-- (2.1 s of a 2.5 s call over 116,644 iron results at the test facility);
-- and a GROUP BY carrying ~60 separate aggregates paid a per-thread setup
-- that took a 10-row query from 0.15 s on one thread to 1.2 s on fourteen.
-- As it stands (2026-10-23, the 3,452,146-row test facility, the 8
-- most-measured analytes x 3 recipes): median 1.5 s under chemduck_0_1 and
-- 1.8 s under chemduck_default, against 0.5 s for aggregate_results_legacy,
-- with chemduck_0_1's output identical to the legacy's in all 24 calls. The
-- difference is a stage the legacy lacks (each sample's re-runs, ~116,000
-- groups for iron) and the provenance carried through every stage.

-- ============================================================================
-- v_nd_policies / nd_policy_choice - Which policy applies, defined once
-- ============================================================================
-- v_nd_policies: one row per policy version, its header and its stage rules
-- as structs {est, f, fl, alln, det}, and the policy's latest version.
-- nd_policy_choice: the policy a row is read under - the call's, else the
-- project's, else the dataset's (chemduck_settings), else chemduck_default -
-- as {name, version}; version NULL means the latest.

CREATE OR REPLACE VIEW v_nd_policies AS
SELECT p.name, p.version, p.limit_basis, p.display_factor, p.across_nd_limit, p.reruns_nd_limit,
       p.event_grain,
       max(p.version) OVER (PARTITION BY p.name) AS latest_version,
       any_value(struct_pack(est := r.estimator, f := r.nd_factor, fl := r.km_floor,
                             alln := r.all_nd, det := r.detected_rule))
           FILTER (WHERE r.stage = 'reruns') AS r_reruns,
       any_value(struct_pack(est := r.estimator, f := r.nd_factor, fl := r.km_floor,
                             alln := r.all_nd, det := r.detected_rule))
           FILTER (WHERE r.stage = 'same_day') AS r_same_day,
       any_value(struct_pack(est := r.estimator, f := r.nd_factor, fl := r.km_floor,
                             alln := r.all_nd, det := r.detected_rule))
           FILTER (WHERE r.stage = 'over_time') AS r_over_time
FROM nd_policies p
LEFT JOIN nd_policy_rules r ON r.policy = p.name AND r.version = p.version
GROUP BY p.name, p.version, p.limit_basis, p.display_factor, p.across_nd_limit, p.reruns_nd_limit,
         p.event_grain;

CREATE OR REPLACE MACRO nd_policy_choice(call_policy, call_version, project_policy,
                                         project_version, ds_policy, ds_version) AS (
    struct_pack(
        name := COALESCE(call_policy, project_policy, ds_policy, 'chemduck_default'),
        version := CAST(CASE
            WHEN call_policy IS NOT NULL THEN call_version
            WHEN project_policy IS NOT NULL THEN project_version
            WHEN ds_policy IS NOT NULL THEN ds_version
        END AS INTEGER))
);

-- The dataset's policy setting, as one row.
CREATE OR REPLACE VIEW v_nd_dataset_policy AS
SELECT max(value) FILTER (WHERE key = 'nd_policy') AS ds_policy,
       TRY_CAST(max(value) FILTER (WHERE key = 'nd_policy_version') AS INTEGER) AS ds_version
FROM chemduck_settings;

-- ============================================================================
-- nd_items_* / nd_stage_value - One stage's rule over one group
-- ============================================================================
-- A stage groups its rows into `items` (a list of {inp, v}; for a computed
-- rule only) and its winner (arg_min by the stage's rank key), then, in a
-- projection of its own, takes the known items' values, limits and flags out
-- of the list ONCE as columns. nd_stage_value reads only
-- those columns. (Macros expand inline: a rule that re-derived them from the
-- list at each use built an expression tree large enough that its
-- per-thread setup cost more than the query.)

CREATE OR REPLACE MACRO nd_items_known(items) AS (
    list_filter(items, lambda item: item.v.detected IS NOT NULL)
);

-- The limit a computed value states: by all_nd, else the highest.
CREATE OR REPLACE MACRO nd_rep_limit(alln, lims) AS (
    CASE alln
        WHEN 'min_limit' THEN list_min(lims)
        WHEN 'median_limit' THEN list_aggregate(lims, 'median')
        ELSE list_max(lims)
    END
);

-- A stage's rule (est, f, alln, det; dfac the policy's display_factor)
-- over one group: win the winner's value; inps / lims / dets the known
-- items' substituted values, limits and detect flags; attrs the group's
-- lowest attribute values (as a computed value reports them). Returns the
-- stage's value v. A selection returns its winner as it is; a computed
-- value is marked computed, so a later stage takes it as it stands. With
-- select, all_nd does not apply: the selected non-detect is a real result,
-- valued at the end by display_factor.
--
-- combine (D2): a group of detects is their mean; a group with a non-detect
-- is its winner by the stage's order - the highest detect, or among
-- non-detects the limit the order prefers. A detect is never averaged with
-- a substituted limit.
CREATE OR REPLACE MACRO nd_stage_value(est, f, alln, det, dfac, n_items, win, inps, lims, dets, attrs) AS (
    CASE WHEN est IN ('select', 'refuse')
              OR (est = 'combine' AND NOT COALESCE(list_aggregate(dets, 'bool_and'), FALSE))
    THEN struct_pack(
        detected := win.detected,
        result := CASE WHEN est = 'refuse' AND n_items > 1 THEN CAST(error(
                      'the non-detect policy refuses to combine results at this stage ('
                      || n_items || ' results); choose a policy with a rule for it') AS DOUBLE)
                  ELSE win.result END,
        lim := win.lim, computed := win.computed,
        lim_source := win.lim_source, lim_type := win.lim_type,
        method_code := win.method_code, sample_id := win.sample_id,
        depth := win.depth, units := win.units, std_units := win.std_units,
        rerun_by := win.rerun_by)
    ELSE struct_pack(
        detected := CAST(CASE
            WHEN len(dets) = 0 THEN NULL
            WHEN det = 'all' THEN list_aggregate(dets, 'bool_and')
            ELSE list_aggregate(dets, 'bool_or')
        END AS BOOLEAN),
        result := CAST(CASE
            WHEN len(dets) = 0 THEN NULL
            WHEN NOT list_aggregate(dets, 'bool_or') AND alln <> 'estimator'
                THEN COALESCE(f, dfac) * nd_rep_limit(alln, lims)
            WHEN est IN ('mean', 'combine') THEN list_avg(inps)
            WHEN est = 'median' THEN list_aggregate(inps, 'median')
            WHEN est = 'max' THEN list_max(inps)
            WHEN est = 'min' THEN list_min(inps)
        END AS DOUBLE),
        lim := CAST(nd_rep_limit(alln, lims) AS DOUBLE),
        computed := len(dets) > 0,
        lim_source := CAST(NULL AS VARCHAR), lim_type := CAST(NULL AS VARCHAR),
        method_code := CAST(NULL AS VARCHAR),
        sample_id := CAST(NULL AS VARCHAR),
        depth := attrs.depth, units := attrs.units, std_units := attrs.std_units,
        rerun_by := CAST(NULL AS VARCHAR))
    END
);

-- ============================================================================
-- matrix_combine_class - The class p_matrices combine into (shared rules D6)
-- ============================================================================
-- NULL without p_matrices; else the one class every listed matrix is curated
-- in, or an error naming why not: p_matrix given too, an empty list, a
-- matrix not in matrix_classes, or matrices of different classes. caller
-- prefixes the message. aggregate_points and the per-sample macros share it.
--
-- One subquery, the listed matrices against matrix_classes: every engine's
-- scope carries this, and the optimizer orders each subquery's joins with
-- the scan's even when p_matrices is NULL (six reads of matrix_classes cost
-- 0.06 s of join ordering a call at the test facility). A NULL in the list
-- is skipped, as list_contains skips it.

CREATE OR REPLACE MACRO matrix_combine_class(caller, p_matrix, p_matrices) AS (
    CASE
        WHEN p_matrices IS NULL THEN NULL
        WHEN p_matrix IS NOT NULL THEN error(
            caller || ': give p_matrix (one matrix) or p_matrices (matrices to combine), not both')
        WHEN len(CAST(p_matrices AS VARCHAR[])) = 0 THEN error(
            caller || ': p_matrices is empty; name the matrices to combine')
        ELSE (
            SELECT CASE
                WHEN count(*) FILTER (WHERE u.m IS NOT NULL AND c.matrix IS NULL) > 0 THEN error(
                    caller || ': matrices not curated in matrix_classes combine with nothing: '
                    || string_agg(u.m, ', ' ORDER BY u.m)
                           FILTER (WHERE u.m IS NOT NULL AND c.matrix IS NULL))
                WHEN count(DISTINCT c.matrix_class) > 1 THEN error(
                    caller || ': matrices of different classes never combine: '
                    || array_to_string(list_transform(
                           list_sort(list_distinct(list(struct_pack(m := c.matrix, k := c.matrix_class))
                                                   FILTER (WHERE c.matrix IS NOT NULL))),
                           lambda s: s.m || ' (' || s.k || ')'), ', '))
                ELSE any_value(c.matrix_class)
            END
            FROM unnest(CAST(p_matrices AS VARCHAR[])) AS u(m)
            LEFT JOIN matrix_classes c ON c.matrix = u.m)
    END
);

-- ============================================================================
-- Scope settings the load recorded (load_data.record_scope)
-- ============================================================================
-- source_lacks(col): the source carries no such column (chemduck_settings
-- source_columns_absent), so a filter on it has nothing to match: it
-- matches everything, and filters_ignored names it on every row.
-- reportable_scope(p): whether a call leaves non-reportable results out -
-- p if given, else the dataset's default (TRUE where its source carries a
-- reportable column), else FALSE.

CREATE OR REPLACE MACRO source_lacks(col) AS (
    COALESCE((SELECT list_contains(string_split(value, ','), col) FROM chemduck_settings
              WHERE key = 'source_columns_absent'), FALSE)
);

CREATE OR REPLACE MACRO reportable_scope(p_reportable_only) AS (
    COALESCE(CAST(p_reportable_only AS BOOLEAN),
             (SELECT value = 'true' FROM chemduck_settings WHERE key = 'reportable_only'),
             FALSE)
);

CREATE OR REPLACE MACRO filters_ignored(p_matrix, p_fraction, p_sample_types, p_method_class,
                                        p_reportable_only) AS (
    list_concat(
        CASE WHEN p_matrix IS NOT NULL AND source_lacks('matrix') THEN ['p_matrix'] ELSE [] END,
        CASE WHEN p_fraction IS NOT NULL AND source_lacks('fraction') THEN ['p_fraction'] ELSE [] END,
        CASE WHEN p_sample_types IS NOT NULL AND source_lacks('sample_type')
             THEN ['p_sample_types'] ELSE [] END,
        CASE WHEN p_method_class IS NOT NULL AND source_lacks('method_code')
             THEN ['p_method_class'] ELSE [] END,
        CASE WHEN p_reportable_only AND source_lacks('reportable')
             THEN ['p_reportable_only'] ELSE [] END)
);

-- display_factor_check - a call's p_display_factor, refused below zero.
CREATE OR REPLACE MACRO display_factor_check(caller, p_display_factor) AS (
    CASE WHEN p_display_factor IS NULL OR p_display_factor >= 0 THEN CAST(p_display_factor AS DOUBLE)
         ELSE error(caller || ': p_display_factor must be 0 or more, not '
                    || CAST(p_display_factor AS VARCHAR)) END
);

-- ============================================================================
-- point_scope / units_by_variable - What a points call reads, and its units
-- ============================================================================
-- aggregate_points and point_counts share both, so what is counted is what
-- would map. point_scope: the rows of one analyte a call's filters keep
-- (see aggregate_points for each filter), matrix being the class's under
-- p_matrices; caller prefixes its refusals. units_by_variable: per variable
-- (vkey) of the rows p_rows names (query_table: the caller's CTE, with
-- vkey, units, unit_source, um and the variable's columns), its number of
-- units and whether they conflict - refused, naming the variable, its
-- units and their counts, unless um is 'flag'.

CREATE OR REPLACE MACRO point_scope(p_caller, p_analyte, p_matrix, p_fraction, p_start_date,
                                    p_end_date, p_sample_types, p_method_class, p_matrices,
                                    p_locations, p_reportable_only) AS TABLE
WITH scope AS (
    SELECT
    -- D6: the class the listed matrices share, or an error saying why not
    matrix_combine_class(p_caller, p_matrix, p_matrices) AS combine_class,
    reportable_scope(p_reportable_only) AS rep_only,
    source_lacks('fraction') AS no_fraction,
    source_lacks('sample_type') AS no_sample_type,
    source_lacks('method_code') AS no_method,
    source_lacks('matrix') AS no_matrix
)
SELECT
    v.location_id, v.x, v.y, v.project,
    COALESCE(scope.combine_class, v.matrix) AS matrix, v.matrix AS src_matrix,
    v.unit_source,
    COALESCE(v.top_depth, 0)    AS top_depth,
    COALESCE(v.bottom_depth, 0) AS bottom_depth,
    COALESCE(v.depth, 0)        AS depth,
    v.sample_date, v.sample_datetime, v.position, v.sample_id, v.fraction, v.meas_basis,
    v.analyte, v.anl_sm_mb, v.analyte_group, v.cas_number,
    v.result, v.detected, v.units, v.std_units, v.result_id, v.method_code, v.date_analyzed,
    v.method_class, v.reportable, v.test_rank,
    v.detection_limit, v.limit_source, v.limit_type,
    v.reporting_limit, v.method_detection_limit, v.quantitation_limit
FROM v_results_core v
CROSS JOIN scope
WHERE v.analyte = p_analyte
  AND (p_matrix IS NULL OR v.matrix = p_matrix OR v.matrix IS NULL)
  AND (p_matrix IS NULL OR v.matrix IS NOT NULL OR scope.no_matrix)
  AND (p_matrices IS NULL OR list_contains(CAST(p_matrices AS VARCHAR[]), v.matrix))
  AND (p_method_class IS NULL OR v.method_class = p_method_class OR scope.no_method)
  AND (p_fraction IS NULL OR v.fraction = p_fraction OR v.fraction IS NULL)
  AND (p_fraction IS NULL OR v.fraction IS NOT NULL OR scope.no_fraction)
  AND (p_start_date IS NULL OR v.sample_date >= p_start_date)
  AND (p_end_date IS NULL OR v.sample_date <= p_end_date)
  AND (p_locations IS NULL
       OR v.location_id IN (SELECT unnest(CAST(p_locations AS VARCHAR[]))))
  AND (scope.no_sample_type
       OR CASE WHEN p_sample_types IS NULL THEN in_normal_scope(v.sample_type)
               ELSE in_sample_scope(v.sample_type, p_sample_types) END)
  AND (v.reportable IS DISTINCT FROM FALSE OR NOT scope.rep_only);

CREATE OR REPLACE MACRO units_by_variable(p_rows, p_caller) AS TABLE
WITH unit_counts AS (
    SELECT vkey, units, count(*) AS n,
           bool_or(unit_source = 'contradicts_matrix') AS contra,
           any_value(analyte || ' in ' || matrix || COALESCE(', ' || fraction, '')
                     || COALESCE(', ' || meas_basis, '')
                     || COALESCE(', method class ' || method_class, '')) AS label,
           any_value(um) AS um
    FROM query_table(p_rows) GROUP BY vkey, units
)
SELECT vkey, count(units) AS n_units,
       count(units) > 1 OR bool_or(contra) AS conflict
FROM unit_counts
GROUP BY vkey
HAVING CASE
    WHEN any_value(um) = 'refuse' AND (count(units) > 1 OR bool_or(contra)) THEN CAST(error(
        p_caller || ': ' || any_value(label)
        || CASE WHEN bool_or(contra) THEN ' has a unit that contradicts its matrix ('
                ELSE ' is in more than one unit (' END
        || string_agg(COALESCE(units, 'no unit') || ': ' || n, ', ' ORDER BY units)
        || '); see load_report (units) and v_unit_conflicts. Narrow the call (p_matrix, '
        || 'p_fraction, p_method_class), curate a conversion and reload, or pass '
        || 'p_units := ''flag''') AS BOOLEAN)
    ELSE TRUE
END;

-- ============================================================================
-- aggregate_points - One value per measurement point, by policy
-- ============================================================================
--
-- One row per measurement point and variable
--   (location_id, matrix, top_depth, bottom_depth, position, fraction,
--    meas_basis, analyte, method_class)
-- (shared rules D3: methods of different classes - a field and a lab pH, a
-- leach test and a total digest - are different variables; within a class,
-- a sample's re-runs are chosen among by the source's reportable flag, then
-- its test-type precedence, then rank_reruns, and rerun_selected_by says
-- which decided)
-- (shared rules D1: a vertical position, by depth or by label, is part of
-- the point; an unknown position is its own value, never merged with a
-- known one). No rollup happens here: one value per location is
-- rollup_points' job, by a rule the caller names.
--
-- An event is a sample at its time: samples of one point merge in the
-- same_day stage only when their times coincide, or their dates when the
-- source records no time (sample_datetime NULL), where rank_across then
-- breaks the tie. A policy with event_grain 'date' merges a whole day
-- instead (D1d's explicit recipe; chemduck_0_1).
--
-- p_sample_types: NULL (the default) = normal samples, typed N or untyped;
-- [] = every type; a list = those types. The non-detect policy is p_nd_policy if
-- given; else the policy of the location's project (project.nd_policy);
-- else the dataset's (chemduck_settings); else chemduck_default. The
-- latest version, unless one is named.
--
-- Stages, each by the policy's rule for it:
--   reruns    the results of one sample   (selects by rank_reruns)
--   same_day  the samples of one event    (selects by rank_across)
--   events    p_event_agg:
--               'most_recent'  the latest event; ties by rank_across
--               'maximum'      the first event by rank_across
--               'statistic'    the policy's over_time rule ('mean' is an
--                              alias, for callers of the old macro)
--
-- A selected non-detect is valued at display_factor x its limit: the
-- policy's, or p_display_factor for this call alone (it changes how the
-- selected non-detect is drawn, never which result is selected), and the
-- row's display_factor says which factor drew it (NULL for a detect or a
-- computed value). Every rule yields one number; NULL only when nothing has
-- a known detect status. An unknown detect status (NULL detected) enters no
-- rule; n_unknown_detect counts it.
--
-- p_locations narrows the call to those locations. p_each_sample (for
-- point_series) makes every sample its own point: its re-runs are resolved
-- as above, events are never combined.
--
-- p_reportable_only: TRUE leaves out results the source marks not
-- reportable; NULL (the default) is the dataset's choice, recorded at load
-- (TRUE where the source carries a reportable column). A filter on a column
-- the source lacks (chemduck_settings source_columns_absent) matches
-- everything; filters_ignored lists such filters on every row.
--
-- selected_by says which order chose the row; where the samples of one
-- date had no time of day to order them, it adds that the tie-break by
-- rank_across decided (shared rules D1).
--
-- Units (shared rules D5): conversion happens once, at load; here there is
-- none. A VARIABLE of the call - (matrix, fraction, meas_basis, analyte,
-- method_class) across every location - still in more than one unit, or in
-- a unit that contradicts its matrix (unit_source 'contradicts_matrix'),
-- is refused: an error names it, its units and their counts. p_units :=
-- 'flag' returns it instead, units_conflict TRUE on its rows. n_units is
-- the variable's number of units. p_method_class narrows the call to one
-- method class.
--
-- Matrices (shared rules D6): p_matrices, a list, combines those matrices
-- into one, and only when every one is curated in matrix_classes and all
-- share one class - water never with a solid, an uncurated matrix with
-- nothing. The point is then keyed on the class (matrix is the class's
-- name), positions and depths still apart, and units still checked per
-- variable, now the class's. matrices lists what each point drew on.
-- Without p_matrices nothing is combined: each matrix is its own point.
-- p_matrix (one matrix) and p_matrices are not given together.
--
-- Provenance on every row: sample_id, method_code, limit_source and
-- limit_type of the winning result (NULL for a computed value);
-- selected_by; stat_method;
-- nd_policy ('name@version'); nd_rule (the rules that produced the number);
-- n_points; matrices; unit_source (how its results reached their unit at
-- load, each source once); below_lowest_detect (true when Kaplan-Meier
-- placed part of the value with the policy's floor).

CREATE OR REPLACE MACRO aggregate_points(
    p_analyte,
    p_event_agg := 'most_recent',
    p_matrix := NULL,
    p_fraction := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_sample_types := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_locations := NULL,
    p_display_factor := NULL,
    p_reportable_only := NULL,
    p_each_sample := FALSE
) AS TABLE
WITH params AS (
    SELECT CASE
        WHEN p_event_agg IN ('most_recent', 'maximum') THEN p_event_agg
        WHEN p_event_agg IN ('statistic', 'mean') THEN 'statistic'
        ELSE error('aggregate_points: p_event_agg must be most_recent, maximum or statistic, not '
                   || COALESCE(CAST(p_event_agg AS VARCHAR), 'NULL'))
    END AS ea,
    CASE
        WHEN p_units IN ('refuse', 'flag') THEN p_units
        ELSE error('aggregate_points: p_units must be refuse or flag, not '
                   || COALESCE(CAST(p_units AS VARCHAR), 'NULL')
                   || ' (units are converted at load, never here)')
    END AS um,
    display_factor_check('aggregate_points', p_display_factor) AS dfac_call,
    filters_ignored(p_matrix, p_fraction, p_sample_types, p_method_class,
                    reportable_scope(p_reportable_only)) AS ignored
),
filtered AS (
    SELECT
        v.* EXCLUDE (project), params.um,
        nd_policy_choice(p_nd_policy, p_nd_policy_version, pr.nd_policy, pr.nd_policy_version,
                         ds.ds_policy, ds.ds_version) AS pol
    FROM point_scope('aggregate_points', p_analyte, p_matrix, p_fraction, p_start_date,
                     p_end_date, p_sample_types, p_method_class, p_matrices, p_locations,
                     p_reportable_only) v
    LEFT JOIN project pr ON pr.project_name = v.project
    CROSS JOIN v_nd_dataset_policy ds
    CROSS JOIN params
),
withpol AS (
    SELECT
        f.*,
        hash(f.location_id, f.matrix, f.top_depth, f.bottom_depth, f.position,
             f.fraction, f.meas_basis, f.analyte, f.method_class,
             CASE WHEN p_each_sample THEN f.sample_id END) AS ikey,
        hash(f.matrix, f.fraction, f.meas_basis, f.analyte, f.method_class) AS vkey,
        CASE WHEN p.event_grain = 'date' THEN CAST(f.sample_date AS TIMESTAMP)
             ELSE COALESCE(f.sample_datetime, CAST(f.sample_date AS TIMESTAMP))
        END AS ev_key,
        p.event_grain = 'timestamp' AND f.sample_datetime IS NOT NULL AS has_time,
        p.name || '@' || p.version AS nd_policy,
        p.display_factor, p.across_nd_limit, p.reruns_nd_limit,
        p.r_reruns, p.r_same_day, p.r_over_time,
        CASE p.limit_basis
            WHEN 'source_chain' THEN f.detection_limit
            WHEN 'reporting' THEN f.reporting_limit
            WHEN 'method_detection' THEN f.method_detection_limit
            ELSE f.quantitation_limit
        END AS lim,
        CASE WHEN p.limit_basis = 'source_chain' THEN f.limit_source ELSE p.limit_basis END AS lim_source,
        CASE WHEN p.limit_basis = 'source_chain' THEN f.limit_type ELSE p.limit_basis END AS lim_type,
        p.event_grain
    FROM filtered f
    LEFT JOIN v_nd_policies p ON p.name = f.pol.name AND p.version = COALESCE(f.pol.version, p.latest_version)
    WHERE CASE
        WHEN p.name IS NULL THEN CAST(error('aggregate_points: no non-detect policy '
            || f.pol.name || COALESCE(' version ' || f.pol.version, '')
            || ' in nd_policies') AS BOOLEAN)
        WHEN p.r_reruns IS NULL OR p.r_same_day IS NULL OR p.r_over_time IS NULL THEN CAST(error(
            'aggregate_points: non-detect policy ' || p.name || '@' || p.version
            || ' lacks a rule for reruns, same_day or over_time') AS BOOLEAN)
        ELSE TRUE
    END
),
-- Each variable's units across the call (D5): refused, unless flagged, when
-- more than one, or one that contradicts the matrix.
unit_vars AS (
    FROM units_by_variable('withpol', 'aggregate_points')
),
-- What is constant per measurement identity, once: its fields, its policy,
-- its units and its unknown detect statuses.
ident AS (
    SELECT ikey,
           any_value(location_id) AS location_id, any_value(x) AS x, any_value(y) AS y,
           any_value(matrix) AS matrix, any_value(top_depth) AS top_depth,
           any_value(bottom_depth) AS bottom_depth, any_value(position) AS position,
           any_value(fraction) AS fraction,
           any_value(meas_basis) AS meas_basis, any_value(analyte) AS analyte,
           any_value(method_class) AS method_class,
           any_value(nd_policy) AS nd_policy, any_value(display_factor) AS display_factor,
           any_value(across_nd_limit) AS across_nd_limit,
           any_value(r_reruns) AS r_reruns, any_value(r_same_day) AS r_same_day,
           any_value(r_over_time) AS r_over_time,
           any_value(event_grain) AS event_grain,
           min(anl_sm_mb) AS anl_sm_mb, min(analyte_group) AS analyte_group,
           min(cas_number) AS cas_number,
           any_value(vkey) AS vkey,
           list(DISTINCT src_matrix ORDER BY src_matrix) AS matrices,
           string_agg(DISTINCT unit_source, ', ' ORDER BY unit_source) AS unit_source,
           count(*) FILTER (WHERE detected IS NULL) AS n_unknown_detect
    FROM withpol GROUP BY ikey
),
-- How many results each event holds. A sample with no re-run, in an event
-- of one sample, gives the next two stages nothing to choose among or
-- pool: its row goes round their GROUP BYs, whose arg_min, list and
-- count(DISTINCT) over every row took 0.23 s of a 0.54 s map at the test
-- facility, where no sample has a re-run and under 1% of rows share an
-- event. IS NOT DISTINCT FROM: ev_key would be NULL for an undated sample,
-- which the schema refuses today; the join must never drop a row.
ev_n AS (
    SELECT ikey, ev_key, count(*) AS n_ev FROM withpol GROUP BY ikey, ev_key
),
-- Stage 1: reruns - the results of one sample.
s1_items AS (
    SELECT w.ikey, sample_id, sample_date, w.ev_key, has_time, display_factor, r_reruns, n.n_ev,
           -- reportable first, then the declared test-type precedence, then rank_reruns
           CASE WHEN reportable THEN 0 WHEN reportable IS NULL THEN 1 ELSE 2 END AS rep_rank,
           COALESCE(test_rank, 0) AS tt_rank,
           struct_pack(r := CASE WHEN reportable THEN 0 WHEN reportable IS NULL THEN 1 ELSE 2 END,
                       tt := COALESCE(test_rank, 0),
                       k := rank_reruns_key(detected, result, lim, date_analyzed, result_id,
                                            reruns_nd_limit)) AS k,
           nd_input_value(detected, result, lim, FALSE, r_reruns.f) AS inp,
           struct_pack(
               detected := CAST(detected AS BOOLEAN), result := CAST(result AS DOUBLE),
               lim := CAST(lim AS DOUBLE), computed := FALSE,
               lim_source := CAST(lim_source AS VARCHAR), lim_type := CAST(lim_type AS VARCHAR),
               method_code := CAST(method_code AS VARCHAR),
               sample_id := CAST(sample_id AS VARCHAR), depth := CAST(depth AS DOUBLE),
               units := CAST(units AS VARCHAR), std_units := CAST(std_units AS VARCHAR),
               rerun_by := CAST(NULL AS VARCHAR)) AS v
    FROM withpol w
    JOIN ev_n n ON n.ikey = w.ikey AND n.ev_key IS NOT DISTINCT FROM w.ev_key
),
s1 AS (
    SELECT ikey, sample_id, sample_date, ev_key, has_time, n_ev, v
    FROM s1_items WHERE r_reruns.est = 'pool'
    UNION ALL
    SELECT ikey, sample_id, sample_date, ev_key, has_time, n_ev,
           struct_update(nd_stage_value(rule.est, rule.f, rule.alln, rule.det, dfac, n_items,
                                        win, inps, lims, dets, attrs),
                         rerun_by := rerun_by) AS v
    FROM (
        SELECT *,
               list_transform(known, lambda item: item.inp) AS inps,
               list_transform(known, lambda item: item.v.lim) AS lims,
               list_transform(known, lambda item: item.v.detected) AS dets
        FROM (
            SELECT *, nd_items_known(items) AS known
            FROM (
                SELECT ikey, sample_id, any_value(sample_date) AS sample_date,
                       any_value(ev_key) AS ev_key, bool_or(has_time) AS has_time,
                       any_value(n_ev) AS n_ev,
                       any_value(r_reruns) AS rule, any_value(display_factor) AS dfac,
                       count(*) AS n_items,
                       CASE
                           WHEN count(*) = 1 THEN NULL
                           WHEN any_value(r_reruns.est) NOT IN ('select', 'refuse')
                               THEN any_value(r_reruns.est)
                           WHEN count(DISTINCT rep_rank) > 1 THEN 'reportable'
                           WHEN count(DISTINCT tt_rank) > 1 THEN 'test_type'
                           ELSE 'rank_reruns'
                       END AS rerun_by,
                       arg_min(v, k) AS win,
                       list(struct_pack(inp := inp, v := v))
                           FILTER (WHERE r_reruns.est NOT IN ('select', 'refuse')) AS items,
                       struct_pack(depth := min(v.depth), units := min(v.units),
                                   std_units := min(v.std_units)) AS attrs
                FROM s1_items WHERE r_reruns.est <> 'pool' AND n_ev > 1
                GROUP BY ikey, sample_id
                UNION ALL
                -- alone in its event: what the GROUP BY makes of one row
                SELECT ikey, sample_id, sample_date, ev_key, has_time, n_ev,
                       r_reruns AS rule, display_factor AS dfac,
                       CAST(1 AS BIGINT) AS n_items, CAST(NULL AS VARCHAR) AS rerun_by,
                       v AS win,
                       CASE WHEN r_reruns.est NOT IN ('select', 'refuse')
                            THEN [struct_pack(inp := inp, v := v)] END AS items,
                       struct_pack(depth := v.depth, units := v.units,
                                   std_units := v.std_units) AS attrs
                FROM s1_items WHERE r_reruns.est <> 'pool' AND n_ev = 1
            )
        )
    )
),
-- Stage 2: same_day - the samples of one date.
s2_items AS (
    SELECT s1.ikey, s1.sample_date, s1.ev_key, s1.has_time, s1.n_ev, s1.v, i.r_same_day,
           i.display_factor, i.event_grain,
           rank_across_key(s1.v.detected, s1.v.result, s1.v.lim, s1.ev_key,
                           s1.sample_id, i.across_nd_limit) AS k,
           nd_input_value(s1.v.detected, s1.v.result, s1.v.lim, s1.v.computed, i.r_same_day.f) AS inp
    FROM s1 JOIN ident i ON i.ikey = s1.ikey
),
s2 AS (
    SELECT ikey, sample_date, ev_key, has_time, FALSE AS untimed_tie, v
    FROM s2_items WHERE r_same_day.est = 'pool'
    UNION ALL
    SELECT ikey, sample_date, ev_key, has_time,
           -- samples of one date ordered only by the tie-break: no time of day
           n_items > 1 AND NOT has_time AND grain = 'timestamp' AS untimed_tie,
           nd_stage_value(rule.est, rule.f, rule.alln, rule.det, dfac, n_items,
                          win, inps, lims, dets, attrs) AS v
    FROM (
        SELECT *,
               list_transform(known, lambda item: item.inp) AS inps,
               list_transform(known, lambda item: item.v.lim) AS lims,
               list_transform(known, lambda item: item.v.detected) AS dets
        FROM (
            SELECT *, nd_items_known(items) AS known
            FROM (
                SELECT ikey, ev_key, any_value(sample_date) AS sample_date,
                       bool_or(has_time) AS has_time, any_value(event_grain) AS grain,
                       any_value(r_same_day) AS rule, any_value(display_factor) AS dfac,
                       count(*) AS n_items,
                       arg_min(v, k) AS win,
                       list(struct_pack(inp := inp, v := v))
                           FILTER (WHERE r_same_day.est NOT IN ('select', 'refuse')) AS items,
                       struct_pack(depth := min(v.depth), units := min(v.units),
                                   std_units := min(v.std_units)) AS attrs
                FROM s2_items WHERE r_same_day.est <> 'pool' AND n_ev > 1
                GROUP BY ikey, ev_key
                UNION ALL
                -- alone in its event: what the GROUP BY makes of one row
                SELECT ikey, ev_key, sample_date, has_time, event_grain AS grain,
                       r_same_day AS rule, display_factor AS dfac,
                       CAST(1 AS BIGINT) AS n_items, v AS win,
                       CASE WHEN r_same_day.est NOT IN ('select', 'refuse')
                            THEN [struct_pack(inp := inp, v := v)] END AS items,
                       struct_pack(depth := v.depth, units := v.units,
                                   std_units := v.std_units) AS attrs
                FROM s2_items WHERE r_same_day.est <> 'pool' AND n_ev = 1
            )
        )
    )
),
-- Stage 3: events.
ev AS (
    SELECT s2.ikey, s2.sample_date, s2.ev_key, s2.has_time, s2.untimed_tie, s2.v, params.ea,
           i.r_over_time,
           nd_input_value(s2.v.detected, s2.v.result, s2.v.lim, s2.v.computed, i.r_over_time.f) AS inp,
           rank_across_key(s2.v.detected, s2.v.result, s2.v.lim, s2.ev_key,
                           s2.v.sample_id, i.across_nd_limit) AS k
    FROM s2 JOIN ident i ON i.ikey = s2.ikey CROSS JOIN params
),
ev_agg AS (
    SELECT ikey,
           any_value(ea) AS ea,
           count(*) AS n_events,
           count(*) FILTER (WHERE v.detected) AS n_detects,
           min(sample_date) AS first_sample_date,
           max(sample_date) AS last_sample_date,
           -- the latest event (an unknown status last); ties by rank_across
           arg_min(struct_pack(v := v, sample_date := sample_date,
                               sample_datetime := CASE WHEN has_time THEN ev_key END,
                               tie := untimed_tie),
                   struct_pack(u := v.detected IS NULL,
                               t := COALESCE(-epoch(ev_key), CAST('Infinity' AS DOUBLE)),
                               k := k)) AS w_recent,
           -- the first event by rank_across
           arg_min(struct_pack(v := v, sample_date := sample_date,
                               sample_datetime := CASE WHEN has_time THEN ev_key END,
                               tie := untimed_tie), k) AS w_max,
           count(*) FILTER (WHERE ea = 'statistic') AS n_items,
           list(struct_pack(inp := inp, v := v))
               FILTER (WHERE ea = 'statistic' AND r_over_time.est NOT IN ('select', 'refuse')) AS items,
           struct_pack(depth := min(v.depth), units := min(v.units),
                                   std_units := min(v.std_units)) AS attrs
    FROM ev
    GROUP BY ikey
),
ev_parts AS (
    SELECT *,
           CASE WHEN ea = 'most_recent' THEN w_recent ELSE w_max END AS w,
           list_transform(known, lambda item: item.inp) AS inps,
           list_transform(known, lambda item: item.v.lim) AS lims,
           list_transform(known, lambda item: item.v.detected) AS dets
    FROM (SELECT *, nd_items_known(items) AS known FROM ev_agg)
),
-- Kaplan-Meier over events, for a km_* over_time rule: the left-censored
-- product-limit estimate, as the QGIS plugin's censored.js computes it,
--   F(x_k) = P(X <= x_k) = product over detected x_j > x_k of (1 - d_j/n_j)
-- with d_k the detects AT x_k and n_k the results known to be <= x_k
-- (detects at or below it; non-detects with a limit at or below it). Below
-- the lowest detect the curve has no steps: f0 is the share it puts there,
-- and the policy's km_floor places it.
km_stream AS (
    SELECT ikey, v.detected AS detected,
           CASE WHEN v.detected THEN v.result ELSE v.lim END AS k
    FROM ev
    WHERE ea = 'statistic' AND r_over_time.est IN ('km_mean', 'km_median')
      AND v.detected IS NOT NULL
      AND (CASE WHEN v.detected THEN v.result ELSE v.lim END) IS NOT NULL
),
km_at AS (
    SELECT ikey, detected, k,
           count(*) OVER (PARTITION BY ikey ORDER BY k
                          RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS n,
           count(*) FILTER (WHERE detected) OVER (PARTITION BY ikey ORDER BY k
                          RANGE BETWEEN CURRENT ROW AND CURRENT ROW) AS d
    FROM km_stream
),
km_steps AS (
    SELECT DISTINCT ikey, k AS x, CAST(n AS DOUBLE) AS n, CAST(d AS DOUBLE) AS d
    FROM km_at WHERE detected
),
km_cum AS (
    SELECT *,
           COALESCE(product(1.0 - d / n) OVER (PARTITION BY ikey ORDER BY x DESC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 1.0) AS f_at,
           lead(x) OVER (PARTITION BY ikey ORDER BY x) AS x_next
    FROM km_steps
),
km_stat AS (
    SELECT ikey,
           sum(x * f_at * d / n) AS mass,
           min(x) AS lowest,
           product(1.0 - d / n) AS f0,
           -- the median: the smallest detect with F >= 0.5; where F is 0.5
           -- exactly there, the midpoint with the next detect up
           arg_min(CASE WHEN abs(f_at - 0.5) < 1e-9 AND x_next IS NOT NULL
                        THEN (x + x_next) / 2 ELSE x END, x)
               FILTER (WHERE f_at >= 0.5 - 1e-9) AS q50
    FROM km_cum GROUP BY ikey
),
resolved AS (
    SELECT i.*, uv.n_units, uv.conflict AS units_conflict, a.ea, a.n_events, a.n_detects, a.first_sample_date, a.last_sample_date,
           a.w.v AS w, a.w.sample_date AS w_date, a.w.sample_datetime AS w_datetime,
           a.w.tie AS w_tie,
           nd_stage_value(i.r_over_time.est, i.r_over_time.f, i.r_over_time.alln,
                          i.r_over_time.det, i.display_factor, a.n_items, a.w_max.v,
                          a.inps, a.lims, a.dets, a.attrs) AS s,
           len(a.dets) AS n_known,
           COALESCE(list_aggregate(a.dets, 'bool_or'), FALSE) AS any_detected,
           list_aggregate(a.dets, 'bool_and') AS all_detected,
           nd_rep_limit(i.r_over_time.alln, a.lims) AS km_lim,
           k.mass, k.lowest, k.f0, k.q50,
           a.ea = 'statistic' AND i.r_over_time.est NOT IN ('select', 'refuse') AS is_stat,
           a.ea = 'statistic' AND i.r_over_time.est IN ('km_mean', 'km_median') AS is_km,
           CASE i.r_over_time.fl
               WHEN 'lowest_detect' THEN k.lowest
               WHEN 'half_lowest_detect' THEN 0.5 * k.lowest
               ELSE 0.0
           END AS km_floor_value
    FROM ev_parts a
    JOIN ident i ON i.ikey = a.ikey
    JOIN unit_vars uv ON uv.vkey = i.vkey
    LEFT JOIN km_stat k ON k.ikey = a.ikey
)
SELECT
    r.location_id, r.x, r.y, r.matrix,
    r.top_depth, r.bottom_depth, r.position,
    CASE WHEN r.is_stat THEN r.s.depth ELSE r.w.depth END AS depth,
    r.fraction, r.meas_basis,
    r.analyte, r.method_class,
    r.anl_sm_mb, r.analyte_group, r.cas_number,
    CASE
        -- Kaplan-Meier: no detect -> the all_nd rule; else the estimate
        WHEN r.is_km AND r.n_known = 0 THEN NULL
        WHEN r.is_km AND NOT r.any_detected THEN
            COALESCE(r.r_over_time.f, r.display_factor) * r.km_lim
        WHEN r.is_km AND r.r_over_time.est = 'km_mean' THEN r.mass + r.f0 * r.km_floor_value
        WHEN r.is_km THEN CASE WHEN r.f0 >= 0.5 - 1e-9 THEN r.km_floor_value ELSE r.q50 END
        WHEN r.is_stat THEN r.s.result
        -- a selection: a non-detect is valued at display_factor x its limit
        WHEN r.w.detected IS NULL THEN NULL
        WHEN r.w.detected OR r.w.computed THEN r.w.result
        ELSE COALESCE(params.dfac_call, r.display_factor) * r.w.lim
    END AS result,
    CASE
        WHEN r.is_km THEN CASE WHEN r.n_known = 0 THEN NULL
                               WHEN r.r_over_time.det = 'all' THEN r.all_detected
                               ELSE r.any_detected END
        WHEN r.is_stat THEN r.s.detected
        ELSE r.w.detected
    END AS detected,
    CASE WHEN r.is_stat THEN r.s.units ELSE r.w.units END AS units,
    CASE WHEN r.is_stat THEN r.s.std_units ELSE r.w.std_units END AS std_units,
    r.n_units,
    r.units_conflict,
    CASE WHEN r.is_stat THEN r.last_sample_date ELSE r.w_date END AS rep_sample_date,
    CASE WHEN NOT r.is_stat THEN r.w_datetime END AS rep_sample_datetime,
    r.first_sample_date, r.last_sample_date,
    r.n_events, r.n_detects,
    CASE
        WHEN r.is_km THEN r.km_lim
        WHEN r.is_stat THEN r.s.lim
        ELSE r.w.lim
    END AS detection_limit,
    r.n_unknown_detect,
    -- provenance
    CASE WHEN NOT r.is_stat THEN r.w.sample_id END AS sample_id,
    CASE WHEN NOT r.is_stat THEN r.w.method_code END AS method_code,
    CASE WHEN NOT r.is_stat THEN r.w.rerun_by END AS rerun_selected_by,
    CASE
        WHEN r.is_stat THEN NULL
        ELSE CASE WHEN r.ea = 'most_recent' THEN 'most_recent, then rank_across' ELSE 'rank_across' END
             || CASE WHEN r.w_tie THEN '; same date, no time of day: tie-break by rank_across'
                     ELSE '' END
    END AS selected_by,
    CASE WHEN NOT r.is_stat THEN r.w.lim_source END AS limit_source,
    CASE WHEN NOT r.is_stat THEN r.w.lim_type END AS limit_type,
    CASE WHEN r.is_stat THEN r.r_over_time.est END AS stat_method,
    r.nd_policy,
    'reruns:' || r.r_reruns.est || ' > same_day:' || r.r_same_day.est || ' > '
        || CASE WHEN r.ea = 'statistic' THEN 'over_time:' || r.r_over_time.est ELSE r.ea END
        AS nd_rule,
    1 AS n_points,
    r.matrices,
    COALESCE(r.unit_source, 'as_reported') AS unit_source,
    CASE
        WHEN NOT r.is_km OR NOT r.any_detected THEN FALSE
        WHEN r.r_over_time.est = 'km_mean' THEN r.f0 > 1e-12
        ELSE r.f0 >= 0.5 - 1e-9
    END AS below_lowest_detect,
    -- the factor that drew a selected non-detect
    CASE WHEN NOT r.is_stat AND r.w.detected = FALSE AND NOT r.w.computed
         THEN COALESCE(params.dfac_call, r.display_factor) END AS display_factor,
    params.ignored AS filters_ignored
FROM resolved r CROSS JOIN params;

-- ============================================================================
-- rollup_points - One value per location, by a rule the caller names
-- ============================================================================
-- Shared rules D1: aggregates return one row per measurement point and never
-- collapse positions or depths on their own. A map that needs one symbol per
-- location rolls the points up here, and must say how - there is no default:
--
--   'max_over_points'  the highest point by rank_across (detects first, the
--                      higher value; among non-detects the limit, by the
--                      policy's direction): screening against a standard
--   'named_position'   one position (p_position) or depth interval
--                      (p_top_depth, p_bottom_depth); two matching points
--                      at one location refuse
--   'none'             every point its own row
--
-- n_points is how many points the location had; rollup names the rule. The
-- other parameters are aggregate_points', passed through (p_units,
-- p_method_class, p_matrices, p_locations, p_display_factor and
-- p_reportable_only included).

CREATE OR REPLACE MACRO rollup_points(
    p_analyte,
    p_rollup,
    p_position := NULL,
    p_top_depth := NULL,
    p_bottom_depth := NULL,
    p_event_agg := 'most_recent',
    p_matrix := NULL,
    p_fraction := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_sample_types := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_locations := NULL,
    p_display_factor := NULL,
    p_reportable_only := NULL
) AS TABLE
WITH params AS (
    SELECT CASE
        WHEN p_rollup IS NULL THEN error(
            'rollup_points: name a rollup (max_over_points, named_position or none); there is no default')
        WHEN p_rollup NOT IN ('max_over_points', 'named_position', 'none') THEN error(
            'rollup_points: unknown rollup ' || p_rollup)
        WHEN p_rollup = 'named_position' AND p_position IS NULL
             AND (p_top_depth IS NULL OR p_bottom_depth IS NULL) THEN error(
            'rollup_points: named_position needs p_position, or p_top_depth and p_bottom_depth')
        ELSE p_rollup
    END AS ru
),
pts AS (
    SELECT a.*, pol.across_nd_limit,
           CASE WHEN p_position IS NOT NULL THEN a.position IS NOT DISTINCT FROM p_position
                ELSE a.top_depth = p_top_depth AND a.bottom_depth = p_bottom_depth
           END AS named
    FROM aggregate_points(p_analyte, p_event_agg := p_event_agg, p_matrix := p_matrix,
                          p_fraction := p_fraction, p_start_date := p_start_date,
                          p_end_date := p_end_date, p_sample_types := p_sample_types,
                          p_nd_policy := p_nd_policy, p_nd_policy_version := p_nd_policy_version,
                          p_units := p_units, p_method_class := p_method_class,
                          p_matrices := p_matrices, p_locations := p_locations,
                          p_display_factor := p_display_factor,
                          p_reportable_only := p_reportable_only) a
    LEFT JOIN v_nd_policies pol ON pol.name || '@' || pol.version = a.nd_policy
),
ranked AS (
    SELECT *,
           count(*) OVER loc AS n_at_location,
           count(*) FILTER (WHERE named) OVER loc AS n_named,
           row_number() OVER (loc ORDER BY rank_across_key(
               detected, result, detection_limit, COALESCE(rep_sample_datetime, rep_sample_date),
               sample_id, across_nd_limit)) AS rk
    FROM pts
    WINDOW loc AS (PARTITION BY location_id, matrix, fraction, meas_basis, analyte, method_class)
)
SELECT r.* EXCLUDE (across_nd_limit, named, n_at_location, n_named, rk, n_points),
       CASE WHEN params.ru = 'none' THEN 1 ELSE r.n_at_location END AS n_points,
       params.ru AS rollup
FROM ranked r CROSS JOIN params
WHERE CASE params.ru
    WHEN 'none' THEN TRUE
    WHEN 'max_over_points' THEN r.rk = 1
    ELSE r.named AND (CASE WHEN r.n_named > 1 THEN CAST(error(
        'rollup_points: ' || r.n_named || ' points at location ' || r.location_id
        || ' match the named position; name a depth interval or a narrower position') AS BOOLEAN)
        ELSE TRUE END)
END;

-- ============================================================================
-- point_series - One row per event of each measurement point
-- ============================================================================
-- A point's history, for a time-series plot: every sample of every
-- measurement point (location_id, matrix, top_depth, bottom_depth, position,
-- fraction, meas_basis, analyte, method_class), its re-runs resolved by the
-- policy's reruns rule exactly as aggregate_points resolves them (it is
-- aggregate_points, each sample its own point), events never combined.
-- Ordered by point, then event.
--
-- Each row: the point; the event (sample_id, sample_date, sample_datetime,
-- NULL where the source records no time); the value (result, detected,
-- detection_limit, units; a non-detect drawn at display_factor x its limit,
-- the policy's or p_display_factor); provenance (method_code,
-- rerun_selected_by, limit_source, limit_type, nd_policy, unit_source,
-- filters_ignored). Units are
-- checked per variable across the call, as aggregate_points checks them.

CREATE OR REPLACE MACRO point_series(
    p_analyte,
    p_locations := NULL,
    p_matrix := NULL,
    p_fraction := NULL,
    p_method_class := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_sample_types := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_display_factor := NULL,
    p_reportable_only := NULL
) AS TABLE
SELECT location_id, x, y, matrix, top_depth, bottom_depth, position, depth,
       fraction, meas_basis, analyte, method_class,
       sample_id, rep_sample_date AS sample_date, rep_sample_datetime AS sample_datetime,
       result, detected, detection_limit, units, std_units, n_units, units_conflict,
       n_unknown_detect, method_code, rerun_selected_by, limit_source, limit_type,
       nd_policy, nd_rule, unit_source, display_factor, filters_ignored
FROM aggregate_points(p_analyte, p_event_agg := 'most_recent', p_matrix := p_matrix,
                      p_fraction := p_fraction, p_start_date := p_start_date,
                      p_end_date := p_end_date, p_sample_types := p_sample_types,
                      p_nd_policy := p_nd_policy, p_nd_policy_version := p_nd_policy_version,
                      p_units := p_units, p_method_class := p_method_class,
                      p_locations := p_locations, p_display_factor := p_display_factor,
                      p_reportable_only := p_reportable_only, p_each_sample := TRUE)
ORDER BY location_id, matrix, top_depth, bottom_depth, position, fraction, meas_basis,
         method_class, sample_date, sample_datetime, sample_id;

-- ============================================================================
-- point_counts - What a points call would map, counted
-- ============================================================================
-- For a footer asked on every click ("N locations, M results would map"):
-- one row, over the rows aggregate_points reads for the same scope
-- (point_scope) and with its unit refusal (units_by_variable), but none of
-- its policy stages, which never drop a point:
--   n_locations  locations with a point (aggregate_points' locations)
--   n_points     measurement points (aggregate_points' rows)
--   n_samples    a point's samples, each once (point_series' rows)
--   n_results    the results those are drawn from
-- The non-detect policy changes none of these, so it takes no policy; a
-- policy aggregate_points would refuse is not checked here.

CREATE OR REPLACE MACRO point_counts(
    p_analyte,
    p_matrix := NULL,
    p_fraction := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_sample_types := NULL,
    p_units := 'refuse',
    p_method_class := NULL,
    p_matrices := NULL,
    p_locations := NULL,
    p_reportable_only := NULL
) AS TABLE
WITH params AS (
    SELECT CASE
        WHEN p_units IN ('refuse', 'flag') THEN p_units
        ELSE error('point_counts: p_units must be refuse or flag, not '
                   || COALESCE(CAST(p_units AS VARCHAR), 'NULL')
                   || ' (units are converted at load, never here)')
    END AS um
),
pc_rows AS (
    SELECT s.location_id, s.sample_id, s.analyte, s.matrix, s.fraction, s.meas_basis,
           s.method_class, s.units, s.unit_source, params.um,
           hash(s.location_id, s.matrix, s.top_depth, s.bottom_depth, s.position,
                s.fraction, s.meas_basis, s.analyte, s.method_class) AS ikey,
           hash(s.matrix, s.fraction, s.meas_basis, s.analyte, s.method_class) AS vkey
    FROM point_scope('point_counts', p_analyte, p_matrix, p_fraction, p_start_date,
                     p_end_date, p_sample_types, p_method_class, p_matrices, p_locations,
                     p_reportable_only) s
    CROSS JOIN params
)
SELECT count(DISTINCT r.location_id) AS n_locations,
       count(DISTINCT r.ikey) AS n_points,
       count(DISTINCT (r.ikey, r.sample_id)) AS n_samples,
       count(*) AS n_results
FROM pc_rows r
JOIN units_by_variable('pc_rows', 'point_counts') u USING (vkey);
