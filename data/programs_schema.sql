DROP TABLE IF EXISTS programs;

CREATE TABLE programs (
    id SERIAL PRIMARY KEY,
    school_id INTEGER NOT NULL,
    year VARCHAR(9) NOT NULL,
    cip_code VARCHAR(10),
    program_name TEXT,
    credential_level INTEGER,
    credential_name TEXT,
    earnings_median_1yr INTEGER,
    earnings_median_4yr INTEGER,
    debt_median NUMERIC,
    debt_monthly_payment NUMERIC,
    completion_rate NUMERIC,
    FOREIGN KEY (school_id, year) REFERENCES schools(id, year)
);

CREATE INDEX idx_programs_school_year ON programs(school_id, year);
CREATE INDEX idx_programs_year ON programs(year);
CREATE INDEX idx_programs_cip ON programs(cip_code);
CREATE INDEX idx_programs_name ON programs(program_name);
