"""Catalog input: a supplier's price list (PDF or Excel) -> variable products for the site, as drafts.

A run named like 'catalog-garoosi' holds one catalog. Steps (each can be re-run on its own):

  read       catalog_read.py: tables, text lines and the catalog's own pictures   -> C1_read.json
  structure  every price-list table -> products; rows of one table that differ only in size
             (or capacity, length, ...) become the variations of one variable product -> C2_items.csv
             (one row per variation). Rule-based (headers, price columns, which columns vary) and,
             for pages with no readable table, a vision model on OpenRouter when a key is set.
  prices     the printed price -> toman -> dollars with today's rate (data/fx_rates.csv). The site
             keeps prices in dollars and multiplies them itself: ONLY the dollar figure ever goes to
             the price field; the toman figure is kept as meta (_caravan_price_toman) for reference.
  category   taxonomy leaf for each product (C3_categories.csv); a fixed category from the run's
             settings, a hand-picked one (kept on re-runs), a language model's choice among the
             closest leaves, or the best word match.
  photos     the maker's own site first (its product page), then the catalog's own pictures, then a
             web image search; made Caravan-style by images.make (white square, logo) -> C4_images.csv
  describe   Persian description (describe.Writer, or its template) -> C5_descriptions.csv
  publish    catalog_publish.py: WooCommerce variable product + variations, as drafts -> C6_published.csv

Run settings (C0_settings.json): brand, supplier, unit (auto | rial | toman | usd), category (a taxonomy
ID or part of a breadcrumb), maker_site (default: the web address printed in the catalog),
price_factor (default 1), attribute (preferred variation name, default سایز).
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import logging
import re
import shutil
import time
from pathlib import Path

import pandas as pd

from .config import run_dir, secret
from .io import read_csv, write_csv
from .normalize import fa_digits_to_en, norm_text

log = logging.getLogger(__name__)
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

SETTINGS = "C0_settings.json"
READ = "C1_read.json"
ITEMS = "C2_items.csv"
CATS = "C3_categories.csv"
IMAGES = "C4_images.csv"
DESCS = "C5_descriptions.csv"
STEPS = ["read", "structure", "prices", "category", "photos", "describe"]

ITEM_COLS = ["pkey", "family", "brand", "model", "page", "table", "attributes_json", "specs_json",
             "common_specs_json", "price_raw", "price_unit", "price_toman", "price_usd", "usd_rate_toman",
             "usd_rate_date", "price_check", "row_y", "method", "title"]

DEFAULTS = {"brand": "", "supplier": "", "unit": "auto", "category": "", "maker_site": "", "price_factor": 1,
            "attribute": "سایز"}

# which column names make a good variation, best first (the user's preference: size)
VARY_PREF = ("سایز", "اندازه", "ظرفیت", "قطر", "طول", "تعداد", "ابعاد", "ارتفاع", "عرض", "مدل", "نوع", "ضخامت",
             "قدرت", "توان", "حجم", "فشار", "دبی", "وزن")
PRICE_WORDS = ("قیمت", "price", "مبلغ", "بها")
MONEY_WORDS = PRICE_WORDS + ("دلار", "usd", "$", "dollar")      # a dollar list's price column may say only 'USD'
PER_UNIT = re.compile(r"(\sهر\s|^هر\s|برحسب\s*هر|قیمت\s*هر|per\s)")
NAME_WORDS = ("شرح", "نام", "عنوان", "محصول", "کالا", "description", "item", "product")
CODE_WORDS = ("کد", "code", "part", "art")
UNITS = {"cm": "cm", "mm": "mm", "میلی متر": "mm", "میلیمتر": "mm", "میلی‌متر": "mm", "اینچ": "اینچ",
         "اینج": "اینچ", "kw": "KW", "فوت": "فوت", "متر": "متر", "لیتر": "لیتر", "کیلوگرم": "کیلوگرم",
         "وات": "وات", "اسب": "اسب", "بار": "بار"}


# ------------------------------------------------------------------ settings and files

def settings(cfg: dict, run: str) -> dict:
    p = run_dir(cfg, run) / SETTINGS
    s = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {**DEFAULTS, **{k: v for k, v in s.items() if v not in (None,)}}


def save_settings(cfg: dict, run: str, new: dict) -> dict:
    cur = settings(cfg, run)
    for k, v in (new or {}).items():
        if k in DEFAULTS or k == "source":
            cur[k] = v.strip() if isinstance(v, str) else v
    (run_dir(cfg, run) / SETTINGS).write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    return cur


def source_file(cfg: dict, run: str) -> Path | None:
    d = run_dir(cfg, run)
    hits = sorted(d.glob("C0_source.*"))
    return hits[0] if hits else None


def add_source(cfg: dict, run: str, data: bytes, filename: str) -> Path:
    d = run_dir(cfg, run)
    suf = Path(filename or "").suffix.lower()
    if data[:4] == b"%PDF":
        suf = ".pdf"
    elif data[:2] == b"PK" and suf not in (".xlsx", ".xlsm"):
        suf = ".xlsx"
    if suf not in (".pdf", ".xlsx", ".xlsm", ".csv", ".xls"):
        raise ValueError("فقط PDF، اکسل (xlsx) یا CSV")
    for old in d.glob("C0_source.*"):
        old.unlink()
    p = d / f"C0_source{suf}"
    p.write_bytes(data)
    save_settings(cfg, run, {"source": filename or p.name})
    return p


def is_catalog_run(cfg: dict, run: str) -> bool:
    return (run_dir(cfg, run) / SETTINGS).exists() or source_file(cfg, run) is not None


# ------------------------------------------------------------------ values

PRICE_CELL = re.compile(r"^\D{0,4}(\d{1,3}([,.٬]\d{3})+|\d+[,.٬]\d{3}([,.٬]\d{3})*|\d{5,})(\.\d{1,2})?\D{0,8}$")


def parse_price(s) -> float | None:
    """'3,400,000' / '9,200.000' / '8500,000' / '11.000.000' -> 3400000 …; '12.5' (dollars) -> 12.5."""
    if s is None:
        return None
    t = fa_digits_to_en(str(s)).replace("٬", ",").replace("٫", ".").replace("$", "").strip()
    m = re.search(r"\d[\d,. ]*\d|\d", t)
    if not m:
        return None
    raw = m.group(0).replace(" ", "")
    if re.fullmatch(r"\d+([,.]\d{3})+", raw):                  # every separator is a thousands one
        return float(re.sub(r"[,.]", "", raw))
    if re.fullmatch(r"\d+(,\d{3})*\.\d{1,2}", raw):             # 1,250.50
        return float(raw.replace(",", ""))
    if re.fullmatch(r"\d+", raw):
        return float(raw)
    return None


SMALL_USD = re.compile(r"^(\$|usd)?\s*\d{1,6}([.,]\d{1,2})?\s*(\$|usd)?$", re.I)


def looks_price(s, usd: bool = False) -> bool:
    """A price cell. Toman/rial prices are big numbers with thousands separators; a dollar list
    (the person set the unit to dollars) also has '12.5', '850', '$450', Excel's 1250.5."""
    t = fa_digits_to_en(str(s or "")).strip()
    if not t:
        return False
    if PRICE_CELL.match(t) and (parse_price(t) or 0) >= 1000:
        return True
    return usd and bool(SMALL_USD.match(t)) and (parse_price(t.replace("$", "")) or 0) > 0


