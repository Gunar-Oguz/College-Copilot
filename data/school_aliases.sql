CREATE EXTENSION IF NOT EXISTS pg_trgm;

DROP TABLE IF EXISTS school_aliases;

-- Alternate names for schools. alias_norm is lowercased with punctuation
-- stripped (see school_lookup.normalize). One alias can map to several schools.
CREATE TABLE school_aliases (
    school_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (school_id, alias_norm)
);

CREATE INDEX idx_school_aliases_norm ON school_aliases(alias_norm);
CREATE INDEX idx_school_aliases_trgm ON school_aliases USING gin (alias_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_schools_name_trgm ON schools USING gin (lower(name) gin_trgm_ops);
