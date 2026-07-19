DROP TABLE IF EXISTS programs;
DROP TABLE IF EXISTS schools;

CREATE TABLE schools (
    id INTEGER NOT NULL,
    year VARCHAR(9) NOT NULL,
    name TEXT,
    city TEXT,
    state VARCHAR(2),
    zip VARCHAR(10),
    school_url TEXT,
    ownership INTEGER,
    admission_rate NUMERIC,
    sat_average INTEGER,
    act_median INTEGER,
    cost_avg_net_price NUMERIC,
    cost_net_price_income_0_30k NUMERIC,
    cost_net_price_income_30_48k NUMERIC,
    cost_net_price_income_48_75k NUMERIC,
    cost_net_price_income_75_110k NUMERIC,
    cost_net_price_income_110k_plus NUMERIC,
    completion_rate NUMERIC,
    earnings_median_10yr NUMERIC,
    student_size INTEGER,
    undergraduate_size INTEGER,
    pell_grant_rate NUMERIC,
    federal_loan_rate NUMERIC,
    avg_loan_amount NUMERIC,
    retention_rate NUMERIC,
    transfer_rate NUMERIC,
    first_gen_rate NUMERIC,
    part_time_rate NUMERIC,
    avg_age NUMERIC,
    female_share NUMERIC,
    online_only BOOLEAN,
    highest_degree INTEGER,
    carnegie_class INTEGER,
    earnings_6yr NUMERIC,
    earnings_8yr NUMERIC,
    loan_repayment_rate NUMERIC,
    PRIMARY KEY (id, year)
);

CREATE INDEX idx_schools_year ON schools(year);
CREATE INDEX idx_schools_state ON schools(state);
CREATE INDEX idx_schools_name ON schools(name);
