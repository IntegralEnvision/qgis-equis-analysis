-- ChemDuck Schema: Extended Views
-- Views supporting river mile analysis and other application features

-- ============================================================================
-- River Mile Analysis Views
-- ============================================================================

-- v_rivermile_results - Results with river mile context
-- This replaces/extends the legacy ida_rivermile_data pattern
CREATE OR REPLACE VIEW v_rivermile_results AS
SELECT
    r.location_id,
    r.x,
    r.y,
    riv.river_id,
    riv.river_name,
    lrm.river_mile,
    lrm.bank_side,
    r.sample_id,
    r.sample_date,
    r.matrix,
    r.top_depth,
    r.bottom_depth,
    r.depth,
    r.depth_units,
    r.meas_basis,
    r.fraction,
    r.result_id,
    r.analyte,
    r.analyte_group,
    r.result,
    r.units,
    r.std_units,
    r.method_detection_limit,
    r.reporting_limit,
    r.detected,
    r.estimated,
    r.dv_qual,
    r.qualifier
FROM v_results_denormalized r
JOIN location_river_miles lrm ON r.location_id = lrm.location_id
JOIN rivers riv ON lrm.river_id = riv.river_id;

-- v_rivermile_summary - Aggregated statistics by river mile
CREATE OR REPLACE VIEW v_rivermile_summary AS
SELECT
    river_name,
    river_mile,
    location_id,
    analyte,
    matrix,
    fraction,
    meas_basis,
    std_units,
    COUNT(*) AS sample_count,
    COUNT(DISTINCT sample_date) AS event_count,
    SUM(CASE WHEN detected THEN 1 ELSE 0 END) AS detect_count,
    SUM(CASE WHEN NOT detected THEN 1 ELSE 0 END) AS nondetect_count,
    MIN(sample_date) AS first_sample,
    MAX(sample_date) AS last_sample,
    MIN(result) AS min_result,
    MAX(result) AS max_result,
    AVG(result) AS mean_result,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY result) AS median_result
FROM v_rivermile_results
GROUP BY river_name, river_mile, location_id, analyte, matrix, fraction, meas_basis, std_units;

-- v_rivermile_latest - Most recent result per location/analyte on river
CREATE OR REPLACE VIEW v_rivermile_latest AS
SELECT *
FROM v_rivermile_results
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY river_id, location_id, analyte, matrix, fraction, meas_basis
    ORDER BY sample_date DESC, sample_id DESC, result_id DESC
) = 1;

-- ============================================================================
-- Key Locations Views
-- ============================================================================

-- v_key_locations_with_data - Key locations enriched with latest data
CREATE OR REPLACE VIEW v_key_locations_with_data AS
SELECT
    kl.key_location_id,
    kl.name AS key_location_name,
    kl.significance,
    kl.river_mile AS key_river_mile,
    l.location_id,
    l.x,
    l.y,
    l.loc_type,
    l.aquifer,
    (SELECT COUNT(DISTINCT s.sample_id) FROM samples s WHERE s.location_id = l.location_id) AS total_samples,
    (SELECT MAX(s.sample_date) FROM samples s WHERE s.location_id = l.location_id) AS last_sample_date
FROM key_locations kl
LEFT JOIN locations l ON kl.location_id = l.location_id
WHERE kl.active = TRUE;

-- ============================================================================
-- Time Series Support Views
-- ============================================================================

-- v_timeseries_data - Optimized view for time series charting
CREATE OR REPLACE VIEW v_timeseries_data AS
SELECT
    location_id,
    sample_date,
    analyte,
    matrix,
    result,
    detected,
    estimated,
    dv_qual,
    method_detection_limit,
    std_units,
    EXTRACT(YEAR FROM sample_date) AS sample_year,
    EXTRACT(MONTH FROM sample_date) AS sample_month,
    EXTRACT(DOY FROM sample_date) AS day_of_year
FROM v_results_denormalized
ORDER BY location_id, analyte, sample_date;

-- v_timeseries_with_events - Time series data with project events
CREATE OR REPLACE VIEW v_timeseries_with_events AS
SELECT
    ts.*,
    pe.event_id,
    pe.name AS event_name,
    pe.event_type,
    pe.event_date
