"""Stamp public/index.html with content hashes of app.js and styles.css.

    python scripts/stamp_assets.py          # rewrite index.html
    python scripts/stamp_assets.py --check  # exit 1 if the stamps are stale

The `?v=<hash>` query changes whenever an asset changes, so a browser can never pair
a fresh page with a stale cached script or stylesheet. tests/test_frontend.py fails
when the stamps are out of date.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "public"
ASSETS = ("app.js", "styles.css")


def asset_hash(name: str) -> str:
    return hashlib.sha256((PUBLIC / name).read_bytes()).hexdigest()[:10]


def stamped(html: str) -> str:
    for name in ASSETS:
        pattern = re.compile(r'(["\'])/' + re.escape(name) + r'(?:\?v=[0-9a-f]*)?\1')
        if not pattern.search(html):
            raise SystemExit(f"index.html does not reference /{name}")
        html = pattern.sub(lambda m: f"{m.group(1)}/{name}?v={asset_hash(name)}{m.group(1)}", html)
    return html


def main() -> int:
    page = PUBLIC / "index.html"
    html = page.read_text(encoding="utf-8")
    new = stamped(html)
    if "--check" in sys.argv:
        if new != html:
            print("index.html asset stamps are stale: run python scripts/stamp_assets.py")
            return 1
        print("index.html asset stamps are current")
        return 0
    if new != html:
        page.write_text(new, encoding="utf-8")
        print("index.html restamped")
    else:
        print("index.html already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
