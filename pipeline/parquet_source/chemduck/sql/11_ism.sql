-- ChemDuck Schema: ISM records, decisions and result uncertainty
--
-- docs/ism-and-uncertainty-spec.md. Everything here reads the ISM tables of
-- 01_tables.sql (location_geometries, field_sample_design,
-- sample_increments, sample_preparations, sample_subsamples,
-- result_aliquots) and uncertainty_components. A dataset with none of them
-- gets empty views, and no engine on the hot path reads this file: the
-- engines read only samples.support and result_aliquots
-- (08_aggregate_points.sql).
--
-- Thresholds are the project's (chemduck_settings, written at load from the
-- mapping's ism_settings), with these defaults:
--   ism_min_increments              30    increments an ISM sample needs
--   ism_min_subsample_increments    30    scoops an ISM aliquot needs
--   ism_replicate_rsd_target        0.35  field-replicate RSD (a fraction)
--   ism_lab_replicate_rsd_target    0.20  laboratory-replicate RSD
--   ism_min_aliquot_mass_ratio      0.90  aliquot mass / its target
--   ism_max_oversize_fraction       0.10  oversize / (passing + oversize)
--   ism_lab_replicate_rate          0.10  laboratory replicate sets per ISM sample

CREATE OR REPLACE MACRO ism_setting(k, dflt) AS (
    COALESCE((SELECT TRY_CAST(value AS DOUBLE) FROM chemduck_settings WHERE key = k),
             CAST(dflt AS DOUBLE))
);

-- ============================================================================
-- v_result_subsamples - which aliquot each result came from (§4.7)
-- ============================================================================
-- One row per result of a sample that has any aliquot. A result belongs to
-- an aliquot when the samples are equal, the laboratory sample ids are equal
-- where both are known, and the result's analysis group (the project
-- mapping's analysis_groups, kept in ism_analysis_groups, 01_tables.sql) equals the
-- aliquot's; an aliquot with no analysis group matches every result of its
-- sample. match: 'one' (subsample_id that aliquot), 'none' or 'ambiguous'
-- (subsample_id NULL). Results of samples with no aliquot have no row.

CREATE OR REPLACE VIEW v_result_analysis_groups AS
SELECT r.result_id, r.sample_id, r.lab_sample_id,
       (SELECT g.analysis_group FROM ism_analysis_groups g
        WHERE regexp_full_match(
                  upper(trim(CASE g.match_column
                      WHEN 'method_class' THEN COALESCE(r.method_class, upper(trim(r.method_code)))
                      ELSE r.method_code END)),
                  g.pattern)
        ORDER BY g.priority LIMIT 1) AS analysis_group
FROM results r
WHERE r.sample_id IN (SELECT sample_id FROM sample_subsamples);

CREATE OR REPLACE VIEW v_result_subsamples AS
WITH cand AS (
    SELECT g.result_id, s.subsample_id
    FROM v_result_analysis_groups g
    JOIN sample_subsamples s
      ON s.sample_id = g.sample_id
     AND (g.lab_sample_id IS NULL OR s.lab_sample_id IS NULL OR g.lab_sample_id = s.lab_sample_id)
     AND (s.analysis_group IS NULL OR s.analysis_group = g.analysis_group)
)
SELECT g.result_id, g.analysis_group,
       CASE WHEN count(c.subsample_id) = 1 THEN any_value(c.subsample_id) END AS subsample_id,
       CASE count(c.subsample_id) WHEN 0 THEN 'none' WHEN 1 THEN 'one' ELSE 'ambiguous' END AS match
FROM v_result_analysis_groups g
LEFT JOIN cand c ON c.result_id = g.result_id
GROUP BY g.result_id, g.analysis_group;

-- ============================================================================
-- v_ism_sample_values - one value per composite/ISM sample and variable
-- ============================================================================
-- For the QC views: a sample's re-runs chosen among as aggregate_points'
-- reruns stage does (reportable, then test-type precedence, then
-- rank_reruns with the lower limit), every sample type, never a
-- laboratory replicate coded on its primary's sample. value: the detect,
-- or the detection limit of a non-detect (flagged by detected).

CREATE OR REPLACE VIEW v_ism_sample_values AS
SELECT * EXCLUDE (rk) FROM (
    SELECT v.sample_id, v.location_id, v.matrix, v.sample_date, v.top_depth, v.bottom_depth,
           v.position, support_code(v.support) AS support, v.replicate_set, v.replicate_no,
           v.sample_type, v.analyte, v.fraction, v.meas_basis, v.method_class,
           ra.particle_size_fraction, v.units,
           CASE WHEN v.detected THEN v.result ELSE v.detection_limit END AS value,
           v.detected, v.detection_limit, v.quantitation_limit, v.reporting_limit,
           row_number() OVER (
               PARTITION BY v.sample_id, v.analyte, v.fraction, v.meas_basis, v.method_class,
                            ra.particle_size_fraction
               ORDER BY CASE WHEN v.reportable THEN 0 WHEN v.reportable IS NULL THEN 1 ELSE 2 END,
                        COALESCE(v.test_rank, 0),
                        rank_reruns_key(v.detected, v.result, v.detection_limit, v.date_analyzed,
                                        v.result_id, 'lower')) AS rk
    FROM v_results_core v
    LEFT JOIN result_aliquots ra ON ra.result_id = v.result_id
    WHERE (v.replicate_set IS NOT NULL OR support_code(v.support) <> 'discrete')
      AND ra.lab_replicate_on_primary IS NOT TRUE
      AND v.detected IS NOT NULL
)
WHERE rk = 1;

-- ============================================================================
-- v_ism_replicate_qc - field-replicate precision, per set and variable (§6.4)
-- ============================================================================
-- rsd = sd / mean of the set's values (a non-detect at its limit, counted in
-- n_nondetect); target the project's ism_replicate_rsd_target. A set with a
-- non-detect is reported, its rsd marked by n_nondetect.

