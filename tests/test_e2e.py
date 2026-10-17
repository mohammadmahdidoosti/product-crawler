"""End-to-end run against a fake local shop: queue -> search -> crawl -> dedupe -> export."""
import json
import os
import tempfile
from unittest import mock
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from crawler import pipeline as P
from crawler.config import load_config
from crawler.io import FILES, read_csv

FIX = Path(__file__).parent / "fixtures"
PRODUCT = (FIX / "woo_product.html").read_text(encoding="utf-8")
STORE = (FIX / "store_api.json").read_text(encoding="utf-8")


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: str, ctype="text/html; charset=utf-8", code=200):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/robots.txt":
            return self._send("User-agent: *\nDisallow: /private/\n", "text/plain")
        if u.path.startswith("/wp-json/wc/store/v1/products"):
            base = f"http://127.0.0.1:{self.server.server_port}"
            items = json.loads(STORE.replace("https://valve.example.ir", base))
            term = q.get("search", [""])[0]
            items = [i for i in items if any(w in i["name"] for w in term.split())]
            return self._send(json.dumps(items, ensure_ascii=False), "application/json")
        if u.path == "/" and "s" in q:
            return self._send('<ul class="products"><li class="product">'
                              '<a class="woocommerce-LoopProduct-link" href="/product/yd101/">'
                              'درایو تک فاز YD101</a></li>'
                              '<li class="product"><a class="woocommerce-LoopProduct-link" '
                              'href="/private/hidden/">درایو مخفی تک فاز</a></li></ul>')
        if u.path == "/product/yd101/":
            return self._send(PRODUCT)
        if u.path == "/":
            return self._send("<html><body>تماس: 02188776655</body></html>")
        return self._send("not found", code=404)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def cfg(self):
        cfg = load_config()
        cfg["_data_dir"] = self.tmp
        (self.tmp / "cache").mkdir(exist_ok=True)
        (self.tmp / "runs").mkdir(exist_ok=True)
        tax = pd.DataFrame([
            {"ID": "1", "Level 1 (دسته اصلی)": "تجهیزات تابلو برق", "Level 2 (زیردسته سطح ۱)": "درایو",
             "Level 3 (زیردسته سطح ۲)": "", "Level 4 (زیردسته سطح ۳)": "",
             "Full Breadcrumb Path": "تجهیزات تابلو برق > درایو", "Parent_ID": "",
             "Is_Inferred_Parent": "False", "Site_Product_Count (تعداد محصول)": "3", "Review_Priority (اولویت بررسی)": ""},
            {"ID": "2", "Level 1 (دسته اصلی)": "تجهیزات تابلو برق", "Level 2 (زیردسته سطح ۱)": "درایو",
             "Level 3 (زیردسته سطح ۲)": "درایو تک فاز", "Level 4 (زیردسته سطح ۳)": "",
             "Full Breadcrumb Path": "تجهیزات تابلو برق > درایو > درایو تک فاز", "Parent_ID": "1",
             "Is_Inferred_Parent": "False", "Site_Product_Count (تعداد محصول)": "0", "Review_Priority (اولویت بررسی)": ""},
            {"ID": "3", "Level 1 (دسته اصلی)": "شیرآلات", "Level 2 (زیردسته سطح ۱)": "شیر برقی",
             "Level 3 (زیردسته سطح ۲)": "شیر برقی 24 ولت", "Level 4 (زیردسته سطح ۳)": "",
             "Full Breadcrumb Path": "شیرآلات > شیر برقی > شیر برقی 24 ولت", "Parent_ID": "",
             "Is_Inferred_Parent": "False", "Site_Product_Count (تعداد محصول)": "0", "Review_Priority (اولویت بررسی)": ""},
        ])
        tp = self.tmp / "tax.csv"
        tax.to_csv(tp, index=False, encoding="utf-8-sig")
        cfg["taxonomy"] = {"file": str(tp), "exclude_level1": []}
        cfg["routes"] = {"default": "iran"}
        cfg["sources"] = {
            "iran_sites": [
                {"domain": f"127.0.0.1:{self.srv.server_port}", "base_url": self.base, "type": "woo_search",
                 "level1": ["تجهیزات تابلو برق"]},
                {"domain": f"127.0.0.1:{self.srv.server_port}", "base_url": self.base, "type": "woo_store_api",
                 "level1": ["شیرآلات"], "seller_name": "شیرآلات نمونه"},
            ],
            "foreign_sites": [],
        }
        cfg["http"] = {**cfg["http"], "delay_seconds": 0}
        cfg["fx"] = {"provider": "manual", "manual_rate_toman": 100000, "rate_type": "free"}
        return cfg

    def test_pipeline(self):
        cfg, run = self.cfg(), "e2e"
        P.step_queue(cfg, run)
        q = read_csv(self.tmp / "runs" / run / FILES["queue"])
        self.assertEqual(sorted(q["taxonomy_id"]), ["2", "3"])  # leaves with no product only

        P.step_search(cfg, run)
        hits = read_csv(self.tmp / "runs" / run / FILES["hits"])
        # drive page + a robots-blocked page (woo_search), valve (store API); the valve's
        # coil is dropped by its title before its page is fetched
        self.assertEqual(len(hits), 3, hits[["url", "found_by"]])
        pre = read_csv(self.tmp / "runs" / run / "02_prefiltered.csv")
        self.assertEqual(list(pre["title"]), ["بوبین شیر برقی"])

        P.step_crawl(cfg, run)
        c = read_csv(self.tmp / "runs" / run / FILES["crawled"])
        self.assertEqual(len(c), 2)
        drive = c[c["taxonomy_id"] == "2"].iloc[0]
        self.assertEqual(drive["price_toman"], "12000000")
        self.assertEqual(drive["price_usd"], "120.0")
        self.assertEqual(drive["brand"], "یولیکو")
        self.assertEqual(drive["claims_representative"], "yes")
        valve = c[c["taxonomy_id"] == "3"].iloc[0]
        self.assertEqual(valve["seller_name"], "شیرآلات نمونه")
        self.assertEqual(valve["seller_phones"], "02188776655")
        self.assertEqual(valve["extracted_by"], "woo_store_api")

        failed = read_csv(self.tmp / "runs" / run / "03_failed.csv")
        self.assertEqual(list(failed["reason"]), ["http blocked/none"])  # robots.txt

        site = self.tmp / "site.csv"
        pd.DataFrame([{"ID": "501", "SKU": "", "Name": "درایو 1 اسب تک فاز YD101-00P7-T2S یولیکو",
                       "Regular price": "11000000", "Categories": ""}]).to_csv(site, index=False, encoding="utf-8-sig")
        # AI filter with a fake Claude reply: the coil is not a "24V solenoid valve"
        def fake_ask(self_, system, user):
            names = [p["name"] for p in json.loads(user.split("Products:\n", 1)[1])]
            return json.dumps([{"i": i, "relevant": "بوبین" not in n, "confidence": 0.9,
                                "reason": "قطعه است نه شیر" if "بوبین" in n else "", "brand": "", "model": ""}
                               for i, n in enumerate(names)], ensure_ascii=False)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch("crawler.llm.LLM.ask", fake_ask):
            P.step_filter(cfg, run)
        self.assertEqual(len(read_csv(self.tmp / "runs" / run / FILES["rejected"])), 0)

        P.step_dedupe(cfg, run, str(site))
        upd = read_csv(self.tmp / "runs" / run / FILES["updates"])
        new = read_csv(self.tmp / "runs" / run / FILES["new"])
        self.assertEqual(list(upd["site_product_id"]), ["501"])
        self.assertEqual(len(new), 1)

        P.step_export(cfg, run)
        woo = read_csv(self.tmp / "runs" / run / FILES["woo"])
        self.assertEqual(len(woo), 1)
        valve_row = woo.iloc[0]
        self.assertEqual(valve_row["Regular price"], "1850000")
        self.assertEqual(valve_row["Categories"], "شیرآلات > شیر برقی > شیر برقی 24 ولت")
        self.assertEqual(valve_row["Published"], "0")
        self.assertIn("یونی", valve_row["Description"])
        up = read_csv(self.tmp / "runs" / run / "06_woo_update_prices.csv")
        self.assertEqual(up.iloc[0]["ID"], "501")

        # re-running a step skips finished rows instead of duplicating them
        P.step_crawl(cfg, run)
        self.assertEqual(len(read_csv(self.tmp / "runs" / run / FILES["crawled"])), 2)
        print("\n" + P.stats(cfg, run))


if __name__ == "__main__":
    unittest.main()
