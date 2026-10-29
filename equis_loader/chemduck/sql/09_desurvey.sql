-- ChemDuck Schema: Materialized Desurvey Grid
-- Phase 9: High-fidelity borehole desurveying via dense fine-grid + tangent SLERP
--
-- ============================================================================
-- WHY THIS EXISTS
-- ============================================================================
-- The naive approach to "where is sample S in 3D space?" is:
--   1. Run minimum-curvature (MC) on the downhole survey stations to get (x,y,z)
--      at each station.
--   2. Linearly interpolate between stations to land on the sample's depth.
--
-- That linear-between-stations step is wrong for sparse surveys (which is the
-- common case in environmental / exploration boreholes — survey stations every
-- 30+ ft, not every 1 ft). The true MC path between two stations is a circular
-- arc; chord-interpolating it underestimates the curvature.
--
-- The standard fix, used by all serious commercial software (Leapfrog, Vulcan,
-- Datamine, etc.) is:
--   1. Pick a fine measured-depth (md) grid, typically 1 ft / 0.3 m.
--   2. At each fine-grid md, interpolate the borehole's *tangent direction*
--      between bracketing survey stations along the shortest path on the unit
--      sphere (great-circle / SLERP). This is the orientation of the borehole
--      at that md.
--   3. Run MC on this dense sequence of (md, dip, az) tuples. Because the
--      segments are now tiny, MC-station-coords and true MC-along-arc are
--      indistinguishable to many decimal places.
--   4. Linearly interpolate sample coords from this dense grid. Error is now
--      bounded by the grid step, on the order of millimeters for 1 ft spacing.
--
-- ============================================================================
-- A NOTE ON QUATERNIONS
-- ============================================================================
-- Many references describe step 2 as "SLERP the orientation quaternions." That
-- terminology is correct but heavier than this problem needs.
--
-- A quaternion encodes a full 3D rotation (3 degrees of freedom: yaw + pitch +
-- ROLL around the borehole axis). The MC algorithm does not use roll at all —
-- it only consumes the tangent direction (which way the hole is pointing).
-- That tangent is a 2-DOF unit vector on the sphere.
--
-- When you SLERP two quaternions that differ only in their tangent direction
-- (no roll between them), the result is mathematically identical to doing
-- great-circle SLERP directly on the tangent unit vectors. The quaternion
-- machinery would be redundant. So this implementation works directly with
-- tangent vectors:
--
--   tangent unit vector at a station with (inc, az) where
--   inc = angle from vertical-down (so inc=0 is straight down) and
--   az  = clockwise from grid north:
--     t = ( sin(inc)*sin(az), sin(inc)*cos(az), -cos(inc) )
--
--   z component is -cos(inc) because we use POSITIVE-UP elevation,
--   so a downward-pointing tangent has negative z.
--
-- The great-circle SLERP between two unit tangents t1 and t2 at parameter
-- u in [0,1] is:
--
--                sin((1-u)*omega) * t1   +   sin(u*omega) * t2
--   t(u)  =     -----------------------------------------------
--                                  sin(omega)
--
--   where omega = acos( t1 . t2 ) is the angle between them.
--
-- When omega -> 0 (consecutive stations point nearly the same way) the formula
-- degenerates to a 0/0 limit. The limit is linear interpolation of the two
-- vectors, which is what NLERP gives. We guard with a small-angle threshold.
--
-- ============================================================================
-- CONVENTIONS (matches sql/01_tables.sql comments and Leapfrog)
-- ============================================================================
--   dip     in downhole_survey is angle from horizontal, NEGATIVE = downward.
--           -90 = vertical down; -85 = 5 degrees off vertical.
--           We convert to inclination-from-vertical via inc = radians(90 + dip)
--           so a vertical hole has inc = 0.
--   azimuth in downhole_survey is degrees clockwise from grid (x,y) north.
--   x, y    are grid coordinates in the project's CRS.
--   z       is POSITIVE-UP elevation. A sample at depth 100 ft in a vertical
--           hole with elev_ground = 200 has z = 100.
--
-- ============================================================================
-- 9.1 desurvey_config - tunable parameters for grid generation
-- ============================================================================
-- Single-row config table. To change the grid step, UPDATE this row and call
-- refresh_desurvey() (or re-run this script).