CREATE OR REPLACE VIEW v_ism_replicate_qc AS
SELECT replicate_set, any_value(location_id) AS location_id, matrix, analyte, fraction,
       meas_basis, method_class, particle_size_fraction, any_value(units) AS units,
       count(*) AS n, count(*) FILTER (WHERE NOT detected) AS n_nondetect,
       avg(value) AS mean, stddev_samp(value) AS sd,
       CASE WHEN avg(value) > 0 THEN stddev_samp(value) / avg(value) END AS rsd,
       ism_setting('ism_replicate_rsd_target', 0.35) AS rsd_target,
       CASE WHEN count(*) < 2 THEN NULL
            ELSE stddev_samp(value) / NULLIF(avg(value), 0) > ism_setting('ism_replicate_rsd_target', 0.35)
       END AS exceeds_target,
       min(sample_date) AS first_sample_date, max(sample_date) AS last_sample_date
FROM v_ism_sample_values
WHERE replicate_set IS NOT NULL
GROUP BY replicate_set, matrix, analyte, fraction, meas_basis, method_class, particle_size_fraction;

-- ============================================================================
-- v_ism_aliquot_values / v_ism_lab_replicate_qc - laboratory replicates (§6.4)
-- ============================================================================
-- One value per aliquot and variable, then per primary aliquot: the RSD of
-- the primary and its laboratory replicates, usable only where every one is
-- a detect at or above its quantitation limit (the reporting limit where
-- the source gives none); target ism_lab_replicate_rsd_target.
-- lab_to_field_rsd: this RSD over the primary sample's field-replicate RSD
-- for the same variable, which shows whether preparation or the field
-- dominates.

CREATE OR REPLACE VIEW v_ism_aliquot_values AS
SELECT * EXCLUDE (rk) FROM (
    SELECT ra.subsample_id, ss.prep_id, ss.prep_role, ss.parent_subsample_id,
           ss.sample_id AS aliquot_sample_id, sp.sample_id AS prep_sample_id,
           s.matrix, r.analyte, r.fraction, r.meas_basis,
           COALESCE(r.method_class, upper(trim(r.method_code))) AS method_class,
           ra.particle_size_fraction, r.units,
           CASE WHEN r.detected THEN r.result ELSE r.nd_limit END AS value, r.detected,
           r.detected AND r.result >= COALESCE(r.quantitation_limit, r.reporting_limit, 0) AS above_ql,
           row_number() OVER (
               PARTITION BY ra.subsample_id, r.analyte, r.fraction, r.meas_basis,
                            COALESCE(r.method_class, upper(trim(r.method_code)))
               ORDER BY CASE WHEN r.reportable THEN 0 WHEN r.reportable IS NULL THEN 1 ELSE 2 END,
                        COALESCE(r.test_rank, 0),
                        rank_reruns_key(r.detected, r.result, r.nd_limit, r.date_analyzed,
                                        r.result_id, 'lower')) AS rk
    FROM result_aliquots ra
    JOIN results r ON r.result_id = ra.result_id
    JOIN sample_subsamples ss ON ss.subsample_id = ra.subsample_id
    JOIN sample_preparations sp ON sp.prep_id = ss.prep_id
    JOIN samples s ON s.sample_id = sp.sample_id
)
WHERE rk = 1;

CREATE OR REPLACE VIEW v_ism_lab_replicate_qc AS
WITH members AS (
    SELECT CASE WHEN prep_role = 'primary' THEN subsample_id ELSE parent_subsample_id END
               AS primary_subsample_id, *
    FROM v_ism_aliquot_values
    WHERE prep_role = 'primary' OR (prep_role = 'lab_replicate' AND parent_subsample_id IS NOT NULL)
),
sets AS (
    SELECT primary_subsample_id, any_value(prep_id) AS prep_id,
           any_value(prep_sample_id) AS sample_id, matrix, analyte, fraction, meas_basis,
           method_class, particle_size_fraction, any_value(units) AS units,
           count(*) AS n, count(*) FILTER (WHERE prep_role = 'lab_replicate') AS n_lab_replicates,
           bool_and(above_ql) AS usable,
           avg(value) AS mean, stddev_samp(value) AS sd
    FROM members
    GROUP BY primary_subsample_id, matrix, analyte, fraction, meas_basis, method_class,
             particle_size_fraction
    HAVING count(*) FILTER (WHERE prep_role = 'lab_replicate') > 0
)
SELECT s.*,
       CASE WHEN s.mean > 0 THEN s.sd / s.mean END AS rsd,
       ism_setting('ism_lab_replicate_rsd_target', 0.20) AS rsd_target,
       CASE WHEN s.usable AND s.n > 1 AND s.mean > 0
            THEN s.sd / s.mean > ism_setting('ism_lab_replicate_rsd_target', 0.20) END AS exceeds_target,
       f.rsd AS field_rsd,
       CASE WHEN s.usable AND s.mean > 0 AND f.rsd > 0 THEN (s.sd / s.mean) / f.rsd END AS lab_to_field_rsd
FROM sets s
LEFT JOIN samples smp ON smp.sample_id = s.sample_id
LEFT JOIN v_ism_replicate_qc f
       ON f.replicate_set = smp.replicate_set AND f.analyte = s.analyte
      AND f.matrix = s.matrix AND f.method_class IS NOT DISTINCT FROM s.method_class
      AND f.fraction IS NOT DISTINCT FROM s.fraction
      AND f.meas_basis IS NOT DISTINCT FROM s.meas_basis
      AND f.particle_size_fraction IS NOT DISTINCT FROM s.particle_size_fraction;

-- ============================================================================
-- v_ism_increment_qc - increments per sample (§6.4)
-- ============================================================================
-- Every composite or ISM sample, and every sample with increments.
-- n_increments counts the increments recorded; n_collected those not
-- planned, refused or relocated... as recorded (status NULL counts as
-- collected); increment_count the best count there is - recorded, else the
-- field record's, else the design's. short_depth: an increment whose
-- bottom is above the sample's (same depth unit). only_planned_coords: no
-- increment has as-sampled coordinates.

