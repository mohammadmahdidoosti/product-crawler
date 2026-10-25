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

import re

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
    "زنجیر", "فلکه", "پولی", "تسمه", "سوهان", "شمشیر", "پارچه",
)]


# part words that make a part only when the name starts with them ('انکودر موتور ABB' is an encoder,
# 'موتور بهران + انکودر' a motor with one)
LEAD_PARTS = [norm_text(w) for w in ("انکودر", "ریموت", "تیغ", "تیغه", "پایه", "باتری", "برد", "کیت تعمیر", "روغن", "گریس")]

ORIGIN_WORDS = {norm_text(w) for w in ("آلمان", "آلمانی", "اصل", "اصلی", "اورجینال", "ایتالیا", "ایتالیایی", "ژاپن", "ژاپنی",
                                        "چین", "چینی", "ترکیه", "ترک", "کره", "کره‌ای", "تایوان", "تایوانی", "اروپایی",
                                        "آمریکایی", "امریکایی", "فرانسه", "سوئیس", "انگلیس")}


COUNTRIES = [{norm_text(w) for w in g} for g in (
    ("آلمان", "آلمانی"), ("ایتالیا", "ایتالیایی"), ("ژاپن", "ژاپنی"), ("ترکیه", "ترک"), ("کره", "کره‌ای"),
    ("تایوان", "تایوانی"), ("اسپانیا", "اسپانیایی"), ("فرانسه", "فرانسوی"), ("سوئیس", "سوئیسی"),
    ("انگلیس", "انگلیسی"), ("آمریکا", "آمریکایی", "امریکایی"), ("سوئد", "سوئدی"), ("اتریش", "اتریشی"))]


COUNTRIES_ALL = COUNTRIES + [{norm_text(w) for w in ("ایران", "ایرانی")}, {norm_text(w) for w in ("چین", "چینی")}]


def _words(s: str) -> list[str]:
    return [w for w in norm_text(s).split() if len(w) > 1 and w not in STOP]


ADJ = {norm_text(w) for w in ("برقی", "شارژی", "دستی", "الکتریکی", "بادی", "پنوماتیک", "پنوماتیکی", "هیدرولیک",
                               "هیدرولیکی", "دیجیتال", "اتوماتیک", "خانگی", "صنعتی", "موتوری", "بنزینی", "گازی")}


def head_words(breadcrumb: str, leaf: str) -> set[str]:
    parts = [p.strip() for p in breadcrumb.split(">")]
    heads: set[str] = set()
    if len(parts) >= 2:
        # the noun of each part of the level-2 title: 'درایو و اینورتر موتور' -> درایو, اینورتر;
        # 'اره شارژی و برقی' -> اره only ('برقی' alone also matches a vacuum cleaner)
        for seg in re.split(r"\s+و\s+|،|,", norm_text(parts[1])):
            sw = [w for w in _words(seg) if len(w) > 2 and w not in ADJ]
            if sw:
                heads.add(sw[0])
    lw = _words(leaf)
    if lw:
        heads.add(lw[0])
    return heads


EN_ADJ = {"electric", "electrical", "electronic", "industrial", "automatic", "digital", "manual", "small", "mini",
          "large", "high", "low", "single", "three", "two", "phase", "way", "type", "heavy", "duty", "portable",
          "smart", "stainless", "steel", "plastic", "brass", "for", "and", "with", "the", "of", "machine", "system",
          "equipment", "device", "unit", "set", "china", "price", "italy", "italian", "germany", "german",
          "japan", "japanese", "iran", "iranian", "turkey", "korea", "taiwan", "original"}