FROM v_timeseries_data ts
LEFT JOIN project_events pe ON ts.sample_date = pe.event_date
    AND pe.display_on_charts = TRUE;

-- ============================================================================
-- Depth Profile Views
-- ============================================================================

-- v_depth_profiles - Results organized by depth for vertical profiling
CREATE OR REPLACE VIEW v_depth_profiles AS
SELECT
    location_id,
    analyte,
    matrix,
    sample_date,
    depth,
    top_depth,
    bottom_depth,
    depth_units,
    result,
    detected,
    dv_qual,
    std_units,
    meas_basis,
    fraction
FROM v_results_denormalized
WHERE depth IS NOT NULL
ORDER BY location_id, analyte, sample_date, depth;

-- v_depth_summary - Aggregated by depth intervals
CREATE OR REPLACE VIEW v_depth_summary AS
SELECT
    location_id,
    analyte,
    matrix,
    fraction,
    meas_basis,
    depth,
    depth_units,
    COUNT(*) AS sample_count,
    AVG(result) AS mean_result,
    MIN(result) AS min_result,
    MAX(result) AS max_result,
    STDDEV(result) AS std_result,
    SUM(CASE WHEN detected THEN 1 ELSE 0 END) AS detect_count
FROM v_results_denormalized
WHERE depth IS NOT NULL
GROUP BY location_id, analyte, matrix, fraction, meas_basis, depth, depth_units;

-- ============================================================================
-- Box Plot / Statistical Views
-- ============================================================================

-- v_boxplot_stats - Pre-computed statistics for box plots
CREATE OR REPLACE VIEW v_boxplot_stats AS
SELECT
    location_id,
    analyte,
    matrix,
    fraction,
    meas_basis,
    std_units,
    COUNT(*) AS n,
    MIN(result) AS min_val,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY result) AS q1,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY result) AS median,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY result) AS q3,
    MAX(result) AS max_val,
    AVG(result) AS mean_val,
    STDDEV(result) AS std_val,
    -- IQR-based outlier bounds
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY result) -
        1.5 * (PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY result) -
               PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY result)) AS lower_fence,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY result) +
        1.5 * (PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY result) -
               PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY result)) AS upper_fence
FROM v_results_denormalized
GROUP BY location_id, analyte, matrix, fraction, meas_basis, std_units
HAVING COUNT(*) >= 5;  -- Require minimum sample size for meaningful stats

-- ============================================================================
-- User Analyte Groups Views
-- ============================================================================

-- v_user_analyte_groups - Analytes with their user-defined group memberships
CREATE OR REPLACE VIEW v_user_analyte_groups AS
SELECT
    uag.group_id,
    uag.group_name,
    uag.description AS group_description,
    uagm.analyte,
    uagm.display_order
FROM user_analyte_groups uag
JOIN user_analyte_group_members uagm ON uag.group_id = uagm.group_id
ORDER BY uag.group_name, uagm.display_order, uagm.analyte;

-- ============================================================================
-- Data Availability Views
-- ============================================================================

-- v_data_availability - Summary of what data exists for filtering UI
CREATE OR REPLACE VIEW v_data_availability AS
SELECT
    'analyte' AS data_type,
    analyte AS value,
    COUNT(*) AS record_count,
    COUNT(DISTINCT location_id) AS location_count,
    MIN(sample_date) AS first_date,
    MAX(sample_date) AS last_date
FROM v_results_denormalized
GROUP BY analyte
UNION ALL
SELECT
    'matrix' AS data_type,
    matrix AS value,
    COUNT(*) AS record_count,
    COUNT(DISTINCT location_id) AS location_count,
    MIN(sample_date) AS first_date,
    MAX(sample_date) AS last_date
FROM v_results_denormalized
GROUP BY matrix
UNION ALL
SELECT
    'analyte_group' AS data_type,
    analyte_group AS value,
    COUNT(*) AS record_count,
    COUNT(DISTINCT location_id) AS location_count,
    MIN(sample_date) AS first_date,
    MAX(sample_date) AS last_date