CREATE OR REPLACE VIEW v_ism_increment_qc AS
WITH inc AS (
    SELECT i.sample_id,
           count(*) AS n_increments,
           count(*) FILTER (WHERE i.status IS NULL OR i.status IN ('collected', 'relocated')) AS n_collected,
           count(*) FILTER (WHERE i.status = 'planned') AS n_planned_only,
           count(*) FILTER (WHERE i.status = 'refused') AS n_refused,
           count(*) FILTER (WHERE i.status = 'relocated') AS n_relocated,
           count(*) FILTER (WHERE i.bottom_depth IS NOT NULL AND s.bottom_depth IS NOT NULL
                            AND COALESCE(lower(i.depth_units), '') = COALESCE(lower(s.depth_units), '')
                            AND i.bottom_depth < s.bottom_depth - 1e-9) AS n_short_depth,
           count(*) FILTER (WHERE i.recovery_pct < d.min_core_recovery_pct) AS n_low_recovery,
           count(*) FILTER (WHERE i.inside_area = FALSE) AS n_outside_area,
           count(*) FILTER (WHERE i.x IS NOT NULL) AS n_with_coords,
           count(*) FILTER (WHERE i.x IS NOT NULL AND COALESCE(i.coord_method, '') <> 'planned') AS n_as_sampled,
           count(DISTINCT i.hole_id) AS n_holes
    FROM sample_increments i
    JOIN samples s ON s.sample_id = i.sample_id
    LEFT JOIN field_sample_design d ON d.sample_id = i.sample_id
    GROUP BY i.sample_id
)
SELECT s.sample_id, s.location_id, support_code(s.support) AS support, s.replicate_set,
       d.design_increments, d.reported_increments,
       COALESCE(inc.n_increments, 0) AS n_increments,
       COALESCE(inc.n_collected, 0) AS n_collected,
       COALESCE(inc.n_planned_only, 0) AS n_planned_only,
       COALESCE(inc.n_refused, 0) AS n_refused,
       COALESCE(inc.n_relocated, 0) AS n_relocated,
       COALESCE(inc.n_short_depth, 0) AS n_short_depth,
       COALESCE(inc.n_low_recovery, 0) AS n_low_recovery,
       COALESCE(inc.n_outside_area, 0) AS n_outside_area,
       COALESCE(inc.n_holes, 0) AS n_holes,
       COALESCE(NULLIF(inc.n_collected, 0), d.reported_increments, d.design_increments) AS increment_count,
       inc.n_with_coords > 0 AND inc.n_as_sampled = 0 AS only_planned_coords
FROM samples s
LEFT JOIN inc ON inc.sample_id = s.sample_id
LEFT JOIN field_sample_design d ON d.sample_id = s.sample_id
-- a laboratory replicate is an aliquot: its field sample's increments are its own
WHERE (support_code(s.support) <> 'discrete' AND upper(trim(COALESCE(s.sample_type, 'N'))) <> 'LR')
   OR inc.sample_id IS NOT NULL;

-- ============================================================================
-- v_ism_prep_qc - preparation and aliquot checks (§6.4)
-- ============================================================================
-- One row per aliquot, with its preparation and field sample. Each check is
-- a column (NULL: not checkable) and flags lists the ones that failed:
--   field_mass_out_of_range    received wet mass outside the plan's range
--   oversize_unweighed         a sieve, but no oversize mass
--   oversize_fraction_high     oversize / (passing + oversize) above the limit
--   subsample_no_method        an ISM aliquot with no splitting method
--   subsample_non_incremental  an ISM aliquot split by grab or cone and quarter
--   subsample_few_increments   fewer subsampling increments than the minimum
--   subsample_under_target     aliquot mass under the ratio of its target
--   subsample_mass_unparsed    a mass the source gave that would not parse
--   replicates_prepared_differently  the field sample's replicate set was
--                              sieved, milled or split differently

CREATE OR REPLACE VIEW v_ism_prep_qc AS
WITH base AS (
    SELECT ss.subsample_id, ss.prep_id, ss.sample_id AS aliquot_sample_id, ss.prep_role,
           ss.lab_replicate_no, ss.analysis_group,
           p.sample_id, s.replicate_set, support_code(s.support) AS support,
           p.lab_name, p.received_wet_mass_g, d.target_mass_min_g, d.target_mass_max_g,
           p.sieve_opening_mm, p.particle_size_fraction, p.milling,
           p.dry_mass_passing_g, p.oversize_mass_g,
           ss.splitting_method, ss.splitting_method_source, ss.n_subsample_increments,
           ss.mass_g, ss.target_mass_g, ss.mass_reported,
           CASE WHEN ss.mass_g > 0 AND ss.target_mass_g > 0 THEN ss.mass_g / ss.target_mass_g END AS mass_ratio,
           CASE WHEN p.oversize_mass_g IS NOT NULL AND p.dry_mass_passing_g IS NOT NULL
                     AND p.oversize_mass_g + p.dry_mass_passing_g > 0
                THEN p.oversize_mass_g / (p.oversize_mass_g + p.dry_mass_passing_g) END AS oversize_fraction
    FROM sample_subsamples ss
    JOIN sample_preparations p ON p.prep_id = ss.prep_id
    JOIN samples s ON s.sample_id = p.sample_id
    LEFT JOIN field_sample_design d ON d.sample_id = p.sample_id
),
-- within one analysis group and particle-size fraction, known values only:
-- a value one member lacks is not a difference
set_prep AS (
    SELECT replicate_set, analysis_group, particle_size_fraction,
           count(DISTINCT sieve_opening_mm) > 1
           OR count(DISTINCT milling) > 1
           OR count(DISTINCT splitting_method) > 1 AS differs
    FROM base
    WHERE replicate_set IS NOT NULL AND prep_role IS DISTINCT FROM 'lab_replicate'
    GROUP BY replicate_set, analysis_group, particle_size_fraction
),
checks AS (
    SELECT b.*,
           CASE WHEN b.received_wet_mass_g IS NOT NULL
                     AND (b.target_mass_min_g IS NOT NULL OR b.target_mass_max_g IS NOT NULL)
                THEN b.received_wet_mass_g < COALESCE(b.target_mass_min_g, -1e300)
                     OR b.received_wet_mass_g > COALESCE(b.target_mass_max_g, 1e300)
           END AS field_mass_out_of_range,
           b.sieve_opening_mm IS NOT NULL AND b.oversize_mass_g IS NULL AS oversize_unweighed,
           b.oversize_fraction > ism_setting('ism_max_oversize_fraction', 0.10) AS oversize_fraction_high,
           CASE WHEN b.support = 'ism' THEN b.splitting_method IS NULL END AS subsample_no_method,
           CASE WHEN b.support = 'ism' THEN b.splitting_method IN ('grab', 'cone_and_quarter') END
               AS subsample_non_incremental,
           b.n_subsample_increments < ism_setting('ism_min_subsample_increments', 30)
               AS subsample_few_increments,
           b.mass_ratio < ism_setting('ism_min_aliquot_mass_ratio', 0.90) AS subsample_under_target,
           b.mass_reported IS NOT NULL AND b.mass_g IS NULL AS subsample_mass_unparsed,
           sp.differs AS replicates_prepared_differently
    FROM base b
    LEFT JOIN set_prep sp ON sp.replicate_set = b.replicate_set
          AND sp.analysis_group IS NOT DISTINCT FROM b.analysis_group
          AND sp.particle_size_fraction IS NOT DISTINCT FROM b.particle_size_fraction
)
SELECT *,
       list_filter([
           CASE WHEN field_mass_out_of_range THEN 'field_mass_out_of_range' END,
           CASE WHEN oversize_unweighed THEN 'oversize_unweighed' END,
           CASE WHEN oversize_fraction_high THEN 'oversize_fraction_high' END,
           CASE WHEN subsample_no_method THEN 'subsample_no_method' END,
           CASE WHEN subsample_non_incremental THEN 'subsample_non_incremental' END,
           CASE WHEN subsample_few_increments THEN 'subsample_few_increments' END,
           CASE WHEN subsample_under_target THEN 'subsample_under_target' END,
           CASE WHEN subsample_mass_unparsed THEN 'subsample_mass_unparsed' END,
           CASE WHEN replicates_prepared_differently THEN 'replicates_prepared_differently' END],
           lambda f: f IS NOT NULL) AS flags
