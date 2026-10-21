"""
Load name-only school designation lists into school_designations (one row per school and
designation), e.g. "which QuestBridge partners have a Phi Beta Kappa chapter?".

Lists (scraped politely, 1 request/second, HTML cached in data/designations/html/):
- AACSB accredited business school (aacsb.edu, U.S. only)
- Phi Beta Kappa chapter (pbk.org chapter directory)
- Colleges That Change Lives (ctcl.org)
- QuestBridge college partner (questbridge.org)
- Posse Foundation partner (possefoundation.org)

Names are matched to schools with, in order: backend/designation_overrides.csv (hand-checked),
an exact name/alias match, the school's website domain (when the page links to it), or a
name that contains every word of the listed name (preferring the listed city or the main
campus). The linked website, when given, must agree. Every decision is written to
data/designations/matches.csv; names that could not be matched confidently are listed in
data/designations/unmatched.csv for review (add them to designation_overrides.csv).

Usage (from backend/):
    python load_designations.py
"""

import csv
import html
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from database import execute_sql, get_db_connection, get_latest_year
from school_lookup import normalize, resolve_school
from source_data import DATA_DIR, ROOT_DIR, replace_table

load_dotenv(ROOT_DIR / ".env")

OUT_DIR = DATA_DIR / "designations"
HTML_DIR = OUT_DIR / "html"
OVERRIDES = Path(__file__).resolve().parent / "designation_overrides.csv"
HEADERS = {"User-Agent": "CollegeCopilot data loader (student project)"}

AACSB = "AACSB accredited business school"
PBK = "Phi Beta Kappa chapter"
CTCL = "Colleges That Change Lives"
QUESTBRIDGE = "QuestBridge college partner"
POSSE = "Posse Foundation partner"

AACSB_URL = "https://www.aacsb.edu/accredited?countries=united-states&page={page}"
PBK_URL = ("https://www.pbk.org/chapters-associations/chapter-directory?searchmode=anyword"
           "&fullsearchtext=&chapterfilter=&stateprovincefilter=&page={page}")
CTCL_URL = "https://ctcl.org/category/college-profiles/"
QUESTBRIDGE_URL = "https://www.questbridge.org/partners/college-partners"
POSSE_URL = "https://www.possefoundation.org/supporting-scholars/college-university-partners"

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Puerto Rico": "PR", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Guam": "GU", "Virgin Islands": "VI", "U.S. Virgin Islands": "VI",
}
GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "pbk.org",
                   "icloud.com", "comcast.net", "me.com", "wordpress.com", "google.com", "wixsite.com",
                   "weebly.com", "squarespace.com"}


def fetch(url: str, name: str) -> str:
    """Page HTML from the cache, or downloaded (politely) and cached."""
    path = HTML_DIR / f"{name}.html"
    if not path.exists():
        time.sleep(1)
        response = requests.get(url, timeout=60, headers=HEADERS)
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(response.text, encoding="utf-8")
    return path.read_text(encoding="utf-8")


def clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def entry(name, url, city=None, state=None, website=None) -> dict:
    return {"name": clean(name), "url": url, "city": city, "state": state, "website": website}


def scrape_aacsb() -> list[dict]:
    entries, page, last = [], 1, 1
    while page <= last:
        text = fetch(AACSB_URL.format(page=page), f"aacsb_us_{page}")
        last = max([last] + [int(p) for p in re.findall(r'data-pagination-page="(\d+)"', text)])
        for href, body in re.findall(r'<a class="member-search-result" href="([^"]+)"(.*?)</a>', text, re.S):
            title = re.search(r'member-search-result__title">(.*?)</div>', body, re.S)
            location = re.search(r'member-search-result__location">(.*?)</div>', body, re.S)
            parts = [p.strip() for p in clean(location.group(1)).split(",")] if location else []
            if not title or not parts or parts[-1] != "United States":
                continue
            state = STATES.get(parts[-2]) if len(parts) >= 3 else None
            entries.append(entry(title.group(1), "https://www.aacsb.edu" + href,
                                 city=parts[0] if len(parts) >= 3 else None, state=state))
        page += 1
    return entries


def scrape_pbk() -> list[dict]:
    entries, page, last = [], 1, 1
    while page <= last:
        url = PBK_URL.format(page=page)
        text = fetch(url, f"pbk_{page}")
        if page == 1:
            last = max(int(p) for p in re.findall(r">\s*(\d+)\s*</a>", text) if int(p) < 200)
        for card in re.findall(r'<div class="search-result-card">(.*?)</div>', text, re.S):
            name = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.S)
            if not name:
                continue
            links = re.findall(r'href="\s*(https?://[^"\s]+)', card) + re.findall(r"[\w.+-]+@([\w.-]+)", card)
            website = next((link for link in links if domain(link)), None)
            entries.append(entry(name.group(1), url, website=website))
        page += 1
    return entries


