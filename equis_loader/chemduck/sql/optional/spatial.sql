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