FROM checks;

-- ============================================================================
-- v_ism_checks - the load-report checks (category 'ism'; §5.6)
-- ============================================================================
-- One row per check: how many it found. The loader copies these into
-- load_report at every load; a consumer can read them live.

CREATE OR REPLACE VIEW v_ism_checks AS
SELECT 'ism_sample_few_increments' AS item, count(*) AS n,
       'ISM samples with fewer than ' || printf('%g', ism_setting('ism_min_increments', 30))
       || ' increments (recorded, else reported, else designed)' AS detail
FROM v_ism_increment_qc WHERE support = 'ism' AND increment_count < ism_setting('ism_min_increments', 30)
UNION ALL
SELECT 'ism_sample_no_increments', count(*), 'ISM samples with no increment recorded'
FROM v_ism_increment_qc WHERE support = 'ism' AND n_increments = 0
UNION ALL
SELECT 'increment_short_depth', COALESCE(sum(n_short_depth), 0), 'increments that did not reach their sample''s bottom depth'
FROM v_ism_increment_qc
UNION ALL
SELECT 'increment_low_recovery', COALESCE(sum(n_low_recovery), 0), 'increments below the design''s minimum recovery'
FROM v_ism_increment_qc
UNION ALL
SELECT 'increment_outside_area', COALESCE(sum(n_outside_area), 0), 'increments outside their DU/SU polygon'
FROM v_ism_increment_qc
UNION ALL
SELECT 'increment_planned_coords', count(*), 'samples whose increments have only planned coordinates'
FROM v_ism_increment_qc WHERE only_planned_coords
UNION ALL
SELECT 'replicate_set_small', count(*), 'replicate sets with fewer than 3 samples'
FROM (SELECT replicate_set FROM samples WHERE replicate_set IS NOT NULL
      GROUP BY 1 HAVING count(*) < 3)
UNION ALL
SELECT 'area_location_no_geometry', count(*), 'area locations with no polygon or no area'
FROM locations l LEFT JOIN location_geometries g ON g.location_id = l.location_id
WHERE l.spatial_support = 'area' AND (g.location_id IS NULL OR g.area IS NULL)
UNION ALL
SELECT 'composite_no_preparation', count(*), 'composite or ISM samples with no preparation record'
FROM samples s WHERE support_code(s.support) <> 'discrete'
  AND upper(trim(COALESCE(s.sample_type, 'N'))) NOT IN ('LR')
  AND NOT EXISTS (SELECT 1 FROM sample_preparations p WHERE p.sample_id = s.sample_id)
  AND EXISTS (SELECT 1 FROM sample_preparations)
UNION ALL
SELECT 'field_mass_out_of_range', count(DISTINCT prep_id), 'preparations whose received wet mass is outside the design range'
FROM v_ism_prep_qc WHERE field_mass_out_of_range
UNION ALL
SELECT 'oversize_unweighed', count(DISTINCT prep_id), 'preparations with a sieve but no oversize mass'
FROM v_ism_prep_qc WHERE oversize_unweighed
UNION ALL
SELECT 'oversize_fraction_high', count(DISTINCT prep_id),
       'oversize above ' || printf('%g', ism_setting('ism_max_oversize_fraction', 0.10)) || ' of the dry mass'
FROM v_ism_prep_qc WHERE oversize_fraction_high
UNION ALL
SELECT 'subsample_no_method', count(*), 'ISM aliquots with no splitting method'
FROM v_ism_prep_qc WHERE subsample_no_method
UNION ALL
SELECT 'subsample_non_incremental', count(*), 'ISM aliquots split by grab or cone and quarter'
FROM v_ism_prep_qc WHERE subsample_non_incremental
UNION ALL
SELECT 'subsample_few_increments', count(*),
       'aliquots with fewer than ' || printf('%g', ism_setting('ism_min_subsample_increments', 30)) || ' subsampling increments'
