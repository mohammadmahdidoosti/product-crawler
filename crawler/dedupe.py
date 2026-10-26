"""Step 4b/5: merge duplicates inside the crawl, then compare with the site.

Inside the crawl: one model found on five sites becomes ONE product with
five price observations; its price is the median.
Against the site: a crawled product that already exists on Caravan is NOT
dropped — it goes to the "updates" file (fresh price + date for the existing
page). Unsure matches go to a review file for a person to decide.
"""
from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict

import pandas as pd

from .normalize import extract_models, median, model_key, norm_text

try:  # faster and better if installed; difflib otherwise
    from rapidfuzz import fuzz

    def similarity(a: str, b: str) -> float:
        return fuzz.token_set_ratio(a, b) / 100
except ImportError:  # pragma: no cover
    def similarity(a: str, b: str) -> float:
        ta, tb = set(a.split()), set(b.split())
        if not ta or not tb:
            return 0.0
        inter = " ".join(sorted(ta & tb))
        sa, sb = " ".join(sorted(ta)), " ".join(sorted(tb))
        r = max(difflib.SequenceMatcher(None, inter, sa).ratio() if inter else 0,
                difflib.SequenceMatcher(None, inter, sb).ratio() if inter else 0,
                difflib.SequenceMatcher(None, sa, sb).ratio())
        return r

BAD_PRICE_FLAGS = ("too_low", "too_high", "no_price")


_VARIANT = ("بدون باتری", "بدون باطری", "تک باتری", "دو باتری", "بدنه", "کیت", "مجموعه", "طرح", "کپی")


def _flags(name: str) -> str:
    """Kit / bare tool / copy: the same code in two of these is two products."""
    return "".join(str(i) for i, w in enumerate(_VARIANT) if w in name)


def _model(row: dict, name: str) -> str:
    mk = model_key(row.get("model"))
    if len(mk) < 4:
        from .pipeline import _codes
        import re as _re
        unit = _re.compile(r"^\d+(kw|w|v|hp|mm|cm|l|lit|bar|a|ah|kg|rpm|nm)$")   # '61kw' is a power, not a model
        codes = sorted((c for c in _codes(name) if len(c) >= 4 and any(ch.isdigit() for ch in c)
                        and not unit.match(c)), key=len, reverse=True)
        mk = codes[0] if codes else ""
    return mk if len(mk) >= 4 else ""


def _cluster_key(row: dict) -> str:
    """Inside one category: the model code ('درایو YD101…' and 'اینورتر yd101…' are one product)."""
    mk = model_key(row.get("model"))
    if len(mk) >= 4:
        # brand left out on purpose: sites spell it differently (پولیکو / Pulico)
        return f"m:{mk}"
    return ""


def _cross_key(row: dict) -> str:
    """Across categories: the kind of thing (first word) + the model code + kit/bare/copy. Two shops'
    'سشوار صنعتی کرون مدل CT19017' became two products on the site when they sat in two categories."""
    name = norm_text(row.get("name"))
    head = (name.split() or [""])[0]
    mk = _model(row, name)
    if mk.isdigit():                 # a bare number ('1500' rpm, '4111'): only with the same brand
        brand = norm_text(row.get("brand"))
        if not brand:
            return ""
        mk = f"{mk}|{brand}"
    return f"{head}|{mk}|{_flags(name)}" if mk and head else ""


def merge_crawl(df: pd.DataFrame, name_threshold: float = 0.9) -> pd.DataFrame:
    """Group rows that are the same product; one output row per group. Inside a category: the model
    code, else a similar name. Then groups with the same kind + model code (worded differently:
    'سشوار صنعتی GHG 23-66 بوش اصلی' / 'سشوار صنعتی بوش مدل GHG 23-66'), in one category or several,
    become one product, listed in all those categories."""
    groups: list[list[dict]] = []
    for _, part in df.groupby("taxonomy_id", sort=False):
        by_key: dict[str, list[dict]] = defaultdict(list)
        loose: list[list[dict]] = []
        for r in part.to_dict("records"):
            k = _cluster_key(r)
            if k:
                by_key[k].append(r)
                continue
            n = norm_text(r.get("name"))
            for g in loose:
                if similarity(n, norm_text(g[0].get("name"))) >= name_threshold:
                    g.append(r)
                    break
            else:
                loose.append([r])
        groups += list(by_key.values()) + loose
    joined: dict[str, list[dict]] = {}
    out: list[list[dict]] = []
    for g in groups:
        k = _cross_key(g[0])
        if k and k in joined:                 # same kind + code: one product (same category or not)
            joined[k] += g
            continue
        if k and k not in joined:
            joined[k] = g
        out.append(g)
    return pd.DataFrame([_aggregate(g) for g in out])