EN_NEGATIVE = ["spare part", "spare parts", "repair", "used", "second hand", "refurbished", "toy", "course",
               "auto parts", "car parts", "puzzle", "vending", "game", "mat", "flooring", "scrubber",
               "blade", "blades", "bearing", "bearings", "contactor", "gear", "surgery", "surgical", "orthopedic", "medical", "accessories", "accessory", "vw", "volkswagen", "toyota", "honda", "bmw", "audi", "hyundai", "kia",
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
    own = f" {(english or '').lower()} {(group_en or '').lower()} "
    neg = next((n for n in EN_NEGATIVE if f" {n} " in name.replace("/", " ").replace(",", " ") and f" {n} " not in own),
               None)                                    # 'blade' is a part, except in a saw-blade category
    if neg:
        return {"relevant": False, "confidence": 0.8, "reason": f"«{neg}» در نام هست (قطعه/غیرمرتبط)"}
    core = [w for w in _en_words(group_en or english) if w not in EN_ADJ] or _en_words(group_en or english)
    need = min(2, len(set(core)))                            # 'Car Jack with safety Valve' shares only 'valve'
    if core and len(set(core) & name_words) < need:
        return {"relevant": False, "confidence": 0.7,
                "reason": f"کمتر از {need} واژه‌ی اصلی دسته در نام هست: " + "، ".join(core)}
    core_hit = len(set(core) & name_words) / len(set(core)) if core else 1.0
    if len(set(core)) >= 4 and core_hit < 0.5:   # 'cable for roller shutter door' is not a 'shutter door control box'
        return {"relevant": False, "confidence": 0.65,
                "reason": f"فقط {round(core_hit * 100)}٪ واژه‌های اصلی دسته در نام هست: " + "، ".join(core)}
    want = [w for w in _en_words(english) if w not in {_stem(x) for x in EN_ADJ if x in ("for", "and", "with")}]
    share = sum(w in name_words for w in want) / len(want) if want else 1.0
    return {"relevant": True, "confidence": round(0.5 + 0.5 * share, 2),
            "reason": f"{round(share * 100)}٪ واژه‌های انگلیسی دسته در نام هست"}


def _flat(s: str) -> str:
    """Latin/digit runs of the name, each glued ('GTA 26' -> 'gta26', 'CCS-2141' -> 'ccs2141'),
    separated by '|' wherever a Persian word stands between them, so '16 نفره سرعت 1.6' is '16|16', not '1616'."""
    from .normalize import fa_digits_to_en
    t = fa_digits_to_en(str(s or "")).lower()
    t = re.sub(r"[\u0600-\u06FF\u200c]+", " | ", t)                       # a Persian word separates runs
    t = re.sub(r"[^0-9a-z|\s]+", "", t)                                      # '1.6' -> '16', 'ccs-2141' -> 'ccs2141'
    t = re.sub(r"(?<=[0-9a-z])\s+(?=[0-9a-z])", "", t)                       # 'gta 26' -> 'gta26'
    return "|" + "|".join(t.replace(" ", "").split("|")) + "|"


def code_in(t: str, k: str) -> bool:
    """'8020' is in '8020h', 'gst8000e', '61kw' (letters around it are fine) but '17' is not in
    'ct18173m' and '6' is not in '16': a number must not run on into more digits."""
    i = k.find(t)
    while i >= 0:
        before = k[i - 1] if i > 0 else ""
        after = k[i + len(t)] if i + len(t) < len(k) else ""
        if not (before.isdigit() and t[0].isdigit()) and not (after.isdigit() and t[-1].isdigit()):
            return True
        i = k.find(t, i + 1)
    return False


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
    measure_units = units | {"وات", "سانت", "سانتی", "سانتیمتر", "میلیمتر", "کیلووات", "ولت", "امپر", "آمپر", "لیتر",
                             "متر", "w", "cm", "mm", "v", "kw"}
    codes, measures = [], []
    for i, w in enumerate(words):
        if re.search(r"\d", w) and w not in parents:
            t = re.sub(r"[^0-9a-z]", "", w)
            if not t:
                continue
            nxt = words[i + 1] if i + 1 < len(words) else ""
            # a number followed by a unit (650 وات, 40 سانتی) is a size; '5056j', 'gta26', '8600' are model codes
            is_measure = t.isdigit() and (nxt in measure_units or len(t) <= 2)
            (measures if is_measure else codes).append(t)
    out = codes or measures               # a model code is enough; sizes count only when there is no code
    return list(dict.fromkeys(out))


def leaf_brand_word(w: str, leaf: str) -> bool:
    """The brand word of a brand category is checked by its own rule (in any spelling), not counted here."""
    from .routing import leaf_brand
    b = leaf_brand(leaf)
    return bool(b) and norm_text(b[0]) == w


# ---------------------------------------------------------------- what a sub-category adds to its parent

MODE = re.compile(r"(?<!\S)(دو|سه|چهار|پنج|شش|[2-6])\s*(حالته|کاره|منظوره)")
_N = {"دو": "2", "سه": "3", "چهار": "4", "پنج": "5", "شش": "6"}
CORDLESS = re.compile(r"شارژی|شارژ|باتری|باطری|براشلس|cordless|battery|li-?ion|\d+(\.\d+)?\s*(ولت|v)\b", re.I)
UNITS = {norm_text(w) for w in ("اینچ", "نفره", "نفر", "اسب", "کیلو", "کیلویی", "متری", "متر", "لیتری", "لیتر", "تن", "تنی",
                                 "فاز", "وات", "کیلووات", "ولت", "آمپر", "امپر", "سانت", "سانتی", "سانتیمتر", "میلیمتر",
                                 "میلی", "سرعت", "مدل", "سایز", "شیار", "حالته", "کاره", "منظوره")}
NUM_WORDS = {norm_text(w) for w in ("یک", "دو", "سه", "چهار", "پنج", "شش", "هفت", "هشت", "نه", "ده",
                                     # words no product name says, so they cannot be checked
                                     "قدیمی", "جدید", "اصل", "اصلی", "اورجینال", "کپی", "طرح", "درجه", "یک", "باکیفیت",
                                     "ارزان", "حرفه", "حرفه‌ای", "خانگی", "نیمه")}
DECISIVE = {norm_text(w) for w in ("شارژی", "برقی", "بنزینی", "دیزلی", "گازی", "دستی", "بادی", "پنوماتیک", "پنوماتیکی",
                                    "هیدرولیک", "هیدرولیکی")}


UNIT_CLASS = [("cm", ("سانت", "سانتی", "سانتیمتر", "سانتی متر", "سانتی‌متر", "cm")),
              ("mm", ("میلیمتر", "میلی متر", "میلی‌متر", "میل", "mm")),
              ("v", ("ولت", "v")), ("w", ("وات", "w")), ("kw", ("کیلووات", "کیلو وات", "kw")),
              ("kg", ("کیلویی", "کیلوگرم", "کیلو", "kg")), ("inch", ("اینچ", "inch")), ("person", ("نفره", "نفر")),
              ("l", ("لیتری", "لیتر", "l")), ("hp", ("اسب",)), ("bar", ("بار", "bar")), ("a", ("آمپر", "امپر", "a"))]
UNIT_FA = {"cm": "سانتی", "mm": "میلیمتر", "v": "ولت", "w": "وات", "kw": "کیلووات", "kg": "کیلو", "inch": "اینچ",
           "person": "نفره", "l": "لیتر", "hp": "اسب", "bar": "بار", "a": "آمپر"}
_UNIT_RE = sorted(((u, c) for c, us in UNIT_CLASS for u in us), key=lambda x: -len(x[0]))


def measures(text: str) -> set[tuple[str, str]]:
    """{('20', 'v'), ('25', 'cm')} from '20 ولت 25 سانتی' (numbers as written, Persian digits too)."""
    from .normalize import fa_digits_to_en
    t = fa_digits_to_en(norm_text(str(text or ""))).lower()
    out = set()
    for m in re.finditer(r"(\d+(?:[.,/]\d+)?)\s*([^\d\s]+(?:\s[^\d\s]+)?)", t):
        num, rest = m.group(1).replace(",", "."), m.group(2)
        for u, c in _UNIT_RE:
            if rest == u or rest.startswith(u + " ") or (rest.startswith(u) and not re.match(r"[a-z\u0600-\u06FF]", rest[len(u):len(u) + 1] or " ")):
                out.add((num, c))
                break
    return out


def _mode(text: str) -> str | None:
    m = MODE.search(norm_text(text))
    return _N.get(m.group(1), m.group(1)) if m else None


def _latin_of(phrase: str) -> list[str]:
    """Latin spellings of a Persian brand word, from the routing list and the brand table."""
    from .routing import LATIN
    out = []
    p = norm_text(phrase)
    for k, v in LATIN.items():
        if norm_text(k) == p or norm_text(k).replace(" ", "") == p.replace(" ", ""):
            out.append(v.lower())
    try:
        from . import brands_fa as BF
        for x in BF._TABLE_CACHE.get("rows", []):
            if x.get("en") and (BF.key(x.get("fa")) == BF.key(phrase) or _fa_key(x.get("fa")) == _fa_key(phrase)):
                out.append(str(x["en"]).lower())
    except Exception:                       # noqa: BLE001 - the table is optional here
        pass
    return [o for o in dict.fromkeys(out) if len(o) >= 2]


def _fa_key(s) -> str:
    return re.sub(r"[\s\u200c\-_.]+", "", norm_text(str(s or "")))


def added_words(breadcrumb: str, leaf: str) -> list[str]:
    """The words a sub-category adds to its parent: 'دریل بتن کن شارژی توتال' under 'دریل بتن کن شارژی'
    -> ['توتال']; 'فلوتر آسانسوری کولر آبی' -> ['کولر', 'آبی']. Sizes, model numbers, origin and
    power-source words have rules of their own. Empty when the leaf does not extend its parent's name."""
    parts = [p.strip() for p in breadcrumb.split(">")]
    if len(parts) < 2:
        return []
    pw = set(norm_text(parts[-2]).split())
    lw = norm_text(leaf).split()
    core = [w for w in pw if w not in STOP and len(w) > 1]
    if not core or not all(w in lw for w in core):
        return []
    return [w for w in lw if w not in pw and w not in STOP and w not in UNITS and w not in DECISIVE
            and w not in ORIGIN_WORDS and w not in NUM_WORDS and not re.search(r"\d", w) and len(w) > 1
            and not re.fullmatch(r"[a-z]{1,3}", w)]


def check_added(breadcrumb: str, leaf: str, product: dict) -> dict | None:
    """A sub-category takes only products that are what it adds: its brand ('توتال'), its use
    ('کولر آبی'), its power source (شارژی), its number of modes (دو حالته). None when it fits."""
    name = str(product.get("name") or "")
    nm = norm_text(f"{name} {product.get('brand') or ''}")
    words, flat, low = set(nm.split()), nm.replace(" ", ""), nm.lower()
    lf = set(norm_text(leaf).split())
    # power source: a cordless category takes battery tools only, a corded one no battery tools
    if "شارژی" in lf and not CORDLESS.search(name):
        return {"relevant": False, "confidence": 0.8, "reason": "دسته‌ی شارژی است ولی محصول شارژی نیست (برقی/سیمی)"}
    if "برقی" in lf and "شارژی" not in lf and "شارژی" in words and "برقی" not in words:
        return {"relevant": False, "confidence": 0.8, "reason": "دسته‌ی برقی (سیمی) است ولی محصول شارژی است"}
    if "دستی" in lf and not ({"برقی", "شارژی", "بنزینی"} & lf) and ({"شارژی", "بنزینی", "موتوری"} & words):
        return {"relevant": False, "confidence": 0.8, "reason": "دسته‌ی دستی است ولی محصول موتوری/شارژی است"}
    if "بنزینی" in lf and ({"برقی", "شارژی"} & words) and "بنزینی" not in words:
        return {"relevant": False, "confidence": 0.8, "reason": "دسته‌ی بنزینی است ولی محصول برقی/شارژی است"}
    if ({"برقی", "شارژی"} & lf) and "بنزینی" in words and "بنزینی" not in lf:
        return {"relevant": False, "confidence": 0.8, "reason": "دسته‌ی برقی/شارژی است ولی محصول بنزینی است"}
    # the same number with another unit: '20 ولت' is not '20 سانتی'
    lmes, pmes = measures(leaf), measures(name)
    for n_, u_ in lmes:
        others = {u for n2, u in pmes if n2 == n_ and u != u_}
        if others and (n_, u_) not in pmes:
            return {"relevant": False, "confidence": 0.8,
                    "reason": f"«{n_} {UNIT_FA.get(u_, u_)}» دسته با «{n_} {UNIT_FA.get(next(iter(others)), '')}» محصول فرق دارد"}
    # number of modes: 'دو حالته' does not take a 'چهار حالته'
    lm, pm = _mode(leaf), _mode(name)
    if lm and pm and lm != pm:
        return {"relevant": False, "confidence": 0.8, "reason": f"دسته‌ی {lm} حالته است ولی محصول {pm} حالته است"}
    # country of make: 'موتور آسانسور گیرلس ایتالیایی' takes what the page says is Italian
    # (or, for 'ایرانی', a brand known to be Iranian)
    leaf_c = [g for g in COUNTRIES_ALL if g & lf]
    if leaf_c:
        from .routing import brand_countries, find_brand
        text = norm_text(f"{name} {product.get('short_description') or ''} {str(product.get('description') or '')[:600]}")
        tw = set(text.split())
        said = any(w in tw or w in text for g in leaf_c for w in g)
        if product.get("source_type") == "foreign" and any(any("چین" in w for w in g) for g in leaf_c):
            said = True                                    # a Made-in-China supplier's own product
        fb = find_brand(name)                               # brands.csv: (brand, iranian | foreign | unknown)
        if not fb:                                         # a Latin brand name: 'STIHL' -> اشتیل
            from .routing import LATIN
            low_ = name.lower()
            for fa_, en_ in LATIN.items():
                if len(en_) >= 3 and re.search(rf"(?<![a-z]){re.escape(en_)}(?![a-z])", low_):
                    fb = find_brand(fa_)
                    if fb:
                        break
        origin = fb[1] if fb else ""
        country_fa = brand_countries().get(fb[0], "") if fb else ""
        country = norm_text(country_fa)
        iran_cat = any(any("ایران" in w for w in g) for g in leaf_c)
        if country:                                        # the brand's country is known: it decides
            by_brand = any(country in g or country + "ی" in g or country + "یی" in g for g in leaf_c)
        else:                                              # only "Iranian or not": enough for 'ایرانی' only
            by_brand = iran_cat and origin == "iranian"
        if fb and country and not by_brand and not said:
            return {"relevant": False, "confidence": 0.8,
                    "reason": f"دسته‌ی ساخت {'/'.join(sorted(leaf_c[0]))} است ولی برند «{fb[0]}» ساخت {country_fa} است"}
        if not said and not by_brand:
            return {"relevant": False, "confidence": 0.75,
                    "reason": f"دسته‌ی ساخت {'/'.join(sorted(leaf_c[0]))} است ولی صفحه‌ی محصول کشور را نمی‌گوید"}
    # what the sub-category adds to its parent must be in the name (any spelling)
    # (a Latin-only name, from the Chinese route, is checked against the category's English term instead)
    add = added_words(breadcrumb, leaf) if re.search(r"[\u0600-\u06FF]", name) else []
    if add:
        found = [w for w in add if w in words or w in flat]
        need = len(add) if len(add) <= 2 else len(add) - 1   # 'سقفی مربع' needs مربع too; 'دی سی ای' two of three
        if len(found) < need:
            phrase = " ".join(add)
            alts = _latin_of(phrase) + [x for w in add for x in _latin_of(w)]
            if not any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", low) for a in alts):
                miss = [w for w in add if w not in found]
                return {"relevant": False, "confidence": 0.8,
                        "reason": f"«{' '.join(miss)}» (آنچه این دسته به دسته‌ی بالاتر اضافه می‌کند) در نام محصول نیست"}
    return None


def judge(breadcrumb: str, leaf: str, product: dict, english: str = "", group_en: str = "") -> dict:
    import re
    from .routing import brand_in, foreign_brand_in, leaf_brand
    name = product.get("name") or ""
    need = model_tokens(breadcrumb, leaf)
    if need:
        flat = _flat(name)
        toks = [t for t in flat.split("|") if t]
        miss = [t for t in need if not any(code_in(t, k) for k in toks)]
        if miss:                     # 'اره شارژی ویوارکس 2008' takes only the 2008, not every Vivarex saw
            return {"relevant": False, "confidence": 0.8,
                    "reason": "مدل یا اندازه‌ی دسته در نام نیست: " + "، ".join(miss)}
    b = leaf_brand(leaf)
    if b and not brand_in(name, b[0]):
        # a brand category (e.g. «محرک الکتریکی شیر سه طرفه بوتان») takes only that brand
        return {"relevant": False, "confidence": 0.8, "reason": f"دسته‌ی برند «{b[0]}» است ولی برند در نام محصول نیست"}
    added = check_added(breadcrumb, leaf, product)
    if added:
        return added
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
    if product.get("source_type") == "foreign" and re.search(
            r"آلمان|ایتالیا|ژاپن|ایران|ترکیه|کره|تایوان|اسپانیا|فرانسه|سوئیس|انگلیس|آمریکا|امریکا|اصل|اورجینال", leaf):
        return {"relevant": False, "confidence": 0.8,
                "reason": "دسته‌ی ساخت کشور دیگر/اصل است؛ کالای مسیر چینی جای آن نیست"}
    if english and not re.search(r"[\u0600-\u06FF]", product.get("name") or ""):
        return judge_en(english, group_en, product)
    name = norm_text(f"{product.get('name', '')} {product.get('short_description', '')[:120]}")
    name_words = set(name.split())
    cat = norm_text(breadcrumb)
    heads = head_words(breadcrumb, leaf)
    only_name = norm_text(product.get("name", ""))           # negative words count in the name only, not the description
    neg = next((n for n in NEGATIVE if f" {n} " in f" {only_name} " and f" {n} " not in f" {cat} "), None)
    if neg:
        return {"relevant": False, "confidence": 0.8, "reason": f"کلمه‌ی «{neg}» در نام هست (قطعه/خدمت/غیرمرتبط)"}
    lead = next((n for n in LEAD_PARTS if f" {only_name} ".startswith(f" {n} ") and f" {n} " not in f" {cat} "
                 and n != norm_text("برد")), None)
    if lead:
        return {"relevant": False, "confidence": 0.8, "reason": f"نام با «{lead}» شروع می‌شود (قطعه است، نه خود دستگاه)"}
    lw0 = _words(leaf)
    if heads and not (heads & name_words) and len(lw0) > 1 and len(lw0[1]) > 2 and lw0[1] not in ADJ \
            and lw0[1] not in ORIGIN_WORDS and lw0[1] in name_words:
        heads = heads | {lw0[1]}                 # 'عمود بر توسن' is an 'اره عمود بر' without the word اره
    # a leaf that names a new thing under its parent ('چشمی گرد' under 'کرکره برقی') needs that word itself
    l2w = set(_words(re.sub(r"\s+و\s+|،", " ", breadcrumb.split(">")[1]))) if breadcrumb.count(">") >= 1 else set()
    if lw0 and len(lw0) <= 3 and lw0[0] not in l2w and len(lw0[0]) > 2 and not (lw0[0] in name_words or lw0[0] in name.replace(" ", "")) \
            and not (len(lw0) > 1 and lw0[1] not in ADJ and only_name.split()[:1] == [lw0[1]]):
        # the first word may be implied only when the name starts with the second ('بتن کن …' is a
        # 'دریل بتن کن'); 'اره گرد بر' is not a 'چشمی گرد'
        return {"relevant": False, "confidence": 0.7, "reason": f"نام اصلی دسته («{lw0[0]}») در نام محصول نیست"}
    if heads and not (heads & name_words):
        return {"relevant": False, "confidence": 0.7,
                "reason": "هیچ‌کدام از کلمه‌های اصلی دسته در نام نیست: " + "، ".join(sorted(heads))}
    # origin words ('آلمان', 'اصل', 'چینی') are rarely in a product name: they are checked
    # as a condition, not counted as words the name must contain
    lw_all = [w for w in _words(leaf) if len(w) > 1]
    origin = [w for w in lw_all if w in ORIGIN_WORDS]
    if any(w in ("چینی", "چین") for w in origin) and not re.search(r"چین|طرح|کپی|china", name, re.I):
        return {"relevant": False, "confidence": 0.7, "reason": "دسته‌ی نمونه‌ی چینی است ولی محصول چینی/طرح نیست"}
    if any(w not in ("چینی", "چین") for w in origin) and re.search(r"ساخت چین|چینی|طرح|کپی|china", name, re.I):
        return {"relevant": False, "confidence": 0.7, "reason": "دسته‌ی اصل/" + "، ".join(origin) + " است ولی محصول چینی/طرح است"}
    leaf_c = [g for g in COUNTRIES if g & set(norm_text(leaf).split())]
    if leaf_c:
        own = set(only_name.split())                   # the name only: descriptions mention other countries
        other = next((w for g in COUNTRIES if g not in leaf_c for w in g if w in own), None)
        if other:
            return {"relevant": False, "confidence": 0.7, "reason": f"کشور دسته فرق دارد: محصول «{other}» است"}
    lw = [w for w in lw_all if w not in ORIGIN_WORDS and not leaf_brand_word(w, leaf)]
    flat = name.replace(" ", "")
    hit = [w for w in lw if w in name_words or w in flat]                     # 'عمود بر' = 'عمودبر', 'تک فاز' = 'تکفاز'
    if lw and lw[0] in heads and lw[0] not in hit and heads & name_words:
        hit.append(lw[0])                         # the category's other name for the same thing (درایو = اینورتر)
    share = len(hit) / len(lw) if lw else 1.0
    if len(lw) >= 3 and share < 0.5 and not leaf_brand(leaf):   # a brand category already matched its brand
        return {"relevant": False, "confidence": 0.6,
                "reason": f"فقط {round(share * 100)}٪ کلمه‌های دسته در نام هست (نوع دیگری است)"}
    return {"relevant": True, "confidence": round(0.5 + 0.5 * share, 2),
            "reason": f"{round(share * 100)}٪ کلمه‌های دسته در نام هست"}