FROM v_results_denormalized
WHERE analyte_group IS NOT NULL
GROUP BY analyte_group
UNION ALL
SELECT
    'river' AS data_type,
    r.river_name AS value,
    COUNT(*) AS record_count,
    COUNT(DISTINCT vrr.location_id) AS location_count,
    MIN(vrr.sample_date) AS first_date,
    MAX(vrr.sample_date) AS last_date
FROM v_rivermile_results vrr
JOIN rivers r ON vrr.river_id = r.river_id
GROUP BY r.river_name;

-- ============================================================================
-- Field Duplicate RPD (Relative Percent Difference)
-- ============================================================================
-- Pairs field duplicate samples with their parent via parent_sample_id,
-- computes RPD for each analyte. Excludes pairs where both are non-detect.

CREATE OR REPLACE VIEW v_field_duplicate_rpd AS
WITH pairs AS (
    SELECT
        p.sample_id AS parent_sample_id,
        p.location_id,
        p.sample_date,
        p.matrix,
        d.sample_id AS duplicate_sample_id
    FROM samples p
    JOIN samples d ON d.parent_sample_id = p.sample_id
    WHERE d.sample_type = 'FD'
)
SELECT
    l.location_id,
    pairs.sample_date,
    pairs.matrix,
    pairs.parent_sample_id,
    pairs.duplicate_sample_id,
    pr.analyte,
    pr.analyte_group,
    pr.fraction,
    pr.meas_basis,
    pr.result AS parent_result,
    dr.result AS duplicate_result,
    pr.detected AS parent_detected,
    dr.detected AS duplicate_detected,
    pr.units,
    rpd(pr.result, dr.result) AS rpd_pct
FROM pairs
JOIN locations l ON pairs.location_id = l.location_id
JOIN results pr ON pr.sample_id = pairs.parent_sample_id
JOIN results dr ON dr.sample_id = pairs.duplicate_sample_id
    AND dr.analyte = pr.analyte
    AND COALESCE(dr.fraction, '') = COALESCE(pr.fraction, '')
    AND COALESCE(dr.meas_basis, '') = COALESCE(pr.meas_basis, '')
WHERE
    -- Exclude pairs where both are non-detect
    NOT (pr.detected = FALSE AND dr.detected = FALSE);

-- ============================================================================
-- Groundwater Elevation
-- ============================================================================
-- Computes groundwater elevation from depth-to-water measurements and
-- location reference elevations. Matches water-level analytes by regex.

CREATE OR REPLACE VIEW v_groundwater_elevation AS
SELECT
    l.location_id,
    l.x,
    l.y,
    l.aquifer,
    l.elev_ground,
    l.elev_measuring,
    s.sample_id,
    s.sample_date,
    r.analyte,
    r.result AS measurement,
    r.units,
    r.qualifier,
    r.method_code,
    -- Compute GW elevation: measuring point minus depth-to-water
    CASE
        WHEN REGEXP_MATCHES(UPPER(r.analyte),
            'DEPTH.?(TO|OF).?WATER|DTW|WATER.?DEPTH')
        THEN l.elev_measuring - r.result
        -- If it's already an elevation measurement, use directly
        WHEN REGEXP_MATCHES(UPPER(r.analyte),
            'WATER.?(TABLE|LEVEL|SURFACE).?ELEV|GW.?ELEV|GROUNDWATER.?ELEV'
            || '|STATIC.?WATER.?LEVEL|SWL|POTENTIOMETRIC.?SURFACE')
        THEN r.result
        ELSE NULL
    END AS gw_elevation,
    CASE
        WHEN REGEXP_MATCHES(UPPER(r.analyte),
            'DEPTH.?(TO|OF).?WATER|DTW|WATER.?DEPTH')
        THEN 'computed'
        ELSE 'measured'
    END AS elevation_source
FROM results r
JOIN samples s ON r.sample_id = s.sample_id
JOIN locations l ON s.location_id = l.location_id
WHERE REGEXP_MATCHES(UPPER(r.analyte),
    'DEPTH.?(TO|OF).?WATER|DTW|WATER.?DEPTH'
    || '|WATER.?(TABLE|LEVEL|SURFACE).?ELEV|GW.?ELEV|GROUNDWATER.?ELEV'
    || '|STATIC.?WATER.?LEVEL|SWL'
    || '|POTENTIOMETRIC.?SURFACE'
);
