-- ChemDuck Schema: Analytical Views
-- Phase 2.1-2.5: Read layer for client queries

-- ============================================================================
-- 2.1 v_results_denormalized - Wide view joining all base tables
-- ============================================================================
-- This is the primary view for most analytical queries
-- Excludes QA/QC sample types and rejected records by default

CREATE OR REPLACE VIEW v_results_denormalized AS
SELECT
    -- Location fields
    l.location_id,
    l.x,
    l.y,
    l.loc_type,
    l.loc_desc,
    l.aquifer,
    l.region,
    l.dataset,
    l.project,
    l.study,

    -- Sample fields
    s.sample_id,
    s.sample_date,
    s.matrix,
    s.top_depth,
    s.bottom_depth,
    s.depth,
    s.depth_units,
    s.sample_type,
    s.composite_type,
    s.event,
    s.sample_datetime,
    s.position,

    -- Result fields
    r.result_id,
    r.analyte,
    r.anl_sm_mb,
    r.analyte_group,
    r.cas_number,
    r.result,
    r.units,
    r.std_units,
    -- units as converted at load (shared rules D5)
    r.reported_units,
    r.unit_factor,
    r.unit_source,
    r.method_detection_limit,
    r.reporting_limit,
    r.quantitation_limit,
    r.upper_quantitation_limit,
    r.dilution_factor,
    r.modeled_relative_uncertainty,
    r.detected,
    r.estimated,
    r.tic,
    r.qualifier,
    r.lab_name,
    r.lab_sample_id,
    r.method_code,
    r.meas_basis,
    r.fraction,
    r.date_analyzed,
    -- what is measured; data loaded before method classes: the code itself
    COALESCE(r.method_class, UPPER(TRIM(r.method_code))) AS method_class,
    r.reportable,
    r.test_type,
    r.test_rank,

    -- Computed fields
    -- The limit that stands for a non-detect: chosen at load by the mapping's
    -- limit_chain (shared rules D4; 'none' = no link had one, so no value);
    -- for data loaded before that, the chain reporting, method detection,
    -- quantitation.
    CASE
        WHEN r.limit_source IS NULL
            THEN COALESCE(r.reporting_limit, r.method_detection_limit, r.quantitation_limit)
        WHEN r.limit_source = 'none' THEN NULL
        ELSE r.nd_limit
    END AS detection_limit,
    -- Which link of the limit chain detection_limit came from
    COALESCE(r.limit_source, CASE
        WHEN r.reporting_limit IS NOT NULL THEN 'reporting'
        WHEN r.method_detection_limit IS NOT NULL THEN 'method_detection'
        WHEN r.quantitation_limit IS NOT NULL THEN 'quantitation'
    END) AS limit_source,
    -- The type of that limit (shared rules D4): a chain link's is its own;
    -- the source's report limit (EQuIS REPORT_RESULT_LIMIT) states none, so
    -- it is the type whose limit on the same row it equals, NULL if none
    CASE
        WHEN r.limit_source = 'none' THEN NULL
        WHEN r.limit_source = 'report' THEN CASE
            WHEN r.nd_limit = r.reporting_limit THEN 'reporting'
            WHEN r.nd_limit = r.method_detection_limit THEN 'method_detection'
            WHEN r.nd_limit = r.quantitation_limit THEN 'quantitation'
        END
        ELSE COALESCE(r.limit_source, CASE
            WHEN r.reporting_limit IS NOT NULL THEN 'reporting'
            WHEN r.method_detection_limit IS NOT NULL THEN 'method_detection'
            WHEN r.quantitation_limit IS NOT NULL THEN 'quantitation'
        END)
    END AS limit_type,
    r.report_limit,
    moist.moisture_result AS percent_moisture,
    make_dup_id(l.location_id, s.sample_date, s.depth, r.fraction, r.meas_basis) AS dup_id,
    dv_qualifier(r.detected, r.estimated) AS dv_qual,
    EXTRACT(YEAR FROM s.sample_date) AS sample_year,
    STRFTIME(s.sample_date, '%Y-%m') AS sample_month

