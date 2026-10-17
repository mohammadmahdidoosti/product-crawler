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
    if core and not (set(core) & name_words):
        return {"relevant": False, "confidence": 0.7, "reason": "واژه‌ی اصلی دسته در نام نیست: " + "، ".join(core)}
    want = [w for w in _en_words(english) if w not in {_stem(x) for x in EN_ADJ if x in ("for", "and", "with")}]
    share = sum(w in name_words for w in want) / len(want) if want else 1.0
    return {"relevant": True, "confidence": round(0.5 + 0.5 * share, 2),
            "reason": f"{round(share * 100)}٪ واژه‌های انگلیسی دسته در نام هست"}


def judge(breadcrumb: str, leaf: str, product: dict, english: str = "", group_en: str = "") -> dict:
    import re
    if english and not re.search(r"[\u0600-\u06FF]", product.get("name") or ""):
        return judge_en(english, group_en, product)
    name = norm_text(f"{product.get('name', '')} {product.get('short_description', '')[:120]}")
    name_words = set(name.split())
    cat = norm_text(breadcrumb)
    heads = head_words(breadcrumb, leaf)
    neg = next((n for n in NEGATIVE if f" {n} " in f" {name} " and n not in cat), None)
    if neg:
        return {"relevant": False, "confidence": 0.8, "reason": f"کلمه‌ی «{neg}» در نام هست (قطعه/خدمت/غیرمرتبط)"}
    if heads and not (heads & name_words):
        return {"relevant": False, "confidence": 0.7,
                "reason": "هیچ‌کدام از کلمه‌های اصلی دسته در نام نیست: " + "، ".join(sorted(heads))}
    lw = [w for w in _words(leaf) if len(w) > 1]
    share = sum(w in name_words for w in lw) / len(lw) if lw else 1.0
    return {"relevant": True, "confidence": round(0.5 + 0.5 * share, 2),
            "reason": f"{round(share * 100)}٪ کلمه‌های دسته در نام هست"}