def _num(x) -> float | None:
    try:
        return float(x) if str(x).strip() not in ("", "nan") else None
    except ValueError:
        return None


_UNITS = [  # every spelling of a unit -> one name, so '125 میلی متری' and '125 میلیمتر' are one feature
    ("میلی‌متر", r"میلی ?‌?متری?|میلیمتری?|mm"), ("سانتی‌متر", r"سانتی ?‌?متری?|سانتیمتری?|سانتی|سانت|cm"),
    ("اینچ", r"اینچی?"), ("وات", r"وات|w"), ("کیلووات", r"کیلو ?وات|kw"), ("ولت", r"ولتی?|v"),
    ("دور", r"دور"), ("آمپر", r"آمپر"), ("اسب", r"اسب"), ("کیلوگرم", r"کیلویی|کیلوگرمی?|کیلو|kg"),
    ("لیتر", r"لیتری?"), ("بار", r"بار"), ("ژول", r"ژول"), ("نفره", r"نفره"), ("تیغ", r"تیغه?")]
_NUM = r"(?<![\w.])(\d+(?:[.,/]\d+)?)\s*"   # not inside a model code (VR2016V)
_WORDS = (r"مشتی|گیربکسی|دیمردار|دیمر دار|براشلس|براش لس|اتوماتیک|چکشی|ضربه ای|سه حالته|دو حالته|دوکاره|دو کاره|"
          r"دو سرعته|دور متغیر|لیزری|چراغ دار|کشویی|رومیزی|مینی|سنگین")
_SAME = {"دیمر دار": "دیمردار", "براش لس": "براشلس", "دو کاره": "دوکاره"}


def features(name: str) -> dict[str, str]:
    """{key: text} of the buyer-facing features in a title: a number with its unit (one key per
    unit, whatever the spelling) and the descriptive words ('مشتی', 'گیربکسی')."""
    import re as _re
    from .normalize import fa_digits_to_en
    t = fa_digits_to_en(norm_text(name))
    out: dict[str, str] = {}
    for unit, pat in _UNITS:
        for m in _re.finditer(_NUM + rf"(?:{pat})(?![\w])", t):
            n = m.group(1).replace(",", ".")
            out.setdefault(f"{n}|{unit}", f"{n} {unit}")
    for m in _re.finditer(_WORDS, t):
        w = _SAME.get(m.group(0), m.group(0))
        out.setdefault(w, w)
    return out


def merged_name(best: str, others: list[str], most: int = 3) -> str:
    """The kept product's title also carries what the other shops' titles told a buyer ('مشتی',
    '550 وات', '4 دور'), so a search for any of them still finds it. A feature the title already
    has, in any spelling, is not added again; nor a second value of a unit it already states."""
    have = features(best)
    units = {k.split("|")[1] for k in have if "|" in k}
    add: dict[str, str] = {}
    for n in others:
        for k, v in features(n).items():
            unit = k.split("|")[1] if "|" in k else ""
            if k in have or k in add or (unit and (unit in units or any(a.endswith("|" + unit) for a in add))):
                continue
            add[k] = v
    return best if not add else f"{best}، " + "، ".join(list(add.values())[:most])


def _aggregate(g: list[dict]) -> dict:
    best = max(g, key=lambda r: (len(r.get("specs_json") or ""), len(r.get("name") or "")))
    out = dict(best)
    others = [str(r.get("name") or "") for r in g if r is not best]
    if others:
        out["name"] = merged_name(str(best.get("name") or ""), others)
        out["alt_names"] = " | ".join(dict.fromkeys(o for o in others if o and o != best.get("name")))
        specs: dict = {}
        for r in g:                                        # every shop's specs; the best page wins a clash
            try:
                specs.update({k: v for k, v in json.loads(r.get("specs_json") or "{}").items() if k not in specs})
            except (ValueError, AttributeError):
                pass
        try:
            specs = {**specs, **json.loads(best.get("specs_json") or "{}")}
        except (ValueError, AttributeError):
            pass
        if specs:
            out["specs_json"] = json.dumps(specs, ensure_ascii=False)
    good = [r for r in g if not any(f in (r.get("price_flags") or "") for f in BAD_PRICE_FLAGS)]
    prices = [_num(r.get("price_toman")) for r in good]
    prices = [p for p in prices if p]
    out["price_toman"] = round(median(prices)) if prices else ""
    out["price_min_toman"] = round(min(prices)) if prices else ""
    out["price_max_toman"] = round(max(prices)) if prices else ""
    usd = _num(out.get("usd_rate_toman"))
    out["price_usd"] = round(out["price_toman"] / usd, 2) if prices and usd else ""
    out["n_sources"] = len({r.get("url") for r in g})
    for col, main in (("extra_taxonomy_ids", "taxonomy_id"), ("extra_breadcrumbs", "breadcrumb")):
        # categories of every merged page: its main one and its extra ones
        vals = [str(r.get(main) or "") for r in g] + \
               [x for r in g for x in str(r.get(col) or "").split(" | ")]
        vals = [x for x in vals if x and x != "nan"]
        out[col] = " | ".join(dict.fromkeys(v for v in vals if v not in (out.get("taxonomy_id"), out.get("breadcrumb"))))
    out["n_priced"] = len(prices)
    brands = Counter(r.get("brand") for r in g if r.get("brand"))
    out["brand"] = brands.most_common(1)[0][0] if brands else ""
    out["urls"] = " | ".join(dict.fromkeys(r.get("url", "") for r in g))
    out["sellers"] = " | ".join(dict.fromkeys(r.get("seller_name", "") for r in g if r.get("seller_name")))
    out["seller_phones"] = " | ".join(dict.fromkeys(
        p for r in g for p in str(r.get("seller_phones") or "").split(" | ") if p))
    imgs = [u for r in g for u in json.loads(r.get("image_urls") or "[]")]
    out["image_urls"] = json.dumps(list(dict.fromkeys(imgs))[:6], ensure_ascii=False)
    return out


