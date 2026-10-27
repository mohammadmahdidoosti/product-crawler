"""Catalog input: a supplier's price list -> variable products, against a fake WooCommerce."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from crawler import catalog as C
from crawler import catalog_publish as CP
from crawler.catalog_read import is_reversed, unreverse_line
from crawler.config import load_config


class Values(unittest.TestCase):
    def test_visual_order_text_is_put_back(self):
        self.assertTrue(is_reversed(["تسیل تمیق لدبم", "زیاس"]))
        self.assertFalse(is_reversed(["لیست قیمت مبدل", "سایز"]))
        self.assertEqual(unreverse_line("تمیق تسیل 3,400,000"), "3,400,000 لیست قیمت")
        self.assertEqual(unreverse_line("ددع20"), "20عدد")
        self.assertEqual(unreverse_line("2 mm"), "2 mm")                      # no Persian: untouched

    def test_prices(self):
        for raw, want in [("3,400,000", 3400000), ("9,200.000", 9200000), ("8500,000", 8500000),
                          ("11.000.000", 11000000), ("۱۲,۵۰۰,۰۰۰ تومان", 12500000), ("12.5", 12.5), ("abc", None)]:
            self.assertEqual(C.parse_price(raw), want, raw)
        self.assertTrue(C.looks_price("27,000,000"))
        self.assertFalse(C.looks_price("2mm"))
        self.assertFalse(C.looks_price("165"))

    def test_values(self):
        self.assertEqual(C.norm_value("2/5mm"), "2.5 mm")
        self.assertEqual(C.norm_value("0/7"), "0.7")
        self.assertEqual(C.norm_value("11/2", "اینچ", inch=True), "1 1/2 اینچ")
        self.assertEqual(C.norm_value("21/2", "اینچ", inch=True), "2 1/2 اینچ")
        self.assertEqual(C.norm_value("1/2 2", "اینچ", inch=True), "2 1/2 اینچ")    # '2 ½' read right to left
        self.assertEqual(C.norm_value("1\n1/4"), "1 1/4")
        self.assertEqual(C.norm_value("2mmسبک"), "2 mm سبک")
        self.assertEqual(C.norm_value("52", "cm"), "52 cm")
        self.assertEqual(C.header_unit("طول مبدل (cm)"), ("طول مبدل", "cm"))
        self.assertEqual(C.header_unit("قطر (میلی متر)"), ("قطر", "mm"))
        self.assertEqual(C.header_unit("سایز ورودی برحسب اینچ"), ("سایز ورودی", "اینچ"))

    def test_attributes_prefer_size_and_fewest_columns(self):
        rows = [{"سایز": "50", "ارتفاع": "100 cm", "قطر": "50 cm"}, {"سایز": "90", "ارتفاع": "100 cm", "قطر": "90 cm"},
                {"سایز": "90", "ارتفاع": "150 cm", "قطر": "90 cm"}]
        self.assertEqual(C.choose_attributes(["قطر", "ارتفاع", "سایز"], rows), ["سایز", "ارتفاع"])
        rows = [{"قطر بدنه": "38 cm", "ظرفیت": "40 KW"}, {"قطر بدنه": "38 cm", "ظرفیت": "60 KW"}]
        self.assertEqual(C.choose_attributes(["قطر بدنه", "ظرفیت"], rows), ["ظرفیت"])


class Tables(unittest.TestCase):
    st = {"brand": "گروسی", "attribute": "سایز"}

    def test_two_price_columns_are_two_products_and_per_foot_price_is_never_a_spec(self):
        t = {"page": 3, "title": "لیست قیمت نازل شوتر و ثقلی", "row_y": [1, 2, 3, 4],
             "rows": [["قیمت ثقلی", "قیمت شوتر", "سایز ورودی", "طول"],
                      ["2,800,000", "2,700,000", "1 اینچ", "20 cm"],
                      ["3,200,000", "2,900,000", "1 اینچ", "30 cm"],
                      ["3,600,000", "3,300,000", "1 اینچ", "40 cm"]]}
        ps = C.rule_products(t, self.st)
        self.assertEqual(sorted(p["family"] for p in ps), ["نازل ثقلی", "نازل شوتر"])
        shooter = next(p for p in ps if p["family"] == "نازل شوتر")
        self.assertEqual(shooter["attributes"], ["طول"])
        self.assertEqual(shooter["common"], {"سایز ورودی": "1 اینچ"})
        self.assertEqual([v["price_raw"] for v in shooter["variants"]], ["2,700,000", "2,900,000", "3,300,000"])
        t = {"page": 2, "title": "لیست قیمت مبدل", "rows": [
            ["قیمت کل (تومان)", "قیمت (تومان) برحسب هر فوت", "ظرفیت (KW)"],
            ["3,400,000", "190,000", "40"], ["4,600,000", "170,000", "60"]]}
        p = C.rule_products(t, self.st)[0]
        self.assertEqual([v["price_raw"] for v in p["variants"]], ["3,400,000", "4,600,000"])
        self.assertFalse(any("190,000" in json.dumps(v, ensure_ascii=False) for v in p["variants"]))

    def test_grouped_header_split_and_merged_cells(self):
        t = {"page": 11, "title": "لیست قیمت کرتین (مدل قوسی و مدل کبری)", "rows": [
            ["مدل کبری", None, None, "مدل قوسی", None, None],
            ["قیمت", "سایز ورودی (اینچ)", "ارتفاع", "قیمت", "سایز ورودی (اینچ)", "ارتفاع"],
            ["7,900,000", "11/2", "60cm", "7,800,000", "11/2", "60cm"],
            ["9,200,000", "2", "70 cm", "9,600,000", "2", "70 cm"]]}
        ps = {p["family"]: p for p in C.rule_products(t, self.st)}
        self.assertEqual(set(ps), {"کرتین (مدل کبری)", "کرتین (مدل قوسی)"})
        v = ps["کرتین (مدل قوسی)"]["variants"][0]
        self.assertEqual(v["price_raw"], "7,800,000")
        self.assertEqual({**v["attributes"], **v["specs"]}, {"سایز ورودی": "1 1/2 اینچ", "ارتفاع": "60 cm"})


class FakeWoo(BaseHTTPRequestHandler):
    products: dict = {}
    variations: dict = {}
    calls: list = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = u.path.split("/")
        if u.path.endswith("/products/brands"):
            return self._json([])
        if u.path.endswith("/variations"):
            return self._json(list(FakeWoo.variations.get(int(parts[-2]), {}).values()))
        if u.path.endswith("/products") and "sku" in q:
            return self._json([p for p in FakeWoo.products.values() if p["sku"] == q["sku"][0]])
        if parts[-1].isdigit():
            p = FakeWoo.products.get(int(parts[-1]))
            return self._json(p) if p else self._json({"code": "invalid"}, 404)
        return self._json([])

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        FakeWoo.calls.append(("POST", u.path, body))
        parts = u.path.split("/")
        if u.path.endswith("/products/brands"):
            return self._json({"id": 77, "name": body["name"]})
        if u.path.endswith("/variations/batch"):
            pid = int(parts[-3])
            vs = FakeWoo.variations.setdefault(pid, {})
            out = {"create": [], "update": []}
            for v in body.get("create") or []:
                vid = 1000 + sum(len(x) for x in FakeWoo.variations.values())
                vs[vid] = {**v, "id": vid}
                out["create"].append(vs[vid])
            for v in body.get("update") or []:
                vs[v["id"]].update(v)
                out["update"].append(vs[v["id"]])
            return self._json(out)
        if u.path.endswith("/products"):
            pid = 500 + len(FakeWoo.products)
            FakeWoo.products[pid] = {**body, "id": pid, "images": body.get("images") or []}
            return self._json(FakeWoo.products[pid], 201)
        return self._json({}, 404)

    def do_PUT(self):
        u = urlparse(self.path)
        body = self._body()
        FakeWoo.calls.append(("PUT", u.path, body))
        pid = int(u.path.split("/")[-1])
        FakeWoo.products[pid].update(body)
        return self._json(FakeWoo.products[pid])


def make_xlsx(path: Path, prices: list[str]):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "پمپ استخر"
    ws.append(["لیست قیمت پمپ تصفیه استخر"])
    ws.append(["سایز", "توان", "قیمت (تومان)"])
    for size, power, price in zip(["1 اینچ", "1 1/2 اینچ", "2 اینچ"], ["0.5 اسب", "1 اسب", "1.5 اسب"], prices):
        ws.append([size, power, price])
    wb.save(path)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "runs").mkdir()
        (d / "taxonomy.csv").write_text(
            "ID,Level 1,Level 2,Level 3,Full Breadcrumb,Parent_ID\n"
            "900,تجهیزات استخر,,,تجهیزات استخر,\n"
            "901,تجهیزات استخر,پمپ استخر,,تجهیزات استخر > پمپ استخر,900\n"
            "902,تجهیزات استخر,فیلتر شنی,,تجهیزات استخر > فیلتر شنی,900\n", encoding="utf-8-sig")
        (d / "wp_categories.csv").write_text("ID,wp_id\n900,30\n901,31\n902,32\n", encoding="utf-8-sig")
        self.cfg = load_config()
        self.cfg["_data_dir"] = d
        self.cfg["taxonomy"]["file"] = str(d / "taxonomy.csv")
        self.cfg["fx"] = {"provider": "manual", "manual_rate_toman": 100000, "sanity_range_toman": [10000, 10000000]}
        self.cfg["catalog"] = {"photos_from": "catalog"}
        FakeWoo.products, FakeWoo.variations, FakeWoo.calls = {}, {}, []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWoo)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = mock.patch.dict(os.environ, {"DATA_DIR": str(d), "WC_URL": f"http://127.0.0.1:{self.srv.server_port}",
                                                "WC_KEY": "k", "WC_SECRET": "s", "WP_APP_PASSWORD": "",
                                                "OPENROUTER_API_KEY": "", "SERPER_API_KEY": ""})
        self.env.start()
        self.dir = d

    def tearDown(self):
        self.env.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_price_list_to_draft_variable_product_in_dollars(self):
        x = self.dir / "list.xlsx"
        make_xlsx(x, ["12,000,000", "15,500,000", "21,000,000"])
        run = "catalog-test"
        C.add_source(self.cfg, run, x.read_bytes(), "list.xlsx")
        C.save_settings(self.cfg, run, {"brand": "آکوا", "supplier": "آکوا", "category": "تجهیزات استخر"})
        s = C.run_steps(self.cfg, run)
        self.assertEqual((s["products"], s["variations"], s["priced"]), (1, 3, 3))
        p = C.products(self.cfg, run)[0]
        self.assertEqual(p["family"], "پمپ تصفیه استخر")
        self.assertEqual(p["attributes"], ["سایز"])
        self.assertEqual(p["category"]["taxonomy_id"], "901")
        self.assertEqual([v["price_usd"] for v in p["variants"]], ["120.00", "155.00", "210.00"])
        # the unit was only guessed from the file's words: nothing goes until a person confirms it
        self.assertTrue(all("unit_unconfirmed" in v["price_check"] for v in p["variants"]))
        self.assertEqual(CP.publish(self.cfg, run)["counts"]["skipped"], 1)
        self.assertEqual(FakeWoo.products, {})
        C.save_settings(self.cfg, run, {"unit": "toman"})
        C.step_prices(self.cfg, run)

        res = CP.publish(self.cfg, run)
        self.assertEqual(res["counts"]["created"], 1)
        (pid, prod), = FakeWoo.products.items()
        self.assertEqual((prod["type"], prod["status"]), ("variable", "draft"))
        self.assertEqual(prod["categories"], [{"id": 31}])
        self.assertEqual(prod["attributes"][0], {"name": "سایز", "options": ["1 اینچ", "1 1/2 اینچ", "2 اینچ"],
                                                 "visible": True, "variation": True})
        self.assertIn("آکوا", prod["name"])
        self.assertNotIn("12,000,000", prod["description"])          # no prices in the text
        vs = sorted(FakeWoo.variations[pid].values(), key=lambda v: float(v["regular_price"]))
        self.assertEqual([v["regular_price"] for v in vs], ["120", "155", "210"])
        self.assertEqual({m["key"]: m["value"] for m in vs[0]["meta_data"]}["_caravan_price_toman"], "12000000")
        sent = json.dumps([c[2] for c in FakeWoo.calls], ensure_ascii=False)
        self.assertNotIn('"regular_price": "12000000"', sent)

        # next month's list: same products, new prices -> the same product and variations are updated
        make_xlsx(x, ["13,000,000", "15,500,000", "22,000,000"])
        C.add_source(self.cfg, run, x.read_bytes(), "list.xlsx")
        C.run_steps(self.cfg, run, ["read", "structure", "category"])
        FakeWoo.products[pid]["status"] = "publish"                   # someone published it meanwhile
        res = CP.publish(self.cfg, run, republish=True)
        self.assertEqual(res["counts"]["updated"], 1)
        self.assertEqual(len(FakeWoo.products), 1)
        self.assertEqual(FakeWoo.products[pid]["status"], "publish")    # status never changed on update
        self.assertEqual(len(FakeWoo.variations[pid]), 3)
        self.assertEqual(sorted(float(v["regular_price"]) for v in FakeWoo.variations[pid].values()), [130, 155, 220])

    def test_unknown_unit_and_toman_figure_never_reach_the_price_field(self):
        v = {"price_usd": "12000000.00", "price_raw": "12,000,000", "price_unit": "toman", "usd_rate_toman": "100000"}
        self.assertEqual(CP.usd_price(v), "")                                      # above max_usd
        v = {"price_usd": "120", "price_raw": "120", "price_unit": "toman", "usd_rate_toman": "100000"}
        self.assertEqual(CP.usd_price(v), "")                                      # the printed number itself
        v = {"price_usd": "120.00", "price_raw": "12,000,000", "price_unit": "toman", "usd_rate_toman": ""}
        self.assertEqual(CP.usd_price(v), "")                                      # no rate behind it
        v = {"price_usd": "120.50", "price_raw": "12,050,000", "price_unit": "toman", "usd_rate_toman": "100000"}
        self.assertEqual(CP.usd_price(v), "120.5")

        x = self.dir / "list.csv"
        x.write_text("سایز,قیمت\n1 اینچ,1200000\n2 اینچ,1500000\n", encoding="utf-8-sig")
        run = "catalog-nounit"
        C.add_source(self.cfg, run, x.read_bytes(), "list.csv")
        C.save_settings(self.cfg, run, {"category": "پمپ استخر"})
        C.run_steps(self.cfg, run, ["read", "structure", "category"])
        p = C.products(self.cfg, run)[0]
        self.assertTrue(all(v["price_usd"] == "" and "unit_unknown" in v["price_check"] for v in p["variants"]))
        self.assertEqual(CP.publish(self.cfg, run)["counts"]["skipped"], 1)
        self.assertEqual(FakeWoo.products, {})
        C.save_settings(self.cfg, run, {"unit": "rial"})                         # a person says: rial
        C.step_prices(self.cfg, run)
        self.assertEqual([v["price_usd"] for v in C.products(self.cfg, run)[0]["variants"]], ["1.20", "1.50"])

    def test_vision_prices_not_on_the_page_are_flagged_and_go_pending(self):
        run = "catalog-scan"
        d = self.dir / "runs" / run
        d.mkdir(parents=True)
        (d / "C1_pages").mkdir()
        (d / "C1_pages" / "001.png").write_bytes(b"png")
        (d / "C0_source.pdf").write_bytes(b"%PDF-1.4")
        (d / "C1_read.json").write_text(json.dumps({"kind": "pdf", "pages": 1, "tables": [], "images": [],
                                                    "lines": [{"page": 1, "n": 1, "y": 1, "text": "پمپ 2,000,000 تومان"}]},
                                                   ensure_ascii=False), encoding="utf-8")
        C.save_settings(self.cfg, run, {"category": "پمپ استخر", "unit": "toman"})
        answer = {"currency": "toman", "products": [{"family": "پمپ استخر", "attributes": ["سایز"], "common": {},
                                                      "variants": [{"values": {"سایز": "1 اینچ"}, "price": "2,000,000"},
                                                                   {"values": {"سایز": "2 اینچ"}, "price": "3,500,000"}]}]}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}), \
                mock.patch.object(C, "_openrouter", return_value=answer):
            C.step_structure(self.cfg, run)
        vs = C.products(self.cfg, run)[0]["variants"]
        self.assertEqual(vs[0]["price_check"], "")
        self.assertTrue(vs[1]["price_check"].startswith("vision"))
        C.step_category(self.cfg, run)
        data, variations, problems, _ = CP.build(self.cfg, run, C.products(self.cfg, run)[0], {"901": "31"})
        self.assertEqual(problems, [])
        self.assertEqual(data["status"], "pending")
        self.assertEqual([v["regular_price"] for v in variations], ["20", "35"])

    def test_live_product_keeps_type_and_doubtful_prices_and_stale_sizes_go_out_of_stock(self):
        x = self.dir / "list.xlsx"
        make_xlsx(x, ["12,000,000", "15,500,000", "21,000,000"])
        run = "catalog-live"
        C.add_source(self.cfg, run, x.read_bytes(), "list.xlsx")
        C.save_settings(self.cfg, run, {"brand": "آکوا", "supplier": "آکوا", "category": "تجهیزات استخر", "unit": "toman"})
        C.run_steps(self.cfg, run)
        CP.publish(self.cfg, run)
        (pid, prod), = FakeWoo.products.items()
        prod.update(status="publish", type="variable")
        # next month two sizes say 'call us': one priced row left -> would be 'simple'; the site keeps variable
        make_xlsx(x, ["13,000,000", "تماس بگیرید", "تماس بگیرید"])
        C.add_source(self.cfg, run, x.read_bytes(), "list.xlsx")
        C.run_steps(self.cfg, run, ["read", "structure", "category"])
        res = CP.publish(self.cfg, run, republish=True)
        self.assertEqual(res["counts"]["updated"], 1)
        put = [c[2] for c in FakeWoo.calls if c[0] == "PUT"][-1]
        self.assertEqual(put.get("type"), "variable")
        self.assertNotIn("regular_price", put)
        self.assertNotIn("attributes", put)
        self.assertEqual(sorted(float(v["regular_price"]) for v in FakeWoo.variations[pid].values()), [120, 155, 210])
        # a three-size list again with one size held: that size goes out of stock, the rest updated
        make_xlsx(x, ["13,000,000", "16,000,000", "abc"])
        C.add_source(self.cfg, run, x.read_bytes(), "list.xlsx")
        C.run_steps(self.cfg, run, ["read", "structure", "category"])
        CP.publish(self.cfg, run, republish=True)
        vs = sorted(FakeWoo.variations[pid].values(), key=lambda v: float(v["regular_price"]))
        self.assertEqual([float(v["regular_price"]) for v in vs], [130, 160, 210])
        self.assertEqual([v.get("stock_status") for v in vs], ["instock", "instock", "outofstock"])

    def test_toman_list_with_dollar_footer_is_held(self):
        x = self.dir / "list.csv"
        x.write_text("سایز,قیمت\n1 اینچ,45000\n2 اینچ,65000\nقیمت ها بر اساس نرخ دلار روز است,\n", encoding="utf-8-sig")
        run = "catalog-footer"
        C.add_source(self.cfg, run, x.read_bytes(), "list.csv")
        C.save_settings(self.cfg, run, {"category": "پمپ استخر"})
        C.run_steps(self.cfg, run, ["read", "structure", "category"])
        self.assertEqual(CP.publish(self.cfg, run)["counts"]["created"], 0)
        self.assertEqual(FakeWoo.products, {})

    def test_dollar_list_small_prices(self):
        self.assertTrue(C.looks_price("12.5", usd=True))
        self.assertTrue(C.looks_price("$450", usd=True))
        self.assertFalse(C.looks_price("12.5"))
        self.assertEqual(C.parse_price("$450"), 450)


if __name__ == "__main__":
    unittest.main()
