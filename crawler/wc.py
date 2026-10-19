"""WooCommerce / WordPress REST client for direct publishing (keys only from the environment).

WC_URL, WC_KEY, WC_SECRET   WooCommerce REST API key (read/write) of the caravan-crawler user
WP_USER, WP_APP_PASSWORD    the same user's WordPress application password (media uploads)
data/wp_categories.csv      taxonomy ID -> WordPress product_cat term ID
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


class WC:
    def __init__(self, timeout: int = 60):
        self.base = (os.environ.get("WC_URL") or "").rstrip("/")
        self.key, self.secret = os.environ.get("WC_KEY", ""), os.environ.get("WC_SECRET", "")
        self.wp_user = os.environ.get("WP_USER", "caravan-crawler")
        self.wp_pass = os.environ.get("WP_APP_PASSWORD", "")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "CaravanSanatCrawler/1.0 (+https://caravansanat.com)"})

    def ok(self) -> bool:
        return bool(self.base and self.key and self.secret)

    def _wc(self, method: str, path: str, **kw):
        return self.s.request(method, f"{self.base}/wp-json/wc/v3/{path.lstrip('/')}", auth=(self.key, self.secret),
                              timeout=self.timeout, **kw)

    def _wp(self, method: str, path: str, **kw):
        return self.s.request(method, f"{self.base}/wp-json/wp/v2/{path.lstrip('/')}",
                              auth=(self.wp_user, self.wp_pass.replace(" ", "")), timeout=self.timeout, **kw)

    def test(self) -> dict:
        """Read-only checks: the site answers, the WooCommerce key works, the app password works."""
        out = {}

        def probe(name, fn):
            try:
                r = fn()
                body = r.text[:200]
                try:
                    j = r.json()
                    if name == "wc_products":
                        body = f"{len(j)} product(s) read" if isinstance(j, list) else body
                    if name == "wp_me":
                        body = f"user: {j.get('name') or j.get('slug')}" if isinstance(j, dict) and r.ok else body
                    if name == "wc_category":
                        body = f"category: {j.get('name')}" if isinstance(j, dict) and r.ok else body
                except ValueError:
                    pass
                out[name] = {"status": r.status_code, "info": body}
            except requests.RequestException as e:
                out[name] = {"status": None, "info": str(e)[:200]}

        probe("site", lambda: self.s.get(f"{self.base}/wp-json/", timeout=self.timeout))
        probe("wc_products", lambda: self._wc("GET", "products", params={"per_page": 1}))
        cats = category_map()
        some = next(iter(cats.values()), None)
        if some:
            probe("wc_category", lambda: self._wc("GET", f"products/categories/{some}"))
        probe("wp_me", lambda: self._wp("GET", "users/me", params={"context": "edit"}))
        return out


def category_map() -> dict[str, str]:
    import pandas as pd
    p = ROOT / "data" / "wp_categories.csv"
    if not p.exists():
        return {}
    d = pd.read_csv(p, dtype=str).fillna("")
    return {a: b for a, b in zip(d["ID"], d["wp_id"]) if b}