FROM v_ism_prep_qc WHERE subsample_few_increments
UNION ALL
SELECT 'subsample_under_target', count(*),
       'aliquots under ' || printf('%g', ism_setting('ism_min_aliquot_mass_ratio', 0.90)) || ' of their target mass'
FROM v_ism_prep_qc WHERE subsample_under_target
UNION ALL
SELECT 'subsample_mass_unparsed', count(*), 'aliquot masses that would not parse or convert'
FROM v_ism_prep_qc WHERE subsample_mass_unparsed
UNION ALL
SELECT 'replicates_prepared_differently', count(DISTINCT replicate_set),
       'replicate sets whose members differ in sieve, milling or splitting method'
FROM v_ism_prep_qc WHERE replicates_prepared_differently
UNION ALL
SELECT 'lab_replicate_rate', count(DISTINCT primary_subsample_id),
       'primary aliquots with laboratory replicates, for '
       || (SELECT count(*) FROM samples WHERE support_code(support) = 'ism'
                                          AND upper(trim(COALESCE(sample_type, 'N'))) <> 'LR')
       || ' ISM samples, the plan asks for ' || printf('%g', ism_setting('ism_lab_replicate_rate', 0.10)) || ' a sample'
FROM v_ism_lab_replicate_qc
UNION ALL
SELECT CASE match WHEN 'none' THEN 'result_no_subsample' ELSE 'result_many_subsamples' END,
       count(*), 'results of samples with aliquots that match ' || match || ' aliquot(s)'
FROM v_result_subsamples WHERE match <> 'one' GROUP BY match;

-- ============================================================================
-- ism_decisions - one row per DU (or SU) and variable (§6.4, §8)
-- ============================================================================
-- The ISM points of one analyte (p_supports fixed at ['ism'], so a decision
-- is never made on a discrete or composite value), each from its latest
-- replicate set: the set's mean, SD, RSD and 95% UCL against the action
-- level, the screening_levels row named p_screening for the analyte and the
-- point's matrix (in the point's units, or refused). A sample in no
-- replicate set is a set of one.
--
-- The headline UCL is the policy's replicates rule where it names a UCL,
-- else ucl95_auto; non-detects enter at the rule's nd_factor, else the
-- policy's display_factor, times their limit. The bracket (§8): every
-- combination of the policy's nd_policy_brackets alternatives for the
-- replicates stage ('nd_factor', 'ucl'), the headline's included. ucl_low /
-- ucl_high the range, bracket_low_by / bracket_high_by the alternatives
-- that gave them, decision_robust TRUE when every alternative gives the
-- same exceeds / passes. decision: 'exceeds' (UCL above the action level;
-- below it for a '<' level), 'passes', 'unreplicated' (a set of one: never
-- passing), or NULL (no action level).
--
-- A DU tiled into SUs (locations.parent_location_id) gets a row of its own,
-- level 'area_weighted': the area-weighted mean of its SUs' means (each
-- SU's latest set, headline factor), decision 'no_ucl': a DU's UCL from its
-- SUs is not defined (open decision 2). Refused when an SU has no area or
-- no value.
--
-- prep_flags: the preparation checks (v_ism_prep_qc) that failed for any
-- member. The other parameters are sample_results'.

