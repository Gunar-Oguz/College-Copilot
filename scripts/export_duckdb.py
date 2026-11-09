"""
Copy the tables the app reads from the local PostgreSQL database into one DuckDB file.

The DuckDB file is what the Streamlit demo queries (see streamlit_app.py and backend/database.py).
Only public data is exported: schools, programs, nicknames, ZIP codes and the source tables listed
in data_sources. User, billing and log tables (user_*, query_logs, feedback, usage_tracking,
stripe_events, school_photos) are never copied.

Besides the data, the file holds:
  - app_schema: each exported column with its PostgreSQL type and description, so the planner's
    prompt is the same on both backends;
  - the miles_between() macro, with the same formula and rounding as the PostgreSQL function.

Usage (needs psycopg2 and duckdb; reads the database named by DATABASE_URL or DB_* like the app):
    python scripts/export_duckdb.py --out /path/to/collegecopilot.duckdb
    python scripts/export_duckdb.py --out ... --programs-years 2024-25   # slimmer programs table

It prints the file size and SHA-256; copy the hash into data/duckdb_manifest.json.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import duckdb
import psycopg2
import psycopg2.extensions
import pyarrow as pa

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "backend"))

from database import MILES_BETWEEN_DUCKDB  # noqa: E402

CORE_TABLES = ["schools", "programs", "school_aliases", "zip_codes", "data_sources"]
NEVER_EXPORT = {"query_logs", "feedback", "usage_tracking", "stripe_events", "school_photos"}
# Columns the app never reads and the planner never sees.
DROP_COLUMNS = {
    "data_sources": {"loaded_at"},
    "programs": {"id"},  # surrogate key; programs are identified by (school_id, year, cip_code, credential_level)
}
# Rows are stored in this order, so DuckDB's min/max zone maps skip most row groups for year filters.
ORDER_BY = {"schools": "year, id", "programs": "year, school_id, cip_code, credential_level"}

PG_TO_DUCKDB = {
    "integer": "INTEGER", "bigint": "BIGINT", "smallint": "SMALLINT",
    "numeric": "DOUBLE", "double precision": "DOUBLE", "real": "DOUBLE",
    "text": "VARCHAR", "character varying": "VARCHAR", "character": "VARCHAR",
    "boolean": "BOOLEAN", "date": "DATE",
    "timestamp without time zone": "TIMESTAMP", "timestamp with time zone": "TIMESTAMP",
}
ARROW = {"INTEGER": pa.int32(), "BIGINT": pa.int64(), "SMALLINT": pa.int16(), "DOUBLE": pa.float64(),
         "VARCHAR": pa.string(), "BOOLEAN": pa.bool_(), "DATE": pa.date32(), "TIMESTAMP": pa.timestamp("us")}
BATCH_ROWS = 200_000

# numeric -> float while exporting (DuckDB stores these columns as DOUBLE).
_FLOAT = psycopg2.extensions.new_type(
    psycopg2.extensions.DECIMAL.values, "NUMERIC_AS_FLOAT", lambda v, _c: float(v) if v is not None else None)


def pg_connect():
    psycopg2.extensions.register_type(_FLOAT)
    if url := os.getenv("DATABASE_URL"):
        return psycopg2.connect(url)
    return psycopg2.connect(dbname=os.getenv("DB_NAME", "collegecopilot"), user=os.getenv("DB_USER", os.getenv("USER")),
                            host=os.getenv("DB_HOST", "localhost"), password=os.getenv("DB_PASSWORD") or None,
                            port=os.getenv("DB_PORT", "5432"))


def query(pg, sql, params=None):
    with pg.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def export_tables(pg) -> list[str]:
    sources = [r[0] for r in query(pg, "SELECT table_name FROM data_sources ORDER BY table_name")]
    tables = list(dict.fromkeys(CORE_TABLES + sources))
    bad = [t for t in tables if t in NEVER_EXPORT or t.startswith("user_")]
    if bad:
        raise SystemExit(f"refusing to export private tables: {bad}")
    return tables


def columns_of(pg, table: str) -> list[dict]:
    rows = query(pg, """
        SELECT c.column_name, c.data_type, c.ordinal_position,
               col_description(format('%%I', c.table_name)::regclass, c.ordinal_position),
               obj_description(format('%%I', c.table_name)::regclass, 'pg_class')
        FROM information_schema.columns c
        WHERE c.table_schema = 'public' AND c.table_name = %s ORDER BY c.ordinal_position""", (table,))
    drop = DROP_COLUMNS.get(table, set())
    return [{"name": r[0], "pg_type": r[1], "position": r[2], "note": r[3], "table_note": r[4],
             "duck_type": PG_TO_DUCKDB[r[1]]} for r in rows if r[0] not in drop]


def copy_table(pg, duck, table: str, columns: list[dict], where: str = "") -> int:
    names = [c["name"] for c in columns]
    col_sql = ", ".join(f'"{n}"' for n in names)
    definitions = ", ".join(f'"{c["name"]}" {c["duck_type"]}' for c in columns)
    duck.execute(f'CREATE TABLE "{table}" ({definitions})')
    schema = pa.schema([(c["name"], ARROW[c["duck_type"]]) for c in columns])
    order = f" ORDER BY {ORDER_BY[table]}" if table in ORDER_BY else ""
    total = 0
    with pg.cursor(name=f"export_{table}") as cur:
        cur.itersize = BATCH_ROWS
        cur.execute(f'SELECT {col_sql} FROM "{table}"{where}{order}')
        while batch := cur.fetchmany(BATCH_ROWS):
            arrays = [pa.array([row[i] for row in batch], type=schema.field(i).type) for i in range(len(names))]
            chunk = pa.Table.from_arrays(arrays, schema=schema)  # noqa: F841 (read by DuckDB's replacement scan)
            duck.execute(f'INSERT INTO "{table}" SELECT * FROM chunk')
            total += len(batch)
    return total


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="DuckDB file to create (overwritten)")
    parser.add_argument("--programs-years", nargs="*", default=None,
                        help="only export these programs years (default: all years)")
    parser.add_argument("--compression", default="auto",
                        help="'auto' (default) lets DuckDB pick per column among zstd, dictionary, FSST, ALP and "
                             "bit-packing (30 MB); 'zstd' forces zstd everywhere (77 MB)")
    args = parser.parse_args()

    out: Path = args.out
    for path in (out, out.with_suffix(out.suffix + ".wal")):
        path.unlink(missing_ok=True)
    started = time.monotonic()
    pg = pg_connect()
    pg.set_session(readonly=True)
    duck = duckdb.connect(str(out))
    if args.compression != "auto":
        duck.execute(f"SET force_compression = '{args.compression}'")

    schema_rows = []
    for table in export_tables(pg):
        columns = columns_of(pg, table)
        where = ""
        if table == "programs" and args.programs_years:
            years = ", ".join("'" + y.replace("'", "") + "'" for y in args.programs_years)
            where = f" WHERE year IN ({years})"
        count = copy_table(pg, duck, table, columns, where)
        print(f"{table:<32} {count:>10,} rows  {len(columns):>3} columns")
        schema_rows += [(table, c["name"], c["pg_type"], c["position"], c["note"], c["table_note"]) for c in columns]

    duck.execute("CREATE TABLE app_schema (table_name VARCHAR, column_name VARCHAR, data_type VARCHAR, "
                 "ordinal_position INTEGER, note VARCHAR, table_note VARCHAR)")
    duck.executemany("INSERT INTO app_schema VALUES (?, ?, ?, ?, ?, ?)", schema_rows)
    duck.execute(MILES_BETWEEN_DUCKDB)
    duck.execute("CHECKPOINT")
    duck.close()
    pg.close()

    size = out.stat().st_size
    digest = sha256(out)
    print(f"\nWrote {out} in {time.monotonic() - started:.0f}s")
    print(json.dumps({"file": out.name, "bytes": size, "mb": round(size / 1e6, 1), "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
