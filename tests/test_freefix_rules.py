import unittest

import numpy as np
from PIL import Image

from crawler.freefix import lama_local, safe_boxes, text_strips


class FreeFixRules(unittest.TestCase):
    def test_small_mark_on_product_allowed(self):
        self.assertTrue(safe_boxes([[480, 300, 560, 360]])[0])           # 0.5% in the middle: local fill
        self.assertFalse(safe_boxes([[300, 300, 700, 700]])[0])          # a big block in the middle

    def test_text_strips(self):
        self.assertTrue(text_strips([[150, 470, 850, 520]]))              # a line of text across the tool
        self.assertFalse(text_strips([[200, 200, 800, 800]]))             # a see-through logo block
        self.assertFalse(text_strips([]))

    def test_lama_local_without_model_is_none_or_same_size(self):
        im = Image.fromarray(np.full((300, 300, 3), 255, np.uint8))
        out = lama_local(im, [[400, 400, 600, 450]])
        self.assertTrue(out is None or out.size == im.size)


if __name__ == "__main__":
    unittest.main()


class LostParts(unittest.TestCase):
    def test_loose_part_dropped_by_cutout(self):
        from PIL import Image, ImageDraw
        from crawler.freefix import lost_parts
        im = Image.new("RGB", (600, 600), "white")
        d = ImageDraw.Draw(im)
        d.rectangle([250, 100, 500, 500], fill=(30, 160, 60))     # the pack
        d.rectangle([120, 120, 140, 520], fill=(180, 140, 40))    # the loose drill bit
        cut = Image.new("RGB", (600, 600), "white")
        ImageDraw.Draw(cut).rectangle([250, 100, 500, 500], fill=(30, 160, 60))
        self.assertTrue(lost_parts(im, cut))
        self.assertFalse(lost_parts(im, im.copy()))
        busy = Image.new("RGB", (600, 600), (120, 120, 120))
        self.assertFalse(lost_parts(busy, cut))
        # the part dropped was a mark in a known box: fine
        self.assertFalse(lost_parts(im, cut, [[190, 190, 250, 880]]))
