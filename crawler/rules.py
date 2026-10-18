"""Free relevance filter: no language model, only rules.

A product is kept when
  1. its name contains a "head word" of the category — a word from the
     level-2 title or the first word of the leaf (درایو, اینورتر, شیر, پمپ…);
  2. it has none of the negative words (spare part, used, repair, course…),
     unless that word is part of the category itself ("بوبین" is negative,
     except in the coil category);
Confidence grows with how many of the leaf's words the name contains.
Weaker than a model, but free, instant, and the output is reviewed anyway.
"""
from __future__ import annotations

from .normalize import norm_text

STOP = {norm_text(w) for w in (
    "و", "با", "برای", "از", "به", "در", "مدل", "برند", "نوع", "سری", "انواع", "خرید", "قیمت", "فروش",
    "صنعتی", "ایرانی", "خارجی", "اصل", "اورجینال", "جدید", "کیفیت", "عمده", "ارزان", "بهترین",
)}

NEGATIVE = [norm_text(w) for w in (
    "بوبین", "قطعه", "قطعات", "یدکی", "لوازم جانبی", "کارکرده", "دست دوم", "استوک", "تعمیر", "تعمیرات",
    "آموزش", "کتاب", "دوره", "فیلم", "نصب", "خدمات", "اجاره", "کاور", "قاب", "برچسب", "کاتالوگ",
    "پکیج آموزشی", "نرم افزار", "شارژ", "اشتراک", "کارت هدیه",
    "چرخدنده", "چرخ دنده", "مهره انتقال", "واشر", "بلبرینگ", "اورینگ", "کاسه نمد", "لنگ دارد", "معیوب", "ایراد دار",
)]


def _words(s: str) -> list[str]:
    return [w for w in norm_text(s).split() if len(w) > 1 and w not in STOP]


def head_words(breadcrumb: str, leaf: str) -> set[str]:
    parts = [p.strip() for p in breadcrumb.split(">")]
    heads: set[str] = set()
    if len(parts) >= 2:
        heads |= {w for w in _words(parts[1]) if len(w) > 2}
    lw = _words(leaf)
    if lw:
        heads.add(lw[0])
    return heads


EN_ADJ = {"electric", "electrical", "electronic", "industrial", "automatic", "digital", "manual", "small", "mini",
          "large", "high", "low", "single", "three", "two", "phase", "way", "type", "heavy", "duty", "portable",
          "smart", "stainless", "steel", "plastic", "brass", "for", "and", "with", "the", "of", "machine", "system",
          "equipment", "device", "unit", "set", "china", "price"}
EN_NEGATIVE = ["spare part", "spare parts", "repair", "used", "second hand", "refurbished", "toy", "course",
               "auto parts", "car parts", "vw", "volkswagen", "toyota", "honda", "bmw", "audi", "hyundai", "kia",
               "nissan", "mercedes", "peugeot", "fabia", "skoda", "certificate"]


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _en_words(s: str) -> list[str]:
    import re
    return [_stem(w) for w in re.findall(r"[a-zA-Z][a-zA-Z0-9\-]+", s or "")]


def judge_en(english: str, group_en: str, product: dict) -> dict:
    """Foreign (English) product names against the category's English term."""
    name = f" {(product.get('name') or '').lower()} "
    name_words = set(_en_words(name))
    neg = next((n for n in EN_NEGATIVE if f" {n} " in name.replace("/", " ").replace(",", " ")), None)
    if neg:
        return {"relevant": False, "confidence": 0.8, "reason": f"«{neg}» در نام هست (قطعه/غیرمرتبط)"}
    core = [w for w in _en_words(group_en or english) if w not in EN_ADJ] or _en_words(group_en or english)
    need = min(2, len(set(core)))                            # 'Car Jack with safety Valve' shares only 'valve'
    if core and len(set(core) & name_words) < need:
        return {"relevant": False, "confidence": 0.7,
                "reason": f"کمتر از {need} واژه‌ی اصلی دسته در نام هست: " + "، ".join(core)}
    want = [w for w in _en_words(english) if w not in {_stem(x) for x in EN_ADJ if x in ("for", "and", "with")}]
    share = sum(w in name_words for w in want) / len(want) if want else 1.0
    return {"relevant": True, "confidence": round(0.5 + 0.5 * share, 2),
            "reason": f"{round(share * 100)}٪ واژه‌های انگلیسی دسته در نام هست"}


