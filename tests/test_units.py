import json
import unittest
from pathlib import Path

import pandas as pd

from crawler.dedupe import merge_crawl, split_against_site
from crawler.extract import extract_page, from_store_api, resolve_price_unit
from crawler.normalize import (extract_models, extract_phones, model_key, norm_text,
                               parse_number)
from crawler.taxonomy import build_query, resolve_route

FIX = Path(__file__).parent / "fixtures"


class Normalize(unittest.TestCase):
    def test_persian_letters_digits(self):
        self.assertEqual(norm_text("كنتاكتور  ۳۲ آمپر"), norm_text("کنتاکتور 32 امپر"))
        self.assertEqual(norm_text("یک‌طرفه"), "یک طرفه")

    def test_parse_number(self):
        self.assertEqual(parse_number("۱۲,۰۰۰,۰۰۰ تومان"), 12_000_000)
        self.assertEqual(parse_number("12.000.000"), 12_000_000)
        self.assertEqual(parse_number("1٬850٬000"), 1_850_000)
        self.assertEqual(parse_number("12.5"), 12.5)
        self.assertIsNone(parse_number("تماس بگیرید"))

    def test_models(self):
        self.assertIn("yd101-00p7-t2s", extract_models("درایو مدل YD101-00P7-T2S برند یولیکو"))
        self.assertEqual(extract_models("پمپ 1.5 اسب 220v 50hz"), [])
        self.assertEqual(model_key("YD101-00P7"), model_key("yd10100p7"))

    def test_phones(self):
        p = extract_phones("تلفن: ۰۲۱-۶۶۷۰ ۱۲۳۴ و ۰۹۱۲۳۴۵۶۷۸۹")
        self.assertEqual(p, ["02166701234", "09123456789"])


class Extract(unittest.TestCase):
    def test_jsonld_page_with_wrong_currency_code(self):
        rec = extract_page((FIX / "woo_product.html").read_text(encoding="utf-8"))
        self.assertEqual(rec["extracted_by"], "jsonld")
        self.assertEqual(rec["brand"], "یولیکو")
        self.assertEqual(rec["price_amount"], 12_000_000)
        # schema says IRR but the page shows "۱۲,۰۰۰,۰۰۰ تومان" -> toman
        unit, flags = resolve_price_unit(rec)
        self.assertEqual(unit, "toman")
        self.assertEqual(rec["specs"]["توان"], "0.75 کیلووات")
        self.assertEqual(rec["claims_representative"], "yes")
        self.assertIn("09123456789", rec["seller_phones"])
        self.assertEqual(rec["in_stock"], "yes")

    def test_store_api(self):
        items = json.loads((FIX / "store_api.json").read_text(encoding="utf-8"))
        a, b = from_store_api(items[0]), from_store_api(items[1])
        self.assertEqual(a["price_amount"], 1_850_000)
        self.assertEqual(resolve_price_unit(a)[0], "toman")
        self.assertEqual(a["brand"], "یونی")
        self.assertEqual(a["model"], "SV-2W-15")
        self.assertEqual(resolve_price_unit(b)[0], "rial")


class MadeInChina(unittest.TestCase):
    def test_company_is_seller_and_real_model(self):
        rec = extract_page((FIX / "mic_product.html").read_text(encoding="utf-8"))
        self.assertEqual(rec["seller_name"], "Wenzhou Heli Automatic Meter Co., Ltd.")
        self.assertEqual((rec["brand"], rec["model"]), ("", "HEQ"))
        self.assertEqual(rec["specs"]["Connection"], "Flange")
        self.assertEqual(rec["specs"]["Power Supply"], "AC220V")
        self.assertEqual(rec["specs"]["MOQ"], "10 Pieces")
        self.assertNotIn("Customization", rec["specs"])
        self.assertEqual(rec["seller_phones"], [])
        self.assertEqual((rec["price_amount"], resolve_price_unit(rec)[0]), (500.0, "usd"))


class Taxonomy(unittest.TestCase):
    def test_short_leaf_gets_parent(self):
        self.assertEqual(build_query(["تصفیه آب", "فیلتر تصفیه آب", "ممبران"]), "ممبران فیلتر تصفیه آب")
        self.assertEqual(build_query(["شیرآلات", "شیر فلکه", "شیر فلکه 1/2"]), "شیر فلکه 1/2")

    def test_route_inheritance(self):
        rules = {"default": "iran", "by_path_prefix": {"شیرآلات": "iran", "شیرآلات > اکچویتور برقی": "both"}}
        row = pd.Series({"route_col": "", "taxonomy_id": "1",
                         "breadcrumb": "شیرآلات > اکچویتور برقی > محرک الکتریکی"})
        self.assertEqual(resolve_route(row, rules), "both")
        row["breadcrumb"] = "شیرآلات > شیر فلکه"
        self.assertEqual(resolve_route(row, rules), "iran")
        row["route_col"] = "foreign"
        self.assertEqual(resolve_route(row, rules), "foreign")