CREATE OR REPLACE MACRO ism_decisions(
    p_analyte,
    p_screening,
    p_matrix := NULL,
    p_fraction := NULL,
    p_method_class := NULL,
    p_start_date := NULL,
    p_end_date := NULL,
    p_locations := NULL,
    p_nd_policy := NULL,
    p_nd_policy_version := NULL,
    p_units := 'refuse',
    p_particle_fractions := NULL,
    p_reportable_only := NULL
) AS TABLE
WITH vals AS (
    SELECT s.*, COALESCE(s.replicate_set, s.sample_id) AS set_key
    FROM sample_results([p_analyte], p_matrix := p_matrix, p_fraction := p_fraction,
                        p_start_date := p_start_date, p_end_date := p_end_date,
                        p_locations := p_locations, p_nd_policy := p_nd_policy,
                        p_nd_policy_version := p_nd_policy_version, p_units := p_units,
                        p_method_class := p_method_class, p_reportable_only := p_reportable_only,
                        p_supports := ['ism'], p_particle_fractions := p_particle_fractions) s
    WHERE s.detected IS NOT NULL
),
pol AS (
    SELECT p.name || '@' || p.version AS nd_policy,
           CASE WHEN p.r_replicates.est LIKE 'ucl95%' THEN p.r_replicates.est ELSE 'ucl95_auto' END AS head_m,
           COALESCE(p.r_replicates.f, p.display_factor) AS head_f,
           (SELECT list(TRY_CAST(a AS DOUBLE)) FROM nd_policy_brackets b, unnest(b.alternatives) AS t(a)
            WHERE b.policy = p.name AND b.version = p.version AND b.stage = 'replicates'
              AND b.parameter = 'nd_factor') AS alt_f,
           (SELECT list(a) FROM nd_policy_brackets b, unnest(b.alternatives) AS t(a)
            WHERE b.policy = p.name AND b.version = p.version AND b.stage = 'replicates'
              AND b.parameter = 'ucl') AS alt_m
    FROM v_nd_policies p
),
sets AS (
    SELECT v.location_id, v.matrix, v.top_depth, v.bottom_depth, v.position,
           v.particle_size_fraction, v.fraction, v.meas_basis, v.analyte, v.method_class,
           v.set_key, any_value(v.replicate_set) AS replicate_set,
           any_value(v.units) AS units, any_value(v.nd_policy) AS nd_policy,
           max(v.sample_date) AS sample_date, count(*) AS n_replicates,
           list(v.sample_id ORDER BY v.sample_id) AS sample_ids,
           list(struct_pack(r := v.result, d := v.detected, l := v.detection_limit)) AS members
    FROM vals v
    GROUP BY ALL
),
latest AS (
    SELECT * FROM sets
    QUALIFY row_number() OVER (
        PARTITION BY location_id, matrix, top_depth, bottom_depth, position,
                     particle_size_fraction, fraction, meas_basis, analyte, method_class
        ORDER BY sample_date DESC, set_key) = 1
),
alts AS (
    SELECT l.*, pol.head_f, pol.head_m, f.f, m.m
    FROM latest l
    JOIN pol ON pol.nd_policy = l.nd_policy
    CROSS JOIN LATERAL (SELECT unnest(list_distinct(list_prepend(pol.head_f, COALESCE(pol.alt_f, [])))) AS f) f
    CROSS JOIN LATERAL (SELECT unnest(list_distinct(list_prepend(pol.head_m, COALESCE(pol.alt_m, [])))) AS m) m
),
alt_stats AS (
    SELECT *, ucl95(m, n_known, mean, sd) AS ucl, ucl95_method(m, n_known, mean, sd) AS ucl_method
    FROM (
        SELECT a.*,
               len(vs) AS n_known, list_avg(vs) AS mean,
               CASE WHEN len(vs) > 1 THEN list_aggregate(vs, 'stddev_samp') END AS sd
        FROM (SELECT *, list_transform(members, lambda x: CASE WHEN x.d THEN x.r ELSE f * x.l END) AS vs
              FROM alts) a
    )
),
screened AS (
    SELECT a.*, sl.value AS screening_value, COALESCE(sl.comparison, '>') AS comparison,
           CASE
               WHEN sl.value IS NULL THEN NULL
               WHEN sl.units IS DISTINCT FROM a.units THEN CAST(error(
                   'ism_decisions: screening level ' || p_screening || ' for ' || a.analyte
                   || ' is in ' || COALESCE(sl.units, 'no unit') || ', the results in '
                   || COALESCE(a.units, 'no unit') || '. Load the level in the results'' unit') AS VARCHAR)
               WHEN a.ucl IS NULL THEN 'unreplicated'
               WHEN COALESCE(sl.comparison, '>') = '<' THEN CASE WHEN a.ucl < sl.value THEN 'exceeds' ELSE 'passes' END
               WHEN a.ucl > sl.value THEN 'exceeds'
               ELSE 'passes'
           END AS decision
    FROM alt_stats a
    LEFT JOIN screening_levels sl
           ON sl.name = p_screening AND sl.analyte = a.analyte AND sl.matrix = a.matrix
),
per_point AS (
    SELECT location_id, matrix, top_depth, bottom_depth, position, particle_size_fraction,
           fraction, meas_basis, analyte, method_class, set_key,
           any_value(replicate_set) AS replicate_set, any_value(units) AS units,
           any_value(nd_policy) AS nd_policy, any_value(sample_date) AS sample_date,
           any_value(sample_ids) AS sample_ids, any_value(n_replicates) AS n_replicates,
           any_value(screening_value) AS screening_value,
           -- the headline
           any_value(mean) FILTER (WHERE f = head_f AND m = head_m) AS rep_mean,
           any_value(sd) FILTER (WHERE f = head_f AND m = head_m) AS rep_sd,
           any_value(ucl) FILTER (WHERE f = head_f AND m = head_m) AS ucl,
           any_value(ucl_method) FILTER (WHERE f = head_f AND m = head_m) AS ucl_method,
           any_value(decision) FILTER (WHERE f = head_f AND m = head_m) AS decision,
           -- the bracket
           min(ucl) AS ucl_low, max(ucl) AS ucl_high,
           arg_min(struct_pack(nd_factor := f, ucl := m), ucl) AS bracket_low_by,
           arg_max(struct_pack(nd_factor := f, ucl := m), ucl) AS bracket_high_by,
           count(*) AS n_alternatives,
           CASE WHEN bool_or(decision IS NULL) OR bool_or(decision = 'unreplicated') THEN NULL
                ELSE count(DISTINCT decision) = 1 END AS decision_robust
    FROM screened
    GROUP BY location_id, matrix, top_depth, bottom_depth, position, particle_size_fraction,
             fraction, meas_basis, analyte, method_class, set_key
),
flags AS (
    SELECT q.sample_id, list_distinct(flatten(list(q.flags))) AS flags
    FROM v_ism_prep_qc q GROUP BY q.sample_id
),
with_flags AS (
    SELECT p.*,
           CASE WHEN p.rep_mean > 0 THEN p.rep_sd / p.rep_mean END AS rep_rsd,
           COALESCE((SELECT list_sort(list_distinct(flatten(list(fl.flags))))
                     FROM flags fl WHERE list_contains(p.sample_ids, fl.sample_id)),
                    CAST([] AS VARCHAR[])) AS prep_flags,
           l.parent_location_id
    FROM per_point p
    LEFT JOIN locations l ON l.location_id = p.location_id
),
du_sus AS (
    SELECT parent_location_id AS du, list(location_id ORDER BY location_id) AS sus
    FROM locations WHERE parent_location_id IS NOT NULL GROUP BY 1
),
du_groups AS (
    SELECT w.parent_location_id AS location_id, w.matrix, w.top_depth, w.bottom_depth,
           w.position, w.particle_size_fraction, w.fraction, w.meas_basis, w.analyte,
           w.method_class, any_value(w.units) AS units, any_value(w.nd_policy) AS nd_policy,
           max(w.sample_date) AS sample_date, any_value(w.screening_value) AS screening_value,
           any_value(d.sus) AS sus, list(DISTINCT w.location_id) AS sus_valued,
           string_agg(w.location_id, ', ' ORDER BY w.location_id)
               FILTER (WHERE g.area IS NULL OR g.area <= 0) AS sus_no_area,
           sum(g.area * w.rep_mean) / sum(g.area) AS awm,
           sum(w.n_replicates) AS n_replicates,
           list_sort(list_distinct(flatten(list(w.prep_flags)))) AS prep_flags
    FROM with_flags w
    JOIN du_sus d ON d.du = w.parent_location_id
    LEFT JOIN location_geometries g ON g.location_id = w.location_id
    GROUP BY w.parent_location_id, w.matrix, w.top_depth, w.bottom_depth, w.position,
             w.particle_size_fraction, w.fraction, w.meas_basis, w.analyte, w.method_class
)
SELECT location_id, parent_location_id, 'replicate_set' AS level, matrix, top_depth, bottom_depth,
       position, particle_size_fraction, fraction, meas_basis, analyte, method_class, units,
       replicate_set, sample_date, n_replicates, rep_mean, rep_sd, rep_rsd, ucl, ucl_method,
       screening_value, decision, prep_flags, ucl_low, ucl_high, bracket_low_by,
       bracket_high_by, n_alternatives, decision_robust, nd_policy