def scrape_ctcl() -> list[dict]:
    text = fetch(CTCL_URL, "ctcl")
    return [entry(name, url) for url, name in
            re.findall(r'<h2 class="entry-title"><a href="([^"]+)"[^>]*>(.*?)</a></h2>', text, re.S)]


def scrape_questbridge() -> list[dict]:
    text = fetch(QUESTBRIDGE_URL, "questbridge")
    found = re.findall(r'href="(https://www\.questbridge\.org/partners/college-partners/[^"]+)">\s*'
                       r"<span[^>]*>(.*?)</span>", text, re.S)
    return [entry(name, url) for url, name in found]


def scrape_posse() -> list[dict]:
    text = fetch(POSSE_URL, "posse")
    found = re.findall(r'<a href="([^"]*)" class="c-school__link[^"]*"[^>]*>(.*?)</a>', text, re.S)
    return [entry(name, POSSE_URL, website=website) for website, name in found]


LISTS = [
    (AACSB, scrape_aacsb), (PBK, scrape_pbk), (CTCL, scrape_ctcl),
    (QUESTBRIDGE, scrape_questbridge), (POSSE, scrape_posse),
]


# --- matching -------------------------------------------------------------------------

def domain(url: str | None) -> str | None:
    """Registrable domain of a URL or email domain, e.g. 'campus.albion.edu' -> 'albion.edu'."""
    if not url:
        return None
    host = urlparse(url if "//" in url else "//" + url).hostname or ""
    labels = host.lower().removeprefix("www.").split(".")
    if len(labels) < 2:
        return None
    result = ".".join(labels[-2:])
    return None if result in GENERIC_DOMAINS else result


def school_domains() -> dict[str, set[int]]:
    rows = execute_sql("SELECT id, school_url FROM schools WHERE year = %s", (get_latest_year(),))
    by_domain: dict[str, set[int]] = {}
    for row in rows:
        d = domain(row["school_url"])
        if d:
            by_domain.setdefault(d, set()).add(row["id"])
    return by_domain


BUSINESS_PREFIX = re.compile(r"^[^,]*\b(School|College|Division|Business|Management)\b[^,]*,\s*(.+)$")
STOPWORDS = {"the", "of", "at", "and", "in", "a"}