FROM results r
JOIN samples s ON r.sample_id = s.sample_id
JOIN locations l ON s.location_id = l.location_id
LEFT JOIN (
    SELECT
        r_m.sample_id,
        r_m.lab_sample_id,
        r_m.result AS moisture_result
    FROM results r_m
    WHERE UPPER(r_m.analyte) IN (
        'PERCENT MOISTURE', '% MOISTURE', 'MOISTURE',
        'PERCENT_MOISTURE', 'MOISTURE CONTENT', '% MOISTURE CONTENT'
    )
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY COALESCE(r_m.lab_sample_id, r_m.sample_id)
        ORDER BY r_m.result_id
    ) = 1
) moist ON COALESCE(r.lab_sample_id, r.sample_id) = COALESCE(moist.lab_sample_id, moist.sample_id)
WHERE
    -- Exclude QA/QC and special sample types
    (s.sample_type IS NULL OR s.sample_type NOT IN ('AVG', 'REAN', 'DNU', 'IDW', 'NR'))
    -- Exclude rejected records (qualifier ending in R)
    AND (r.qualifier IS NULL OR r.qualifier NOT LIKE '%R');

-- ============================================================================
-- 2.2 v_results_for_mapping - Pre-aggregated for spatial display
-- ============================================================================
-- Aggregates results by location/analyte for quick mapping queries.
-- Fraction and meas_basis are part of the grouping: dissolved and total are
-- different measurements and are never averaged together.

CREATE OR REPLACE VIEW v_results_for_mapping AS
SELECT
    location_id,
    x,
    y,
    matrix,
    fraction,
    meas_basis,
    analyte,
    anl_sm_mb,
    analyte_group,
    units,
    std_units,
    MIN(sample_date) AS min_date,
    MAX(sample_date) AS max_date,
    COUNT(*) AS sample_count,
    AVG(result) AS avg_result,
    MIN(result) AS min_result,
    MAX(result) AS max_result,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY result) AS median_result,
    STDDEV(result) AS std_result,
    SUM(CASE WHEN detected THEN 1 ELSE 0 END) AS detect_count,
    SUM(CASE WHEN NOT detected THEN 1 ELSE 0 END) AS nondetect_count,
    SUM(CASE WHEN detected IS NULL THEN 1 ELSE 0 END) AS unknown_detect_count,
    COUNT(DISTINCT sample_date) AS n_events
FROM v_results_denormalized
GROUP BY
    location_id, x, y,
    matrix, fraction, meas_basis, analyte, anl_sm_mb, analyte_group,
    units, std_units;

-- ============================================================================
-- 2.3 v_most_recent_results - Latest sample per location/analyte
-- ============================================================================
-- Returns only the most recent measurement for each
-- location/analyte/matrix/fraction/meas_basis.
-- Same-date ties break on natural columns (sample_id, method_code) before
-- result_id, so the winner does not depend on load order.

CREATE OR REPLACE VIEW v_most_recent_results AS
SELECT *
FROM v_results_denormalized
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY location_id, analyte, matrix, fraction, meas_basis
    ORDER BY sample_date DESC, sample_id DESC, method_code DESC NULLS LAST,
             result_id DESC
) = 1;

-- ============================================================================
-- 2.4 v_results_with_screening - Results joined with screening levels
-- ============================================================================
-- Pre-joins results with applicable screening levels for exceedance analysis
-- Uses LEFT JOIN so results without screening levels are still included
--
-- A non-detect says "below L", not "equal to L", so its limit is never
-- compared as if it were a concentration:
--   upper criteria ('>', the default):
--     ND_BELOW         limit <= screening value: absent at that level
--     ND_INCONCLUSIVE  limit >  screening value: the lab could not see low
--                      enough to say. Never EXCEEDS.
--   lower criteria ('<', e.g. a minimum pH or DO):
--     EXCEEDS          limit <= screening value: the true value is below the
--                      limit, so it is certainly below the minimum
--     ND_INCONCLUSIVE  otherwise
-- The ND limit is detection_limit, falling back to the reported result
-- (EQuIS exports report the limit as the result of a non-detect).
-- exceedance_ratio is NULL for non-detects; limit_ratio (limit / screening
-- value) is the separately named ratio for them.
-- A result whose detect status is unknown (NULL detected) is DETECT_UNKNOWN,
-- with both ratios NULL: its value may be a concentration or a limit.