FROM with_flags
UNION ALL BY NAME
SELECT location_id, CAST(NULL AS VARCHAR) AS parent_location_id, 'area_weighted' AS level,
       matrix, top_depth, bottom_depth, position, particle_size_fraction, fraction, meas_basis,
       analyte, method_class, units, sample_date, n_replicates,
       CASE
           WHEN len(sus_valued) < len(sus) THEN CAST(error(
               'ism_decisions: DU ' || location_id || ' has ' || len(sus) || ' SUs but values at '
               || len(sus_valued) || ' ('
               || array_to_string(list_filter(sus, lambda u: NOT list_contains(sus_valued, u)), ', ')
               || ' missing). Every SU needs a value') AS DOUBLE)
           WHEN sus_no_area IS NOT NULL THEN CAST(error(
               'ism_decisions: DU ' || location_id || ': SU(s) ' || sus_no_area
               || ' have no area in location_geometries') AS DOUBLE)
           ELSE awm
       END AS rep_mean,
       'no_ucl' AS ucl_method, screening_value, 'no_ucl' AS decision, prep_flags, nd_policy
FROM du_groups
ORDER BY analyte, location_id, level;

-- ============================================================================
-- v_uncertainty_components - stored and derived evidence (§7.1)
-- ============================================================================
-- The stored rows (uncertainty_components), and those ChemDuck derives from
-- the data, never stored:
--   field_heterogeneity  per replicate set, basis field_replicates, from
--                        sets of 2+ with no non-detect; includes preparation,
--                        subsampling and analytical
--   subsampling          per preparation, basis lab_replicates, from the
--                        laboratory replicate sets above their quantitation
--                        limits; includes analytical
--   field_heterogeneity  per field sample, basis field_duplicate, RSD = RPD /
--                        sqrt(2) from a pair of detects, df 1; includes
--                        subsampling and analytical (small-scale, at one spot)

CREATE OR REPLACE VIEW v_uncertainty_components AS
SELECT component, scope_kind, scope_id, analyte, method_class, matrix, kind, estimate, df,
       basis, includes, note
FROM uncertainty_components
UNION ALL
SELECT 'field_heterogeneity', 'replicate_set', replicate_set, analyte, method_class, matrix,
       'rsd', rsd, CAST(n - 1 AS INTEGER), 'field_replicates',
       ['preparation', 'subsampling', 'analytical'], NULL
FROM v_ism_replicate_qc
WHERE n >= 2 AND n_nondetect = 0 AND rsd IS NOT NULL
UNION ALL
SELECT 'subsampling', 'preparation', prep_id, analyte, method_class, matrix,
       'rsd', sd / mean, CAST(n - 1 AS INTEGER), 'lab_replicates', ['analytical'], NULL
FROM v_ism_lab_replicate_qc
WHERE n >= 2 AND usable AND mean > 0
UNION ALL
-- the pairs of v_field_duplicate_rpd (05_extended_views.sql, which a
-- consumer does not load), detects only
SELECT 'field_heterogeneity', 'sample', p.sample_id, pr.analyte,
       COALESCE(pr.method_class, upper(trim(pr.method_code))), p.matrix,
       'rsd', rpd(pr.result, dr.result) / 100.0 / sqrt(2.0), 1, 'field_duplicate',
       ['subsampling', 'analytical'], NULL
FROM samples p
JOIN samples d ON d.parent_sample_id = p.sample_id AND d.sample_type = 'FD'
JOIN results pr ON pr.sample_id = p.sample_id
JOIN results dr ON dr.sample_id = d.sample_id AND dr.analyte = pr.analyte
 AND COALESCE(dr.fraction, '') = COALESCE(pr.fraction, '')
 AND COALESCE(dr.meas_basis, '') = COALESCE(pr.meas_basis, '')
 AND COALESCE(dr.method_class, upper(trim(dr.method_code)), '')
     = COALESCE(pr.method_class, upper(trim(pr.method_code)), '')
WHERE pr.detected AND dr.detected AND rpd(pr.result, dr.result) IS NOT NULL;