CREATE TABLE IF NOT EXISTS desurvey_config (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    grid_step DOUBLE NOT NULL DEFAULT 1.0,  -- in the project's depth units
    CHECK (singleton = TRUE)
);

INSERT OR IGNORE INTO desurvey_config (singleton, grid_step)
VALUES (TRUE, 1.0);

-- ============================================================================
-- 9.2 desurveyed_stations_fine - materialized fine-grid desurvey table
-- ============================================================================
-- One row per (location_id, md) on the configured fine grid. Holds the
-- desurveyed coordinates and the tangent direction at that md.
--
-- Re-running this CREATE OR REPLACE TABLE statement is the "refresh" operation.
-- The Python helper refresh_desurvey() in tests/desurvey.py simply executes
-- this file. Safe to call repeatedly; idempotent.

CREATE OR REPLACE TABLE desurveyed_stations_fine AS
WITH cfg AS (
    SELECT grid_step FROM desurvey_config
),
-- ----------------------------------------------------------------------------
-- Per-location depth extent: the deeper of (deepest survey station,
-- deepest sample bottom_depth), rounded UP to a grid multiple. NO buffer
-- past observed data -- consumers plotting borehole traces should never
-- see manufactured depth that no real datum supports.
--
-- Locations with no survey are excluded from this table entirely (samples
-- there use the vertical fallback in v_sample_xyz).
--
-- If samples appear deeper than the deepest survey station, the grid still
-- extends to cover them, and those rows are flagged extrapolated=TRUE
-- (tangent held from the last survey station). The trace is real where
-- the data is real; the extrapolation flag lets consumers exclude the
-- held-tangent portion if they want a strictly-surveyed trace.
-- ----------------------------------------------------------------------------
loc_extent AS (
    SELECT
        ds.location_id,
        MAX(ds.depth) AS survey_max_md,
        COALESCE(
            (SELECT MAX(COALESCE(s.bottom_depth, s.depth, s.top_depth))
             FROM samples s
             WHERE s.location_id = ds.location_id),
            0.0
        ) AS sample_max_depth,
        GREATEST(
            MAX(ds.depth),
            COALESCE(
                (SELECT MAX(COALESCE(s.bottom_depth, s.depth, s.top_depth))
                 FROM samples s
                 WHERE s.location_id = ds.location_id),
                0.0
            )
        ) AS observed_max_md
    FROM downhole_survey ds
    GROUP BY ds.location_id
),
loc_ceiling AS (
    SELECT
        le.location_id,
        le.observed_max_md,
        CEIL(le.observed_max_md / cfg.grid_step) * cfg.grid_step AS ceiling_md,
        cfg.grid_step
    FROM loc_extent le CROSS JOIN cfg
),
-- ----------------------------------------------------------------------------
-- Build a per-location fine-grid of md values from 0 to ceiling_md, step.
-- DuckDB's range() generates [0, n) as bigints; we scale to doubles.
-- ----------------------------------------------------------------------------
grid AS (
    SELECT
        lc.location_id,
        lc.grid_step,
        lc.ceiling_md,
        lc.observed_max_md,
        CAST(g.range AS DOUBLE) * lc.grid_step AS md
    FROM loc_ceiling lc,
         range(0, CAST(lc.ceiling_md / lc.grid_step AS BIGINT) + 1) AS g
),
-- ----------------------------------------------------------------------------
-- Convert survey stations to tangent unit vectors.
--   inc = radians(90 + dip)  so dip=-90 (vertical down) -> inc=0
--   az  = radians(azimuth)
--   tangent t = (sin(inc)*sin(az), sin(inc)*cos(az), -cos(inc))
-- The -cos(inc) on z is because z is positive-up and the borehole is
-- generally pointing down.
-- ----------------------------------------------------------------------------
stations AS (
    SELECT
        ds.location_id,
        ds.depth AS station_md,
        sin(radians(90 + ds.dip)) * sin(radians(ds.azimuth)) AS tx,
        sin(radians(90 + ds.dip)) * cos(radians(ds.azimuth)) AS ty,
        -cos(radians(90 + ds.dip)) AS tz
    FROM downhole_survey ds
),
-- ----------------------------------------------------------------------------
-- For each fine-grid md, find the bracketing stations (md_lo <= md <= md_hi)
-- and the parameter u = (md - md_lo) / (md_hi - md_lo) in [0,1].
--
-- Edge cases:
--   md < first station md  -> hold the first station's tangent (u clamped to 0)
--   md > last station md   -> hold the LAST station's tangent (u clamped to 1)
--                              [this is the "extrapolate by holding last tangent"
--                              policy chosen for past-end extension]
-- ----------------------------------------------------------------------------
bracketed AS (
    SELECT
        g.location_id,
        g.md,
        g.grid_step,
        g.ceiling_md,
        g.observed_max_md,
        -- station_lo: greatest station_md <= g.md  (or first station if none)
        COALESCE(
            (SELECT s.station_md FROM stations s
             WHERE s.location_id = g.location_id AND s.station_md <= g.md
             ORDER BY s.station_md DESC LIMIT 1),
            (SELECT MIN(s.station_md) FROM stations s WHERE s.location_id = g.location_id)
        ) AS md_lo,
        -- station_hi: smallest station_md >= g.md  (or last station if none)
        COALESCE(
            (SELECT s.station_md FROM stations s
             WHERE s.location_id = g.location_id AND s.station_md >= g.md
             ORDER BY s.station_md ASC LIMIT 1),
            (SELECT MAX(s.station_md) FROM stations s WHERE s.location_id = g.location_id)
        ) AS md_hi
    FROM grid g
),
bracketed_tangents AS (
    SELECT
        b.location_id,
        b.md,
        b.grid_step,
        b.observed_max_md,
        b.md_lo,
        b.md_hi,
        s_lo.tx AS tx_lo, s_lo.ty AS ty_lo, s_lo.tz AS tz_lo,
        s_hi.tx AS tx_hi, s_hi.ty AS ty_hi, s_hi.tz AS tz_hi,
        -- u = fractional position in [0,1] within the bracketing segment.
        -- If md_lo == md_hi (md is exactly at a station, or outside the
        -- survey range so both fall back to the same station) we use 0.
        CASE
            WHEN b.md_hi = b.md_lo THEN 0.0
            WHEN b.md < b.md_lo THEN 0.0   -- before first station: hold first
            WHEN b.md > b.md_hi THEN 1.0   -- after last station: hold last
            ELSE (b.md - b.md_lo) / (b.md_hi - b.md_lo)
        END AS u,
        b.md > (SELECT MAX(station_md) FROM stations s WHERE s.location_id = b.location_id) AS is_extrapolated
    FROM bracketed b
    JOIN stations s_lo ON s_lo.location_id = b.location_id AND s_lo.station_md = b.md_lo
    JOIN stations s_hi ON s_hi.location_id = b.location_id AND s_hi.station_md = b.md_hi
),
-- ----------------------------------------------------------------------------
-- Great-circle SLERP between the two bracketing tangent vectors.
--
--   omega = acos(clamp(t_lo . t_hi, -1, 1))
--   t(u)  = ( sin((1-u)*omega) * t_lo + sin(u*omega) * t_hi ) / sin(omega)
--
-- When omega is very small (consecutive stations nearly aligned, OR md sits
-- outside the survey range so t_lo == t_hi), sin(omega) -> 0 and we'd divide
-- by zero. We fall back to NLERP: linear blend of the two vectors then
-- renormalize. For tiny omega, NLERP and SLERP agree to many decimal places.
--
-- The CLAMP on the dot product guards against floating-point drift pushing
-- it slightly outside [-1, 1], which would make acos() return NaN.
-- ----------------------------------------------------------------------------
slerped AS (
    SELECT
        bt.*,
        acos(LEAST(1.0, GREATEST(-1.0,
            bt.tx_lo * bt.tx_hi + bt.ty_lo * bt.ty_hi + bt.tz_lo * bt.tz_hi
        ))) AS omega
    FROM bracketed_tangents bt
),
tangents AS (
    SELECT
        s.location_id,
        s.md,
        s.grid_step,
        s.observed_max_md,
        s.is_extrapolated,
        s.omega,
        -- Compute the SLERP'd unit tangent. For omega < 1e-9 fall back to
        -- NLERP (linear blend) to avoid division by zero.
        CASE
            WHEN s.omega < 1e-9 THEN (1.0 - s.u) * s.tx_lo + s.u * s.tx_hi
            ELSE (sin((1.0 - s.u) * s.omega) * s.tx_lo + sin(s.u * s.omega) * s.tx_hi) / sin(s.omega)
        END AS tx,
        CASE
            WHEN s.omega < 1e-9 THEN (1.0 - s.u) * s.ty_lo + s.u * s.ty_hi
            ELSE (sin((1.0 - s.u) * s.omega) * s.ty_lo + sin(s.u * s.omega) * s.ty_hi) / sin(s.omega)
        END AS ty,
        CASE
            WHEN s.omega < 1e-9 THEN (1.0 - s.u) * s.tz_lo + s.u * s.tz_hi
            ELSE (sin((1.0 - s.u) * s.omega) * s.tz_lo + sin(s.u * s.omega) * s.tz_hi) / sin(s.omega)
        END AS tz
    FROM slerped s
),
-- Renormalize (NLERP branch can produce slightly non-unit vectors; SLERP
-- branch is unit by construction but rounding can drift it). Cheap insurance.
tangents_unit AS (
    SELECT
        t.*,
        sqrt(t.tx * t.tx + t.ty * t.ty + t.tz * t.tz) AS tnorm
    FROM tangents t
),
tangents_normalized AS (
    SELECT
        location_id, md, grid_step, observed_max_md, is_extrapolated,
        tx / NULLIF(tnorm, 0) AS tx,
        ty / NULLIF(tnorm, 0) AS ty,
        tz / NULLIF(tnorm, 0) AS tz
    FROM tangents_unit
),
-- ----------------------------------------------------------------------------
-- Now run minimum-curvature integration on the dense (md, tangent) sequence.
-- Pair each fine-grid row with the next via LEAD().
--
-- The MC per-segment displacement between two tangent vectors t_i and t_{i+1}
-- separated by dmd = md_{i+1} - md_i is:
--
--   beta = acos(t_i . t_{i+1})        (the dogleg angle)
--   rf   = (2/beta) * tan(beta/2)     (ratio factor; limit -> 1 as beta -> 0)
--   d    = (dmd / 2) * (t_i + t_{i+1}) * rf
--
-- For tiny segments (1 ft step, slowly turning hole) beta is microscopic and
-- rf is essentially 1, so this reduces to the trapezoidal rule: average
-- the two tangents and multiply by dmd. That is the regime we are designing
-- for — the whole point of the dense grid is to make beta tiny.
-- ----------------------------------------------------------------------------
paired AS (
    SELECT
        tn.location_id,
        tn.md,
        tn.is_extrapolated,
        tn.observed_max_md,
        tn.tx, tn.ty, tn.tz,
        LEAD(tn.md) OVER w AS md_next,
        LEAD(tn.tx) OVER w AS tx_next,
        LEAD(tn.ty) OVER w AS ty_next,
        LEAD(tn.tz) OVER w AS tz_next
    FROM tangents_normalized tn
    WINDOW w AS (PARTITION BY tn.location_id ORDER BY tn.md)
),
deltas AS (
    SELECT
        p.*,
        (p.md_next - p.md) AS dmd,
        acos(LEAST(1.0, GREATEST(-1.0,
            p.tx * p.tx_next + p.ty * p.ty_next + p.tz * p.tz_next
        ))) AS beta
    FROM paired p
    WHERE p.md_next IS NOT NULL
),
segment_displacements AS (
    SELECT
        d.location_id,
        d.md,
        d.dmd,
        -- rf -> 1 in the small-beta limit; guard the division
        CASE WHEN d.beta < 1e-9 THEN 1.0 ELSE (2.0 / d.beta) * tan(d.beta / 2.0) END AS rf,
        d.tx, d.ty, d.tz, d.tx_next, d.ty_next, d.tz_next
    FROM deltas d
),
segment_dxyz AS (
    SELECT
        sd.location_id,
        sd.md,
        (sd.dmd / 2.0) * (sd.tx + sd.tx_next) * sd.rf AS dx,
        (sd.dmd / 2.0) * (sd.ty + sd.ty_next) * sd.rf AS dy,
        (sd.dmd / 2.0) * (sd.tz + sd.tz_next) * sd.rf AS dz
    FROM segment_displacements sd
),
-- ----------------------------------------------------------------------------
-- Cumulative sum from collar. The displacement attributed to fine-grid row
-- with md = M is the integral from md=0 to md=M, which equals the sum of
-- all segment displacements whose starting md is strictly less than M.
-- ----------------------------------------------------------------------------
cumulative AS (
    SELECT
        tn.location_id,
        tn.md,
        tn.is_extrapolated,
        tn.tx, tn.ty, tn.tz,
        COALESCE(SUM(s.dx) OVER w, 0.0) AS cum_dx,
        COALESCE(SUM(s.dy) OVER w, 0.0) AS cum_dy,
        COALESCE(SUM(s.dz) OVER w, 0.0) AS cum_dz
    FROM tangents_normalized tn
    LEFT JOIN segment_dxyz s
        ON s.location_id = tn.location_id AND s.md = tn.md
    WINDOW w AS (
        PARTITION BY tn.location_id ORDER BY tn.md
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    )
)
SELECT
    c.location_id,
    c.md,
    l.x + c.cum_dx AS x,
    l.y + c.cum_dy AS y,
    COALESCE(l.elev_ground, 0.0) + c.cum_dz AS z,
    c.tx, c.ty, c.tz,
    c.is_extrapolated AS extrapolated,
    l.x AS collar_x,
    l.y AS collar_y,
    COALESCE(l.elev_ground, 0.0) AS collar_z
