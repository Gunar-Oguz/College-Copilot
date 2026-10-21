"""
Campus photos from Wikimedia Commons, cached in school_photos.

Wikidata links colleges to their IPEDS id (property P1771) and to an image (P18) hosted on
Commons. The first profile view looks the school up, asks Commons for a 1200px thumbnail plus the
author and license (shown as attribution), and caches the result, including "no photo".
"""

import re
from html import unescape
from urllib.parse import unquote

import requests

from database import execute_sql, get_db_connection

USER_AGENT = "CollegeCopilot/1.0 (https://github.com/Gunar-Oguz/College-Copilot)"
TIMEOUT_SEC = 6
THUMB_WIDTH = 1200

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS school_photos (
    school_id INTEGER PRIMARY KEY,
    image_url TEXT,
    file_page_url TEXT,
    artist TEXT,
    license TEXT,
    fetched_at TIMESTAMP DEFAULT NOW()
)
"""

SPARQL = 'SELECT ?image WHERE {{ ?item wdt:P1771 "{unitid}" ; wdt:P18 ?image . }} LIMIT 1'


def ensure_photo_table() -> None:
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(SCHEMA_SQL)


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return unescape(re.sub(r"<[^>]+>", "", text)).strip() or None


def fetch_photo(unitid: int) -> dict:
    """Look up a school's Commons photo. Returns empty values when there is none."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    response = requests.get("https://query.wikidata.org/sparql", params={"query": SPARQL.format(unitid=unitid)},
                            headers=headers, timeout=TIMEOUT_SEC)
    response.raise_for_status()
    bindings = response.json()["results"]["bindings"]
    if not bindings:
        return {}
    file_name = unquote(bindings[0]["image"]["value"].rsplit("/", 1)[1])
    info = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={"action": "query", "titles": f"File:{file_name}", "prop": "imageinfo",
                "iiprop": "url|extmetadata", "iiurlwidth": THUMB_WIDTH, "format": "json"},
        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC,
    ).json()
    page = next(iter(info["query"]["pages"].values()))
    image = (page.get("imageinfo") or [{}])[0]
    meta = image.get("extmetadata", {})
    return {
        "image_url": image.get("thumburl") or image.get("url"),
        "file_page_url": image.get("descriptionurl"),
        "artist": _strip_html(meta.get("Artist", {}).get("value")),
        "license": _strip_html(meta.get("LicenseShortName", {}).get("value")),
    }


def get_photo(unitid: int) -> dict | None:
    """Cached photo for a school (fetching it once), or None if it has no photo."""
    cached = execute_sql("SELECT image_url, file_page_url, artist, license FROM school_photos WHERE school_id = %s",
                         (unitid,))
    if cached:
        return cached[0] if cached[0]["image_url"] else None
    try:
        photo = fetch_photo(unitid)
    except (requests.RequestException, KeyError, ValueError):
        return None  # try again next time
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO school_photos (school_id, image_url, file_page_url, artist, license) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (school_id) DO NOTHING",
            (unitid, photo.get("image_url"), photo.get("file_page_url"), photo.get("artist"), photo.get("license")),
        )
    return photo if photo.get("image_url") else None