CREATE OR REPLACE VIEW v_results_with_screening AS
SELECT
    r.*,
    sl.screening_id,
    sl.name AS screening_name,
    sl.level_type,
    sl.value AS screening_value,
    sl.comparison AS screening_comparison,
    sl.source AS screening_source,
    CASE
        WHEN sl.value IS NULL THEN NULL
        -- Unknown detect status: neither compared as a detect nor as a limit
        WHEN r.detected IS NULL THEN 'DETECT_UNKNOWN'
        WHEN r.detected = FALSE AND COALESCE(r.detection_limit, r.result) IS NULL
            THEN 'ND_INCONCLUSIVE'
        WHEN r.detected = FALSE AND sl.comparison = '<'
            THEN CASE WHEN COALESCE(r.detection_limit, r.result) <= sl.value
                      THEN 'EXCEEDS' ELSE 'ND_INCONCLUSIVE' END
        WHEN r.detected = FALSE
            THEN CASE WHEN COALESCE(r.detection_limit, r.result) <= sl.value
                      THEN 'ND_BELOW' ELSE 'ND_INCONCLUSIVE' END
        WHEN sl.comparison = '<' AND r.result < sl.value THEN 'EXCEEDS'
        WHEN sl.comparison = '<' AND r.result < sl.value * 1.2 THEN 'APPROACHING'
        WHEN sl.comparison = '<' THEN 'ABOVE'
        WHEN r.result > sl.value THEN 'EXCEEDS'
        WHEN r.result > sl.value * 0.8 THEN 'APPROACHING'
        ELSE 'BELOW'
    END AS exceedance_status,
    CASE
        WHEN sl.value IS NULL OR sl.value = 0 THEN NULL
        WHEN r.detected = FALSE OR r.detected IS NULL THEN NULL
        WHEN sl.comparison = '<' THEN sl.value / r.result
        ELSE r.result / sl.value
    END AS exceedance_ratio,
    CASE
        WHEN sl.value IS NULL OR sl.value = 0 THEN NULL
        WHEN r.detected = FALSE THEN COALESCE(r.detection_limit, r.result) / sl.value
        ELSE NULL
    END AS limit_ratio
FROM v_results_denormalized r
LEFT JOIN screening_levels sl
    ON r.analyte = sl.analyte
    AND r.matrix = sl.matrix
    AND r.std_units = sl.units;

-- ============================================================================
-- 2.5 v_exceedances - Filtered view of results that exceed screening levels
-- ============================================================================
-- Convenience view showing only records that exceed their screening level

CREATE OR REPLACE VIEW v_exceedances AS
SELECT *
FROM v_results_with_screening
WHERE exceedance_status = 'EXCEEDS';

-- ============================================================================
-- Additional utility views
-- ============================================================================

-- v_analyte_summary - Summary statistics per variable: analyte, matrix,
-- fraction, basis and method class (shared rules D3: a field and a lab pH
-- are two variables, so a picker offers both)
CREATE OR REPLACE VIEW v_analyte_summary AS
SELECT
    analyte,
    matrix,
    fraction,
    meas_basis,
    method_class,
    std_units,
    COUNT(*) AS n_results,
    COUNT(DISTINCT sample_id) AS n_samples,
    COUNT(DISTINCT location_id) AS n_locations,
    COUNT(DISTINCT sample_date) AS n_dates,
    SUM(CASE WHEN detected THEN 1 ELSE 0 END) AS n_detect,
    SUM(CASE WHEN NOT detected THEN 1 ELSE 0 END) AS n_nondetect,
    SUM(CASE WHEN detected IS NULL THEN 1 ELSE 0 END) AS n_detect_unknown,
    ROUND(100.0 * SUM(CASE WHEN detected THEN 1 ELSE 0 END) / COUNT(*), 1) AS detect_pct,
    MIN(result) AS min_result,
    MAX(result) AS max_result,
    AVG(result) AS mean_result,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY result) AS median_result
