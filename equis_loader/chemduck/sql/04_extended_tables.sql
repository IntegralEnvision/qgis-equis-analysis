-- ChemDuck Schema: Extended Tables
-- Additional tables to support application-layer functionality
-- Referenced from: doRiverMiles.py, doTimeSeries.py, doMapConcentrations.py, etc.

-- ============================================================================
-- River Systems and River Miles Support
-- ============================================================================
-- These tables support river mile-based analysis and charting
-- Referenced in doRiverMiles.py as ida_rivernames and ida_rivermile_data

-- River definitions table (replaces/extends ida_rivernames)
CREATE TABLE IF NOT EXISTS rivers (
    river_id INTEGER PRIMARY KEY,
    river_name VARCHAR NOT NULL,
    huc_code VARCHAR,              -- Hydrologic Unit Code
    start_mile DOUBLE,             -- Starting river mile marker
    end_mile DOUBLE,               -- Ending river mile marker
    description VARCHAR,
    active BOOLEAN DEFAULT TRUE
);

-- Location-to-river-mile mapping
-- This is a denormalized view of results with river mile context
-- In legacy system this was ida_rivermile_data
CREATE TABLE IF NOT EXISTS location_river_miles (
    location_id VARCHAR NOT NULL REFERENCES locations(location_id),
    river_id INTEGER REFERENCES rivers(river_id),
    river_mile DOUBLE NOT NULL,    -- Precise river mile marker for this location
    bank_side VARCHAR,             -- 'left', 'right', 'center', 'island'
    notes VARCHAR,
    PRIMARY KEY (location_id, river_id)
);

-- Index for efficient river mile queries
CREATE INDEX IF NOT EXISTS idx_location_river_miles_river
ON location_river_miles(river_id, river_mile);

-- ============================================================================
-- Key Locations Support
-- ============================================================================
-- User-defined key/reference locations for charts and reports
-- Referenced in doRiverMiles.py as ida_key_locations

CREATE TABLE IF NOT EXISTS key_locations (
    key_location_id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    location_id VARCHAR REFERENCES locations(location_id),
    river_mile DOUBLE,             -- Optional: river mile for key location marker
    significance VARCHAR,          -- 'reference', 'compliance_point', 'source', etc.
    description VARCHAR,
    display_order INTEGER,
    active BOOLEAN DEFAULT TRUE
);

-- ============================================================================
-- Project Major Events Support
-- ============================================================================
-- Significant events to display on time series charts
-- Referenced in doTimeSeries.py as ida_project_major_events

CREATE TABLE IF NOT EXISTS project_events (
    event_id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    event_date DATE NOT NULL,
    event_type VARCHAR,            -- 'remediation', 'spill', 'construction', 'regulatory'
    description VARCHAR,
    display_on_charts BOOLEAN DEFAULT TRUE,
    line_style VARCHAR DEFAULT 'dashed',  -- For chart rendering
    line_color VARCHAR DEFAULT 'red'
);

-- ============================================================================
-- Saved Queries/Filters Support
-- ============================================================================
-- Persistence of user query configurations
-- Referenced in multiple tools as ida_prj_queries

CREATE TABLE IF NOT EXISTS saved_queries (
    query_id INTEGER PRIMARY KEY,
    title VARCHAR NOT NULL,
    query_type VARCHAR NOT NULL,   -- 'RiverMiles', 'TimeSeries', 'BoxPlot', 'MapConcentrations'
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    output_file VARCHAR,           -- Path to saved chart/output
    sql_text VARCHAR,              -- The generated SQL query
    y_series VARCHAR,              -- Selected analytes (semicolon-delimited)
    locations VARCHAR,             -- Selected locations
    x_series VARCHAR,              -- X-axis field/values
    filter_text VARCHAR,           -- Human-readable filter description
    key_locations VARCHAR,         -- Selected key locations for display
    screening_levels VARCHAR,      -- Selected screening levels
    options_json VARCHAR           -- Additional options as JSON
);

-- ============================================================================
-- User-Defined Analyte Groups
-- ============================================================================
-- Custom groupings of analytes defined by users
-- Complements the analyte_group field in results table

CREATE TABLE IF NOT EXISTS user_analyte_groups (
    group_id INTEGER PRIMARY KEY,
    group_name VARCHAR NOT NULL UNIQUE,
    description VARCHAR,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_analyte_group_members (
    group_id INTEGER NOT NULL REFERENCES user_analyte_groups(group_id),
    analyte VARCHAR NOT NULL,
    display_order INTEGER,
    PRIMARY KEY (group_id, analyte)
);

-- ============================================================================
-- Chart/Report Configuration
-- ============================================================================
-- Persistent chart styling and configuration

CREATE TABLE IF NOT EXISTS chart_configurations (
    config_id INTEGER PRIMARY KEY,
    config_name VARCHAR NOT NULL,
    chart_type VARCHAR NOT NULL,   -- 'timeseries', 'boxplot', 'rivermile', 'scatter'
    config_json VARCHAR NOT NULL,  -- Full configuration as JSON
    is_default BOOLEAN DEFAULT FALSE,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
