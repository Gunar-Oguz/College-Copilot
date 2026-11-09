"""
Print a personal access link without the email round trip (e.g. for yourself as the owner, or
for someone you invite directly). Uses the same signed tokens as the app (backend/access.py).

    ACCESS_SECRET=... python scripts/issue_access_link.py someone@example.com --name "Ada" --days 30
    python scripts/issue_access_link.py you@example.com --secrets .streamlit/secrets.toml
"""

import argparse
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import access  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--name", default="")
    parser.add_argument("--days", type=int, default=access.ACCESS_TTL_SEC // 86400)
    parser.add_argument("--secrets", type=Path, help="read ACCESS_SECRET and APP_URL from this secrets.toml")
    args = parser.parse_args()
    settings = tomllib.loads(args.secrets.read_text()) if args.secrets else {}
    secret = settings.get("ACCESS_SECRET") or os.getenv("ACCESS_SECRET")
    app_url = settings.get("APP_URL") or os.getenv("APP_URL") or "http://localhost:8501"
    token = access.make_token(access.ACCESS, args.email, secret, args.days * 86400, name=args.name)
    print(access.link(app_url, "access", token))


if __name__ == "__main__":
    main()