def _flat(s: str) -> str:
    import re
    from .normalize import fa_digits_to_en
    return re.sub(r"[^0-9a-z]", "", fa_digits_to_en(str(s or "")).lower())


def model_tokens(breadcrumb: str, leaf: str) -> list[str]:
    """Number/code words that only the leaf has ('2008', '6.1', 'sr5', '40'), not its parents."""
    import re
    from .normalize import fa_digits_to_en
    parents = set(fa_digits_to_en(" ".join(breadcrumb.split(">")[:-1])).lower().split())
    words = fa_digits_to_en(leaf).lower().split()
    nums = {"یک": "1", "دو": "2", "سه": "3", "چهار": "4", "پنج": "5", "شش": "6", "هفت": "7", "هشت": "8",
            "نه": "9", "ده": "10"}
    units = {"اینچ", "نفره", "اسب", "کیلو", "متری", "لیتری", "تن", "تنی", "فاز", "زمانه", "راهه", "پره"}
    words = [nums[w] if w in nums and i + 1 < len(words) and words[i + 1] in units else w
             for i, w in enumerate(words)]                      # 'یک اینچ' -> '1 اینچ'
    out = []
    for w in words:
        if re.search(r"\d", w) and w not in parents:
            t = re.sub(r"[^0-9a-z]", "", w)
            if t and t not in out:
                out.append(t)
    return out


def judge(breadcrumb: str, leaf: str, product: dict, english: str = "", group_en: str = "") -> dict:
    import re
    from .routing import brand_in, foreign_brand_in, leaf_brand
    name = product.get("name") or ""
    need = model_tokens(breadcrumb, leaf)
    if need:
        flat = _flat(name)
        miss = [t for t in need if t not in flat]
        if miss:                     # 'اره شارژی ویوارکس 2008' takes only the 2008, not every Vivarex saw
            return {"relevant": False, "confidence": 0.8,
                    "reason": "مدل یا اندازه‌ی دسته در نام نیست: " + "، ".join(miss)}
    b = leaf_brand(leaf)
    if b and not brand_in(name, b[0]):
        # a brand category (e.g. «محرک الکتریکی شیر سه طرفه بوتان») takes only that brand
        return {"relevant": False, "confidence": 0.8, "reason": f"دسته‌ی برند «{b[0]}» است ولی برند در نام محصول نیست"}
    if not b and product.get("source_type") == "foreign":
        fb = foreign_brand_in(name)
        if fb:
            return {"relevant": False, "confidence": 0.8,
                    "reason": f"کالای برند {fb} در مسیر چینی (مسیر چینی فقط برای کالای عمومی است)"}
    kinds = {"برقی": ("پنوماتیک", "پنوماتیکی", "بادی", "دستی", "هیدرولیک"), "پنوماتیک": ("برقی", "الکتریکی")}
    lf, nm = norm_text(leaf), norm_text(name)
    for k, other in kinds.items():          # electric category, pneumatic product (or the reverse)
        if k in lf.split() and not any(norm_text(k) in nm.split() for _ in [0]):
            bad = next((o for o in other if norm_text(o) in nm.split() and norm_text(o) not in lf.split()), None)
            if bad:
                return {"relevant": False, "confidence": 0.8, "reason": f"دسته «{k}» است ولی محصول «{bad}» است"}
    if english and not re.search(r"[\u0600-\u06FF]", product.get("name") or ""):
        return judge_en(english, group_en, product)
    name = norm_text(f"{product.get('name', '')} {product.get('short_description', '')[:120]}")
    name_words = set(name.split())
    cat = norm_text(breadcrumb)
    heads = head_words(breadcrumb, leaf)
    only_name = norm_text(product.get("name", ""))           # negative words count in the name only, not the description
    neg = next((n for n in NEGATIVE if f" {n} " in f" {only_name} " and n not in cat), None)
    if neg:
        return {"relevant": False, "confidence": 0.8, "reason": f"کلمه‌ی «{neg}» در نام هست (قطعه/خدمت/غیرمرتبط)"}
    if heads and not (heads & name_words):
        return {"relevant": False, "confidence": 0.7,
                "reason": "هیچ‌کدام از کلمه‌های اصلی دسته در نام نیست: " + "، ".join(sorted(heads))}
    lw = [w for w in _words(leaf) if len(w) > 1]
    share = sum(w in name_words for w in lw) / len(lw) if lw else 1.0
    return {"relevant": True, "confidence": round(0.5 + 0.5 * share, 2),
            "reason": f"{round(share * 100)}٪ کلمه‌های دسته در نام هست"}
