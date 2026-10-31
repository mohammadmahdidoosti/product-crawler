import unittest

import numpy as np
from PIL import Image

from crawler.shopwm import learn_from, remove


def products(n, rng, size=200):
    out = []
    for i in range(n):
        a = np.full((size, size, 3), 255.0)
        w, h = rng.integers(40, 90, 2)
        x0, y0 = rng.integers(10, size - 100, 2)
        col = rng.integers(20, 200, 3)
        a[y0:y0 + h, x0:x0 + w] = col                        # a product of its own size, colour and place
        a[y0 + 10:y0 + 18, x0:x0 + w] = col * 0.5             # with some detail
        out.append(a)
    return out


class ShopWatermark(unittest.TestCase):
    def test_see_through_logo_reversed(self):
        rng = np.random.default_rng(1)
        clean = products(12, rng)
        W, alpha = np.array([30.0, 60.0, 200.0]), 0.4
        logo = np.zeros((200, 200), bool)
        logo[90:120, 40:160] = True                          # a band of logo across the middle
        logo[95:115, 60:70] = False                          # with holes (its letters)
        marked = []
        for c in clean:
            j = c.copy()
            j[logo] = alpha * W + (1 - alpha) * c[logo]
            marked.append(j)
        t = learn_from(marked)
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t["alpha"], 0.4, delta=0.051)
        errs = []
        for c, m in zip(clean, marked):
            out, why = remove(Image.fromarray(m.astype(np.uint8)), t)
            errs.append(np.abs(np.asarray(out, np.float32) - c)[logo].mean())
        self.assertLess(float(np.mean(errs)), 6)            # the logo is gone and what was under it is back

    def test_frame_goes_product_stays(self):
        rng = np.random.default_rng(2)
        clean = products(8, rng)
        marked = []
        for c in clean:
            j = c.copy()
            frame = np.ones((200, 200), bool)
            frame[10:190, 10:190] = False
            j[frame] = [200, 200, 205]                       # a grey card around every photo
            j[170:185, 20:80] = [20, 40, 160]                # the shop's phone badge
            prod = c.min(axis=2) < 250
            j[170:185, 20:80][prod[170:185, 20:80]] = c[170:185, 20:80][prod[170:185, 20:80]]
            marked.append(j)
        t = learn_from(marked)
        self.assertIsNotNone(t)
        out, _ = remove(Image.fromarray(marked[0].astype(np.uint8)), t)
        o = np.asarray(out, np.float32)
        self.assertTrue((o[0:10, :] > 250).all())             # the card is white now
        prod = clean[0].min(axis=2) < 250
        self.assertLess(np.abs(o - clean[0])[prod].mean(), 3)  # the product untouched

    def test_photos_without_common_mark(self):
        rng = np.random.default_rng(3)
        self.assertIsNone(learn_from(products(8, rng)))


if __name__ == "__main__":
    unittest.main()