class Dedupe(unittest.TestCase):
    def rows(self):
        base = {"taxonomy_id": "7", "breadcrumb": "x", "specs_json": "", "price_flags": "",
                "usd_rate_toman": "100000", "image_urls": "[]", "seller_phones": ""}
        return pd.DataFrame([
            {**base, "name": "درایو YD101-00P7-T2S", "model": "YD101-00P7-T2S", "brand": "یولیکو",
             "price_toman": "12000000", "url": "a", "seller_name": "A"},
            {**base, "name": "اینورتر تکفاز yd101 00p7 t2s", "model": "yd10100p7t2s", "brand": "Pulico",
             "price_toman": "13000000", "url": "b", "seller_name": "B"},
            {**base, "name": "درایو YD101-00P7-T2S", "model": "YD101-00P7-T2S", "brand": "یولیکو",
             "price_toman": "1200", "price_flags": "too_low", "url": "c", "seller_name": "C"},
            {**base, "name": "شیر برقی 24 ولت 1/2 برنجی", "model": "", "brand": "",
             "price_toman": "1850000", "url": "d", "seller_name": "D"},
        ])

    def test_merge_median_and_bad_price(self):
        m = merge_crawl(self.rows())
        self.assertEqual(len(m), 2)
        drive = m[m["name"].str.contains("YD101", case=False)].iloc[0]
        self.assertEqual(drive["price_toman"], 12_500_000)   # too_low row excluded
        self.assertEqual(drive["n_sources"], 3)

    def test_existing_product_goes_to_updates(self):
        site = pd.DataFrame([{"id": "501", "sku": "", "name": "درایو 1 اسب تک فاز مدل YD101-00P7-T2S برند یولیکو",
                              "price": "12000000", "categories": ""}])
        site["name_n"] = site["name"].map(norm_text)
        new, upd, rev = split_against_site(merge_crawl(self.rows()), site)
        self.assertEqual(list(upd["site_product_id"]), ["501"])
        self.assertEqual(len(new), 1)


class NameOnlySiteExport(unittest.TestCase):
    def test_names_only(self):
        import tempfile
        from crawler.dedupe import load_site_catalog
        p = Path(tempfile.mkdtemp()) / "site.csv"
        pd.DataFrame({"نام": ["درایو 1 اسب تک فاز مدل YD101-00P7-T2S برند یولیکو", ""]}).to_csv(p, index=False, encoding="utf-8-sig")
        site = load_site_catalog(p, {"id": ["ID"], "name": ["Name"]})
        self.assertEqual(list(site["id"]), ["name-1"])
        site["name_n"] = site["name"].map(norm_text)
        new, upd, rev = split_against_site(merge_crawl(Dedupe().rows()), site)
        self.assertEqual(len(upd), 1)                       # recognised as already on the site


if __name__ == "__main__":
    unittest.main()


class RulesFilter(unittest.TestCase):
    def test_rules(self):
        from crawler.rules import judge
        crumb = "تجهیزات تابلو برق و برق صنعتی > درایو و اینورتر موتور > درایو تک فاز"
        self.assertTrue(judge(crumb, "درایو تک فاز", {"name": "اینورتر تکفاز YD101 یولیکو"})["relevant"])
        self.assertFalse(judge(crumb, "درایو تک فاز", {"name": "کابل شیلدار 4 در 1"})["relevant"])
        self.assertFalse(judge(crumb, "درایو تک فاز", {"name": "تعمیر درایو تک فاز"})["relevant"])
        coil = "شیرآلات > بوبین شیر برقی > بوبین"
        self.assertTrue(judge(coil, "بوبین", {"name": "بوبین شیر برقی 220 ولت"})["relevant"])
        valve = "شیرآلات > شیر برقی > شیر برقی 24 ولت"
        self.assertFalse(judge(valve, "شیر برقی 24 ولت", {"name": "بوبین شیر برقی"})["relevant"])


