"""Price for products that have none, from the most to the least exact source.

  A1  the same model priced elsewhere in this crawl (any category, any shop)
  A2  the same model searched in the known WooCommerce shops (free, Store API)
  A3  the same model in a Persian web search (Serper; one search, capped per run)
  B   same brand in the same group, 2+ prices from real products
  C   category median, only when it is meaningful: 3+ prices, 2+ shops, max/min <= 3,
      and no 'X with actuator' combo sets in it
  D   nearest priced products of the same group (similar names, same brand first), scaled by the
      main size (نفره، کیلووات، وات، سانت، ولت، اینچ) when the two differ
  E   category, then group median of real prices (same Iran/foreign route), kept inside the group's range
A product that still has no price (no priced product anywhere near it) is left out of the import
and counted in 05_low_categories.csv.
price_estimated keeps the method (internal only).
"""
from __future__ import annotations

import json
import logging
import re

import pandas as pd

from .normalize import extract_models, fa_digits_to_en, model_key, norm_text, parse_number

log = logging.getLogger(__name__)
COMBO = re.compile(r"\sبا\s|\+|همراه|ست\s", re.I)


def _p(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


SPECISH = re.compile(r"^(dn|pn|ac|dc|ip|m)?\d+([.,/-]\d+)*\s*(v|vac|vdc|w|kw|mm|cm|nm|n|bar|ma|hz|inch|a)?$|"
                     r"\d+\s*v\b|\bdn\s?\d|\d+\s*(nm|mm|ma)\b|series|type|vvvf|\dm/?s|kw|\d+(kg|cm|mm)$", re.I)


def _model_keys(r: dict) -> list[str]:
    """Only the product's own model code; spec text (DC24V, DN50, 4-20mA) is not a model."""
    from .fa_name import clean_model
    m = clean_model(r.get("model") or "")
    if not m or SPECISH.search(m.strip()):
        return []
    k = model_key(m)
    return [k] if len(k) >= 4 and re.search(r"[a-z]", k) and re.search(r"\d", k) else []


def _snippet_prices(organic: list[dict], mk: str) -> list[float]:
    out = []
    for o in organic:
        text = fa_digits_to_en(f"{o.get('title', '')} {o.get('snippet', '')} {o.get('price', '')}")
        if mk not in model_key(text):
            continue
        for m in re.finditer(r"(\d[\d,٬.]{3,})\s*(تومان|تومن|ریال)", text):
            v = parse_number(m.group(1))
            if v:
                out.append(v / 10 if m.group(2) == "ریال" else v)
    return out


class Estimator:
    def __init__(self, cfg: dict, crawled: pd.DataFrame, fetcher=None):
        self.cfg = cfg
        pc = cfg.get("price", {})
        self.f = fetcher
        self.serper_left = int(pc.get("serper_lookups_per_run", 60)) if pc.get("serper_lookup", True) else 0
        self.woo_left = int(pc.get("woo_lookups_per_run", 300))
        from .routing_state import group_of
        self.by_model: dict[str, list[float]] = {}
        self.urls: dict[str, set] = {}
        for r in crawled.to_dict("records"):          # A1: every real price in this crawl, by model
            v = _p(r.get("price_toman"))
            if v and "price_outlier" not in (r.get("price_flags") or ""):
                for k in _model_keys(r):
                    self.by_model.setdefault(k, []).append(v)
                    self.urls.setdefault(k, set()).add(r.get("url"))
                g = group_of(r.get("breadcrumb") or "")
                for c in _codes(r.get("name") or ""):          # codes in the name too ('GST 90 BE'), within the group
                    self.by_model.setdefault(f"{g}|{c}", []).append(v)
                    self.urls.setdefault(f"{g}|{c}", set()).add(r.get("url"))
        self.shops = self._woo_shops()
        self.fails: dict[str, int] = {}                 # a shop whose API keeps failing is skipped for the run

    def _woo_shops(self) -> list[dict]:
        from .search import _base
        shops = [s for s in (self.cfg["sources"].get("iran_sites") or [])
                 if s.get("type") in ("woo_store_api", "woo_search")]
        p = self.cfg["_data_dir"] / "discovered.csv"
        if p.exists():
            d = pd.read_csv(p, dtype=str).fillna("")
            shops += [{"domain": r["domain"]} for r in d.to_dict("records") if r.get("type") == "woo_store_api"]
        seen, out = set(), []
        for s in shops:
            if s["domain"] not in seen:
                seen.add(s["domain"])
                out.append({**s, "base": _base(s)})
        return out[:12]

    def by_model_search(self, r: dict) -> tuple[float | None, str]:
        from .routing_state import group_of
        keys = _model_keys(r)
        g = group_of(r.get("breadcrumb") or "")
        for k in keys + [f"{g}|{c}" for c in _codes(r.get("name") or "")]:     # A1
            vals = self.by_model.get(k)
            if vals and r.get("url") and self.urls.get(k) == {r.get("url")}:
                continue                                   # only the product itself: no second opinion
            if vals and max(vals) / min(vals) <= 4:        # shops disagreeing 10x: unit error somewhere, skip
                return float(pd.Series(vals).median()), f"same model {k.split('|')[-1]} in crawl ({len(vals)})"
        if not keys or not self.f or r.get("source_type") == "foreign":
            return None, ""
        model = r.get("model") or keys[0]
        shops = [x for x in self.shops if self.fails.get(x["domain"], 0) < 2][:max(0, self.woo_left)]
        self.woo_left -= len(shops)

        def ask(x):                                     # A2: the shops are asked in parallel
            res = self.f.get(f"{x['base']}/wp-json/wc/store/v1/products", params={"search": model, "per_page": 5},
                             as_json=True)
            if not res or res.get("status") != 200 or not isinstance(res.get("json"), list):
                self.fails[x["domain"]] = self.fails.get(x["domain"], 0) + 1
                return None
            for it in res["json"]:
                if keys[0] not in model_key(it.get("name", "")):
                    continue
                pr = it.get("prices") or {}
                amt = parse_number(pr.get("price"))
                if amt:
                    amt = amt / (10 ** int(pr.get("currency_minor_unit") or 0))
                    rial = "ریال" in f"{pr.get('currency_suffix', '')}{pr.get('currency_symbol', '')}" \
                           or (pr.get("currency_code") or "").upper() == "IRR"
                    return (amt / 10 if rial else amt), f"same model {model} at {x['domain']}"
            return None
        if shops:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(min(8, len(shops))) as ex:
                for hit in ex.map(ask, shops):           # in shop order: the first shop with the model wins
                    if hit:
                        return hit
        if self.serper_left > 0:                       # A3
            self.serper_left -= 1
            try:
                from .config import secret
                from .probe import record_usage
                key = secret("SERPER_API_KEY")
                if key:
                    q = f"قیمت {r.get('brand') or ''} {model}".strip()
                    res = self.f.post_json("https://google.serper.dev/search", {"q": q, "gl": "ir", "hl": "fa", "num": 10},
                                           {"X-API-KEY": key, "Content-Type": "application/json"})
                    record_usage(self.cfg)
                    vals = _snippet_prices((res or {}).get("json", {}).get("organic", []), keys[0])
                    if vals:
                        return float(pd.Series(vals).median()), f"web search for {model} ({len(vals)} prices)"
            except Exception as e:
                log.warning("price search failed for %s: %s", model, e)
        return None, ""


FEATS = [("persons", r"(\d+)\s*نفره"), ("kw", r"(\d+(?:\.\d+)?)\s*(?:کیلو\s?وات|kw)\b"),
         ("w", r"(\d{2,5})\s*(?:وات|w)\b"), ("cm", r"(\d+(?:\.\d+)?)\s*(?:سانتی\s?متر|سانتیمتر|سانت|سانتی|cm)\b"),
         ("v", r"(\d+(?:\.\d+)?)\s*(?:ولت|v)\b"), ("inch", r"(\d+(?:\.\d+)?)\s*(?:اینچ|inch)")]
NOISE = {norm_text(w) for w in ("قیمت", "خرید", "مدل", "برند", "اصل", "اصلی", "و", "با", "+", "-", "فروش", "مشخصات",
                                 "ابزار", "امروز", "سرعت", "متر", "بر", "ثانیه")}


def feats(name: str) -> dict[str, float]:
    t = fa_digits_to_en(str(name or "")).lower()
    out = {}
    for k, rx in FEATS:
        m = re.search(rx, t)
        if m:
            try:
                out[k] = float(m.group(1))
            except ValueError:
                pass
    return out


def _words(name: str) -> set[str]:
    return {w for w in norm_text(fa_digits_to_en(str(name or ""))).lower().split() if w not in NOISE and len(w) > 1}


def scaled(price: float, target: dict, ref: dict) -> float:
    """Bigger motor/saw, higher price — but not linearly (exponent 0.7), and never more than 3x either way."""
    for k, _ in FEATS:
        if k in target and k in ref and ref[k] > 0 and target[k] > 0 and target[k] != ref[k]:
            ratio = min(3.0, max(1 / 3, (target[k] / ref[k]) ** 0.7))
            return price * ratio
    return price


class Neighbours:
    """Real prices of everything crawled (this run), for products that have none."""
    def __init__(self, crawled: pd.DataFrame):
        from .routing_state import group_of
        c = crawled.copy()
        for col in ("url", "name", "breadcrumb", "brand", "source_type"):
            if col not in c.columns:
                c[col] = c.index.astype(str) if col == "url" else ""
        c["_p"] = pd.to_numeric(c.get("price_toman"), errors="coerce")
        c = c[c["_p"] > 0]
        if "price_flags" in c:
            c = c[~c["price_flags"].fillna("").str.contains("outlier|too_high")]
        c = c[~c["name"].fillna("").map(lambda n: bool(COMBO.search(n)))]
        c = c.drop_duplicates("url")
        c["_g"] = c["breadcrumb"].fillna("").map(group_of)
        med = c.groupby(["_g", "source_type"])["_p"].transform("median")
        c = c[(c["_p"] <= med * 8) & (c["_p"] >= med / 8)]  # a wild price (wrong unit, bulk lot) teaches nothing
        self.rows = [{"g": r["_g"], "st": r.get("source_type", ""), "p": float(r["_p"]), "url": r["url"],
                      "w": _words(r["name"]), "f": feats(r["name"]), "brand": str(r.get("brand") or "")}
                     for r in c.to_dict("records")]

    def guess(self, r: dict, group: str) -> tuple[float | None, str]:
        pool = [x for x in self.rows if x["g"] == group and x["st"] == r.get("source_type", "") and x["url"] != r.get("url")]
        if not pool:
            return None, ""
        generic = _words(r.get("breadcrumb", "").replace(">", " "))      # 'موتور آسانسور گیرلس' says nothing
        w, f, brand = _words(r.get("name")) - generic, feats(r.get("name")), str(r.get("brand") or "")
        scored = []
        for x in pool:
            xw = x["w"] - generic
            j = len(w & xw) / len(w | xw) if w and xw else 0
            if brand and x["brand"] and brand == x["brand"]:
                j += 0.25
            scored.append((j, x))
        scored.sort(key=lambda t: -t[0])
        near = [x for j, x in scored[:5] if j >= 0.3]                # same brand / model family
        prices = pd.Series([x["p"] for x in pool])
        lo, hi = prices.quantile(0.1), prices.quantile(0.9)
        if near:
            v = float(pd.Series([scaled(x["p"], f, x["f"]) for x in near]).median())
            return min(max(v, lo / 2), hi * 2), f"{len(near)} similar products in group"
        for k, _ in FEATS:                                            # same size elsewhere in the group
            if k in f:
                same = sorted(((abs(x["f"][k] - f[k]) / max(f[k], 1e-9), x) for x in pool if k in x["f"]),
                              key=lambda t: t[0])[:3]
                same = [x for d, x in same if d <= 0.5]
                if same:
                    v = float(pd.Series([scaled(x["p"], f, x["f"]) for x in same]).median())
                    return v, f"{len(same)} products of similar size ({k})"
        if len(pool) >= 3 and hi / lo <= 5:                           # a narrow group: its median is meaningful
            return float(prices.median()), f"group median of {len(pool)}"
        return None, ""


def _codes(name: str) -> list[str]:
    """Model codes in a name, glued: 'GST 8000 E' -> 'gst8000e', 'مدل 4165' -> '4165'."""
    from .rules import _flat
    # a whole English name glued together is not a code: 4-14 characters, letters/digits only
    return [c for c in _flat(name).split("|") if 4 <= len(c) <= 14 and re.search(r"\d", c)
            and not re.fullmatch(r"\d{5,}", c) and not SPECISH.search(c)]


def fix_units(df: pd.DataFrame, ref: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rial read as toman (or the reverse) is the commonest price error: a shop's JSON-LD says IRR for
    toman, or its page shows rial. The same model sold by several shops tells: a price about 10x the
    others' is divided by 10 (about 1/10 is multiplied). A shop that is 10x off on most of its models
    is corrected as a whole. 'ref' is the wider crawl to learn from (default: df itself)."""
    from .routing_state import group_of
    if df.empty:
        return df
    ref = df if ref is None else ref
    def table(x: pd.DataFrame) -> pd.DataFrame:
        t = pd.DataFrame({"p": pd.to_numeric(x.get("price_toman"), errors="coerce"),
                          "dom": x.get("source_domain", pd.Series("", index=x.index)).fillna(""),
                          "st": x.get("source_type", pd.Series("", index=x.index)).fillna(""),
                          "g": x.get("breadcrumb", pd.Series("", index=x.index)).fillna("").map(group_of),
                          "codes": x.get("name", pd.Series("", index=x.index)).fillna("").map(_codes)}, index=x.index)
        return t
    rt = table(ref)
    rt = rt[(rt["p"] > 0) & (rt["st"] != "foreign")]
    # per (group, code): each shop's median price
    by: dict[tuple, dict[str, float]] = {}
    for _, r in rt.iterrows():
        for c in r["codes"]:
            by.setdefault((r["g"], c), {}).setdefault(r["dom"], []).append(r["p"])
    by = {k: {d: float(pd.Series(v).median()) for d, v in shops.items()} for k, shops in by.items() if len(shops) >= 2}
    def others(key, dom):
        return [v for d, v in by.get(key, {}).items() if d != dom]
    def tenfold(q):
        return 0.1 if 6 <= q <= 16 else 10.0 if 1 / 16 <= q <= 1 / 6 else 1.0
    # shop-level factor: nearly all of the shop's shared models are ~10x (or ~1/10) of other shops
    ratios: dict[str, list[float]] = {}
    for key, shops in by.items():
        for dom, v in shops.items():
            o = others(key, dom)
            if len(o) >= 2 and max(o) / min(o) <= 2.5:
                ratios.setdefault(dom, []).append(v / float(pd.Series(o).median()))
    shop_factor = {}
    for dom, rs in ratios.items():
        fs = [tenfold(q) for q in rs]
        for f in (0.1, 10.0):
            if len(rs) >= 3 and fs.count(f) >= 0.8 * len(rs):
                shop_factor[dom] = f
    df = df.copy()
    t = table(df)
    n_shop = n_row = 0
    for i, r in t.iterrows():
        if not (r["p"] > 0) or r["st"] == "foreign":
            continue
        f = shop_factor.get(r["dom"], 1.0)
        if f == 1.0:
            for c in r["codes"]:                           # this row against the same model in other shops
                o = others((r["g"], c), r["dom"])
                if not o:
                    continue
                if len(o) < 2 or max(o) / min(o) > 2.5:
                    continue                               # one shop against one: no way to tell who is right
                f = tenfold(r["p"] / float(pd.Series(o).median()))
                break
            n_row += f != 1.0
        else:
            n_shop += 1
        if f != 1.0:
            df.at[i, "price_toman"] = str(round(r["p"] * f))
            if "price_flags" in df:
                df.at[i, "price_flags"] = " | ".join(x for x in (str(df.at[i, "price_flags"] or ""), "unit_fixed") if x)
    if n_shop or n_row:
        log.info("price units fixed: %d rows of %d shops 10x off (%s), %d single rows",
                 n_shop, len(shop_factor), ", ".join(shop_factor), n_row)
    return df


def _bounded(v: float, p: pd.Series, df: pd.DataFrame, groups: pd.Series, i, combo: pd.Series) -> float | None:
    """An estimate must look like its category's (or group's) real prices: within half the cheapest and
    twice the dearest; an estimate far outside is a wrong match, so the product gets none."""
    same = (df["source_type"] == df.at[i, "source_type"]) & p.notna() & ~combo   # sets ('X with Y') are not like X
    for m in ((df["taxonomy_id"] == df.at[i, "taxonomy_id"]) & same, (groups == groups[i]) & same):
        vals = p[m]
        if len(vals) >= 2:
            lo, hi = float(vals.quantile(0.1)), float(vals.quantile(0.9))
            if v < lo / 3 or v > hi * 3:
                return None
            return min(max(v, lo / 2), hi * 2)
    return v


def estimate(cfg: dict, df: pd.DataFrame, crawled: pd.DataFrame, fetcher=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (with prices, needs_price)."""
    if df.empty:
        return df, df
    from .routing_state import group_of
    df = df.copy()
    df["price_estimated"] = ""
    est = Estimator(cfg, crawled, fetcher)
    p = pd.to_numeric(df["price_toman"], errors="coerce")
    usd = pd.to_numeric(df.get("usd_rate_toman"), errors="coerce")
    groups = df["breadcrumb"].map(group_of)
    real_brand = df.get("brand", pd.Series("", index=df.index)).fillna("")
    combo = df["name"].fillna("").map(lambda n: bool(COMBO.search(n)))
    counts = {"A": 0, "B": 0, "C": 0}
    for i in df.index[p.isna()]:
        r = df.loc[i].to_dict()
        v, how = est.by_model_search(r)
        tier = "A"
        if v is None and real_brand[i]:
            vals = p[(groups == groups[i]) & (real_brand == real_brand[i]) & p.notna() & ~combo]
            if len(vals) >= 2:
                v, how, tier = vals.median(), f"median of {len(vals)} {real_brand[i]} in group", "B"
        if v is None:
            m = (df["taxonomy_id"] == r["taxonomy_id"]) & p.notna() & ~combo & (df["source_type"] == r["source_type"])
            vals = p[m]
            shops = df.loc[m, "source_domain"].nunique()
            if len(vals) >= 3 and shops >= 2 and vals.max() / vals.min() <= 3:
                v, how, tier = vals.median(), f"median of {len(vals)} in category ({shops} shops)", "C"
        if v is not None:
            v = _bounded(v, p, df, groups, i, combo)
        if v is not None:
            df.at[i, "price_toman"] = str(round(v))
            if pd.notna(usd.get(i)) and usd[i]:
                df.at[i, "price_usd"] = str(round(v / usd[i], 2))
            df.at[i, "price_estimated"] = how
            counts[tier] += 1
    nb = Neighbours(crawled if len(crawled) else df)
    p1 = pd.to_numeric(df["price_toman"], errors="coerce")
    counts["D"] = counts["E"] = 0
    for i in df.index[p1.isna()]:
        r = df.loc[i].to_dict()
        # E1: this category's own real prices, when the products are alike (narrow range)
        m = (df["taxonomy_id"] == r["taxonomy_id"]) & p.notna() & ~combo & (df["source_type"] == r["source_type"])
        v, how = nb.guess(r, groups[i])
        tier = "D" if v is not None and "similar" in how else "E"
        if (v is None or tier == "E") and m.sum() >= 1:
            vals = p[m]
            if vals.max() / vals.min() <= 4:
                v, how, tier = float(vals.median()), f"category median of {len(vals)}", "E"
        if v is not None:
            v = _bounded(v, p, df, groups, i, combo)       # stay within the category's / group's real prices
        if v is not None:
            df.at[i, "price_toman"] = str(round(v, -3) if v > 10000 else round(v))
            if pd.notna(usd.get(i)) and usd[i]:
                df.at[i, "price_usd"] = str(round(v / usd[i], 2))
            df.at[i, "price_estimated"] = how
            counts[tier] += 1
    p2 = pd.to_numeric(df["price_toman"], errors="coerce")
    need = df[p2.isna()]
    log.info("prices filled: same model %d, same brand %d, category %d, similar products %d, medians %d; "
             "no price anywhere near (left out): %d", counts["A"], counts["B"], counts["C"], counts["D"], counts["E"],
             len(need))
    return df[p2.notna()], need
