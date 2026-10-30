import unittest

import pandas as pd

from crawler.ranking import select_top


def rows():
    out = []
    for i in range(20):
        out.append({"taxonomy_id": "1", "breadcrumb": "ابزار > برس سیمی کاسه ای", "name": f"برس {i}",
                    "source_domain": "x.en.made-in-china.com", "url": f"https://x.en.made-in-china.com/{i}",
                    "price_toman": "100000"})
    for i in range(20):
        out.append({"taxonomy_id": "2", "breadcrumb": "ابزار > دستگاه جوش لوله", "name": f"دستگاه {i}",
                    "source_domain": f"f{i}.en.made-in-china.com", "url": f"https://f{i}.en.made-in-china.com/p",
                    "price_toman": "50000000"})
    for i in range(3):
        out.append({"taxonomy_id": "2", "breadcrumb": "ابزار > دستگاه جوش لوله", "name": f"ایرانی {i}",
                    "source_domain": f"shop{i}.ir", "url": f"https://shop{i}.ir/p", "price_toman": "40000000"})
    return pd.DataFrame(out)


class CostCuts(unittest.TestCase):
    def test_foreign_consumables_skipped_and_capped(self):
        kept, dropped = select_top(rows(), 24, 3, max_foreign=8, foreign_skip="برس|اسپیسر")
        self.assertEqual(len(kept[kept["taxonomy_id"] == "1"]), 0)          # Chinese brushes: none
        k2 = kept[kept["taxonomy_id"] == "2"]
        self.assertEqual(int(k2["source_domain"].str.contains("made-in-china").sum()), 8)
        self.assertEqual(int((~k2["source_domain"].str.contains("made-in-china")).sum()), 3)
        self.assertEqual(len(kept) + len(dropped), 43)

    def test_factory_machine_in_hand_tool_category(self):
        df = pd.DataFrame([
            {"taxonomy_id": "3", "breadcrumb": "ابزار > قیچی لوله > قیچی لوله سفید", "name": "دستگاه برش",
             "source_domain": "m.en.made-in-china.com", "url": "https://m.en.made-in-china.com/1", "price_toman": "900000000"},
            {"taxonomy_id": "3", "breadcrumb": "ابزار > قیچی لوله > قیچی لوله سفید", "name": "قیچی چینی",
             "source_domain": "n.en.made-in-china.com", "url": "https://n.en.made-in-china.com/1", "price_toman": "1500000"},
            {"taxonomy_id": "4", "breadcrumb": "ابزار > قیچی لوله > قیچی لوله بر", "name": "قیچی نووا",
             "source_domain": "shop.ir", "url": "https://shop.ir/1", "price_toman": "2000000"},
        ])
        kept, dropped = select_top(df, 24, 3, max_ratio=10)
        self.assertEqual(sorted(kept["name"]), ["قیچی نووا", "قیچی چینی"])
        self.assertEqual(list(dropped["name"]), ["دستگاه برش"])

    def test_foreign_kept_out_of_brand_category(self):
        df = pd.DataFrame([
            {"taxonomy_id": "5", "breadcrumb": "ابزار > ترانسمیتر اختلاف فشار > ترانسمیتر اختلاف فشار اندرس هاوزر",
             "name": "ترانسمیتر چینی", "source_domain": "q.en.made-in-china.com", "url": "https://q.en.made-in-china.com/1",
             "price_toman": "20000000"},
            {"taxonomy_id": "5", "breadcrumb": "ابزار > ترانسمیتر اختلاف فشار > ترانسمیتر اختلاف فشار اندرس هاوزر",
             "name": "ترانسمیتر اندرس هاوزر PMD75", "source_domain": "shop.ir", "url": "https://shop.ir/2",
             "price_toman": "60000000", "brand": "اندرس هاوزر"},
            {"taxonomy_id": "6", "breadcrumb": "ابزار > ترموستات دیجیتال > ترموستات دیجیتال فن کویل",
             "name": "ترموستات چینی", "source_domain": "r.en.made-in-china.com", "url": "https://r.en.made-in-china.com/1",
             "price_toman": "5000000"},
        ])
        df.loc[3] = {"taxonomy_id": "7", "breadcrumb": "ابزار > چکش تخریب > چکش تخریب دی سی ای",
                     "name": "DCA AZG15 demolition hammer", "source_domain": "dca.en.made-in-china.com",
                     "url": "https://dca.en.made-in-china.com/1", "price_toman": "20000000", "brand": ""}
        df.loc[4] = {"taxonomy_id": "7", "breadcrumb": "ابزار > چکش تخریب > چکش تخریب دی سی ای",
                     "name": "Electric demolition hammer 1500W", "source_domain": "x.en.made-in-china.com",
                     "url": "https://x.en.made-in-china.com/2", "price_toman": "15000000", "brand": ""}
        kept, dropped = select_top(df, 24, 3, brand_names={"اندرس هاوزر", "دی سی ای"})
        # the brand's own product from China stays (DCA is a Chinese brand); another factory's does not
        self.assertEqual(sorted(kept["name"]), ["DCA AZG15 demolition hammer", "ترانسمیتر اندرس هاوزر PMD75", "ترموستات چینی"])
        self.assertEqual(sorted(dropped["name"]), ["Electric demolition hammer 1500W", "ترانسمیتر چینی"])

    def test_no_caps_is_unchanged(self):
        kept, dropped = select_top(rows(), 24, 3)
        self.assertEqual(len(kept), 20 + 23)


if __name__ == "__main__":
    unittest.main()
