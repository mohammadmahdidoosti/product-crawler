import unittest


class Describe(unittest.TestCase):
    R = {"url": "u", "name": "اره عمودبر رونیکس مدل 4165", "brand": "رونیکس", "breadcrumb": "ابزار > اره عمود بر",
         "specs_json": '{"توان": "450 وات", "%d8%aa%d9%88%d8%a7%d9%86": "450 وات", "Customized": "Yes", "ولتاژ": "220 ولت"}',
         "description": "این اره با موتور قدرتمند برای برش چوب و فلز طراحی شده و دارای سیستم ضد لرزش است و کار با آن ساده است.",
         "seller_name": "ابزار فلان"}

    def test_specs_cleaned(self):
        from crawler.describe import clean_specs
        self.assertEqual(clean_specs(self.R), {"توان": "450 وات", "ولتاژ": "220 ولت"})

    def test_template_and_rules(self):
        from crawler.describe import check, parse_answer, template
        html, short = template(self.R)
        self.assertIn("رونیکس", html)
        good = {"html": "<p>" + "این اره عمودبر رونیکس با توان ۴۵۰ وات برای برش دقیق قطعات چوبی مناسب است. " * 9 + "</p>",
                "short": "اره عمودبر رونیکس"}
        self.assertTrue(check(good, self.R))
        self.assertFalse(check({"html": good["html"] + "<p>قیمت ۲ میلیون تومان</p>", "short": ""}, self.R))
        self.assertFalse(check({"html": "<p>" + "This is English text only. " * 30 + "</p>", "short": ""}, self.R))
        copied = {"html": good["html"] + "<p>" + self.R["description"] + "</p>", "short": ""}
        self.assertFalse(check(copied, self.R))
        a = parse_answer('```json\n{"short": "x", "html": "<p>a</p><script>b</script><img src=x>"}\n```')
        self.assertEqual(a["html"], "<p>a</p>b")
