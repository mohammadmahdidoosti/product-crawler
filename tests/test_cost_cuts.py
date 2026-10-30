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

    def test_no_caps_is_unchanged(self):
        kept, dropped = select_top(rows(), 24, 3)
        self.assertEqual(len(kept), 20 + 23)


if __name__ == "__main__":
    unittest.main()
