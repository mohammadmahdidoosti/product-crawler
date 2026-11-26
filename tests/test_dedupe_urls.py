import unittest

from crawler.dedupe import _urls


class Urls(unittest.TestCase):
    def test_nested(self):          # 26 Nov: batches 303-304 stopped on a nested list in image_urls
        self.assertEqual(_urls('["a", ["b", "c"], {"url": "d"}, 5]'), ["a", "b", "c", "d"])

    def test_bad(self):
        self.assertEqual(_urls("not json"), [])
        self.assertEqual(_urls(None), [])


if __name__ == "__main__":
    unittest.main()
