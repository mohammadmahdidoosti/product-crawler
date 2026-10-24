"""Product names without the source shop's sales wording.

Iranian shops write their SEO into the title: 'قیمت و خرید …', '… (( اقساط یک ساله ))',
'{ قیمت امروز + مشخصات }', '- ابزار بیات', 'خرید چکی … با چک صیادی در کرج و تهران', or an
English copy of the name after the Persian one. On Caravan's site the name is the product only.

What the product is never goes: model, power, size, speed, material and also '(های کپی)',
which says the product is not original and must stay visible.
"""
from __future__ import annotations

import re

from .normalize import norm_text

FA = re.compile(r"[؀-ۿ]")

# whole groups that are only sales talk: {…}, ((…)), (…) holding these words
SALES_WORDS = r"(قیمت|خرید|فروش|اقساط|قسطی|چک\s*صیادی|موجودی|تخفیف|ارسال|رایگان|قابلیت\s*انتخاب|مشاوره|تماس)"
BRACED = re.compile(r"\{[^{}]*\}|\(\([^()]*\)\)")
PAREN_SALES = re.compile(r"\(\s*[^()]*" + SALES_WORDS + r"[^()]*\)")

LEAD = re.compile(r"^\s*(مشخصات\s*[،,]?\s*)?(قیمت|خرید(\s*چکی|\s*اقساطی)?)(\s*(و|،|,|\+)?\s*(قیمت|خرید|مشخصات|فروش))*\s*[:،,\-]?\s+")
TAIL_PHRASES = [
    r"با\s+چک\s+صیادی(\s+در\s+[^\-–|]+)?",
    r"(\+\s*)?مشخصات\s+کامل",
    r"\+\s*مشخصات",
    r"(به\s+)?(صورت\s+)?اقساط(ی)?(\s+\S+){0,2}\s+(ساله|ماهه)",
    r"اقساط(ی)?",
    r"(با\s+)?(بهترین|ارزان\s*ترین|ارزانترین)\s+قیمت",
    r"قیمت\s+(امروز|روز|عمده)",
    r"فروش\s+(ویژه|عمده)",
    r"ارسال\s+(رایگان|فوری)",
    r"فروشگاه\s+اینترنتی(\s+\S+){0,3}$",
    r"و\s+گارانتی",                               # the source shop's warranty, not Caravan's
    r"اصل(\s+(آلمان|ایتالیا|ژاپن|چین|ترکیه|اروپا))?$",
]
TAIL = [re.compile(r"\s*" + p + r"\s*", re.I) for p in TAIL_PHRASES]
LONE = re.compile(r"(^|\s)(قیمت|خرید)(?=\s|$)")


def _drop_shop_suffix(name: str, shops: list[str]) -> str:
    """'اره عمودبر … - ابزار بیات' -> 'اره عمودبر …' when the part after the dash is the seller."""
    parts = re.split(r"\s+[\-–—|]\s+", name)
    if len(parts) < 2:
        return name
    last = norm_text(parts[-1])
    keys = [norm_text(s) for s in shops if s]
    if any(k and (k in last or last in k) for k in keys) or re.match(r"^(ابزار|فروشگاه|مرکز|بازار)\s", last):
        return " - ".join(parts[:-1])
    return name


def _drop_english_copy(name: str) -> str:
    """'پک ۱۰ کاره ماکیتا … شارژی ا Pack of 10 pieces Makita DCG405' -> Persian part only."""
    m = re.match(r"^(.*[؀-ۿ].*?)\s+[ا|/]\s+([A-Za-z][A-Za-z0-9 .,()\-/+]{8,})$", name)
    if m and not FA.search(m.group(2)):
        return m.group(1)
    return name


GROUP_SALES = re.compile(r"(قیمت(\s+(امروز|روز|عمده))?|خرید|موجودی(\s+روز)?|مشخصات(\s+کامل)?|اقساط(ی)?(\s+\S+){0,2}\s*(ساله|ماهه)?|"
                         r"فروش|تخفیف|ارسال(\s+رایگان)?)")


def _group(m: re.Match) -> str:
    """'{ قیمت + سرعت های 1 و 1.6 }' -> ' (سرعت های 1 و 1.6) '; '{ قیمت امروز + مشخصات}' -> ' '."""
    inner = m.group(0).strip("{}() ")
    inner = GROUP_SALES.sub(" ", inner)
    inner = re.sub(r"\s+", " ", inner).strip(" +،,-")
    inner = re.sub(r"^(و|\+)(\s+|$)|\s+(و|\+)$", "", inner).strip()
    left = re.sub(r"(^|\s)(و|\+|،|,)(?=\s|$)", " ", inner).strip()     # only connectors left: nothing to keep
    return f" ({inner}) " if left and (FA.search(left) or re.search(r"\d", left)) else " "


def clean_name(name: str, shops: list[str] | None = None) -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    if not n:
        return n
    n = BRACED.sub(_group, n)
    n = PAREN_SALES.sub(" ", n)
    n = _drop_english_copy(n)
    n = _drop_shop_suffix(n, shops or [])
    for _ in range(2):
        n = LEAD.sub("", n)
        for p in TAIL:
            n = p.sub(" ", n)
        n = LONE.sub(" ", n)
        n = re.sub(r"\s+", " ", n).strip(" -–—|،,:+")
    n = re.sub(r"\(\s*\)", "", n)
    n = re.sub(r"\s+", " ", n).strip(" -–—|،,:+")
    return n or str(name).strip()
