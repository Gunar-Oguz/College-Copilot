"""Resolve a place (ZIP code or city) to coordinates for distance searches."""

import re

from database import execute_sql

ZIP_SQL = """
SELECT zip, city, state, latitude, longitude, population
FROM zip_codes WHERE zip = %(zip)s
"""

# A city's location is the population-weighted center of its ZIP codes.
CITY_SQL = """
SELECT city, state,
       round(sum(latitude * coalesce(population, 1)) / sum(coalesce(population, 1)), 5) AS latitude,
       round(sum(longitude * coalesce(population, 1)) / sum(coalesce(population, 1)), 5) AS longitude,
       sum(population) AS population
FROM zip_codes
WHERE lower(city) = %(city)s AND (%(state)s IS NULL OR state = %(state)s)
GROUP BY city, state
ORDER BY sum(population) DESC NULLS LAST
LIMIT %(limit)s
"""

STATE_SUFFIX = re.compile(r"^(.*?)[,\s]+([A-Za-z]{2})$")
# USPS city names spell these out ("Saint Louis", "Fort Worth", "Mount Vernon").
ABBREVIATIONS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}


def normalize_city(city: str) -> str:
    words = re.sub(r"[.,]", " ", city.lower()).split()
    return " ".join(ABBREVIATIONS.get(w, w) for w in words)


def resolve_location(place: str, state: str | None = None, limit: int = 5) -> list[dict]:
    """Return candidate locations, largest population first.

    place is a 5-digit ZIP code or a city name, optionally ending in a state code
    ("Austin, TX"). Each candidate has a label, latitude, longitude and population.
    """
    place = place.strip()
    zip_match = re.fullmatch(r"(\d{5})(-\d{4})?", place)
    if zip_match:
        rows = execute_sql(ZIP_SQL, {"zip": zip_match.group(1)})
        for row in rows:
            row["label"] = f"{row.pop('zip')} ({row['city']}, {row['state']})"
    else:
        city = place
        suffix = STATE_SUFFIX.match(place)
        if suffix and not state:
            city, state = suffix.groups()
        rows = execute_sql(
            CITY_SQL,
            {"city": normalize_city(city), "state": state.upper() if state else None, "limit": limit},
        )
        for row in rows:
            row["label"] = f"{row['city']}, {row['state']}"
    for row in rows:
        row["latitude"] = float(row["latitude"])
        row["longitude"] = float(row["longitude"])
        row["population"] = int(row["population"]) if row["population"] is not None else None
    return rows