FROM cumulative c
JOIN locations l ON c.location_id = l.location_id
ORDER BY c.location_id, c.md;

-- Index for the per-sample lookup in v_sample_xyz.
CREATE INDEX IF NOT EXISTS idx_desurveyed_stations_fine_loc_md
    ON desurveyed_stations_fine(location_id, md);

-- ============================================================================
-- 9.3 v_desurvey_staleness - flag locations whose grid no longer covers samples
-- ============================================================================
-- The materialized table is built once and rebuilt only when
-- refresh_desurvey() is called. Since the grid extends only to the deepest
-- observed datum (no buffer), ANY new survey station or sample deeper than
-- the current materialized max immediately makes the location stale. This
-- view flags those cases for QA.
--
-- A non-empty result means: refresh_desurvey() should be called.

CREATE OR REPLACE VIEW v_desurvey_staleness AS
WITH cur_max AS (
    SELECT
        location_id,
        MAX(md) AS materialized_max_md
    FROM desurveyed_stations_fine
    GROUP BY location_id
),
needed AS (
    SELECT
        s.location_id,
        MAX(COALESCE(s.bottom_depth, s.depth, s.top_depth)) AS sample_max_depth
    FROM samples s
    GROUP BY s.location_id
),
survey_max AS (
    SELECT location_id, MAX(depth) AS survey_max_md
    FROM downhole_survey
    GROUP BY location_id
)
SELECT
    COALESCE(cm.location_id, n.location_id, sm.location_id) AS location_id,
    cm.materialized_max_md,
    n.sample_max_depth,
    sm.survey_max_md,
    CASE
        WHEN cm.materialized_max_md IS NULL AND sm.survey_max_md IS NOT NULL
            THEN 'never_built'
        WHEN cm.materialized_max_md IS NOT NULL
             AND GREATEST(COALESCE(n.sample_max_depth, 0), COALESCE(sm.survey_max_md, 0))
                 > cm.materialized_max_md
            THEN 'samples_below_ceiling'
        ELSE 'ok'
    END AS staleness
FROM cur_max cm
FULL OUTER JOIN needed n ON cm.location_id = n.location_id
FULL OUTER JOIN survey_max sm ON COALESCE(cm.location_id, n.location_id) = sm.location_id
WHERE
    -- Only surface rows that need attention
    (cm.materialized_max_md IS NULL AND sm.survey_max_md IS NOT NULL)
    OR (cm.materialized_max_md IS NOT NULL
        AND GREATEST(COALESCE(n.sample_max_depth, 0), COALESCE(sm.survey_max_md, 0))
            > cm.materialized_max_md);
