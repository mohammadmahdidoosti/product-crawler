import io
import os
import unittest

os.environ["CARAVAN_NO_MODELS"] = "1"          # tests never download the open models

import numpy as np
from PIL import Image, ImageDraw


class ImageStep(unittest.TestCase):
    def test_shop_mark_removed_framed_and_watermarked(self):
        from crawler.images import make, shop_mark, thumb
        rng = np.random.default_rng(0)
        datas = []
        for _ in range(8):
            im = Image.new("RGB", (800, 600), "white")
            d = ImageDraw.Draw(im)
            x, y = int(rng.integers(100, 300)), int(rng.integers(80, 200))
            d.rectangle([x, y, x + 250, y + 200], fill=tuple(int(v) for v in rng.integers(0, 200, 3)))
            d.rectangle([620, 540, 780, 585], fill=(30, 30, 160))          # the shop's mark, always here
            b = io.BytesIO()
            im.save(b, "PNG")
            datas.append(b.getvalue())
        mark = shop_mark([thumb(x) for x in datas])
        self.assertIsNotNone(mark)
        pic, notes = make(Image.open(io.BytesIO(datas[0])), mark)
        self.assertEqual(pic.size, (1000, 1000))
        self.assertIn("shop_mark_removed", notes)
        a = np.asarray(pic)
        self.assertTrue((a[:40, :40] > 250).all())                         # white frame
        blue = (a[..., 2] > 140) & (a[..., 0] < 60) & (a[..., 1] < 60)
        self.assertLess(blue.sum(), 50)                                    # the shop's mark is gone

    def test_no_mark_without_enough_photos(self):
        from crawler.images import shop_mark
        self.assertIsNone(shop_mark([np.zeros((256, 256), np.float32)] * 2))


class BestPhotoToFix(unittest.TestCase):
    def test_the_whole_product_with_fewest_marks_is_fixed(self):
        from crawler.aiphotos import best_photo
        files = ["k/1.webp", "k/2.webp", "k/3.webp"]
        v = {"k/1.webp": {"matches_name": True, "product_whole": False, "overlay_text": True, "shop_logo": True},
             "k/2.webp": {"matches_name": True, "product_whole": True, "overlay_text": True, "shop_logo": True},
             "k/3.webp": {"matches_name": True, "product_whole": True, "overlay_text": False, "shop_logo": True}}
        self.assertEqual(best_photo(files, v), "k/3.webp")
        v["k/3.webp"]["matches_name"] = False                      # another thing in the photo: never
        self.assertEqual(best_photo(files, v), "k/2.webp")
        self.assertEqual(best_photo(files, {}), "k/1.webp")        # nothing known: the shop's first
