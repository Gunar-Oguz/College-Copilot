"""
The demo's DuckDB file: a local path (DUCKDB_PATH), or a download from a GitHub Release asset
(DB_URL) that is checked against the SHA-256 pinned in data/duckdb_manifest.json and cached on disk.
"""

import hashlib
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT_DIR / "data" / "duckdb_manifest.json"
DEFAULT_DB_URL = "https://github.com/Gunar-Oguz/College-Copilot/releases/download/data-v1/collegecopilot.duckdb"
DOWNLOAD_TIMEOUT_SEC = 120


class DataFileError(RuntimeError):
    pass


def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def default_cache_dir() -> Path:
    return Path(os.getenv("DUCKDB_CACHE_DIR") or Path(tempfile.gettempdir()) / "collegecopilot")


def download(url: str, target: Path, expected_sha256: str, opener=urllib.request.urlopen) -> Path:
    """Download to a temporary file, check the hash, then move it into place (never a partial file)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    logger.info("Downloading the database from %s", url)
    with opener(url, timeout=DOWNLOAD_TIMEOUT_SEC) as response, partial.open("wb") as out:
        for block in iter(lambda: response.read(1 << 20), b""):
            digest.update(block)
            out.write(block)
    if digest.hexdigest() != expected_sha256:
        partial.unlink(missing_ok=True)
        raise DataFileError(f"Downloaded database has SHA-256 {digest.hexdigest()}, expected {expected_sha256}")
    partial.replace(target)
    return target


def ensure_database(get=os.getenv, cache_dir: Path | None = None, opener=urllib.request.urlopen) -> Path:
    """Path of a verified database file.

    get(name) reads settings (Streamlit secrets, then the environment). A local DUCKDB_PATH is used as
    is (for development with a fresh export); a download is always checked against the manifest.
    """
    if local := get("DUCKDB_PATH"):
        path = Path(local).expanduser()
        if not path.is_file():
            raise DataFileError(f"DUCKDB_PATH {path} does not exist (build it with scripts/export_duckdb.py)")
        return path
    expected = (get("DB_SHA256") or manifest()["sha256"]).lower()
    url = get("DB_URL") or DEFAULT_DB_URL
    target = (cache_dir or default_cache_dir()) / f"collegecopilot-{expected[:12]}.duckdb"
    if target.is_file():
        if sha256_of(target) == expected:
            return target
        target.unlink()
    return download(url, target, expected, opener=opener)