FROM v_results_denormalized
GROUP BY analyte, matrix, fraction, meas_basis, method_class, std_units;

-- v_method_classes - The method classes in the data, each with the method
-- codes it holds and its results (classes curated in method_classes; a code
-- nothing matched is its own class)
CREATE OR REPLACE VIEW v_method_classes AS
SELECT
    method_class,
    list_sort(list_distinct(list(method_code))) AS method_codes,
    COUNT(*) AS n_results,
    COUNT(DISTINCT analyte) AS n_analytes
FROM v_results_denormalized
GROUP BY method_class
ORDER BY method_class;

-- v_positions - The vertical positions in the data per matrix (shared rules
-- D1): each labelled position, and NULL for samples placed by depth alone,
-- with their distinct depth intervals and samples
CREATE OR REPLACE VIEW v_positions AS
SELECT
    matrix,
    position,
    COUNT(DISTINCT (top_depth, bottom_depth))
        FILTER (WHERE top_depth IS NOT NULL OR bottom_depth IS NOT NULL) AS n_depth_intervals,
    COUNT(*) AS n_samples,
    COUNT(DISTINCT location_id) AS n_locations
FROM samples
GROUP BY matrix, position
ORDER BY matrix, position;

-- v_unit_conflicts - Variables still in more than one unit after the load's
-- conversion (shared rules D5), method class included (D3)
-- Aggregating across these ranks results by unit, not by concentration
-- (0.57 mg/L loses to 168 ug/L); aggregates refuse them by default. Missing units alongside real ones are
-- listed too: a unitless number cannot be compared with a mg/L one.
CREATE OR REPLACE VIEW v_unit_conflicts AS
SELECT
    analyte,
    matrix,
    fraction,
    meas_basis,
    method_class,
    COUNT(DISTINCT units) AS n_units,
    list_sort(list_distinct(list(units))) AS units_found,
    list_sort(list_distinct(list(unit_source))) AS unit_sources,
    COUNT(*) FILTER (WHERE units IS NULL) AS n_missing_units,
    COUNT(*) AS n_results,
    COUNT(DISTINCT location_id) AS n_locations
FROM v_results_denormalized
GROUP BY analyte, matrix, fraction, meas_basis, method_class
HAVING COUNT(DISTINCT units) > 1
    OR (COUNT(DISTINCT units) = 1 AND COUNT(*) FILTER (WHERE units IS NULL) > 0);

-- v_location_summary - Summary by location
CREATE OR REPLACE VIEW v_location_summary AS
SELECT
    location_id,
    x,
    y,
    matrix,
    COUNT(DISTINCT analyte) AS n_analytes,
    COUNT(*) AS n_results,
    MIN(sample_date) AS first_sample,
    MAX(sample_date) AS last_sample,
    COUNT(DISTINCT sample_date) AS n_events
FROM v_results_denormalized
GROUP BY location_id, x, y, matrix;

-- ============================================================================
-- 2.6 (Borehole trace) - see desurveyed_stations_fine in sql/09_desurvey.sql
-- ============================================================================
-- The dense desurveyed trace per location is materialized as the table
-- desurveyed_stations_fine, built by refresh_desurvey(). For plotting a
-- borehole trace, SELECT location_id, md, x, y, z FROM desurveyed_stations_fine
-- ORDER BY location_id, md. Filter extrapolated=FALSE to restrict to the
-- strictly-surveyed portion (the held-tangent extension that covers samples
-- below the deepest survey station).
--
-- The simpler one-row-per-survey-station view that previously lived here was
-- removed. If a consumer specifically needs only the original survey
-- station coords, join desurveyed_stations_fine to downhole_survey on
-- (location_id, md = depth).
--
-- The legacy v_desurveyed_stations view below is preserved unchanged for
-- backward compatibility but reads from downhole_survey directly with the
-- segment-linear approximation -- prefer desurveyed_stations_fine for new
-- code.

