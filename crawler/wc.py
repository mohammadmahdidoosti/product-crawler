"""WooCommerce / WordPress REST client for direct publishing (keys only from the environment).

WC_URL, WC_KEY, WC_SECRET   WooCommerce REST API key (read/write) of the caravan-crawler user
WP_USER, WP_APP_PASSWORD    the same user's WordPress application password (media uploads)
assets/wp_categories.csv    taxonomy ID -> WordPress product_cat term ID
                            (a copy in DATA_DIR wins, so the map can be replaced without a deploy;
                            not kept under data/ itself: on Render the disk is mounted there and
                            hides whatever the repo has in that folder)
"""
from __future__ import annotations

import logging
import mimetypes
import os
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


class WCError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


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

    def can_upload(self) -> bool:
        return bool(self.base and self.wp_user and self.wp_pass)

    def _wc(self, method: str, path: str, **kw):
        return self.s.request(method, f"{self.base}/wp-json/wc/v3/{path.lstrip('/')}", auth=(self.key, self.secret),
                              timeout=self.timeout, **kw)

    def _wp(self, method: str, path: str, **kw):
        return self.s.request(method, f"{self.base}/wp-json/wp/v2/{path.lstrip('/')}",
                              auth=(self.wp_user, self.wp_pass.replace(" ", "")), timeout=self.timeout, **kw)

    # ------------------------------------------------------------------ writes

    def _call(self, fn, what: str, tries: int = 3):
        """One REST call with retries on network errors and 5xx/429; a clear error otherwise."""
        last = None
        for i in range(tries):
            try:
                r = fn()
            except requests.RequestException as e:
                last = WCError(f"{what}: {e}")
            else:
                if r.ok:
                    try:
                        return r.json()
                    except ValueError:
                        raise WCError(f"{what}: not JSON ({r.text[:150]})", r.status_code)
                msg = r.text[:300]
                try:
                    j = r.json()
                    msg = f"{j.get('code', '')}: {j.get('message', '')}".strip(": ") or msg
                except ValueError:
                    pass
                last = WCError(f"{what}: HTTP {r.status_code} {msg}", r.status_code)
                if r.status_code < 500 and r.status_code != 429:
                    raise last
            time.sleep(2 * (i + 1))
        raise last

    def find_by_sku(self, sku: str) -> dict | None:
        j = self._call(lambda: self._wc("GET", "products", params={"sku": sku, "status": "any", "per_page": 5}),
                       f"find {sku}")
        return next((p for p in j if p.get("sku") == sku), None) if isinstance(j, list) else None

    def create_product(self, data: dict) -> dict:
        return self._call(lambda: self._wc("POST", "products", json=data), "create product", tries=1)

    def update_product(self, pid: int, data: dict) -> dict:
        return self._call(lambda: self._wc("PUT", f"products/{pid}", json=data), f"update product {pid}")

    def list_brands(self) -> list[dict] | None:
        """All WooCommerce brand terms (WooCommerce 9.6+ 'Brands'); None if the site has no such endpoint."""
        out, page = [], 1
        while True:
            try:
                j = self._call(lambda: self._wc("GET", "products/brands", params={"per_page": 100, "page": page,
                                                                               "hide_empty": "false"}), "brands")
            except WCError as e:
                if e.status == 404:
                    return None
                raise
            if not isinstance(j, list) or not j:
                return out
            out += j
            if len(j) < 100:
                return out
            page += 1

    def products_of_brand(self, brand_id: int) -> list[dict]:
        """Every product (any status) carrying a brand; refuses if the site ignores the brand filter."""
        out, page = [], 1
        while True:
            j = self._call(lambda: self._wc("GET", "products", params={"brand": brand_id, "status": "any",
                                                                       "per_page": 100, "page": page}), "products of brand")
            if not isinstance(j, list) or not j:
                return out
            wrong = [p["id"] for p in j if brand_id not in [b.get("id") for b in p.get("brands") or []]]
            if wrong:
                raise WCError(f"the site ignored the brand filter (product {wrong[0]} lacks brand {brand_id})")
            out += j
            if len(j) < 100:
                return out
            page += 1

    def merge_brands(self, src: int, dst: int, dry_run: bool = True) -> dict:
        """Move every product of brand `src` to brand `dst`, then delete `src`."""
        terms = {t["id"]: t for t in (self.list_brands() or [])}
        if src not in terms or dst not in terms or src == dst:
            raise WCError(f"brand IDs not found or equal: {src} -> {dst}")
        prods = self.products_of_brand(src)
        rep = {"from": terms[src]["name"], "to": terms[dst]["name"], "products": [
            {"id": p["id"], "name": p.get("name"), "status": p.get("status")} for p in prods], "deleted": False}
        if dry_run:
            return rep
        for p in prods:
            ids = [b["id"] for b in p.get("brands") or [] if b.get("id") != src]
            ids = ids + [dst] if dst not in ids else ids
            self.update_product(p["id"], {"brands": [{"id": i} for i in ids]})
        left = self.products_of_brand(src)
        if left:
            raise WCError(f"{len(left)} product(s) still on brand {src}; it was not deleted")
        self._call(lambda: self._wc("DELETE", f"products/brands/{src}", params={"force": "true"}), f"delete brand {src}")
        rep["deleted"] = True
        log.info("brands merged: %s -> %s (%d products)", rep["from"], rep["to"], len(prods))
        return rep

    def create_brand(self, name: str) -> dict:
        try:
            return self._call(lambda: self._wc("POST", "products/brands", json={"name": name}), f"brand {name}", tries=1)
        except WCError as e:
            # a term with this name exists already (e.g. made a moment ago): WordPress returns its ID
            if e.status == 400 and "term_exists" in str(e):
                r = self._wc("POST", "products/brands", json={"name": name})
                tid = ((r.json() or {}).get("data") or {}).get("resource_id")
                if tid:
                    return {"id": tid, "name": name}
            raise

    def upload_media(self, path: Path, filename: str, alt: str = "") -> dict:
        ctype = mimetypes.guess_type(filename)[0] or "image/webp"
        data = path.read_bytes()
        j = self._call(lambda: self._wp("POST", "media", data=data, headers={
            "Content-Type": ctype, "Content-Disposition": f'attachment; filename="{filename}"'}),
            f"upload {filename}")
        if alt and j.get("id"):
            try:
                self._wp("POST", f"media/{j['id']}", json={"alt_text": alt, "title": alt})
            except requests.RequestException:
                pass
        return j

    # ------------------------------------------------------------------ checks

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
                    if name == "wp_media":
                        body = "application password ok (media library readable)" if r.ok else body
                    if name == "wp_me":
                        if isinstance(j, dict) and r.ok:
                            caps = j.get("capabilities") or {}
                            body = (f"user: {j.get('name') or j.get('slug')}, upload_files: "
                                    f"{bool(caps.get('upload_files'))}, edit_products: {bool(caps.get('edit_products'))}")
                    if name == "wc_brands":
                        body = (f"brands endpoint ok (total: {r.headers.get('X-WP-Total', '?')})"
                                if isinstance(j, list) and r.ok else body)
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
        out["category_map"] = {"status": 200 if cats else None, "info": f"{len(cats)} taxonomy IDs mapped"}
        some = next(iter(cats.values()), None)
        if some:
            probe("wc_category", lambda: self._wc("GET", f"products/categories/{some}"))
        probe("wc_brands", lambda: self._wc("GET", "products/brands", params={"per_page": 1}))
        # the media library with the application password (what photo uploads need); users/me is
        # often closed by the site's firewall, so it is not used for this check
        probe("wp_media", lambda: self._wp("GET", "media", params={"per_page": 1, "context": "edit"}))
        out["ok"] = all((v.get("status") or 0) == 200 for k, v in out.items() if isinstance(v, dict))
        log.info("WC_TEST %s", {k: v.get("status") for k, v in out.items() if isinstance(v, dict)})
        return out


_CAT_CACHE: dict = {"key": None, "map": {}}


def category_map() -> dict[str, str]:
    import pandas as pd
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    p = next((x for x in (data_dir / "wp_categories.csv", ROOT / "assets" / "wp_categories.csv") if x.exists()), None)
    if p is None:
        return {}
    key = (str(p), p.stat().st_mtime)
    if _CAT_CACHE["key"] != key:
        d = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
        _CAT_CACHE.update(key=key, map={a.strip(): b.strip() for a, b in zip(d["ID"], d["wp_id"]) if b.strip()})
    return _CAT_CACHE["map"]