def core_name(name: str) -> str:
    """Drop a business-school prefix: 'Eller College of Management, The University of Arizona'."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # San José -> San Jose
    found = BUSINESS_PREFIX.match(name)
    core = found.group(2) if found and re.search(r"University|College|Institute", found.group(2)) else name
    core = re.sub(r"^The ", "", core)
    core = re.sub(r"^U\.S\. ", "United States ", core)
    return re.sub(r" of PA$", " of Pennsylvania", core)


def name_variants(name: str) -> list[str]:
    core = core_name(name)
    variants = [name, core, core.replace("&", " "), re.sub(r",\s*", "-", core),
                re.sub(r"\s+at\s+", "-", core), core.split(" - ")[0]]
    return list(dict.fromkeys(variants))


def tokens(text: str) -> set[str]:
    return set(normalize(text.replace("&", " ")).split()) - STOPWORDS


def candidates_for(name: str) -> list[dict]:
    best: dict[int, dict] = {}
    for variant in name_variants(name):
        for c in resolve_school(variant, limit=10):
            if c["id"] not in best or c["score"] > best[c["id"]]["score"]:
                best[c["id"]] = c
    return sorted(best.values(), key=lambda c: -c["score"])


def same_city(candidate: dict, item: dict) -> bool:
    return bool(item["city"]) and normalize(candidate["city"] or "") == normalize(item["city"])


def match(item: dict, by_domain: dict[str, set[int]]) -> tuple[dict | None, str, list[dict]]:
    """(chosen candidate or None, method, candidates). When the list links to the school's
    website, a name match on a school with a different website is rejected."""
    candidates = candidates_for(item["name"])
    if item["state"]:
        candidates = [c for c in candidates if c["state"] == item["state"]]
    domain_ids = by_domain.get(domain(item["website"]) or "", set())
    if domain_ids:
        hits = [c for c in candidates if c["id"] in domain_ids and c["score"] >= 0.5]
        if len(hits) == 1:
            return hits[0], "domain", candidates
        if any(c["id"] in domain_ids for c in candidates):
            candidates = [c for c in candidates if c["id"] in domain_ids]

    exact = [c for c in candidates if c["score"] == 1.0]
    if len(exact) > 1:
        exact = [c for c in exact if same_city(c, item)] or exact
    if len(exact) == 1:
        return exact[0], "exact", candidates

    # Every word of the listed name appears in the school's name, e.g. "Kutztown University"
    # -> "Kutztown University of Pennsylvania", "University of Pittsburgh" -> "...-Pittsburgh Campus".
    wanted = tokens(core_name(item["name"]))
    containing = [c for c in candidates if wanted <= tokens(c["name"])]
    for prefer in (lambda c: same_city(c, item), lambda c: "main campus" in c["name"].lower()):
        if len(containing) > 1:
            containing = [c for c in containing if prefer(c)] or containing
    if len(containing) == 1:
        return containing[0], "contained", candidates
    return None, "", candidates


def load_overrides() -> dict[tuple[str, str], list[int]]:
    """(designation, source name) -> school ids (space-separated in the file; a blank list
    means the listed institution has no matching school and is skipped)."""
    if not OVERRIDES.exists():
        return {}
    with OVERRIDES.open(newline="", encoding="utf-8") as handle:
        return {(r["designation"], r["source_name"]): [int(i) for i in r["school_id"].split()]
                for r in csv.DictReader(handle)}


def main():
    overrides = load_overrides()
    by_domain = school_domains()
    names = {r["id"]: r["name"] for r in
             execute_sql("SELECT id, name FROM schools WHERE year = %s", (get_latest_year(),))}
    rows, decisions, unmatched = {}, [], []
    for designation, scrape in LISTS:
        items = scrape()
        print(f"{designation}: {len(items)} entries ({len({i['name'] for i in items})} names)")
        for item in items:
            key = (designation, item["name"])
            candidates = []
            if key in overrides:
                chosen = [{"id": i, "name": names.get(i), "score": ""} for i in overrides[key]]
                method = "override" if chosen else "override: skip"
            else:
                best, method, candidates = match(item, by_domain)
                chosen = [best] if best else []
            missing = [c["id"] for c in chosen if c["name"] is None]
            if missing:
                raise ValueError(f"designation_overrides.csv: unknown school_id {missing} for {key}")
            decisions.append({
                "designation": designation, "source_name": item["name"],
                "city": item["city"] or "", "state": item["state"] or "", "website": item["website"] or "",
                "school_id": " ".join(str(c["id"]) for c in chosen),
                "matched_name": "; ".join(c["name"] for c in chosen),
                "score": chosen[0]["score"] if chosen else "", "method": method or "unmatched",
                "candidates": "" if chosen else "; ".join(
                    f"{c['id']} {c['name']} ({c['city']}, {c['state']}) {c['score']}" for c in candidates[:4]),
            })
            for c in chosen:
                rows.setdefault((c["id"], designation), (c["id"], designation, item["url"], item["name"]))
            if not chosen and method != "override: skip":
                unmatched.append(decisions[-1])

    fields = list(decisions[0].keys())
    for path, data in ((OUT_DIR / "matches.csv", decisions), (OUT_DIR / "unmatched.csv", unmatched)):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(data)
    print(f"Unmatched: {len(unmatched)} (see {OUT_DIR / 'unmatched.csv'})")

    with get_db_connection() as conn:
        replace_table(
            conn, "school_designations",
            [("school_id", "INTEGER NOT NULL", "IPEDS UNITID, joins to schools.id"),
             ("designation", "TEXT NOT NULL",
              f"One of: '{AACSB}', '{PBK}', '{CTCL}', '{QUESTBRIDGE}', '{POSSE}'"),
             ("source_url", "TEXT", "Page the designation was taken from"),
             ("matched_name", "TEXT", "Institution name as written on the source list")],
            sorted(rows.values()),
            "School memberships and recognitions from public lists, one row per school and designation: "
            "AACSB business school accreditation, Phi Beta Kappa chapters, Colleges That Change Lives, "
            "QuestBridge college partners and Posse Foundation partners (as listed on each organization's "
            "website in October 2026). A school missing here does not have that designation",
            {"name": "AACSB, Phi Beta Kappa, CTCL, QuestBridge and Posse Foundation websites",
             "url": "https://www.aacsb.edu/accredited", "year": "2026"},
        )
        with conn.cursor() as cursor:
            cursor.execute("CREATE INDEX ON school_designations (designation)")
        conn.commit()


if __name__ == "__main__":
    main()