FRACTIONS = {"½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3"}


def norm_value(v, unit: str = "", inch: bool = False) -> str:
    """A cell as a clean variation value: Persian decimal slash -> point ('2/5mm' -> '2.5 mm'),
    inch fractions kept ('11/2' in an inch column -> '1 1/2'), the column's unit added to bare numbers."""
    s = fa_digits_to_en(str(v or "")).replace("\n", " ").replace("\u2044", "/")
    for a, b in FRACTIONS.items():
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(\d/\d+) (\d+)$", r"\2 \1", s)               # a '2 ½' read right to left: '1/2 2' -> '2 1/2'
    # Latin glued to Persian: '2mmسبک' -> '2mm سبک'
    s = re.sub(r"(?<=[A-Za-z])(?=[\u0600-\u06FF])|(?<=[\u0600-\u06FF])(?=[A-Za-z])", " ", s)
    inch = inch or "اینچ" in s or '"' in s

    def frac(m):
        a, b = m.group(1), m.group(2)
        if inch and b in ("2", "4", "8", "16") and len(a) == 2 and a[1] < b and a[1] != "0":
            return f"{a[0]} {a[1]}/{b}"                    # '11/2' -> '1 1/2', '21/2' -> '2 1/2'
        if inch and b in ("2", "4", "8", "16") and len(a) == 1 and int(a) < int(b):
            return f"{a}/{b}"                              # a half / quarter inch
        if int(b) > 0 and len(b) == 1:
            return f"{a}.{b}"                              # Persian decimal: '2/5' -> '2.5', '0/7' -> '0.7'
        return m.group(0)
    s = re.sub(r"(\d) (\d)/(2|4|8|16)\b", "\\1 \\2\u2215\\3", s)       # '1 1/4' is always a mixed fraction
    s = re.sub(r"(?<![\d/])(\d+)/(\d+)(?![\d/])", frac, s).replace("\u2215", "/")
    s = re.sub(r"(\d)\s*(mm|cm|kw|KW)\b", lambda m: f"{m.group(1)} {m.group(2)}", s)
    s = re.sub(r"(\d)(?=[؀-ۿ])", r"\1 ", s)              # '6اینچ' -> '6 اینچ'
    s = re.sub(r"(?<=[؀-ۿ])(?=\d)", " ", s)               # 'سبک2' -> 'سبک 2'
    s = re.sub(r"\s+", " ", s).strip()
    if unit and re.fullmatch(r"[\d.,]+( \d/\d+)?", s):
        s = f"{s} {unit}"
    return s


def _join_header_lines(cell: str) -> str:
    """Header text the PDF broke over lines: 'برح\\nسب\\nKW' -> 'برحسب KW' (pieces of 1-2 letters
    belong to the word before them)."""
    out = ""
    for part in str(cell or "").split("\n"):
        part = part.strip()
        if not part:
            continue
        # 'ورودی_خر' + 'وجی' -> 'ورودی_خروجی': a short piece after a short word part is the same word
        glue = len(part) <= 2 or ("_" in out and len(out.rsplit("_", 1)[1]) <= 3 and len(part) <= 3)
        if out and glue and re.search(r"[؀-ۿ]", part) and re.search(r"[؀-ۿ]$", out):
            out += part
        else:
            out = f"{out} {part}".strip()
    out = re.sub(r"(قیمت|ظرفیت|لوله)(?=[؀-ۿ])", r"\1 ", out)       # 'قیمتکل' -> 'قیمت کل'
    out = re.sub(r"\(\s*\)|\)\s*\(", " ", out).replace("_", "/")
    return re.sub(r"\s+", " ", out).strip(" -_:")


def header_unit(h: str) -> tuple[str, str]:
    """'طول مبدل (cm)' -> ('طول مبدل', 'cm'); 'قطر (میلی متر)' -> ('قطر', 'mm'); 'برحسب KW' -> ('', 'KW')."""
    unit = ""
    name = h
    for m in re.finditer(r"[()]\s*([^()]+?)\s*[()]", h):
        u = UNITS.get(norm_text(m.group(1)).replace(" ", " "), "") or UNITS.get(m.group(1).strip().lower(), "")
        if u:
            unit = u
            name = name.replace(m.group(0), " ")
    m = re.search(r"برحسب\s*(\S+(?:\s\S+)?)", name)
    if m:
        u = UNITS.get(m.group(1).strip().lower()) or UNITS.get(m.group(1).split()[0].strip().lower(), "")
        if u:
            unit = unit or u
            name = name.replace(m.group(0), " ")
    if not unit:
        tail = name.strip().split(" ")[-1].lower() if name.strip() else ""
        if tail in ("mm", "cm", "kw"):
            unit = UNITS[tail]
            name = name.strip()[: -len(tail)]
    name = re.sub(r"[()]", " ", name)
    return re.sub(r"\s+", " ", name).strip(" -_"), unit


def _clean_title(t: str) -> str:
    t = re.sub(r"^\s*(لیست|ليست)\s*(قیمت|قيمت)\s*", "", str(t or "")).strip()
    t = t.replace("_", " ")
    t = re.sub(r"\(\s*", "(", re.sub(r"\s*\)", ")", t))
    t = re.sub(r"\s*\(", " (", t)
    return re.sub(r"\s+", " ", t).strip()