# ------------------------------------------------------------------ site catalog

def load_site_catalog(path, colmap: dict) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)

    def pick(key: str) -> str | None:
        for c in colmap.get(key, []):
            if c in df.columns:
                return c
        return None
    cols = {k: pick(k) for k in ("id", "sku", "name", "price", "categories")}
    if not cols["name"] and len(df.columns) == 1:
        cols["name"] = df.columns[0]                 # a plain list of product names
    if not cols["name"]:
        raise ValueError(f"site export needs a name column; found {list(df.columns)[:15]}")
    out = pd.DataFrame({k: df[c] if c else "" for k, c in cols.items()})
    if not cols["id"]:                               # names only: products are matched, prices cannot be updated
        out["id"] = [f"name-{i + 1}" for i in range(len(out))]
    out = out[out["name"].str.strip() != ""].reset_index(drop=True)
    out["name_n"] = out["name"].map(norm_text)
    return out


class SiteIndex:
    def __init__(self, site: pd.DataFrame):
        self.site = site.reset_index(drop=True)
        self.by_model: dict[str, list[int]] = defaultdict(list)
        self.by_token: dict[str, set[int]] = defaultdict(set)
        for i, r in self.site.iterrows():
            for m in extract_models(r["name"], r["sku"]):
                self.by_model[model_key(m)].append(i)
            if model_key(r["sku"]) and len(model_key(r["sku"])) >= 4:
                self.by_model[model_key(r["sku"])].append(i)
            for t in r["name_n"].split():
                if len(t) > 2:
                    self.by_token[t].add(i)

    def match(self, row: dict) -> tuple[int | None, float, str]:
        mk = model_key(row.get("model"))
        if len(mk) >= 4 and mk in self.by_model:
            cands = self.by_model[mk]
            name = norm_text(row.get("name"))
            best = max(cands, key=lambda i: similarity(name, self.site.at[i, "name_n"]))
            return best, 1.0, "model"
        name = norm_text(row.get("name"))
        toks = [t for t in name.split() if len(t) > 2]
        counts: Counter = Counter()
        for t in toks:
            counts.update(self.by_token.get(t, ()))
        cands = [i for i, c in counts.most_common(40) if c >= min(2, len(toks))]
        if not cands:
            return None, 0.0, ""
        scored = [(similarity(name, self.site.at[i, "name_n"]), i) for i in cands]
        s, i = max(scored)
        return i, s, "name"


def split_against_site(merged: pd.DataFrame, site: pd.DataFrame | None,
                       same: float = 0.92, maybe: float = 0.8):
    """Return (new, updates, review) DataFrames."""
    if site is None or site.empty:
        return merged, merged.iloc[0:0], merged.iloc[0:0]
    idx = SiteIndex(site)
    new, upd, rev = [], [], []
    for r in merged.to_dict("records"):
        i, score, how = idx.match(r)
        if i is not None:
            s = idx.site.iloc[i]
            r = {**r, "site_product_id": s["id"], "site_name": s["name"], "site_price": s["price"],
                 "match_score": round(score, 3), "match_by": how}
        if i is not None and score >= same:
            upd.append(r)
        elif i is not None and score >= maybe:
            rev.append(r)
        else:
            new.append(r)
    cols = list(merged.columns) + ["site_product_id", "site_name", "site_price", "match_score", "match_by"]
    return (pd.DataFrame(new, columns=cols), pd.DataFrame(upd, columns=cols),
            pd.DataFrame(rev, columns=cols))
