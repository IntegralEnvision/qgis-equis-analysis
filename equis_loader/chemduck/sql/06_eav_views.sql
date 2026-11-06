-- ChemDuck Schema: EAV Attribute Views
-- Helper views for EAV table discovery.
-- Dynamic wide/full views are created by tools/eav_helpers.py refresh_eav_views()

-- ============================================================================
-- Helper views: distinct field names per EAV table
-- ============================================================================

CREATE OR REPLACE VIEW v_location_attribute_fields AS
SELECT DISTINCT field_name FROM location_attributes ORDER BY field_name;

CREATE OR REPLACE VIEW v_sample_attribute_fields AS
SELECT DISTINCT field_name FROM sample_attributes ORDER BY field_name;

CREATE OR REPLACE VIEW v_result_attribute_fields AS
SELECT DISTINCT field_name FROM result_attributes ORDER BY field_name;