def _family_for(title: str, label: str, others: list[str]) -> str:
    """'نازل شوتر و ثقلی' for the column 'ثقلی' -> 'نازل ثقلی'; 'کرتین (مدل قوسی و مدل کبری)' -> 'کرتین (مدل کبری)'."""
    if not label:
        return title
    t = title
    for o in others:
        if o and o != label:
            t = t.replace(o, " ")
    t = re.sub(r"(^|\s|\()و(\s|\)|$)", r"\1\2", t)
    t = re.sub(r"\(\s*\)", "", re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", t)))
    t = re.sub(r"\s+", " ", t).strip()
    if norm_text(label) not in norm_text(t):
        t = f"{t} {label}"
    return t


def product_key(brand: str, family: str, supplier: str = "") -> str:
    return hashlib.sha1(f"{norm_text(supplier)}|{norm_text(brand)}|{norm_text(family)}".encode()).hexdigest()[:12]


# ------------------------------------------------------------------ rule-based structure

def _headers(rows: list[list], usd: bool = False) -> tuple[list[str], list[str], int]:
    """(group header, sub header, first data row) per column: the leading rows without a price are the
    header; the last of them names the columns, the ones above group them (a merged cell, None to its
    right, covers the columns after it)."""
    first = len(rows)
    money_cols: set[int] = set()                 # dollar lists: columns whose heading names a price or currency
    for i, r in enumerate(rows):
        if any(looks_price(c) for c in r if c) or (usd and any(
                j in money_cols and looks_price(c, True) for j, c in enumerate(r) if c)):
            first = i
            break
        money_cols |= {j for j, c in enumerate(r) if c and any(w in norm_text(str(c)) or w in str(c).lower()
                                                               for w in MONEY_WORDS)}
    width = max(len(r) for r in rows)
    head_rows = [list(r) + [None] * (width - len(r)) for r in rows[:first]]
    group, sub = [""] * width, [""] * width
    for k, r in enumerate(head_rows):
        last = k == len(head_rows) - 1
        carry = ""
        for j in range(width):
            c = r[j]
            if c is None:
                txt = "" if last else carry
            else:
                txt = carry = _join_header_lines(c)
            if not txt:
                continue
            if last:
                sub[j] = txt
            elif txt not in group[j]:
                group[j] = f"{group[j]} {txt}".strip()
    return group, sub, first


def _short_group(g: str) -> str:
    return re.sub(r"^(نوع|مشخصات)\s+", "", g).strip()


def _name_columns(cols: list[dict]) -> None:
    """Each column's spec name and unit: its own header ('قطر (cm)'), with its group's name added only
    where two columns would otherwise share a name ('قطر لوله' / 'قطر لوله داخلی'), then the unit."""
    for c in cols:
        sn, su = header_unit(c["sub"]) if c["sub"] else ("", "")
        gn, gu = header_unit(c["group"]) if c["group"] else ("", "")
        c["unit"] = su or gu
        c["gname"] = gn
        c["name"] = sn or gn or c["header"]
    for step in ("group", "unit", "index"):
        seen: dict[str, list] = {}
        for c in cols:
            seen.setdefault(c["name"], []).append(c)
        clash = [x for v in seen.values() if len(v) > 1 for x in v]
        if not clash:
            return
        for n, c in enumerate(clash, 1):
            if step == "group" and c["gname"] and _short_group(c["gname"]) not in c["name"]:
                c["name"] = f"{c['name']} {_short_group(c['gname'])}"
            elif step == "unit" and c["unit"] and c["unit"] not in c["name"]:
                c["name"] = f"{c['name']} ({c['unit']})"
            elif step == "index":
                c["name"] = f"{c['name']} {n}"


def _vary_rank(name: str, pref: str) -> int:
    n = norm_text(name)
    if pref and norm_text(pref) in n:
        return -1
    for i, w in enumerate(VARY_PREF):
        if w in n:
            return i
    return len(VARY_PREF)


def choose_attributes(cols: list[str], rows: list[dict], pref: str = "سایز", max_k: int = 3) -> list[str]:
    """The fewest varying columns that tell every row apart, preferred names first ('سایز')."""
    varying = [c for c in cols if len({r.get(c, "") for r in rows}) > 1]
    if not varying:
        return []
    order = sorted(varying, key=lambda c: (_vary_rank(c, pref), cols.index(c)))
    for k in range(1, min(max_k, len(order)) + 1):
        for combo in itertools.combinations(order, k):
            if len({tuple(r.get(c, "") for c in combo) for r in rows}) == len(rows):
                return list(combo)
    return order[:max_k]


def rule_products(table: dict, st: dict) -> list[dict]:
    """One price-list table -> products [{family, attributes: [names], common, variants: [...]}]."""
    rows = [list(r) for r in table["rows"]]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [None] * (width - len(r)) for r in rows]
    usd = st.get("unit") == "usd"
    group, sub, first = _headers(rows, usd)
    full = [f"{g} {x}".strip() for g, x in zip(group, sub)]
    data = rows[first:]
    ys = (table.get("row_y") or [])[first:]
    if not data:
        return []
    # columns that hold nothing are dropped; a column whose header was lost gets a name from its values
    cols = []
    for j in range(width):
        vals = [r[j] for r in data if r[j] not in (None, "")]
        if not vals:
            continue
        h = full[j]
        if len(re.sub(r"\s", "", h)) <= 1:
            h = "اندازه" if any(re.search(r"\d\s*(cm|mm|متر|اینچ)", fa_digits_to_en(str(v))) for v in vals) else f"ستون {j + 1}"
        cols.append({"j": j, "header": h, "group": group[j], "sub": sub[j] if len(re.sub(r"\s", "", full[j])) > 1 else h})
    words = MONEY_WORDS if usd else PRICE_WORDS
    price_cols = [c for c in cols if any(w in norm_text(c["header"]) or w in c["header"].lower() for w in words)
                  and sum(1 for r in data if looks_price(r[c["j"]], usd)) >= max(1, len(data) // 3)]
    if not price_cols:
        # no column says it is a price: only big, separated numbers count (never a size or a PN column)
        price_cols = [c for c in cols if sum(1 for r in data if looks_price(r[c["j"]])) >= max(1, len(data) // 2)]
    if not price_cols:
        return []
    whole = [c for c in price_cols if not PER_UNIT.search(" " + norm_text(c["header"]) + " ")]
    price_cols = whole or price_cols
    if len(price_cols) > 1:                       # 'قیمت کل' / consumer price wins over others in one product
        best = [c for c in price_cols if re.search(r"(کل|مصرف|فروش|retail)", c["header"])]
        groups = {c["group"] for c in price_cols}
        if best and len(groups) == 1 and len(best) == 1:
            price_cols = best
    title = _clean_title(table.get("title") or "")
    labels = []
    for c in price_cols:                          # what tells several price columns apart
        if len(price_cols) == 1:
            labels.append("")
        elif len({x["group"] for x in price_cols}) == len(price_cols):
            labels.append(_clean_title(c["group"]))
        else:
            lab = c["header"]
            for w in PRICE_WORDS + ("(تومان)", "(ریال)", "تومان", "ریال"):
                lab = lab.replace(w, " ")
            labels.append(re.sub(r"\s+", " ", lab).strip())
    out = []
    name_col = next((c for c in cols if c not in price_cols and any(norm_text(c["header"]).startswith(w) for w in NAME_WORDS)), None)
    for pc, label in zip(price_cols, labels):
        own = [c for c in cols if c not in price_cols and (not pc["group"] or len(price_cols) == 1 or c["group"] in ("", pc["group"])
                                                          or len({x["group"] for x in price_cols}) < len(price_cols))]
        # every price column stays out of the specs (a per-foot price is a price too, never a spec)
        specs_cols = [dict(c) for c in own if c is not name_col and not any(w in norm_text(c["header"]) for w in PRICE_WORDS)]
        if label and len({x["group"] for x in price_cols}) == len(price_cols):
            for c in specs_cols:                       # the group is the product here, not part of the name
                if c["group"] == pc["group"]:
                    c["group"] = ""
        _name_columns(specs_cols)
        recs, last = [], {}
        for i, r in enumerate(data):
            raw = r[pc["j"]]
            vals = {}
            for c in specs_cols:
                name, unit = c["name"], c["unit"]
                v = r[c["j"]]
                if v in (None, ""):
                    v = last.get(name, "")                # a cell merged down: same as the row above
                vals[name] = norm_value(v, unit, inch=unit == "اینچ") if v else ""
            last = {k: v for k, v in vals.items()}
            if not looks_price(raw, usd):
                continue
            recs.append({"price_raw": str(raw).strip(), "vals": vals,
                         "name": str(r[name_col["j"]] or "").strip() if name_col else "",
                         "row_y": ys[i] if i < len(ys) else ""})
        if not recs:
            continue
        fam = _family_for(title, label, labels) if title else (label or table.get("sheet", "") or "محصول")
        if name_col:                                   # a list of different products: grouped by name
            groups: dict[str, list] = {}
            for x in recs:
                base, size = split_size(x["name"])
                x["vals"] = {"سایز": size, **x["vals"]} if size else x["vals"]
                groups.setdefault(base or x["name"], []).append(x)
            for base, xs in groups.items():
                out.append(_product(base, xs, table, st))
        else:
            out.append(_product(fam, recs, table, st))
    return out


SIZE_IN_NAME = re.compile(r"(\d+(?:[./]\d+)?(?:\s+\d/\d+)?\s*(?:اینچ|\"|mm|cm|میلیمتر|میلی متر|سانت|متر|لیتر|اسب|کیلووات|kw|وات)"
                          r"|سایز\s*\S+|DN\s*\d+)", re.I)


def split_size(name: str) -> tuple[str, str]:
    n = fa_digits_to_en(str(name or ""))
    m = SIZE_IN_NAME.search(n)
    if not m:
        return n.strip(), ""
    base = re.sub(r"\s+", " ", (n[:m.start()] + " " + n[m.end():])).strip(" -_،,")
    return base, norm_value(m.group(0))


def _product(family: str, recs: list[dict], table: dict, st: dict) -> dict:
    names = list(dict.fromkeys(k for x in recs for k in x["vals"]))
    attrs = choose_attributes(names, [x["vals"] for x in recs], st.get("attribute") or "سایز")
    common = {k: recs[0]["vals"][k] for k in names if k not in attrs and len({x["vals"].get(k, "") for x in recs}) == 1
              and recs[0]["vals"][k]}
    variants = []
    for x in recs:
        a = {k: x["vals"].get(k, "") for k in attrs}
        sp = {k: v for k, v in x["vals"].items() if k not in attrs and k not in common and v}
        variants.append({"attributes": a, "specs": sp, "price_raw": x["price_raw"], "row_y": x["row_y"]})
    return {"family": family, "brand": st.get("brand", ""), "model": "", "attributes": attrs, "common": common,
            "variants": variants, "page": table.get("page"), "title": table.get("title", ""), "method": "rules"}


# ------------------------------------------------------------------ vision model (pages without a table)

VISION_SYSTEM = """You read one page of a supplier's price list for an Iranian online store of industrial and pool
equipment. Return the products on the page as JSON. Rules:
- Rows that are the same product in different sizes (or capacity, length, persons, ... when size is not what
  varies) are ONE product with several variants. Several price columns for different product types (e.g.
  'قیمت شوتر' and 'قیمت ثقلی') are separate products.
- "family": the Persian product name without the varying value, as a customer would search it.
- "attributes": the Persian names of what varies between the variants (prefer "سایز"); each variant gives a
  value for each. Values as printed, with the unit ('1 1/2 اینچ', '50 cm', '4 نفره'); a Persian decimal
  written with '/' ('2/5') is 2.5.
- Copy each price exactly as printed, digits only. Never compute, round or invent a number. A row with no price
  is left out. If there are several price columns for the same product, use the total / consumer price.
- "specs": other printed facts (same for all variants under "common", per variant under the variant).
- "currency": "rial", "toman", "usd" or "" as printed on the page.
Reply JSON only: {"currency": "", "products": [{"family": "", "brand": "", "attributes": ["سایز"],
 "common": {}, "variants": [{"values": {"سایز": ""}, "price": "", "specs": {}}]}]}"""


def _openrouter(cfg: dict, messages: list, max_tokens: int = 6000) -> dict | None:
    import requests
    key = secret("OPENROUTER_API_KEY")
    if not key:
        return None
    model = cfg.get("catalog", {}).get("vision_model") or cfg.get("images", {}).get("vision_model") \
        or "deepseek/deepseek-v4.1-flash"
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0, "messages": messages,
            "response_format": {"type": "json_object"}}
    for attempt in range(3):
        try:
            r = requests.post(OPENROUTER, json=body, timeout=180, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan crawler"})
        except requests.RequestException as e:
            log.info("openrouter: %s", e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            log.warning("catalog model: HTTP %s %s", r.status_code, r.text[:200])
            return None
        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", text, flags=re.S), re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except ValueError:
            return None
    return None


def vision_products(cfg: dict, d: Path, page: int, lines: list[dict], st: dict) -> tuple[list[dict], str]:
    png = d / "C1_pages" / f"{page:03d}.png"
    if not png.exists():
        return [], ""
    text = "\n".join(f"L{x['n']}: {x['text']}" for x in lines if x["page"] == page)
    msg = [{"role": "system", "content": VISION_SYSTEM},
           {"role": "user", "content": [
               {"type": "text", "text": f"Page {page}. Text read from the page (may be broken):\n{text[:5000]}"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()}}]}]
    try:
        j = _openrouter(cfg, msg)
    except Exception as e:                               # noqa: BLE001 - one page never stops the run
        log.warning("catalog model page %d: %s", page, e)
        return [], ""
    if not isinstance(j, dict):
        return [], ""
    page_digits = re.sub(r"\D", "", fa_digits_to_en(text))
    out = []
    for p in j.get("products") or []:
        if not isinstance(p, dict):
            continue
        attrs = [str(a) for a in (p.get("attributes") or []) if isinstance(a, (str, int, float))][:3]
        variants = []
        for v in p.get("variants") or []:
            if not isinstance(v, dict):
                continue
            raw = re.sub(r"\D", "", fa_digits_to_en(str(v.get("price") or "")))
            if not raw:
                continue
            seen = raw in page_digits
            vals = v.get("values") if isinstance(v.get("values"), dict) else {}
            specs = v.get("specs") if isinstance(v.get("specs"), dict) else {}
            variants.append({"attributes": {a: norm_value(vals.get(a, "")) for a in attrs},
                             "specs": {str(k): str(x) for k, x in specs.items() if x},
                             "price_raw": raw, "row_y": "",
                             "check": "" if seen else "vision: price read from the page image, check it"})
        if variants:
            out.append({"family": _clean_title(p.get("family", "")), "brand": p.get("brand") or st.get("brand", ""),
                        "model": "", "attributes": attrs,
                        "common": {str(k): str(x) for k, x in (p.get("common") if isinstance(p.get("common"), dict) else {}).items() if x},
                        "variants": variants, "page": page, "title": "", "method": "vision"})
    return out, str(j.get("currency") or "")


# ------------------------------------------------------------------ step: structure

def step_read(cfg: dict, run: str) -> dict:
    from .catalog_read import read
    src = source_file(cfg, run)
    if src is None:
        raise ValueError("فایل کاتالوگ این اجرا بارگذاری نشده")
    d = run_dir(cfg, run)
    for sub in ("C1_pages", "C1_img"):
        shutil.rmtree(d / sub, ignore_errors=True)
    out = read(src, d)
    st = settings(cfg, run)
    if not st.get("maker_site"):                  # the web address printed in the catalog is the maker's site
        site = maker_site_in(out)
        if site:
            save_settings(cfg, run, {"maker_site": site})
            log.info("catalog: maker's site from the catalog: %s", site)
    return out


def maker_site_in(read_out: dict) -> str:
    text = " ".join(x["text"] for x in read_out.get("lines") or [])
    m = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:ir|com|net|org|co|shop))\b",
                  text, re.I)
    return m.group(1).lower() if m else ""


def detect_unit(read_out: dict, override: str = "auto") -> tuple[str, str]:
    """(unit, how): the settings' unit, else what the catalog prints next to its prices."""
    if override in ("rial", "toman", "usd"):
        return override, "settings"
    text = norm_text(" ".join([x["text"] for x in read_out.get("lines") or []] +
                              [str(c) for t in read_out.get("tables") or [] for r in t["rows"][:3] for c in r if c]))
    hits = {"toman": len(re.findall(r"تومان|تومن|toman", text)), "rial": len(re.findall(r"ریال|rial", text)),
            "usd": len(re.findall(r"دلار|usd|\$", text))}
    best = max(hits, key=hits.get)
    if hits[best] and list(hits.values()).count(hits[best]) == 1:
        return best, "catalog"
    return "", "unknown"


def _vocab(cfg: dict) -> set[str]:
    """Words of the store's category names (the taxonomy), for mending words the PDF glued together."""
    from .taxonomy import load_taxonomy
    p = Path(cfg.get("taxonomy", {}).get("file") or "")
    if not p.exists():
        return set()
    try:
        tx = load_taxonomy(p)
    except Exception:                                       # noqa: BLE001
        return set()
    return {w for b in tx["breadcrumb"] for w in norm_text(b).replace(">", " ").split() if len(w) >= 3}


def unglue(name: str, vocab: set[str]) -> str:
    """'فیلترشنی نازل' -> 'فیلتر شنی نازل' when both halves are words of the store's categories."""
    if not vocab:
        return name
    out = []
    for w in str(name).split(" "):
        n = norm_text(w)
        if n in vocab or len(n) < 6 or not re.fullmatch(r"[\u0600-\u06FF]+", n):
            out.append(w)
            continue
        cut = next((k for k in range(3, len(n) - 2) if n[:k] in vocab and n[k:] in vocab), None)
        out.append(f"{w[:cut]} {w[cut:]}" if cut and len(w) == len(n) else w)
    return " ".join(out)


def step_structure(cfg: dict, run: str) -> Path:
    d = run_dir(cfg, run)
    p = d / READ
    if not p.exists():
        step_read(cfg, run)
    rd = json.loads(p.read_text(encoding="utf-8"))
    st = settings(cfg, run)
    products, done_pages = [], set()
    for ti, t in enumerate(rd["tables"]):
        for prod in rule_products(t, st):
            prod["table"] = ti
            products.append(prod)
            done_pages.add(t["page"])
    # pages with text or pictures but no table that gave prices: the vision model, when available
    use_model = cfg.get("catalog", {}).get("vision", True) and secret("OPENROUTER_API_KEY")
    page_currency = {}
    if use_model:
        for page in range(1, int(rd.get("pages") or 0) + 1):
            if page in done_pages or rd.get("kind") != "pdf":
                continue
            ps, cur = vision_products(cfg, d, page, rd["lines"], st)
            products += ps
            if cur:
                page_currency[page] = cur
            if ps:
                log.info("catalog: page %d read by the vision model: %d product(s)", page, len(ps))
    unit, how = detect_unit(rd, st.get("unit", "auto"))
    vocab = _vocab(cfg)
    for prod in products:
        prod["family"] = unglue(prod["family"], vocab)
    # the same name twice (two tables): numbered in the order of what they hold (their first size),
    # not the order of the file, so a table added in front next month does not move the numbers
    base: dict[str, list[dict]] = {}
    for prod in products:
        prod["family"] = prod["family"] or "محصول"
        base.setdefault(product_key(prod.get("brand") or st.get("brand", ""), prod["family"],
                                    st.get("supplier", "")), []).append(prod)

    def _sig(prod):                              # the first size it holds: a size added later rarely moves it
        return min((json.dumps(sorted(v["attributes"].items()), ensure_ascii=False) for v in prod["variants"]), default="")
    for same in base.values():
        if len(same) > 1:
            for n, prod in enumerate(sorted(same, key=_sig)[1:], 2):
                prod["family"] = f"{prod['family']} - {n}"
    rows = []
    for prod in products:
        fam = prod["family"]
        k = product_key(prod.get("brand") or st.get("brand", ""), fam, st.get("supplier", ""))
        # variations with the same values but different prices cannot be told apart: held for a person
        combos: dict[tuple, int] = {}
        for v in prod["variants"]:
            combos[tuple(v["attributes"].items())] = combos.get(tuple(v["attributes"].items()), 0) + 1
        for v in prod["variants"]:
            dup = combos[tuple(v["attributes"].items())] > 1
            u = page_currency.get(prod.get("page")) if prod.get("method") == "vision" else ""
            rows.append({
                "pkey": k, "family": fam, "brand": prod.get("brand") or st.get("brand", ""), "model": prod.get("model", ""),
                "page": prod.get("page", ""), "table": prod.get("table", ""),
                "attributes_json": json.dumps(v["attributes"], ensure_ascii=False),
                "specs_json": json.dumps(v["specs"], ensure_ascii=False),
                "common_specs_json": json.dumps(prod["common"], ensure_ascii=False),
                "price_raw": v["price_raw"], "price_unit": u if u in ("rial", "toman", "usd") else unit,
                "price_check": "; ".join(x for x in (
                    v.get("check", ""),
                    "duplicate: same values as another row with another price — not sent" if dup else "",
                    "" if (u or unit) else "unit_unknown: the catalog does not say rial/toman/dollar — set it in the settings")
                    if x),
                "row_y": v.get("row_y", ""), "method": prod.get("method", "rules"), "title": prod.get("title", "")})
    df = pd.DataFrame(rows, columns=ITEM_COLS)
    out = write_csv(df, d / ITEMS, ITEM_COLS)
    log.info("catalog structure: %d product(s), %d variation(s); prices in %s (%s)",
             df["pkey"].nunique() if len(df) else 0, len(df), unit or "?", how)
    step_prices(cfg, run)
    return out


# ------------------------------------------------------------------ step: prices (toman -> dollars)

def step_prices(cfg: dict, run: str) -> Path | None:
    """Printed price -> toman -> dollars with today's rate. Only the dollar price is ever sent to the
    site's price field; anything that cannot be converted safely is left without a price."""
    from .fx import today_rate
    d = run_dir(cfg, run)
    if not (d / ITEMS).exists():
        return None
    df = read_csv(d / ITEMS)
    if df.empty:
        return d / ITEMS
    st = settings(cfg, run)
    try:
        factor = float(st.get("price_factor") or 1)
    except (TypeError, ValueError):
        factor = 1.0
    max_usd = float(cfg.get("catalog", {}).get("max_usd", 100_000))
    confirmed = st.get("unit") in ("rial", "toman", "usd")
    if confirmed:                                    # a person chose the unit: it is used for every row
        df["price_unit"] = st["unit"]
        df["price_check"] = df["price_check"].str.replace(r"(; )?unit_unknown:[^;]*", "", regex=True).str.strip("; ")
    need_rate = (df["price_unit"].isin(["rial", "toman"])).any()
    rate = today_rate(cfg) if need_rate else {"usd_rate_toman": "", "usd_rate_date": ""}
    for i, r in df.iterrows():
        raw = parse_price(r["price_raw"])
        unit = r["price_unit"]
        toman = usd = None
        if raw and unit in ("rial", "toman"):
            toman = raw / 10 if unit == "rial" else raw
            usd = toman * factor / float(rate["usd_rate_toman"])
        elif raw and unit == "usd":
            usd = raw * factor
            toman = usd * float(rate["usd_rate_toman"]) if rate.get("usd_rate_toman") else None
        checks = [x for x in str(r["price_check"]).split("; ")
                  if x and not x.startswith(("too_high", "too_low", "unit_unconfirmed", "usd_doubt"))]
        if usd is not None and usd > max_usd:            # a toman figure would land here: never sent
            checks.append(f"too_high: {usd:,.0f} USD — check the unit (rial/toman/dollar)")
            usd = None
        if usd is not None and not confirmed:
            # the unit was guessed from words in the file ('بر اساس نرخ دلار روز' in a toman list):
            # nothing is sent until a person sets it in the settings
            checks.append(f"unit_unconfirmed: unit guessed as {unit} from the catalog text — confirm it in the settings")
        if usd is not None and unit == "usd" and raw and raw >= 20_000 and float(raw).is_integer():
            checks.append(f"usd_doubt: {raw:,.0f} as dollars is unusual — a toman/rial figure?")
        if usd is not None and unit in ("rial", "toman") and usd < 0.5:
            checks.append(f"too_low: {usd:.2f} USD — rial read as toman, or a per-unit price?")
        df.at[i, "price_toman"] = "" if toman is None else str(int(round(toman)))
        df.at[i, "price_usd"] = "" if usd is None else f"{usd:.2f}"
        df.at[i, "usd_rate_toman"] = str(rate.get("usd_rate_toman") or "") if unit in ("rial", "toman") else ""
        df.at[i, "usd_rate_date"] = str(rate.get("usd_rate_date") or "") if unit in ("rial", "toman") else ""
        df.at[i, "price_check"] = "; ".join(checks)
    write_csv(df, d / ITEMS, ITEM_COLS)
    log.info("catalog prices: %d of %d variation(s) priced in dollars%s", (df["price_usd"] != "").sum(), len(df),
             f" (rate {rate['usd_rate_toman']} toman, {rate['usd_rate_date']}, factor {factor})" if need_rate else "")
    return d / ITEMS


# ------------------------------------------------------------------ products view

def products(cfg: dict, run: str) -> list[dict]:
    """C2_items.csv grouped back into products (with category, photos and description when made)."""
    d = run_dir(cfg, run)
    if not (d / ITEMS).exists():
        return []
    df = read_csv(d / ITEMS)
    cats = {r["pkey"]: r for r in read_csv(d / CATS).to_dict("records")} if (d / CATS).exists() else {}
    imgs = {r["pkey"]: r for r in read_csv(d / IMAGES).to_dict("records")} if (d / IMAGES).exists() else {}
    descs = {r["pkey"]: r for r in read_csv(d / DESCS).to_dict("records")} if (d / DESCS).exists() else {}
    out = []
    for k, g in df.groupby("pkey", sort=False):
        f = g.iloc[0]
        variants = []
        for r in g.to_dict("records"):
            variants.append({**r, "attributes": json.loads(r["attributes_json"] or "{}"),
                             "specs": json.loads(r["specs_json"] or "{}")})
        attrs = list(dict.fromkeys(a for v in variants for a in v["attributes"]))
        out.append({"pkey": k, "family": f["family"], "brand": f["brand"], "model": f["model"], "page": f["page"],
                    "method": f["method"], "title": f["title"], "attributes": attrs,
                    "common": json.loads(f["common_specs_json"] or "{}"), "variants": variants,
                    "category": cats.get(k, {}), "images": imgs.get(k, {}), "description": descs.get(k, {})})
    return out


# ------------------------------------------------------------------ step: category

def _taxonomy(cfg: dict) -> pd.DataFrame | None:
    from .taxonomy import load_taxonomy
    from .wc import category_map
    p = Path(cfg.get("taxonomy", {}).get("file") or "")
    if not p.exists():
        return None
    tx = load_taxonomy(p)
    cats = category_map()
    if cats:
        tx = tx[tx["taxonomy_id"].isin(set(cats))]
    return tx


def _words(text: str) -> list[str]:
    return norm_text(re.sub(r"[-_/()]", " ", str(text or ""))).split()


def _score(words: set[str], row, head: str = "") -> float:
    """Word overlap with the leaf (double) and the whole path; the product's first word (what it is:
    'فیلترشنی' in 'فیلترشنی نازل نمکدانی') found in the leaf counts extra."""
    lw = _words(row["leaf"])
    pw = _words(row["breadcrumb"].replace(">", " "))
    leaf = set(lw) | {a + b for a, b in zip(lw, lw[1:])}          # 'فیلترشنی' (glued in the PDF) = 'فیلتر شنی'
    path = set(pw) | {a + b for a, b in zip(pw, pw[1:])}
    return (2 * len(words & leaf) + len(words & path) + (1.5 if head and head in leaf else 0)
            + (0.5 if row["is_leaf"] else 0) - 0.05 * len(leaf - words))


STOP_WORDS = {"و", "با", "برای", "از", "مدل", "نوع", "در", "به", "استیل", "گالوانیزه"}


def step_category(cfg: dict, run: str) -> Path | None:
    d = run_dir(cfg, run)
    prods = products(cfg, run)
    if not prods:
        return None
    tx = _taxonomy(cfg)
    st = settings(cfg, run)
    old = {r["pkey"]: r for r in read_csv(d / CATS).to_dict("records")} if (d / CATS).exists() else {}
    rows = []
    fixed = str(st.get("category") or "").strip()
    sub = None
    if tx is not None and fixed:
        if fixed in set(tx["taxonomy_id"]):
            sub = tx[tx["breadcrumb"].str.startswith(tx.loc[tx["taxonomy_id"] == fixed, "breadcrumb"].iloc[0])]
        else:
            sub = tx[tx["breadcrumb"].map(norm_text).str.contains(norm_text(fixed), regex=False)]
        if sub is not None and sub.empty:
            log.warning("catalog: category '%s' is not in the taxonomy (or has no WordPress category)", fixed)
            sub = None
    for p in prods:
        k = p["pkey"]
        if old.get(k, {}).get("how") == "manual":            # chosen by a person: kept
            rows.append(old[k])
            continue
        if tx is None:
            rows.append({"pkey": k, "taxonomy_id": "", "breadcrumb": "", "how": "none",
                         "note": "تاکسونومی بارگذاری نشده"})
            continue
        pool = sub if sub is not None else tx
        if sub is not None and len(sub) == 1:
            r = sub.iloc[0]
            rows.append({"pkey": k, "taxonomy_id": r["taxonomy_id"], "breadcrumb": r["breadcrumb"], "how": "settings", "note": ""})
            continue
        words = {w for w in _words(f"{p['family']} {p['title']}") if len(w) > 1 and w not in STOP_WORDS}
        head = next((w for w in _words(p["family"]) if w not in STOP_WORDS), "")
        scored = sorted(((_score(words, r, head), i) for i, r in pool.iterrows()), reverse=True)[:12]
        cands = [pool.loc[i] for s, i in scored if s > 0]
        pick, how = None, "rule"
        if cands and secret("OPENROUTER_API_KEY") and cfg.get("catalog", {}).get("category_model", True):
            j = _openrouter(cfg, [{"role": "system", "content":
                                   "Pick the store category that fits the product best. Reply JSON only: "
                                   "{\"i\": <index>, \"why\": \"<few Persian words>\"}; i = -1 when none fits."},
                                  {"role": "user", "content": json.dumps(
                                      {"product": p["family"], "specs": p["common"],
                                       "categories": [{"i": n, "path": c["breadcrumb"]} for n, c in enumerate(cands)]},
                                      ensure_ascii=False)}], max_tokens=300)
            if j and isinstance(j.get("i"), int) and 0 <= j["i"] < len(cands):
                pick, how = cands[j["i"]], "model"
            elif j and j.get("i") == -1:
                pick, how = None, "model_none"
        if pick is None and how == "rule" and cands:
            best = scored[0][0]
            if best >= 2:                                    # at least one word of the leaf itself
                pick = cands[0]
        if pick is None and sub is not None:                 # inside the chosen branch: its top node
            top = sub.sort_values("breadcrumb", key=lambda s: s.str.len()).iloc[0]
            pick, how = top, "settings_branch"
        note = {"settings_branch": "دسته‌ی کلیِ شاخه؛ بهتر است دسته‌ی دقیق‌تر انتخاب شود"}.get(how, "")
        rows.append({"pkey": k, "taxonomy_id": "" if pick is None else pick["taxonomy_id"],
                     "breadcrumb": "" if pick is None else pick["breadcrumb"], "how": how if pick is not None else "none",
                     "note": note if pick is not None else "دسته پیدا نشد؛ در صفحه‌ی کاتالوگ دسته را انتخاب کنید"})
    out = write_csv(pd.DataFrame(rows, columns=["pkey", "taxonomy_id", "breadcrumb", "how", "note"]), d / CATS)
    log.info("catalog categories: %d of %d product(s) placed", sum(1 for r in rows if r["taxonomy_id"]), len(rows))
    return out


def set_category(cfg: dict, run: str, pkey: str, taxonomy_id: str) -> dict:
    d = run_dir(cfg, run)
    df = read_csv(d / CATS) if (d / CATS).exists() else pd.DataFrame(columns=["pkey", "taxonomy_id", "breadcrumb", "how", "note"])
    tx = _taxonomy(cfg)
    bc = ""
    if tx is not None and taxonomy_id:
        hit = tx[tx["taxonomy_id"] == str(taxonomy_id)]
        if hit.empty:
            raise ValueError("این شناسه در تاکسونومی نیست یا دسته‌ی وردپرس ندارد")
        bc = hit.iloc[0]["breadcrumb"]
    row = {"pkey": pkey, "taxonomy_id": str(taxonomy_id or ""), "breadcrumb": bc, "how": "manual", "note": ""}
    df = pd.concat([df[df["pkey"] != pkey], pd.DataFrame([row])], ignore_index=True)
    write_csv(df, d / CATS)
    return row


# ------------------------------------------------------------------ step: photos

def _maker_images(cfg: dict, f, site: str, p: dict, n: int) -> list[str]:
    """The maker's own product page: its shop search (WooCommerce) or a site: web search."""
    from .extract import extract_page, from_store_api
    from .search import detect_type, search_store_api, search_web, search_woo
    q = p["family"]
    row = {"taxonomy_id": "", "query": q, "route": "iran"}
    kind = detect_type(f, cfg, site)
    src = {"domain": site, "kind": "iran"}
    hits = []
    if kind == "woo_store_api":
        hits = search_store_api(f, src, row, 3)
    elif kind == "woo_search":
        hits = search_woo(f, src, row, 3)
    if not hits:
        hits = search_web(f, cfg, row, q, 3, site=site)
    urls: list[str] = []
    for h in hits[:2]:
        rec = None
        if h.get("prefilled_json"):
            rec = from_store_api(json.loads(h["prefilled_json"]))
        if not rec or not rec.get("image_urls"):
            res = f.get(h["url"])
            if res and res.get("status") == 200:
                rec = extract_page(res["text"], h["url"])
        for u in (rec or {}).get("image_urls") or []:
            if u not in urls:
                urls.append(u)
        if len(urls) >= n:
            break
    return urls[:n]


def _catalog_pictures(d: Path, rd: dict, p: dict, prods_on_page: int) -> list[Path]:
    """The catalog's own pictures beside this product's table (decoration on every page is left out)."""
    page = p.get("page")
    pics = [x for x in rd.get("images") or [] if str(x["page"]) == str(page) and x.get("file") and not x.get("repeated")]
    if not pics:
        return []
    if prods_on_page > 1:
        ys = [float(v["row_y"]) for v in p["variants"] if str(v.get("row_y") or "").replace(".", "", 1).isdigit()]
        if ys:
            lo, hi = min(ys), max(ys)
            pics = sorted(pics, key=lambda x: min(abs((x["y0"] + x["y1"]) / 2 - lo), abs((x["y0"] + x["y1"]) / 2 - hi)))[:1]
    return [d / x["file"] for x in pics]


def step_photos(cfg: dict, run: str, redo: bool = False) -> Path | None:
    import io as _io
    from PIL import Image
    from .images import download, make, product_key as img_key, search_images, free_models
    from .pipeline import _fetchers, _progress
    d = run_dir(cfg, run)
    prods = products(cfg, run)
    if not prods:
        return None
    rd = json.loads((d / READ).read_text(encoding="utf-8")) if (d / READ).exists() else {}
    st = settings(cfg, run)
    ic = cfg.get("images", {})
    per, size, quality = int(ic.get("per_product", 3)), int(ic.get("size", 1000)), int(ic.get("quality", 85))
    order = [x.strip() for x in str(cfg.get("catalog", {}).get("photos_from", "maker,catalog,search")).split(",")]
    old = {r["pkey"]: r for r in read_csv(d / IMAGES).to_dict("records")} if (d / IMAGES).exists() else {}
    fx = _fetchers(cfg)
    f = fx["iran"]
    per_page: dict = {}
    for p in prods:
        per_page[p["page"]] = per_page.get(p["page"], 0) + 1
    site = str(st.get("maker_site") or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    site = site.removeprefix("www.")
    rows = []
    for i, p in enumerate(prods, 1):
        if not redo and old.get(p["pkey"], {}).get("files"):
            rows.append(old[p["pkey"]])
            continue
        made, sources, how = [], [], ""
        for src in order:
            if made:
                break
            try:
                if src == "maker" and site:
                    for u in _maker_images(cfg, f, site, p, per):
                        data = download(f, u)
                        if data:
                            with Image.open(_io.BytesIO(data)) as im:
                                made.append(make(im, None, size, bool(ic.get("remove_background", True))))
                            sources.append(u)
                elif src == "catalog":
                    for path in _catalog_pictures(d, rd, p, per_page.get(p["page"], 1)):
                        with Image.open(path) as im:
                            made.append(make(im, None, size, bool(ic.get("remove_background", True))))
                        sources.append(f"catalog:{path.name}")
                elif src == "search":
                    q = " ".join(x for x in (p["family"], p["brand"]) if x)
                    for u in search_images(cfg, f, {"name": q}):
                        data = download(f, u, min_side=350)
                        if not data:
                            continue
                        with Image.open(_io.BytesIO(data)) as im:
                            pic = make(im, None, size, bool(ic.get("remove_background", True)))
                        if "text_overlay" in pic[1]:
                            continue
                        made.append(pic)
                        sources.append(u)
                        if len(made) >= per:
                            break
            except Exception as e:                       # noqa: BLE001 - one source failing never stops the step
                log.info("catalog photos %s (%s): %s", p["family"], src, e)
            if made:
                how = src
        key = img_key(f"catalog:{p['pkey']}")
        folder = cfg["_data_dir"] / "images" / key
        if folder.exists():
            for x in folder.glob("*.webp"):
                x.unlink()
        files = []
        for n, (pic, _) in enumerate(made[:per], 1):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                pic.save(folder / f"{n}.webp", "WEBP", quality=quality, method=4)
                files.append(f"{key}/{n}.webp")
            except Exception as e:                       # noqa: BLE001 - a photo that cannot be saved is skipped
                log.info("catalog photo save %s: %s", p["family"], e)
        rows.append({"pkey": p["pkey"], "key": key, "files": " | ".join(files), "sources": " | ".join(sources[:per]),
                     "how": how or "none"})
        if _progress("catalog photos", i, len(prods)):
            break
    free_models()
    write_csv(pd.DataFrame(rows, columns=["pkey", "key", "files", "sources", "how"]), d / IMAGES)
    log.info("catalog photos: %d of %d product(s) have photos (%s)", sum(1 for r in rows if r["files"]), len(rows),
             ", ".join(f"{k} {v}" for k, v in pd.Series([r["how"] for r in rows]).value_counts().items()))
    return d / IMAGES


# ------------------------------------------------------------------ step: describe

def _record(p: dict) -> dict:
    """A product as the description writer expects it (facts only; no prices)."""
    specs = dict(p["common"])
    for a in p["attributes"]:
        vals = list(dict.fromkeys(v["attributes"].get(a, "") for v in p["variants"] if v["attributes"].get(a)))
        if vals:
            specs[re.sub(r"\s*\([^()]*\)", "", a) + "‌های موجود"] = "، ".join(vals)
    return {"url": p["pkey"], "name": p["family"], "breadcrumb": p.get("category", {}).get("breadcrumb") or p["title"],
            "brand": p["brand"], "model": p["model"], "specs_json": json.dumps(specs, ensure_ascii=False),
            "description": "", "seller_name": ""}


def spec_html(p: dict) -> str:
    """The specs table and the variations table (sizes and their own specs, never prices)."""
    from html import escape as E
    facts = {"برند": p.get("brand_display") or p["brand"], **p["common"]}
    rows = "".join(f"<tr><th>{E(str(k))}</th><td>{E(str(v))}</td></tr>" for k, v in facts.items() if v)
    html = f"<h3>مشخصات فنی</h3><table>{rows}</table>" if rows else ""
    cols = p["attributes"] + list(dict.fromkeys(k for v in p["variants"] for k in v["specs"]))
    if len(p["variants"]) > 1 and cols:
        head = "".join(f"<th>{E(str(c))}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{E(str(v['attributes'].get(c) or v['specs'].get(c, '')))}</td>" for c in cols)
                       + "</tr>" for v in p["variants"] if not str(v.get("price_check", "")).startswith("duplicate"))
        html += f"<h3>مدل‌ها و {E(str(p['attributes'][0])) if p['attributes'] else 'اندازه'}‌ها</h3><table><tr>{head}</tr>{body}</table>"
    return html


def step_describe(cfg: dict, run: str, redo: bool = False) -> Path | None:
    from concurrent.futures import ThreadPoolExecutor
    from .describe import Writer, template
    d = run_dir(cfg, run)
    prods = products(cfg, run)
    if not prods:
        return None
    old = {r["pkey"]: r for r in read_csv(d / DESCS).to_dict("records")} if (d / DESCS).exists() else {}
    w = Writer(cfg)
    use = w.ok() and cfg.get("describe", {}).get("provider", "auto") in ("auto", "openrouter")

    def one(p):
        o = old.get(p["pkey"])
        if o and not redo and (o.get("method") == "ai" or not use):
            return o
        rec = _record(p)
        res = w.write(rec) if use else None
        if res:
            return {"pkey": p["pkey"], "html": res["html"], "short": res["short"], "method": "ai", "model": w.model}
        html, short = template(rec)
        return {"pkey": p["pkey"], "html": html, "short": short, "method": "template", "model": ""}
    with ThreadPoolExecutor(int(cfg.get("describe", {}).get("workers", 4)) if use else 1) as ex:
        rows = list(ex.map(one, prods))
    write_csv(pd.DataFrame(rows, columns=["pkey", "html", "short", "method", "model"]), d / DESCS)
    log.info("catalog descriptions: %d (%s)", len(rows), "OpenRouter " + w.model if use else "template")
    return d / DESCS


# ------------------------------------------------------------------ all steps

def run_steps(cfg: dict, run: str, steps: list[str] | None = None, progress=None) -> dict:
    steps = [s for s in (steps or STEPS) if s in STEPS]
    fns = {"read": step_read, "structure": step_structure, "prices": step_prices, "category": step_category,
           "photos": step_photos, "describe": step_describe}
    for n, s in enumerate(steps, 1):
        if progress:
            progress(s, n - 1, len(steps))
        fns[s](cfg, run)
    return summary(cfg, run)


def summary(cfg: dict, run: str) -> dict:
    prods = products(cfg, run)
    v = [x for p in prods for x in p["variants"]]
    return {"products": len(prods), "variations": len(v), "priced": sum(1 for x in v if x["price_usd"]),
            "held": sum(1 for x in v if x["price_check"]),
            "with_category": sum(1 for p in prods if p["category"].get("taxonomy_id")),
            "with_photos": sum(1 for p in prods if p["images"].get("files")),
            "settings": settings(cfg, run)}