CREATE OR REPLACE VIEW v_desurveyed_stations AS
WITH stations AS (
    -- Pair each station with the next (LEAD) per location, ordered by depth.
    -- Convert Leapfrog dip (angle from horizontal, negative down) to
    -- inclination measured from vertical-down: inc = radians(90 + dip).
    -- A dip of -90 yields inc = 0 (straight down); -85 yields inc = 5 deg.
    SELECT
        ds.location_id,
        ds.depth AS md1,
        radians(90 + ds.dip) AS inc1,
        radians(ds.azimuth) AS az1,
        LEAD(ds.depth) OVER w AS md2,
        LEAD(radians(90 + ds.dip)) OVER w AS inc2,
        LEAD(radians(ds.azimuth)) OVER w AS az2
    FROM downhole_survey ds
    WINDOW w AS (PARTITION BY ds.location_id ORDER BY ds.depth)
),
segments AS (
    -- Compute per-segment dogleg beta and minimum-curvature ratio factor.
    -- beta = arccos(cos(inc1)cos(inc2) + sin(inc1)sin(inc2)cos(az2 - az1))
    -- rf   = (2/beta) * tan(beta/2), with limit 1 as beta -> 0.
    -- Per-segment deltas in grid coords (z is positive-up; deepening -> -z):
    --   dx = (dmd/2) * (sin(inc1)sin(az1) + sin(inc2)sin(az2)) * rf
    --   dy = (dmd/2) * (sin(inc1)cos(az1) + sin(inc2)cos(az2)) * rf
    --   dz = (dmd/2) * (-cos(inc1) - cos(inc2)) * rf
    SELECT
        s.location_id,
        s.md1,
        s.md2,
        s.inc1,
        s.inc2,
        s.az1,
        s.az2,
        CASE
            WHEN s.md2 IS NULL THEN NULL
            ELSE acos(LEAST(1.0, GREATEST(-1.0,
                cos(s.inc1) * cos(s.inc2)
                + sin(s.inc1) * sin(s.inc2) * cos(s.az2 - s.az1)
            )))
        END AS beta
    FROM stations s
),
seg_deltas AS (
    SELECT
        sg.*,
        CASE
            WHEN sg.md2 IS NULL THEN NULL
            WHEN sg.beta < 1e-9 THEN 1.0
            ELSE (2.0 / sg.beta) * tan(sg.beta / 2.0)
        END AS rf,
        (sg.md2 - sg.md1) AS dmd
    FROM segments sg
),
seg_xyz AS (
    SELECT
        sd.*,
        CASE WHEN sd.md2 IS NULL THEN NULL
             ELSE (sd.dmd / 2.0) * (sin(sd.inc1) * sin(sd.az1) + sin(sd.inc2) * sin(sd.az2)) * sd.rf
        END AS dx,
        CASE WHEN sd.md2 IS NULL THEN NULL
             ELSE (sd.dmd / 2.0) * (sin(sd.inc1) * cos(sd.az1) + sin(sd.inc2) * cos(sd.az2)) * sd.rf
        END AS dy,
        CASE WHEN sd.md2 IS NULL THEN NULL
             ELSE (sd.dmd / 2.0) * (-cos(sd.inc1) - cos(sd.inc2)) * sd.rf
        END AS dz
    FROM seg_deltas sd
),
cumulative AS (
    -- cum_* are the coordinates AT the station (md1). Collar offset added at end.
    SELECT
        sx.location_id,
        sx.md1,
        sx.md2,
        sx.dmd,
        sx.dx,
        sx.dy,
        sx.dz,
        COALESCE(SUM(sx.dx) OVER (
            PARTITION BY sx.location_id ORDER BY sx.md1
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ), 0.0) AS cum_dx_at_station,
        COALESCE(SUM(sx.dy) OVER (
            PARTITION BY sx.location_id ORDER BY sx.md1
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ), 0.0) AS cum_dy_at_station,
        COALESCE(SUM(sx.dz) OVER (
            PARTITION BY sx.location_id ORDER BY sx.md1
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ), 0.0) AS cum_dz_at_station
    FROM seg_xyz sx
)
SELECT
    c.location_id,
    c.md1 AS depth,
    c.md2 AS next_depth,
    c.dmd AS segment_length,
    c.dx AS segment_dx,
    c.dy AS segment_dy,
    c.dz AS segment_dz,
    l.x + c.cum_dx_at_station AS x,
    l.y + c.cum_dy_at_station AS y,
    COALESCE(l.elev_ground, 0.0) + c.cum_dz_at_station AS z,
    l.x AS collar_x,
    l.y AS collar_y,
    COALESCE(l.elev_ground, 0.0) AS collar_z
