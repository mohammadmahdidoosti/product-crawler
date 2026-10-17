"""Persian name, specs and description for foreign (English) products — no model, no tokens.

Name    = the Caravan category name + model + up to 3 key specs, e.g.
          «اکچویتور برقی تدریجی مدل HEQ، AC220V، اتصال فلنجی، آلیاژ آلومینیوم»
Specs   = keys and values through glossary/specs_fa.csv and values_fa.csv.
          Numbers, units and codes (AC220V, DN50, IP67) pass unchanged.
          Anything else not in the dictionary keeps its English text and is
          counted in data/untranslated.csv, to be added to the dictionary later.
The English original stays in the data (source_name) for the team.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .config import ROOT
from .normalize import norm_text

FA = re.compile(r"[؀-ۿ]")
CODE = re.compile(r"^[A-Za-z]{0,4}[\s\-]?\d[\w\s.,/~\-+°%()*:x×]*$|^[A-Z0-9][A-Z0-9\-/+.]{1,14}$")
TITLE_KEYS = [  # spec keys worth putting in the name, in order
    ("ولتاژ", ("Power Supply", "Voltage", "Rated Voltage", "Operating Voltage", "Input Voltage")),
    ("توان", ("Power", "Rated Power", "Output Power", "Motor Power")),
    ("گشتاور", ("Max Torque", "Output Torque", "Torque", "Rated Torque")),
    ("سایز", ("Nominal Diameter", "Size")),
    ("اتصال", ("Connection", "Connection Form", "Connection Type")),
    ("جنس", ("Body Material", "Material", "Housing Material")),
]
UNITS = {"piece": "عدد", "set": "دستگاه", "unit": "عدد", "pair": "جفت", "meter": "متر", "ton": "تن", "kg": "کیلوگرم",
         "roll": "رول", "boxe": "جعبه", "box": "جعبه"}
SKIP_KEYS = {"customization", "after-sales service", "warranty", "package size", "package gross weight",
             "transport package", "hs code", "production capacity", "trademark", "specification"}


def is_foreign(name: str) -> bool:
    return bool(name) and not FA.search(name)


@lru_cache(maxsize=None)
def _dict(name: str) -> dict[str, str]:
    p = ROOT / "glossary" / name
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig") as f:
        return {r["en"].strip().lower(): r["fa"].strip() for r in csv.DictReader(f) if r.get("fa")}


def tr_key(k: str) -> str | None:
    return _dict("specs_fa.csv").get(k.strip().rstrip(":").lower())


def tr_value(v: str) -> tuple[str, bool]:
    """(text, translated?) — codes and numbers count as translated."""
    v = v.strip()
    d = _dict("values_fa.csv")
    if v.lower() in d:
        return d[v.lower()], True
    m = re.match(r"^([\d,.]+)\s*(Pieces?|Pcs|Sets?|Units?|Pairs?|Meters?|Tons?|Kg|Rolls?|Boxes?)$", v, re.I)
    if m:
        return f"{m.group(1)} {UNITS[m.group(2).lower().rstrip('s') if m.group(2).lower() != 'pcs' else 'piece']}", True
    parts = [p.strip() for p in re.split(r"[,/;]|\s+or\s+|\s+and\s+|\s*&\s*", v) if p.strip()]
    if len(parts) > 1:
        out = [tr_value(p) for p in parts]
        joiner = " یا " if re.search(r"\sor\s", v, re.I) else "، "
        return joiner.join(t for t, _ in out), all(ok for _, ok in out)
    m = re.match(r"^(.+?)\s*\((.+)\)$", v)                 # 'Brass(Hpb59)' -> 'برنج (Hpb59)'
    if m:
        a, ok_a = tr_value(m.group(1))
        b, ok_b = tr_value(m.group(2))
        return f"{a} ({b})", ok_a and ok_b
    if CODE.match(v) or FA.search(v) or (" " not in v and sum(c.isupper() for c in v) >= 2):
        return v, True
    words = v.split()
    if 1 < len(words) <= 5:                                    # word by word, only if every word is known
        tw = [tr_word(w) for w in words]
        if all(tw):
            return " ".join(_fa_order(tw)), True
    return v, False


WORDS_EXTRA = {
    "type": "نوع", "valve": "شیر", "actuator": "عملگر", "joint": "اتصال", "body": "بدنه", "motor": "موتور",
    "pump": "پمپ", "switch": "کلید", "control": "کنترل", "power": "برق", "supply": "تغذیه", "high": "بالا",
    "low": "پایین", "medium": "متوسط", "speed": "سرعت", "pressure": "فشار", "temperature": "دما",
    "normal": "معمولی", "ordinary": "معمولی", "steel": "فولاد", "stainless": "استنلس", "carbon": "کربنی",
    "iron": "آهن", "cast": "ریختگی", "ductile": "داکتیل", "alloy": "آلیاژ", "aluminum": "آلومینیوم",
    "aluminium": "آلومینیوم", "brass": "برنج", "copper": "مس", "plastic": "پلاستیک", "rubber": "لاستیک",
    "water": "آب", "oil": "روغن", "gas": "گاز", "air": "هوا", "steam": "بخار", "electric": "برقی",
    "pneumatic": "پنوماتیک", "hydraulic": "هیدرولیک", "manual": "دستی", "automatic": "اتوماتیک", "rotary": "دورانی",
    "linear": "خطی", "single": "تک", "double": "دو", "three": "سه", "phase": "فاز", "way": "راه", "acting": "کاره",
    "seal": "آب‌بندی", "soft": "نرم", "hard": "سخت", "metal": "فلزی", "flange": "فلنج", "thread": "رزوه",
    "wafer": "ویفری", "ball": "توپی", "butterfly": "پروانه‌ای", "gate": "کشویی", "globe": "سوزنی", "check": "یکطرفه",
    "explosion": "انفجار", "proof": "ضد", "waterproof": "ضد آب", "intelligent": "هوشمند", "smart": "هوشمند",
    "standard": "استاندارد", "customized": "سفارشی", "industrial": "صنعتی", "general": "عمومی", "purpose": "منظوره",
    "black": "مشکی", "white": "سفید", "blue": "آبی", "red": "قرمز", "silver": "نقره‌ای", "gray": "خاکستری",
    "grey": "خاکستری", "yellow": "زرد", "green": "سبز", "modulating": "تدریجی", "regulating": "تنظیمی",
    "on-off": "قطع و وصل", "on/off": "قطع و وصل", "switching": "قطع و وصل", "quarter": "یک‌چهارم", "turn": "دور",
    "multi": "چند", "part": "جزئی", "vertical": "عمودی", "horizontal": "افقی", "mini": "مینی", "small": "کوچک",
    "large": "بزرگ", "compact": "کامپکت", "portable": "پرتابل", "digital": "دیجیتال", "analog": "آنالوگ",
    "signal": "سیگنال", "input": "ورودی", "output": "خروجی", "with": "با", "without": "بدون", "yes": "دارد", "no": "ندارد",
}
ADJ_AFTER = {"برقی", "پنوماتیک", "هیدرولیک", "دستی", "اتوماتیک", "دورانی", "خطی", "نرم", "سخت", "فلزی",
             "ویفری", "توپی", "پروانه‌ای", "کشویی", "سوزنی", "یکطرفه", "هوشمند", "استاندارد", "سفارشی", "صنعتی",
             "مشکی", "سفید", "آبی", "قرمز", "نقره‌ای", "خاکستری", "زرد", "سبز", "تدریجی", "تنظیمی", "عمودی",
             "افقی", "کوچک", "بزرگ", "کامپکت", "پرتابل", "دیجیتال", "آنالوگ", "معمولی", "بالا", "پایین", "متوسط",
             "کربنی", "ریختگی", "داکتیل", "عمومی"}


def tr_word(w: str) -> str | None:
    lw = w.lower().strip(".,:;")
    d = _dict("values_fa.csv")
    if lw in d:
        return d[lw]
    if lw in WORDS_EXTRA:
        return WORDS_EXTRA[lw]
    if CODE.match(w) or (sum(c.isupper() for c in w) >= 2 and " " not in w):
        return w
    return None


def _fa_order(tw: list[str]) -> list[str]:
    """English puts adjectives first ('Rotary Joint Type'); Persian after the noun ('نوع اتصال دورانی')."""
    adj = [t for t in tw if t in ADJ_AFTER]
    nouns = [t for t in tw if t not in ADJ_AFTER]
    return list(reversed(nouns)) + adj if nouns else adj


def translate_specs(specs: dict, missing: Counter | None = None) -> dict[str, str]:
    out = {}
    for k, v in specs.items():
        if not v or k.strip().lower() in SKIP_KEYS:
            continue
        fk = tr_key(k)
        fv, ok = tr_value(str(v))
        if missing is not None:
            if not fk:
                missing[("key", k.strip())] += 1
            if not ok:
                missing[("value", str(v).strip())] += 1
        out[fk or k.strip()] = fv
    return out


TITLE_FEATURES = [  # (pattern in the English title, Persian) — the most telling traits, in this order
    (r"explosion[- ]?proof|\bex[- ]?proof|\batex\b", "ضد انفجار"),
    (r"modulating|proportional|4[- ]?20\s?ma|regulating", "تدریجی"),
    (r"quarter[- ]?turn|part[- ]?turn|90\s?(deg|°)", "یک‌چهارم دور"),
    (r"multi[- ]?turn", "چند دور"),
    (r"\blinear\b", "خطی"),
    (r"\bon[- /]?off\b|switch(ing)? type", "قطع و وصل"),
    (r"intelligent|smart", "هوشمند"),
    (r"waterproof|\bip6[5-8]\b", "ضد آب"),
    (r"\bmini\b|\bsmall\b|\bmicro\b|compact", "کوچک"),
    (r"butterfly", "پروانه‌ای"), (r"\bball valve", "توپی"), (r"\bgate valve", "کشویی"),
    (r"3[- ]?way|three[- ]?way", "سه راه"), (r"2[- ]?way|two[- ]?way", "دو راه"),
    (r"damper", "دمپر"), (r"stainless", "استیل"), (r"\bbrass\b", "برنجی"),
    (r"\b(\d{2,3})\s?v(ac|dc)?\b|\b(ac|dc)\s?(\d{2,3})\s?v\b", None),        # voltage, kept as written
]


def title_features(title: str, have: str, n: int = 2) -> list[str]:
    out = []
    for pat, fa in TITLE_FEATURES:
        m = re.search(pat, title or "", re.I)
        if not m:
            continue
        val = fa or re.sub(r"\s+", "", m.group(0)).upper().replace("V", " ولت").replace("AC", "AC ").replace("DC", "DC ").strip()
        val = re.sub(r"\s+", " ", val)
        if norm_text(val) not in norm_text(have) and all(norm_text(val) not in norm_text(o) for o in out):
            out.append(val)
        if len(out) >= n:
            break
    return out


def persian_name(leaf: str, model: str, specs: dict, title: str = "") -> str:
    bits = []
    for _, keys in TITLE_KEYS:
        for k in keys:
            v = next((val for kk, val in specs.items() if kk.lower() == k.lower() and val), None)
            if v:
                fv, ok = tr_value(str(v))
                if ok and len(fv) <= 22:
                    label = tr_key(k) or ""
                    bits.append(f"{label} {fv}" if label.startswith(("اتصال", "نوع اتصال")) else fv)
                break
        if len(bits) >= 3:
            break
    bits = [b.replace("نوع اتصال ", "اتصال ") for b in bits]
    have = leaf + " " + " ".join(bits)
    feats = title_features(title, have, 2 if model else 3)
    bits = [b for b in bits if not (("ولت" in " ".join(feats)) and re.search(r"\d\s?V", b))]
    name = leaf.strip() + (" " + " ".join(feats) if feats else "")
    if model:
        name += f" مدل {model}"
    return name + (f"، {'، '.join(bits)}" if bits else "")


def save_missing(path: Path, missing: Counter) -> None:
    old = Counter()
    if path.exists():
        with path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                old[(r["kind"], r["text"])] = int(r["count"])
    old.update(missing)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "text", "count", "fa"])
        for (kind, text), n in old.most_common():
            w.writerow([kind, text, n, ""])
