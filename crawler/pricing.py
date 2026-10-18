"""Price for products that have none, from the most to the least exact source.

  A1  the same model priced elsewhere in this crawl (any category, any shop)
  A2  the same model searched in the known WooCommerce shops (free, Store API)
  A3  the same model in a Persian web search (Serper; one search, capped per run)
  B   same brand in the same group, 2+ prices from real products
  C   category median, only when it is meaningful: 3+ prices, 2+ shops, max/min <= 3,
      and no 'X with actuator' combo sets in it
Anything still without a price goes to 05_needs_price.csv and is not exported.
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
                     r"\d+\s*v\b|\bdn\s?\d|\d+\s*(nm|mm|ma)\b|series|type", re.I)


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
        self.by_model: dict[str, list[float]] = {}
        for r in crawled.to_dict("records"):          # A1: every real price in this crawl, by model
            v = _p(r.get("price_toman"))
            if v and "price_outlier" not in (r.get("price_flags") or ""):
                for k in _model_keys(r):
                    self.by_model.setdefault(k, []).append(v)
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
        keys = _model_keys(r)
        for k in keys:                                  # A1
            if k in self.by_model:
                vals = self.by_model[k]
                return float(pd.Series(vals).median()), f"same model {k} in crawl ({len(vals)})"
        if not keys or not self.f or r.get("source_type") == "foreign":
            return None, ""
        model = r.get("model") or keys[0]
        for s in self.shops:                            # A2
            if self.woo_left <= 0:
                break
            if self.fails.get(s["domain"], 0) >= 2:
                continue
            self.woo_left -= 1
            res = self.f.get(f"{s['base']}/wp-json/wc/store/v1/products", params={"search": model, "per_page": 5},
                             as_json=True)
            if not res or res.get("status") != 200 or not isinstance(res.get("json"), list):
                self.fails[s["domain"]] = self.fails.get(s["domain"], 0) + 1
                continue
            for it in res["json"]:
                if keys[0] not in model_key(it.get("name", "")):
                    continue
                pr = it.get("prices") or {}
                amt = parse_number(pr.get("price"))
                if amt:
                    amt = amt / (10 ** int(pr.get("currency_minor_unit") or 0))
                    rial = "ریال" in f"{pr.get('currency_suffix', '')}{pr.get('currency_symbol', '')}" \
                           or (pr.get("currency_code") or "").upper() == "IRR"
                    return (amt / 10 if rial else amt), f"same model {model} at {s['domain']}"
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
            df.at[i, "price_toman"] = str(round(v))
            if pd.notna(usd.get(i)) and usd[i]:
                df.at[i, "price_usd"] = str(round(v / usd[i], 2))
            df.at[i, "price_estimated"] = how
            counts[tier] += 1
    p2 = pd.to_numeric(df["price_toman"], errors="coerce")
    need = df[p2.isna()]
    log.info("prices filled: same model %d, same brand %d, category %d; still without price: %d (05_needs_price.csv)",
             counts["A"], counts["B"], counts["C"], len(need))
    return df[p2.notna()], need
