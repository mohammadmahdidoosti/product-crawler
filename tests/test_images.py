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


class NoPaidEditWithoutTheProduct(unittest.TestCase):
    def test_placeholder_is_not_sent_for_an_edit(self):
        from crawler.aiphotos import worth_editing
        self.assertFalse(worth_editing({"note": "placeholder image, no product shown", "overlay_text": True}))
        self.assertFalse(worth_editing({"matches_name": False, "shop_logo": True}))
        self.assertTrue(worth_editing({"matches_name": True, "shop_logo": True}))
        self.assertTrue(worth_editing({"product_whole": True, "overlay_text": True}))


class FreeFixLocks(unittest.TestCase):
    """29 Oct ladder test: a faint watermark over the product (#16) and a cluttered photo (#05) came
    out as 'free' fixes; both must go on to the paid model."""

    def test_faint_watermark_or_busy_background_fail(self):
        from crawler.freefix import passes
        ok = {"overlay_text": False, "shop_logo": False, "faint_watermark": False, "white_background": True,
              "product_whole": True, "matches_name": True}
        self.assertTrue(passes(ok)[0])
        self.assertFalse(passes({**ok, "faint_watermark": True})[0])
        self.assertFalse(passes({**ok, "white_background": False})[0])
        self.assertFalse(passes({**ok, "white_background": None})[0])
        self.assertFalse(passes({**ok, "shop_logo": True})[0])
        self.assertFalse(passes({"_error": "x"})[0])

    def test_only_small_marks_at_the_edges_are_filled(self):
        from crawler.freefix import safe_boxes
        self.assertTrue(safe_boxes([[20, 20, 200, 120]])[0])
        self.assertFalse(safe_boxes([[400, 400, 600, 600]])[0])          # on the product
        self.assertFalse(safe_boxes([[0, 0, 400, 300]])[0])              # too big
        self.assertFalse(safe_boxes([])[0])

    def test_white_fill_only_on_white(self):
        from crawler.freefix import white_fill
        im = Image.new("RGB", (500, 500), "white")
        ImageDraw.Draw(im).rectangle([20, 20, 80, 50], fill="red")
        out = white_fill(im, [[30, 30, 170, 110]])
        self.assertIsNotNone(out)
        self.assertEqual(out.getpixel((50, 35)), (255, 255, 255))
        busy = Image.new("RGB", (500, 500), (90, 120, 60))
        self.assertIsNone(white_fill(busy, [[40, 40, 160, 100]]))

    def test_a_fail_goes_on_and_costs_are_counted(self):
        from unittest import mock
        from crawler import freefix
        im = Image.new("RGB", (600, 600), (90, 120, 60))
        ImageDraw.Draw(im).rectangle([150, 150, 450, 450], fill="blue")
        with mock.patch.object(freefix, "find_boxes", return_value=([[400, 400, 600, 600]], 0.001)):
            out, how, cost = freefix.free_fix(im, "x", "m", "k", check=lambda *a: {"_cost": 0.001})
        self.assertIsNone(out)                    # no cut-out model in tests, mark in the middle: no free fix
        self.assertIn("middle", how)
        self.assertAlmostEqual(cost, 0.001)


class MarkOnTheProduct(unittest.TestCase):
    def test_a_mark_left_inside_its_box_is_seen(self):
        from crawler.freefix import left_in_boxes
        im = Image.new("RGB", (1000, 1000), "white")
        ImageDraw.Draw(im).rectangle([300, 300, 700, 700], fill="blue")      # the product, mark on it
        self.assertGreater(left_in_boxes(im, [[450, 450, 520, 520]]), 0.9)
        self.assertLess(left_in_boxes(im, [[20, 20, 200, 100]]), 0.05)       # a mark that went with the background


class SmallBoxGrows(unittest.TestCase):
    def test_half_a_logo_is_not_left(self):
        from crawler.freefix import white_fill
        im = Image.new("RGB", (1000, 1000), "white")
        ImageDraw.Draw(im).rectangle([400, 40, 680, 140], fill=(20, 120, 40))     # the logo
        ImageDraw.Draw(im).rectangle([300, 400, 700, 900], fill="blue")           # the product
        out = white_fill(im, [[450, 60, 600, 120]])                               # the finder's box: too small
        self.assertIsNotNone(out)
        a = np.asarray(out)[40:141, 400:681]
        self.assertTrue((a.min(axis=2) >= 235).all())
        self.assertEqual(out.getpixel((500, 600)), (0, 0, 255))                    # product untouched


class FixedPhotoReused(unittest.TestCase):
    def test_same_photo_is_found_and_another_is_not(self):
        import tempfile
        from pathlib import Path
        from crawler.photoprint import Prints
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "images" / "k1"
            root.mkdir(parents=True)
            a = Image.new("RGB", (1000, 1000), "white")
            ImageDraw.Draw(a).rectangle([300, 200, 700, 800], fill="blue")
            ImageDraw.Draw(a).text((20, 20), "SHOP.IR", fill="red")
            a.save(root / "1.webp")
            a.resize((800, 800)).save(root / "2.webp")                    # the same photo, smaller
            b = Image.new("RGB", (1000, 1000), "white")
            ImageDraw.Draw(b).ellipse([200, 300, 800, 700], fill="red")    # another product
            b.save(root / "3.webp")
            c = a.copy()
            ImageDraw.Draw(c).rectangle([300, 200, 700, 800], fill=(200, 160, 30))   # same shape, gold
            c.save(root / "4.webp")
            Image.new("RGB", (1000, 1000), "white").save(root / "ai1_1.webp")
            pr = Prints(Path(t))
            pr.add(root / "1.webp", "k1/ai1_1.webp", "ai_fixed:qwen")
            self.assertEqual(Prints(Path(t)).find(root / "2.webp")["fixed"], "k1/ai1_1.webp")
            self.assertIsNone(pr.find(root / "3.webp"))
            self.assertIsNone(pr.find(root / "4.webp"))


class ShopMarkMemory(unittest.TestCase):
    def test_a_learned_mark_is_used_when_a_run_cannot_learn_it(self):
        import tempfile
        from pathlib import Path
        from crawler.images import remembered_marks
        with tempfile.TemporaryDirectory() as t:
            m = np.full((256, 256), -1.0)
            m[5:20, 5:60] = 40
            remembered_marks(Path(t), {"shop.ir": m})
            got = remembered_marks(Path(t), {"shop.ir": None, "other.ir": None})
            self.assertIsNotNone(got["shop.ir"])
            self.assertIsNone(got["other.ir"])
