-- ChemDuck Schema: optional geometry
-- Needs the spatial extension; ChemDuck's core does not, and nothing in it
-- depends on this file. load_schema reads only sql/*.sql, so this file is
-- never loaded with the core: a consumer with spatial loaded runs it on top
-- (tools/load_data.py load_spatial).
--
-- locations keeps x and y as plain doubles; the CRS they are in is the
-- project's (project.crs), when one is recorded.

-- ============================================================================
-- v_locations_geom - A point per located location
-- ============================================================================

CREATE OR REPLACE VIEW v_locations_geom AS
SELECT
    l.location_id,
    l.x,
    l.y,
    p.crs,
    ST_Point(l.x, l.y) AS geom
FROM locations l
LEFT JOIN project p ON p.project_name = l.project
WHERE l.x IS NOT NULL AND l.y IS NOT NULL;

-- ============================================================================
-- v_location_areas_geom / v_increments_geom - ISM areas and increments
-- ============================================================================
-- docs/ism-and-uncertainty-spec.md §4.1, §4.4: a DU's or SU's polygon (from
-- location_geometries' WKB, in project.crs) with its stored area and its
-- parent DU; and each increment as a point, with its sample and hole.

CREATE OR REPLACE VIEW v_location_areas_geom AS
SELECT
    g.location_id,
    l.parent_location_id,
    l.loc_type,
    g.area,
    g.area_units,
    p.crs,
    ST_GeomFromWKB(g.geom_wkb) AS geom
FROM location_geometries g
JOIN locations l ON l.location_id = g.location_id
LEFT JOIN project p ON p.project_name = l.project
WHERE g.geom_wkb IS NOT NULL;

CREATE OR REPLACE VIEW v_increments_geom AS
SELECT
    i.increment_id,
    i.location_id,
    i.sample_id,
    i.hole_id,
    i.increment_no,
    i.coord_method,
    i.status,
    i.inside_area,
    i.top_depth,
    i.bottom_depth,
    i.depth_units,
    ST_Point(i.x, i.y) AS geom
FROM sample_increments i
WHERE i.x IS NOT NULL AND i.y IS NOT NULL;