FROM cumulative c
JOIN locations l ON c.location_id = l.location_id;

-- ============================================================================
-- 2.7 v_sample_xyz - Per-sample desurveyed coordinates
-- ============================================================================
-- For each sample, returns the (x, y, z) at the sample's effective depth.
-- Effective depth = COALESCE(depth, midpoint of top/bottom, top_depth).
--
-- For locations WITH a downhole_survey, looks up the materialized fine-grid
-- in desurveyed_stations_fine (built by sql/09_desurvey.sql) and linearly
-- interpolates between adjacent fine-grid rows. Because the fine grid is
-- typically 1 ft, this linear interpolation introduces sub-millimeter error
-- relative to true MC-on-arc.
--
-- For locations WITHOUT a downhole_survey, falls back to a vertical-hole
-- assumption: x = collar x, y = collar y, z = elev_ground - depth.
--
-- If desurveyed_stations_fine does not exist yet (refresh_desurvey() has
-- never been called) the LEFT JOIN returns NULL for all rows and every
-- sample uses the vertical fallback. The has_survey flag is FALSE in that
-- case so consumers can detect the situation.

CREATE OR REPLACE VIEW v_sample_xyz AS
WITH sample_eff AS (
    SELECT
        s.sample_id,
        s.location_id,
        COALESCE(s.depth, (s.top_depth + s.bottom_depth) / 2.0, s.top_depth) AS eff_depth
    FROM samples s
),
-- Find the fine-grid row at-or-below the sample's effective depth (lo) and
-- the next row above it (hi). The fractional position u within that interval
-- gives the linear interpolation weight.
bracketed AS (
    SELECT
        se.sample_id,
        se.location_id,
        se.eff_depth,
        (SELECT MAX(d.md) FROM desurveyed_stations_fine d
         WHERE d.location_id = se.location_id AND d.md <= se.eff_depth) AS md_lo,
        (SELECT MIN(d.md) FROM desurveyed_stations_fine d
         WHERE d.location_id = se.location_id AND d.md >= se.eff_depth) AS md_hi
    FROM sample_eff se
),
interp AS (
    SELECT
        b.sample_id,
        b.location_id,
        b.eff_depth,
        d_lo.x AS x_lo, d_lo.y AS y_lo, d_lo.z AS z_lo,
        d_hi.x AS x_hi, d_hi.y AS y_hi, d_hi.z AS z_hi,
        d_lo.extrapolated OR d_hi.extrapolated AS extrapolated,
        CASE
            WHEN b.md_hi IS NULL OR b.md_lo IS NULL THEN NULL
            WHEN b.md_hi = b.md_lo THEN 0.0
            ELSE (b.eff_depth - b.md_lo) / (b.md_hi - b.md_lo)
        END AS u
    FROM bracketed b
    LEFT JOIN desurveyed_stations_fine d_lo
        ON d_lo.location_id = b.location_id AND d_lo.md = b.md_lo
    LEFT JOIN desurveyed_stations_fine d_hi
        ON d_hi.location_id = b.location_id AND d_hi.md = b.md_hi
)
SELECT
    se.sample_id,
    se.location_id,
    se.eff_depth AS depth,
    CASE
        WHEN i.x_lo IS NULL THEN l.x
        WHEN i.u IS NULL THEN COALESCE(i.x_lo, i.x_hi)
        ELSE i.x_lo + i.u * (i.x_hi - i.x_lo)
    END AS x,
    CASE
        WHEN i.y_lo IS NULL THEN l.y
        WHEN i.u IS NULL THEN COALESCE(i.y_lo, i.y_hi)
        ELSE i.y_lo + i.u * (i.y_hi - i.y_lo)
    END AS y,
    CASE
        WHEN i.z_lo IS NULL THEN COALESCE(l.elev_ground, 0.0) - se.eff_depth
        WHEN i.u IS NULL THEN COALESCE(i.z_lo, i.z_hi)
        ELSE i.z_lo + i.u * (i.z_hi - i.z_lo)
    END AS z,
    l.x AS collar_x,
    l.y AS collar_y,
    COALESCE(l.elev_ground, 0.0) AS collar_z,
    CASE WHEN i.x_lo IS NULL THEN FALSE ELSE TRUE END AS has_survey,
    COALESCE(i.extrapolated, FALSE) AS extrapolated
