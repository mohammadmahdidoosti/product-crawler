"""English search term for one leaf = its group's term + the leaf's own modifiers.

The glossary (glossary/groups_en.csv) gives one term per group, so siblings
like «اکچویتور برقی خطی» and «اکچویتور برقی تدریجی» would otherwise search
Made-in-China with the same words and get the same products. Here the words
of the leaf that are not in the group title are translated from a small
technical dictionary (phase, size, material, type, use) and appended.
Numbers and Latin tokens pass through; unknown Persian words are dropped
(they are mostly brands or wording, which a foreign search cannot use).
"""
from __future__ import annotations

import re

from .normalize import fa_digits_to_en, norm_text

PHRASES = {  # two-word modifiers first
    "تک فاز": "single phase", "سه فاز": "three phase", "دو فاز": "two phase",
    "سه طرفه": "3 way", "دو طرفه": "2 way", "یک طرفه": "check", "چهار طرفه": "4 way",
    "سه راهه": "3 way", "دو راهه": "2 way", "پروانه ای": "butterfly", "ضد انفجار": "explosion proof",
    "ضد اب": "waterproof", "ضد زنگ": "stainless", "پلی اتیلن": "pe", "پلی پروپیلن": "pp",
    "دنده ای": "gear", "فشار قوی": "high voltage", "فشار ضعیف": "low voltage", "تمام اتوماتیک": "fully automatic",
    "نیمه اتوماتیک": "semi automatic", "دو قلو": "twin", "روی زمین": "floor", "زیر زمینی": "underground",
    "کف کش": "submersible drainage", "طبقاتی عمودی": "vertical multistage", "طبقاتی افقی": "horizontal multistage",
    "استپر موتور": "stepper motor", "سروو موتور": "servo motor", "حالت جامد": "solid state",
    "اب گرم": "hot water", "اب سرد": "cold water", "اب شرب": "drinking water", "اب استخر": "pool water",
    "خورشیدی": "solar", "دو زمانه": "2 stroke", "چهار زمانه": "4 stroke", "دو حالته": "dual mode",
}
WORDS = {
    # size / units
    "اینچ": "inch", "امپر": "A", "ولت": "V", "وات": "W", "کیلووات": "kW", "اسب": "hp", "لیتری": "liter",
    "لیتر": "liter", "متری": "m", "متر": "m", "میلیمتر": "mm", "سانتی": "cm", "بار": "bar", "کیلویی": "kg",
    "کیلوگرمی": "kg", "تن": "ton", "تنی": "ton", "درجه": "degree", "گالن": "gallon", "نمره": "size",
    # type / mechanism
    "تدریجی": "modulating", "خطی": "linear", "کوچک": "mini", "مینی": "mini", "مینیاتوری": "miniature",
    "بزرگ": "large", "دستی": "manual", "اتوماتیک": "automatic", "اتومات": "automatic", "دیجیتال": "digital",
    "دیجیتالی": "digital", "انالوگ": "analog", "عقربه ای": "analog", "هوشمند": "smart", "پنوماتیک": "pneumatic",
    "هیدرولیک": "hydraulic", "هیدرولیکی": "hydraulic", "برقی": "electric", "الکتریکی": "electric",
    "مکانیکی": "mechanical", "الکترونیکی": "electronic", "شارژی": "cordless", "بنزینی": "gasoline",
    "دیزلی": "diesel", "گازوییلی": "diesel", "گازی": "gas", "پیستونی": "piston", "فنری": "spring",
    "سلونوییدی": "solenoid", "سلونویید": "solenoid", "اهرمی": "lever", "کشویی": "gate", "توپی": "ball",
    "سوزنی": "needle", "ویفری": "wafer", "فلنجی": "flanged", "فلنج": "flange", "رزوه ای": "threaded",
    "رزوه": "threaded", "جوشی": "welded", "پرسی": "press", "چسبی": "glue", "پیچی": "screw",
    "دوبل": "double", "یکسر": "single", "دوقلو": "twin", "پرتابل": "portable", "ثابت": "fixed",
    "متغیر": "variable", "پروپرشنال": "proportional", "سایلنت": "silent", "سینوسی": "pure sine wave",
    "اینورتری": "inverter", "سوییچینگ": "switching", "تایمردار": "with timer", "تایمر": "timer",
    "حرارتی": "thermal", "مغناطیسی": "magnetic", "ترموستاتیک": "thermostatic", "ترموستات": "thermostat",
    "کنترل": "control", "کنترلی": "control", "فشارشکن": "pressure reducing", "اطمینان": "safety",
    "یکطرفه": "check", "دوطرفه": "double acting", "تبدیلی": "reducer", "تبدیل": "adapter",
    "سانتریفیوژ": "centrifugal", "محیطی": "peripheral", "شناور": "submersible", "طبقاتی": "multistage",
    "جتی": "jet", "خودمکش": "self priming", "دنده": "gear", "حلزونی": "worm", "سیاره ای": "planetary",
    "شفت": "shaft", "پایه": "base", "پایه دار": "with base", "دیواری": "wall mounted", "سقفی": "ceiling",
    "ایستاده": "floor standing", "توکار": "concealed", "روکار": "surface mounted", "تابلویی": "panel mount",
    "ریلی": "din rail", "کامپکت": "compact", "صنعتی": "industrial", "خانگی": "household",
    "کشاورزی": "agricultural", "ابیاری": "irrigation", "گلخانه": "greenhouse", "گلخانه ای": "greenhouse",
    "استخر": "pool", "استخری": "pool", "فاضلاب": "sewage", "فاضلابی": "sewage", "کفکش": "drainage",
    "لباسشویی": "washing machine", "ظرفشویی": "dishwasher", "یخچال": "refrigerator", "کولر": "cooler",
    "اسانسور": "elevator", "تراکتور": "tractor", "ژنراتور": "generator", "موتوری": "motorized",
    "دمپر": "damper", "شیر": "valve", "پمپ": "pump", "موتور": "motor", "فن": "fan", "کمپرسور": "compressor",
    "سنسور": "sensor", "رگولاتور": "regulator", "کنتاکتور": "contactor", "کلید": "switch", "سوکت": "socket",
    "کابل": "cable", "سیم": "wire", "شلنگ": "hose", "شلنگی": "hose", "لوله": "pipe", "اتصال": "fitting",
    "کوپلینگ": "coupling", "فیلتر": "filter", "کارتریج": "cartridge", "قطره": "drip", "قطره ای": "drip",
    "بارانی": "sprinkler", "سمپاش": "sprayer", "جک": "jack", "سیلندر": "cylinder", "تایمری": "timer",
    "بخار": "steam", "هوا": "air", "هوایی": "air", "گاز": "gas", "اسید": "acid", "روغنی": "oil", "روغن": "oil",
    "خشک": "dry", "سرد": "cold", "گرم": "hot", "نسوز": "refractory", "ضد": "anti",
    # material / finish
    "برنجی": "brass", "برنج": "brass", "استیل": "stainless steel", "فولادی": "steel", "چدنی": "cast iron",
    "چدن": "cast iron", "الومینیومی": "aluminum", "الومینیوم": "aluminum", "مسی": "copper", "مس": "copper",
    "پلاستیکی": "plastic", "پلیمری": "polymer", "گالوانیزه": "galvanized", "فلزی": "metal", "چوبی": "wooden",
    "شیشه ای": "glass", "سرامیکی": "ceramic", "لاستیکی": "rubber", "سیلیکونی": "silicone", "کرومی": "chrome",
    "upvc": "upvc", "u-pvc": "upvc", "pvc": "pvc", "c-pvc": "cpvc", "pp": "pp", "pe": "pe",
    "سفید": "white", "مشکی": "black", "شفاف": "transparent",
    # count
    "تک": "single", "دو": "2", "سه": "3", "چهار": "4", "پنج": "5", "یک": "1", "نیم": "half",
    "تکفاز": "single phase", "سهفاز": "three phase", "دوزمانه": "2 stroke", "چهارزمانه": "4 stroke",
    "سولار": "solar", "led": "led", "dc": "dc", "ac": "ac", "smd": "smd",
}
_P = {norm_text(k): v for k, v in PHRASES.items()}
_W = {norm_text(k): v for k, v in WORDS.items()}
LATIN = re.compile(r"^[a-z0-9][a-z0-9./\-]*$", re.I)


def leaf_english(leaf: str, group_title: str, group_en: str, max_extra: int = 5) -> str:
    if not group_en:
        return ""
    g = set(norm_text(group_title).split())
    words = [w for w in norm_text(fa_digits_to_en(leaf)).split() if w not in g]
    extra, i = [], 0
    while i < len(words):
        two = " ".join(words[i:i + 2])
        if two in _P:
            extra.append(_P[two])
            i += 2
            continue
        w = words[i]
        if w in _W:
            extra.append(_W[w])
        elif LATIN.match(w) and (any(c.isdigit() for c in w) or w in _W):
            extra.append(w)                      # sizes and model-like tokens: 1/2, 3/4, 220v
        i += 1
    have = set(group_en.lower().split())
    out = []
    for e in extra:
        if not set(e.lower().split()) <= have and e not in out:
            out.append(e)
    return " ".join([group_en] + out[:max_extra])
