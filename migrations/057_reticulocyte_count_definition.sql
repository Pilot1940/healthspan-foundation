-- Migration 057: Add reticulocyte_count metric definition
-- Needed to ingest reticulocyte count from Jun 6 2026 Metropolis panel (2%)
-- Was blocked because authenticated role cannot INSERT into metric_definitions

INSERT INTO metric_definitions (
    name, display_name, category, data_type, unit,
    description, min_value, max_value, decimal_places,
    plausible_min, plausible_max, is_active
) VALUES (
    'reticulocyte_count',
    'Reticulocyte Count',
    'blood_count',
    'numeric',
    '%',
    'Percentage of immature red blood cells; reflects red cell production rate. RPI (Reticulocyte Productive Index) classifies anemia type.',
    0.5,   -- clinical ref min
    2.5,   -- clinical ref max
    1,
    0,     -- plausible min (can be near-zero in marrow suppression)
    15,    -- plausible max (can spike in hemolytic anemia / acute blood loss)
    true
)
ON CONFLICT (name) DO NOTHING;