FROM sample_eff se
JOIN locations l ON se.location_id = l.location_id
LEFT JOIN interp i ON i.sample_id = se.sample_id;

-- ============================================================================
-- 2.8 v_results_denormalized_xyz - v_results_denormalized with desurveyed x,y,z
-- ============================================================================
-- Same columns and filters as v_results_denormalized, except:
--   x, y       = desurveyed sample coords (NOT collar coords)
--   z          = desurveyed elevation (positive-up)
--   collar_x/y/z = original location coords (preserved)
--   has_survey = TRUE if a downhole_survey existed for this location

CREATE OR REPLACE VIEW v_results_denormalized_xyz AS
SELECT
    -- Location fields (x, y now desurveyed; collar_* preserved)
    l.location_id,
    xyz.x,
    xyz.y,
    xyz.z,
    xyz.collar_x,
    xyz.collar_y,
    xyz.collar_z,
    xyz.has_survey,
    xyz.extrapolated AS xyz_extrapolated,
    l.loc_type,
    l.loc_desc,
    l.aquifer,
    l.region,
    l.dataset,
    l.project,
    l.study,

    -- Sample fields
    s.sample_id,
    s.sample_date,
    s.matrix,
    s.top_depth,
    s.bottom_depth,
    s.depth,
    s.depth_units,
    s.sample_type,
    s.composite_type,
    s.event,
    s.sample_datetime,
    s.position,

    -- Result fields
    r.result_id,
    r.analyte,
    r.anl_sm_mb,
    r.analyte_group,
    r.cas_number,
    r.result,
    r.units,
    r.std_units,
    -- units as converted at load (shared rules D5)
    r.reported_units,
    r.unit_factor,
    r.unit_source,
    r.method_detection_limit,
    r.reporting_limit,
    r.quantitation_limit,
    r.upper_quantitation_limit,
    r.dilution_factor,
    r.modeled_relative_uncertainty,
    r.detected,
    r.estimated,
    r.tic,
    r.qualifier,
    r.lab_name,
    r.lab_sample_id,
    r.method_code,
    r.meas_basis,
    r.fraction,
    r.date_analyzed,
    -- what is measured; data loaded before method classes: the code itself
    COALESCE(r.method_class, UPPER(TRIM(r.method_code))) AS method_class,
    r.reportable,
    r.test_type,
    r.test_rank,

    -- Computed fields
    -- The limit that stands for a non-detect: chosen at load by the mapping's
    -- limit_chain (shared rules D4; 'none' = no link had one, so no value);
    -- for data loaded before that, the chain reporting, method detection,
    -- quantitation.
    CASE
        WHEN r.limit_source IS NULL
            THEN COALESCE(r.reporting_limit, r.method_detection_limit, r.quantitation_limit)
        WHEN r.limit_source = 'none' THEN NULL
        ELSE r.nd_limit
    END AS detection_limit,
    -- Which link of the limit chain detection_limit came from
    COALESCE(r.limit_source, CASE
        WHEN r.reporting_limit IS NOT NULL THEN 'reporting'
        WHEN r.method_detection_limit IS NOT NULL THEN 'method_detection'
        WHEN r.quantitation_limit IS NOT NULL THEN 'quantitation'
    END) AS limit_source,
    -- The type of that limit (shared rules D4): a chain link's is its own;
    -- the source's report limit (EQuIS REPORT_RESULT_LIMIT) states none, so
    -- it is the type whose limit on the same row it equals, NULL if none
    CASE
        WHEN r.limit_source = 'none' THEN NULL
        WHEN r.limit_source = 'report' THEN CASE
            WHEN r.nd_limit = r.reporting_limit THEN 'reporting'
            WHEN r.nd_limit = r.method_detection_limit THEN 'method_detection'
            WHEN r.nd_limit = r.quantitation_limit THEN 'quantitation'
        END
        ELSE COALESCE(r.limit_source, CASE
            WHEN r.reporting_limit IS NOT NULL THEN 'reporting'
            WHEN r.method_detection_limit IS NOT NULL THEN 'method_detection'
            WHEN r.quantitation_limit IS NOT NULL THEN 'quantitation'
        END)
    END AS limit_type,
    r.report_limit,
    moist.moisture_result AS percent_moisture,
    make_dup_id(l.location_id, s.sample_date, s.depth, r.fraction, r.meas_basis) AS dup_id,
    dv_qualifier(r.detected, r.estimated) AS dv_qual,
    EXTRACT(YEAR FROM s.sample_date) AS sample_year,
    STRFTIME(s.sample_date, '%Y-%m') AS sample_month

