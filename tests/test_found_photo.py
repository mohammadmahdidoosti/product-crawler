import unittest

from crawler.rules import judge  # noqa: F401  (import check)
from crawler.vision import found_ok


class FoundPhoto(unittest.TestCase):
    ok = {"overlay_text": False, "shop_logo": False, "frame": False, "diagram": False, "white_background": True,
          "product_whole": True, "is_part": False, "shows_whole_tool": True, "matches_name": True}

    def test_sure_is_used(self):
        self.assertTrue(found_ok(self.ok))

    def test_unsure_is_not(self):
        self.assertFalse(found_ok({**self.ok, "matches_name": None}))

    def test_part_shown_as_tool(self):
        self.assertFalse(found_ok({**self.ok, "is_part": True}))

    def test_diagram_banner_frame(self):
        for k in ("diagram", "overlay_text", "frame", "shop_logo"):
            self.assertFalse(found_ok({**self.ok, k: True}), k)

    def test_error(self):
        self.assertFalse(found_ok({"_error": "x"}))


if __name__ == "__main__":
    unittest.main()
