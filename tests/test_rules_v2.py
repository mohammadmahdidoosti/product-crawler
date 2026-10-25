"""What a sub-category adds to its parent, decisive attributes, and the second chance."""
import unittest

import pandas as pd

from crawler.harvest import _deepest, strict
from crawler.rules import code_in, judge, measures


def j(crumb, name, **kw):
    return judge(crumb, crumb.split(">")[-1].strip(), {"name": name, **kw})


class Added(unittest.TestCase):
    def test_unknown_brand_in_category(self):
        c = "ابزار برقی و شارژی > دریل > دریل بتن کن شارژی > دریل بتن کن شارژی توتال"
        self.assertFalse(j(c, "دریل بتن کن شارژی 20 ولت هیوندای HP2020")["relevant"])
        self.assertTrue(j(c, "دریل بتن کن شارژی 20 ولت توتال مدل TRHLI1601")["relevant"])

    def test_cordless_and_modes(self):
        c = "ابزار برقی و شارژی > دریل > دریل بتن کن > دریل بتن کن شارژی"
        self.assertFalse(j(c, "دریل بتن کن 1200 وات رونیکس مدل 2735")["relevant"])
        self.assertTrue(j(c, "دریل بتن کن شارژی 20 ولت اینکو CRHLI2201")["relevant"])
        m = "ابزار برقی و شارژی > دریل > دریل بتن کن > دریل بتن کن دو حالته"
        self.assertFalse(j(m, "دریل بتن کن چهار حالته رونیکس 2735")["relevant"])
        self.assertTrue(j(m, "دریل بتن کن دو حالته رونیکس 2701")["relevant"])
        g = "ابزار > اره > اره شارژی > اره شارژی اشتیل اصل"
        self.assertFalse(j(g, "اره زنجیری بنزینی 40سانتی اشتیل مدل MS291")["relevant"])

    def test_numbers_and_units(self):
        self.assertTrue(code_in("8020", "8020h") and code_in("61", "61kw") and not code_in("17", "ct18173m"))
        self.assertEqual(measures("اره 20 ولت 25 سانتی"), {("20", "v"), ("25", "cm")})
        c = "ابزار > اره شارژی و برقی > اره شارژی رونیکس > اره شارژی رونیکس 20 سانتی"
        self.assertFalse(j(c, "اره گردبر شارژی 20 ولت رونیکس مدل 8902")["relevant"])
        h = "ابزار > چکش تخریب > چکش تخریب 17 کیلویی"
        self.assertFalse(j(h, "چکش تخریب 7.5 کیلویی 1300 وات کرون CT18173M")["relevant"])

    def test_country(self):
        c = "آسانسور > موتور و گیربکس آسانسور > موتور آسانسور گیرلس > موتور آسانسور گیرلس ایتالیایی"
        self.assertFalse(j(c, "موتور آسانسور گیرلس بلولایت 10 نفره")["relevant"])
        self.assertTrue(j(c, "موتور گیرلس مونتاناری ایتالیا 8 نفره")["relevant"])


class SecondChance(unittest.TestCase):
    def node(self, crumb):
        return {"breadcrumb": crumb, "leaf": crumb.split(">")[-1].strip(), "taxonomy_id": crumb}

    def test_type_of_every_parent(self):
        tree = "ابزار > اره درخت و باغبانی > اره درخت بر > اره درخت بر شارژی"
        self.assertIsNotNone(strict(self.node(tree), {"name": "اره عمودبر شارژی کرون 20 ولت مدل CT25003"}))
        self.assertIsNone(strict(self.node(tree), {"name": "اره درخت بر شارژی 20 ولت رونیکس 8651"}))
        pipe = "لوله و اتصالات > لوله پلی اتیلن > لوله سه لایه > لوله سه لایه آب"
        self.assertIsNotNone(strict(self.node(pipe), {"name": "ست خم کن دستی اهرمی لوله 5 لایه شمس مدل 7001"}))
        old = "ابزار > اره عمود بر > اره عمود بر ماکیتا > اره عمود بر ماکیتا قدیمی"
        self.assertIsNotNone(strict(self.node(old), {"name": "اره عمود بر ماکیتا 4327"}))

    def test_deepest_only(self):
        a, b = self.node("ابزار > اره عمود بر > اره عمود بر رونیکس"), self.node("ابزار > اره عمود بر")
        self.assertEqual([n["breadcrumb"] for n in _deepest([a, b])], [a["breadcrumb"]])


if __name__ == "__main__":
    unittest.main()