FROM results r
JOIN samples s ON r.sample_id = s.sample_id
JOIN locations l ON s.location_id = l.location_id
JOIN v_sample_xyz xyz ON xyz.sample_id = s.sample_id
LEFT JOIN (
    SELECT
        r_m.sample_id,
        r_m.lab_sample_id,
        r_m.result AS moisture_result
    FROM results r_m
    WHERE UPPER(r_m.analyte) IN (
        'PERCENT MOISTURE', '% MOISTURE', 'MOISTURE',
        'PERCENT_MOISTURE', 'MOISTURE CONTENT', '% MOISTURE CONTENT'
    )
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY COALESCE(r_m.lab_sample_id, r_m.sample_id)
        ORDER BY r_m.result_id
    ) = 1
) moist ON COALESCE(r.lab_sample_id, r.sample_id) = COALESCE(moist.lab_sample_id, moist.sample_id)
WHERE
    -- Exclude QA/QC and special sample types
    (s.sample_type IS NULL OR s.sample_type NOT IN ('AVG', 'REAN', 'DNU', 'IDW', 'NR'))
    -- Exclude rejected records (qualifier ending in R)
    AND (r.qualifier IS NULL OR r.qualifier NOT LIKE '%R');

-- ============================================================================
-- Additional utility views
-- ============================================================================

-- v_available_filters - Distinct values for UI filter population
CREATE OR REPLACE VIEW v_available_filters AS
SELECT
    'matrix' AS filter_type,
    matrix AS filter_value,
    COUNT(*) AS n_records
FROM v_results_denormalized
GROUP BY matrix
UNION ALL
SELECT
    'analyte_group' AS filter_type,
    analyte_group AS filter_value,
    COUNT(*) AS n_records
FROM v_results_denormalized
WHERE analyte_group IS NOT NULL
GROUP BY analyte_group
UNION ALL
SELECT
    'lab_name' AS filter_type,
    lab_name AS filter_value,
    COUNT(*) AS n_records
FROM v_results_denormalized
WHERE lab_name IS NOT NULL
GROUP BY lab_name
UNION ALL
SELECT
    'event' AS filter_type,
    event AS filter_value,
    COUNT(*) AS n_records
FROM v_results_denormalized
WHERE event IS NOT NULL
GROUP BY event;
