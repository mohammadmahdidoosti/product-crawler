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