-- ============================================================================
-- result_uncertainty - each result's combined uncertainty (§7.2)
-- ============================================================================
-- For each result of p_analyte (p_sample_ids narrows it), every component
-- estimate whose scope holds the result: the result itself, its aliquot,
-- its preparation, its sample, its replicate set, its location or that
-- location's DU, its batch (lab_package_id, the SDG) and its laboratory's
-- method class in the quarter it was analysed
-- ('<lab_name>|<method_class>|<yyyy>-Q<q>'), each where the estimate's
-- analyte, method class and matrix are its own or NULL.
--
-- Chosen most inclusive first: the most components in includes, then the
-- higher df, then basis in the order of chemduck_settings
-- uncertainty_basis_order (default reported, field_replicates,
-- field_duplicate, lab_replicates, lab_duplicate, lcs_control_limits,
-- model:...). An estimate whose component a chosen one already includes is
-- not added again; one whose includes overlap what is already covered,
-- while its own component is not, means the nesting is ambiguous, and the
-- call refuses. The rest combine by root-sum-of-squares of their relative
-- SDs (an sd is divided by the result). Bias is never variance: flags and
-- bias factors are listed beside the number, never in it.
--
-- One row per result: rsd_total and sd_total (NULL where nothing applies),
-- components (component, basis, scope_kind, rsd, df, includes), flags (the
-- flags' notes) and bias_factors.

CREATE OR REPLACE MACRO uncertainty_basis_rank(basis) AS (
    COALESCE(list_position(
        string_split(COALESCE((SELECT value FROM chemduck_settings
                               WHERE key = 'uncertainty_basis_order'),
                              'reported,field_replicates,field_duplicate,lab_replicates,'
                              || 'lab_duplicate,lcs_control_limits,model'), ','),
        CASE WHEN basis LIKE 'model:%' THEN 'model' ELSE basis END), 99)
);

CREATE OR REPLACE MACRO result_uncertainty(p_analyte, p_sample_ids := NULL) AS TABLE
WITH RECURSIVE res AS (
    SELECT r.result_id, r.sample_id, r.analyte, r.result, r.units, s.matrix,
           COALESCE(r.method_class, upper(trim(r.method_code))) AS method_class,
           s.location_id, l.parent_location_id, s.replicate_set, r.lab_package_id,
           ra.subsample_id, ss.prep_id,
           r.lab_name || '|' || COALESCE(r.method_class, upper(trim(r.method_code))) || '|'
               || year(COALESCE(r.date_analyzed, s.sample_date)) || '-Q'
               || quarter(COALESCE(r.date_analyzed, s.sample_date)) AS lab_period
    FROM results r
    JOIN samples s ON s.sample_id = r.sample_id
    JOIN locations l ON l.location_id = s.location_id
    LEFT JOIN result_aliquots ra ON ra.result_id = r.result_id
    LEFT JOIN sample_subsamples ss ON ss.subsample_id = ra.subsample_id
    WHERE r.analyte = p_analyte
      AND (p_sample_ids IS NULL OR list_contains(CAST(p_sample_ids AS VARCHAR[]), r.sample_id))
),
cand AS (
    SELECT res.result_id, res.result, u.*
    FROM res
    JOIN v_uncertainty_components u
      ON (u.analyte IS NULL OR u.analyte = res.analyte)
     AND (u.method_class IS NULL OR u.method_class = res.method_class)
     AND (u.matrix IS NULL OR u.matrix = res.matrix)
     AND CASE u.scope_kind
             WHEN 'result' THEN u.scope_id = CAST(res.result_id AS VARCHAR)
             WHEN 'subsample' THEN u.scope_id = res.subsample_id
             WHEN 'preparation' THEN u.scope_id = res.prep_id
             WHEN 'sample' THEN u.scope_id = res.sample_id
             WHEN 'replicate_set' THEN u.scope_id = res.replicate_set
             WHEN 'location' THEN u.scope_id IN (res.location_id, res.parent_location_id)
             WHEN 'batch' THEN u.scope_id = res.lab_package_id
             WHEN 'lab_method_period' THEN u.scope_id = res.lab_period
             ELSE FALSE
         END
),
ranked AS (
    SELECT result_id,
           list(struct_pack(component := component, basis := basis, scope_kind := scope_kind,
                            rsd := CASE WHEN kind = 'rsd' THEN estimate
                                        ELSE estimate / NULLIF(abs(result), 0) END,
                            df := df, includes := COALESCE(includes, CAST([] AS VARCHAR[])))
                ORDER BY len(COALESCE(includes, CAST([] AS VARCHAR[]))) DESC, df DESC NULLS LAST,
                         uncertainty_basis_rank(basis), component, scope_kind)
               FILTER (WHERE kind IN ('rsd', 'sd')) AS cands,
           list(note ORDER BY note) FILTER (WHERE kind = 'flag') AS flags,
           list(struct_pack(component := component, basis := basis, factor := estimate))
               FILTER (WHERE kind = 'bias_factor') AS bias_factors
    FROM cand GROUP BY result_id
),
walk(result_id, i, covered, chosen, conflict) AS (
    SELECT result_id, 0, CAST([] AS VARCHAR[]),
           CAST([] AS STRUCT(component VARCHAR, basis VARCHAR, scope_kind VARCHAR, rsd DOUBLE,
                             df INTEGER, includes VARCHAR[])[]),
           CAST(NULL AS VARCHAR)
    FROM ranked WHERE len(COALESCE(cands, [])) > 0
    UNION ALL
    SELECT w.result_id, w.i + 1,
           CASE WHEN list_contains(w.covered, x.component) THEN w.covered
                ELSE list_distinct(list_concat(w.covered, [x.component], x.includes)) END,
           CASE WHEN list_contains(w.covered, x.component) OR x.rsd IS NULL THEN w.chosen
                ELSE list_append(w.chosen, x) END,
           COALESCE(w.conflict,
                    CASE WHEN NOT list_contains(w.covered, x.component)
                              AND list_has_any(x.includes, w.covered)
                         THEN x.component || ' (' || x.basis || ') includes '
                              || array_to_string(list_intersect(x.includes, w.covered), ', ')
                              || ', which a chosen estimate already covers' END)
    FROM walk w
    JOIN ranked r ON r.result_id = w.result_id
    CROSS JOIN LATERAL (SELECT r.cands[w.i + 1] AS x) nxt
    WHERE w.i < len(r.cands)
),
final AS (
    SELECT w.* FROM walk w JOIN ranked r ON r.result_id = w.result_id
    WHERE w.i = len(r.cands)
)
SELECT res.result_id, res.sample_id, res.analyte, res.result, res.units,
       CASE WHEN f.conflict IS NOT NULL THEN CAST(error(
                'result_uncertainty: result ' || res.result_id || ' (' || res.sample_id || '): '
                || f.conflict || '. The nesting is ambiguous: fix the includes of the stored '
                || 'estimates') AS DOUBLE)
            WHEN len(f.chosen) > 0
                THEN sqrt(list_sum(list_transform(f.chosen, lambda c: c.rsd * c.rsd)))
       END AS rsd_total,
       CASE WHEN len(f.chosen) > 0
                THEN abs(res.result) * sqrt(list_sum(list_transform(f.chosen, lambda c: c.rsd * c.rsd)))
       END AS sd_total,
       COALESCE(f.chosen, []) AS components,
       COALESCE(rk.flags, []) AS flags,
       COALESCE(rk.bias_factors, []) AS bias_factors
FROM res
LEFT JOIN final f ON f.result_id = res.result_id
LEFT JOIN ranked rk ON rk.result_id = res.result_id
ORDER BY res.sample_id, res.result_id;