class OpenAICompatible(unittest.TestCase):
    def test_chat_completions_reply(self):
        from unittest import mock
        from crawler.llm import LLM, relevance_batch
        cfg = {"llm": {"provider": "openai_compatible", "base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"}}
        llm = LLM(cfg)
        reply = {"choices": [{"message": {"content": '<think>x</think>```json\n[{"i":0,"relevant":true,"confidence":0.9,"reason":"ok","brand":"یولیکو","model":"YD101"}]\n```'}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        with mock.patch.object(LLM, "_post", return_value=reply) as p:
            v = relevance_batch(llm, "a > b", [{"name": "درایو"}])
        self.assertEqual(p.call_args[0][0], "http://localhost:11434/v1/chat/completions")
        self.assertTrue(v[0]["relevant"])
        self.assertEqual(v[0]["brand"], "یولیکو")
        self.assertEqual(llm.usage["input_tokens"], 100)


class Routing(unittest.TestCase):
    def test_rules(self):
        from crawler.routing import rule_route
        self.assertEqual(rule_route("گرمایش، سرمایش و تهویه > پکیج > پکیج ایران رادیاتور")["brand"], "ایران رادیاتور")
        r = rule_route("شیرآلات > اکچویتور برقی و محرک شیر > اکچویتور برقی تدریجی")
        self.assertEqual(r["decision"], "probe")
        self.assertEqual(rule_route("تجهیزات تابلو برق و برق صنعتی > کنتاکتور > کنتاکتور اشنایدر")["brand_origin"], "foreign")
        self.assertEqual(rule_route("بذر > بذر ریحان > بذر سبزی ریحان")["decision"], "iran")
        # the top-level title is not a brand, and "قطعات" in it is not a spare part
        self.assertEqual(rule_route("مصالح و شیمی ساختمان > چسب کاشی")["kind"], "local_category")
        self.assertEqual(rule_route("قطعات مکانیکی و انتقال قدرت > بلبرینگ")["decision"], "probe")



class DeadSites(unittest.TestCase):
    def test_dead_site_is_remembered_across_runs(self):
        import tempfile, time as _t
        from pathlib import Path
        from crawler.fetch import Fetcher
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_data_dir": Path(tmp), "http": {"dead_after_failures": 2, "dead_days": 3}}
            f = Fetcher(cfg)
            f._failed("slow.example.ir")
            f._failed("slow.example.ir")
            self.assertTrue(f.is_dead("https://slow.example.ir/p/1"))
            g = Fetcher(cfg)                                   # the next run
            self.assertTrue(g.is_dead("https://slow.example.ir/p/2"))
            self.assertFalse(g.is_dead("https://ok-shop.ir/"))
            cfg["http"]["dead_days"] = 0
            import json
            p = Path(tmp) / "dead_sites.json"
            d = json.loads(p.read_text())
            d["example.ir"]["until"] = _t.time() - 1      # expired: tried afresh
            p.write_text(json.dumps(d))
            self.assertFalse(Fetcher(cfg).is_dead("https://slow.example.ir/p/3"))


class CrossCategoryMerge(unittest.TestCase):
    def test_same_model_in_two_categories_is_one_product(self):
        base = {"specs_json": "", "price_flags": "", "usd_rate_toman": "100000", "image_urls": "[]",
                "seller_phones": "", "brand": "کرون", "model": ""}
        df = pd.DataFrame([
            {**base, "taxonomy_id": "5660", "breadcrumb": "a > سشوار", "name": "سشوار صنعتی کرون مدل CT19017",
             "price_toman": "5954000", "url": "u1", "seller_name": "A"},
            {**base, "taxonomy_id": "5652", "breadcrumb": "a > سشوار کرون", "name": "سشوار صنعتی کرون مدل CT19017",
             "price_toman": "10244000", "url": "u2", "seller_name": "B"},
            {**base, "taxonomy_id": "5652", "breadcrumb": "a > سشوار کرون", "name": "نازل سشوار کرون CT19017",
             "price_toman": "300000", "url": "u3", "seller_name": "C"},
            {**base, "taxonomy_id": "5653", "breadcrumb": "a > سشوار طرح", "name": "سشوار طرح کرون CT19017",
             "price_toman": "900000", "url": "u4", "seller_name": "D"},
        ])
        m = merge_crawl(df)
        self.assertEqual(len(m), 3)                  # the two shops' heat gun merge; the nozzle and the copy stay apart
        gun = m[m["n_sources"] == 2].iloc[0]
        self.assertIn("5652", gun["extra_taxonomy_ids"] + gun["taxonomy_id"])
        self.assertIn("5660", gun["extra_taxonomy_ids"] + gun["taxonomy_id"])


class MergedName(unittest.TestCase):
    def test_features_of_other_titles_are_kept(self):
        from crawler.dedupe import merged_name
        n = merged_name("اره عمودبر رونیکس مدل 4101",
                        ["اره عمود بر رونیکس مشتی 550 وات مدل 4101", "اره عمود بر 550 وات رونیکس مدل 4101",
                         "اره عمودبر 4 دور رونیکس 4101"])
        self.assertEqual(n, "اره عمودبر رونیکس مدل 4101، 550 وات، مشتی، 4 دور")
        self.assertEqual(merged_name("سشوار بوش GHG 20-63", ["سشوار بوش GHG 20-63"]), "سشوار بوش GHG 20-63")
