"""3 Nov: a 'پمپ ۲ اسب' category took a 'پمپ سه اسب' whose name had a '۲' elsewhere (دهانه ۲ اینچ)."""
import unittest

from crawler.rules import judge, measures

B = "پمپ و الکتروپمپ > پمپ آب > پمپ 2 اسب"


class SizeOfCategory(unittest.TestCase):
    def test_words_are_numbers(self):
        self.assertIn(("3", "hp"), measures("پمپ آب سه اسب"))
        self.assertIn(("1.5", "hp"), measures("پمپ یک و نیم اسب"))

    def test_other_size_rejected(self):
        self.assertFalse(judge(B, "پمپ 2 اسب", {"name": "پمپ آب سه اسب دو اینچ ارتفاع بالا، دهانه ورودی ۲ اینچ"})["relevant"])
        self.assertFalse(judge(B, "پمپ 2 اسب", {"name": "پمپ آب 3 اسب 2 اینچ"})["relevant"])

    def test_same_size_kept(self):
        self.assertTrue(judge(B, "پمپ 2 اسب", {"name": "پمپ آب دو اسب دو اینچ تک فاز ۲۲۰ ولت"})["relevant"])
        self.assertTrue(judge(B, "پمپ 2 اسب", {"name": "پمپ آب ۲ اسب بشقابی پنتاکس"})["relevant"])


if __name__ == "__main__":
    unittest.main()
