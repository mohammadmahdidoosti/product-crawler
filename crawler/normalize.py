"""Persian text normalization, price parsing and model-number extraction.

Everything that compares or matches text goes through `norm_text` first, so
"ي/ی", "ك/ک", Persian/Arabic digits, ZWNJ and spacing differences never
cause a missed match.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from typing import Iterable

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CHARS = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه",
    "أ": "ا", "إ": "ا", "ٱ": "ا", "آ": "ا",
    "‌": " ", "‍": "", "‎": "", "‏": "", "\xa0": " ",
    "ـ": "",  # tatweel
})
_DIACRITICS = re.compile(r"[ً-ٰٟ]")


def fa_digits_to_en(s: str) -> str:
    return s.translate(_DIGITS)


def norm_text(s: object) -> str:
    """Lowercase, unify Persian letters/digits, collapse spaces."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = fa_digits_to_en(s).translate(_CHARS)
    s = _DIACRITICS.sub("", s)
    s = s.lower()
    s = re.sub(r"[\"'«»“”()\[\]{}،,؛;:|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(s: object) -> set[str]:
    return {t for t in norm_text(s).split() if t}


# ---------------------------------------------------------------- prices

_NUM = re.compile(r"\d[\d,٬'\s.]*\d|\d")


def parse_number(s: object) -> float | None:
    """'۱۲,۰۰۰,۰۰۰ تومان' -> 12000000.0 ; returns None if no number."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = fa_digits_to_en(str(s)).replace("٬", ",")
    m = _NUM.search(txt)
    if not m:
        return None
    raw = re.sub(r"[\s',]", "", m.group(0))
    # "12.000.000" (dots as thousand separators) vs "12.5"
    if raw.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


RIAL_WORDS = ("ریال", "rial", "irr")
TOMAN_WORDS = ("تومان", "تومن", "toman", "irt")


def detect_unit(text: object) -> str | None:
    """Return 'toman', 'rial' or None from a currency code or visible text."""
    t = norm_text(text)
    if not t:
        return None
    has_t = any(w in t for w in TOMAN_WORDS)
    has_r = any(w in t for w in RIAL_WORDS)
    if has_t and not has_r:
        return "toman"
    if has_r and not has_t:
        return "rial"
    return None


def dominant_unit_in_page(page_text: str) -> str | None:
    """Which unit word appears more often next to numbers on the page."""
    t = norm_text(page_text)
    n_t = len(re.findall(r"\d\s*(?:تومان|تومن)", t))
    n_r = len(re.findall(r"\d\s*ریال", t))
    if n_t > n_r:
        return "toman"
    if n_r > n_t:
        return "rial"
    return None


def to_toman(amount: float | None, unit: str | None) -> float | None:
    if amount is None:
        return None
    if unit == "rial":
        return amount / 10
    return amount


def median(values: Iterable[float]) -> float | None:
    v = [x for x in values if x is not None]
    return statistics.median(v) if v else None


# ---------------------------------------------------------------- models

# A model number: latin letters + digits, optionally with - / . e.g.
# YD101-00P7-T2S, LC1D32M7, 3RT2026-1AP00, ABB-AF40
_MODEL = re.compile(r"\b(?=[a-z0-9./-]*\d)(?=[a-z0-9./-]*[a-z])[a-z0-9][a-z0-9./-]{3,}[a-z0-9]\b")
# tokens that look like models but are units/specs
_NOT_MODEL = re.compile(
    r"^\d+(\.\d+)?(v|kw|w|hp|a|mm|cm|m|bar|psi|ma|hz|rpm|l|lit|kg|vac|vdc|ip\d\d)$"
)


def extract_models(*texts: object) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in _MODEL.findall(norm_text(t)):
            m = m.strip("-./")
            if _NOT_MODEL.match(m) or len(m) < 4:
                continue
            if m not in out:
                out.append(m)
    return out


def model_key(model: object) -> str:
    """Compare models ignoring separators: 'YD101-00P7' == 'yd10100p7'."""
    return re.sub(r"[^a-z0-9]", "", norm_text(model))


# ---------------------------------------------------------------- phones

_PHONE = re.compile(r"(?<!\d)(?:\+98|0098|0)(?:9\d{9}|[1-8]\d{1,2}[\s-]?\d{7,8})(?!\d)")


def extract_phones(text: object, limit: int = 3) -> list[str]:
    t = fa_digits_to_en(str(text or ""))
    t = re.sub(r"(?<=\d)[\s-](?=\d)", "", t)  # 021-8888 8888 -> 02188888888
    found: list[str] = []
    for p in _PHONE.findall(t):
        p = re.sub(r"[\s-]", "", p)
        p = re.sub(r"^(\+98|0098)", "0", p)
        if 10 <= len(p) <= 11 and p not in found:
            found.append(p)
        if len(found) >= limit:
            break
    return found
