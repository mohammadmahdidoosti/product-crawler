"""Promising shops: counts from past crawls on disk, read-only crawlability checks."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from crawler import shops as S
from crawler.config import load_config
from crawler.io import CRAWLED_COLS, FILES, write_csv

TEH = ZoneInfo("Asia/Tehran")
IMG = json.dumps(["https://x/a.jpg"])
WORDS = ["دریل", "فرز", "پمپ", "شیر", "کمپرسور", "اره", "سنباده", "جوشکاری", "هویه", "پیچ‌گوشتی", "بکس", "آچار"]
BRANDS = ["رونیکس", "توسن", "بوش", "ماکیتا", "اکتیو", "هیوندای", "دیوالت", "کرون", "پوکا", "اسکیل", "آروا", "نووا"]


def page(dom, i, *, price="1500000", img=IMG, kind="iran", leaf="شیر برقی", seller=None, phone=""):
    return {"source_domain": dom, "source_type": kind, "url": f"https://{dom}/product/p{i}",
            "name": f"{WORDS[i % 12]} {BRANDS[(i + len(dom)) % 12]} مدل {dom[:2].upper()}{i * 37 + len(dom):04d}", "model": "", "brand": "",
            "price_toman": price, "image_urls": img, "taxonomy_id": "T1", "breadcrumb": f"ابزار > {leaf}",
            "seller_name": seller or "", "seller_phones": phone, "crawled_at": "2026-10-01T10:00:00"}


class FakeShop(BaseHTTPRequestHandler):
    methods: list = []
    robots: dict = {}

    def log_message(self, *a):
        pass

    def _any(self):
        FakeShop.methods.append(self.command)
        self.send_response(405)
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _any

    def _send(self, code, body, ctype="text/html", extra=None):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        FakeShop.methods.append("GET")
        host = self.headers.get("Host", "").split(":")[0]
        u = urlparse(self.path)
        if u.path == "/robots.txt":
            return self._send(200, FakeShop.robots.get(host, "User-agent: *\nAllow: /\n"), "text/plain")
        if u.path == "/wp-json/wc/store/v1/products" and host.startswith("api"):
            return self._send(200, json.dumps([{"id": 1, "name": "x"}]), "application/json", {"X-WP-Total": "1234"})
        if u.path == "/" and host.startswith("woo"):
            return self._send(200, "<html class='woocommerce'>shop</html>")
        return self._send(404, "nope")


class Shops(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeShop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"DATA_DIR": str(self.data), "WC_URL": ""})
        self.env.start()
        self.cfg = load_config()
        self.cfg["_data_dir"] = self.data
        self.cfg["site_catalog"] = {**self.cfg.get("site_catalog", {}), "file": None}
        self.cfg["http"] = {**self.cfg["http"], "delay_seconds": 0, "retries": 0, "timeout_seconds": 5,
                            "dead_days": 0}
        self.cfg["sources"] = {**self.cfg["sources"], "iran_sites": [{"domain": "woo.test", "type": "woo_search"}]}
        self.cfg["shops"] = {"min_missing": 3, "check_top": 30}
        FakeShop.methods, FakeShop.robots = [], {"blocked.test": "User-agent: *\nDisallow: /\n"}
        # every test host resolves to the fake shop (http on its port)
        self.p_scheme = mock.patch.object(S, "SCHEME", "http")
        self.p_scheme.start()
        import requests
        real = requests.Session.request

        def to_fake(sess, method, url, *a, **kw):
            p = urlparse(url)
            if p.hostname and p.hostname.endswith(".test"):
                kw.setdefault("headers", {})
                kw["headers"] = {**(kw["headers"] or {}), "Host": p.hostname}
                url = url.replace(f"{p.scheme}://{p.netloc}", f"http://127.0.0.1:{self.port}", 1)
            return real(sess, method, url, *a, **kw)
        self.p_req = mock.patch.object(requests.Session, "request", to_fake)
        self.p_req.start()
        self._fixture()

    def tearDown(self):
        self.p_req.stop()
        self.p_scheme.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _fixture(self):
        runs = self.data / "runs"
        r1, r2 = runs / "batch-1", runs / "batch-2"
        r1.mkdir(parents=True)
        r2.mkdir(parents=True)
        # api.test: 10 pages in batch-1. 2 on the site, 6 new (4 priced with photo), 1 in review, 1 nowhere.
        a = [page("api.test", i, seller="فروشگاه ای‌پی‌آی", phone="0211 | 0212") for i in range(10)]
        a[5]["price_toman"] = ""
        a[6]["image_urls"] = "[]"
        crawled1 = pd.DataFrame(a)
        # woo.test (already a configured source): 5 pages in both runs, all new
        w = [page("woo.test", i, leaf="پمپ آب") for i in range(5)]
        # digikala.com: a marketplace, 4 new pages
        dk = [page("digikala.com", i) for i in range(4)]
        # blocked.test: robots says no; 3 new
        bl = [page("blocked.test", i) for i in range(3)]
        # mostly.test: 3 new + 6 unknown (only in the pool) -> doubt
        mo = [page("mostly.test", i) for i in range(9)]
        # few.test: only 2 new -> below min_missing
        fw = [page("few.test", i) for i in range(2)]
        # caravansanat.com: our own site, never listed
        own = [page("caravansanat.com", i) for i in range(5)]
        write_csv(pd.DataFrame(a + w + dk + bl + mo[:3] + fw + own), r1 / FILES["crawled"], CRAWLED_COLS)
        write_csv(pd.DataFrame(w), r2 / FILES["crawled"], CRAWLED_COLS)
        # the pool has the unknown pages of mostly.test and one more of api.test (also unknown)
        write_csv(pd.DataFrame(mo[3:] + [page("api.test", 99)]), self.data / "crawl_pool.csv")
        u = lambda d, i: f"https://{d}/product/p{i}"           # noqa: E731
        # dedupe output of batch-1: two api.test pages matched a site product; merged rows keep "urls"
        write_csv(pd.DataFrame([{"url": u("api.test", 0), "urls": f"{u('api.test', 0)} | {u('api.test', 1)}",
                                 "site_product_id": "77", "match_score": "1.0"}]), r1 / FILES["updates"])
        write_csv(pd.DataFrame([{"url": u("api.test", 2), "urls": u("api.test", 2)}]), r1 / FILES["review"])
        new = [u("api.test", i) for i in range(3, 9)] + [u("woo.test", i) for i in range(5)] + \
              [u("digikala.com", i) for i in range(4)] + [u("blocked.test", i) for i in range(3)] + \
              [u("mostly.test", i) for i in range(3)] + [u("few.test", i) for i in range(2)]
        write_csv(pd.DataFrame([{"url": x, "urls": x} for x in new]), r1 / FILES["new"])
        # api.test p9 was not in any dedupe file -> unknown (filtered out, not selected, ...)

    def rows(self, name="2026-10-27_1000"):
        return {r["domain"]: r for r in S.rows_of(self.cfg, name).to_dict("records")}

    def test_counts_and_ranking(self):
        res = S.report(self.cfg, check=False, now=datetime(2026, 10, 27, 10, 0, tzinfo=TEH))
        rows = self.rows()
        self.assertNotIn("caravansanat.com", rows)                     # our own site
        self.assertNotIn("few.test", rows)                             # below min_missing
        a = rows["api.test"]
        self.assertEqual(int(a["seen"]), 11)                           # 10 pages + 1 in the pool
        self.assertEqual(int(a["on_site"]), 2)                         # matched (url + merged urls)
        self.assertEqual(int(a["missing"]), 6)
        self.assertEqual(int(a["unknown"]), 3)                         # review, not in dedupe, pool only
        self.assertEqual(int(a["missing_priced_photo"]), 4)            # p5 no price, p6 no photo
        self.assertEqual(a["seller"], "فروشگاه ای‌پی‌آی")
        self.assertEqual(int(a["phones_count"]), 2)
        self.assertEqual(a["categories"].split(" | ")[0], "شیر برقی")
        w = rows["woo.test"]
        self.assertEqual((int(w["seen"]), int(w["missing"]), int(w["runs_count"])), (5, 5, 2))
        self.assertEqual(w["source"], "configured")
        self.assertEqual(w["categories"], "پمپ آب")
        self.assertEqual(res["counts"]["source"], 1)
        # ranked by missing with price + photo first
        order = list(S.rows_of(self.cfg, "2026-10-27_1000")["domain"])
        self.assertEqual(order[0], "woo.test")
        self.assertEqual(order[1], "api.test")
        self.assertEqual(FakeShop.methods, [])                         # check=False: no request at all

    def test_unknown_and_matched_never_missing(self):
        S.report(self.cfg, check=False, now=datetime(2026, 10, 27, 10, 0, tzinfo=TEH))
        for r in self.rows().values():
            self.assertEqual(int(r["seen"]), int(r["on_site"]) + int(r["missing"]) + int(r["unknown"]))
        m = self.rows()["mostly.test"]
        self.assertEqual((int(m["missing"]), int(m["unknown"])), (3, 6))
        self.assertEqual(m["doubt"], "yes")
        self.assertIn("معلوم نیست", m["note"])

    def test_site_export_uses_dedupe_matcher(self):
        # with a site export, pages are matched like the dedupe step: api.test p3 is on the site
        site = self.data / "site_export.csv"
        pd.DataFrame([{"ID": "5", "SKU": "", "Name": page("api.test", 3)["name"], "Regular price": "1"},
                      {"ID": "6", "SKU": "", "Name": "لوله پلی اتیلن ۲ اینچ", "Regular price": "1"}]).to_csv(
            site, index=False, encoding="utf-8-sig")
        self.cfg["site_catalog"]["file"] = str(site)
        S.report(self.cfg, check=False, now=datetime(2026, 10, 27, 10, 0, tzinfo=TEH))
        a = self.rows()["api.test"]
        self.assertEqual(int(a["on_site"]), 3)                         # 2 from the run + p3 by the matcher
        self.assertEqual(int(a["unknown"]), 2)                         # p2 (a run's review), p99 (review band)
        self.assertEqual(int(a["missing"]), 6)                         # p4-p9: not on the site export

    def test_aggregator_and_robots_doubt_and_get_only(self):
        res = S.report(self.cfg, check=True, now=datetime(2026, 10, 27, 10, 0, tzinfo=TEH))
        rows = self.rows()
        self.assertEqual(rows["digikala.com"]["doubt"], "yes")
        self.assertIn("بازارگاه", rows["digikala.com"]["note"])
        self.assertEqual(rows["digikala.com"]["checked"], "")          # marketplaces are not probed
        self.assertEqual(rows["blocked.test"]["robots_ok"], "no")
        self.assertEqual(rows["blocked.test"]["doubt"], "yes")
        self.assertEqual(rows["api.test"]["store_api"], "yes")
        self.assertEqual(rows["api.test"]["catalog_size"], "1234")
        self.assertEqual(rows["api.test"]["crawlable"], "yes")
        self.assertEqual((rows["woo.test"]["woo_search"], rows["woo.test"]["crawlable"]), ("yes", "yes"))
        self.assertEqual(rows["woo.test"]["doubt"], "")
        self.assertGreater(len(FakeShop.methods), 0)
        self.assertEqual(set(FakeShop.methods), {"GET"})                # nothing but reads
        self.assertEqual(res["counts"]["crawlable"], 2)
        self.assertEqual(S.rows_of(self.cfg, "2026-10-27_1000", "doubt")["domain"].tolist().count("digikala.com"), 1)

    def test_foreign_flagged(self):
        c = self.data / "runs" / "batch-3"
        c.mkdir()
        write_csv(pd.DataFrame([page("cn.test", i, kind="foreign") for i in range(4)]), c / FILES["crawled"], CRAWLED_COLS)
        write_csv(pd.DataFrame([{"url": f"https://cn.test/product/p{i}"} for i in range(4)]), c / FILES["new"])
        write_csv(pd.DataFrame([{"url": "https://zz.test/x"}]), c / FILES["updates"])
        S.report(self.cfg, check=True, now=datetime(2026, 10, 27, 10, 0, tzinfo=TEH))
        r = self.rows()["cn.test"]
        self.assertEqual((r["doubt"], r["checked"]), ("yes", ""))
        self.assertIn("خارجی", r["note"])

    def test_keeps_last_reports(self):
        self.cfg["shops"]["keep_reports"] = 2
        for h in (8, 9, 10):
            S.report(self.cfg, check=False, now=datetime(2026, 10, 27, h, 0, tzinfo=TEH))
        self.assertEqual(S.reports(self.cfg), ["2026-10-27_1000", "2026-10-27_0900"])

    def test_app_routes(self):
        import importlib
        import app
        A = importlib.reload(app)
        cfg = self.cfg
        with mock.patch.object(A, "build_cfg", lambda: cfg):
            A.JOBS["health"]._one(cfg, "shops", "shops", {"check": False})
            self.assertTrue(any("فروشگاه‌های مستعد" in x for x in A.JOBS["health"].logs))
            h = A.Handler.__new__(A.Handler)
            out = {}
            h._json = lambda obj, code=200: out.update(obj=obj, code=code)
            h._download = lambda data, name, ctype: out.update(data=data, name=name)
            h._authed = lambda: True
            name = S.reports(cfg)[0]
            for path in ("/api/shops", f"/api/shops/{name}/rows?filter=source&q=woo",
                         f"/api/shops/{name}/download?filter=doubt"):
                h.path = path
                out.clear()
                h.do_GET()
                if "rows?" in path:
                    self.assertEqual([r["domain"] for r in out["obj"]["rows"]], ["woo.test"])
                elif "download" in path:
                    self.assertTrue(out["data"].startswith("﻿".encode("utf-8")))
                    self.assertIn("digikala.com", out["data"].decode("utf-8"))
                else:
                    self.assertEqual(out["obj"]["latest"]["report"], name)
        self.assertEqual(A.JOBS["health"].lane, "health")
        with mock.patch.object(A.JOBS["health"], "start", return_value=None) as st:
            A.start_job("shops", "shops", {})
            st.assert_called_once()


if __name__ == "__main__":
    unittest.main()
