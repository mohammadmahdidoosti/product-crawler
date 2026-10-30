"""Keep the best N products per category by a data-quality score.

Sales counts and ratings are not visible on Made-in-China or on most Iranian
shops, so "best" means: most likely to make a good, sellable product page.

  relevance (filter confidence)        30
  has a price                          20
  sold by several shops (n_sources)    10   <- the closest thing to "popular"
  has a model number                   10
  specs (up to 8)                      15
  images (up to 3)                     10
  supplier signal                       5   Diamond/Gold/Audited (China) or a phone (Iran)
                                      ---
                                      100
Then the top N are taken with at most `max_per_seller` from one seller, so
one big shop cannot fill a category (relaxed when there are not enough others).
"""
from __future__ import annotations

import json

import pandas as pd


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def score(r: dict) -> tuple[float, str]:
    try:
        specs = len(json.loads(r.get("specs_json") or "{}"))
    except ValueError:
        specs = 0
    try:
        imgs = len(json.loads(r.get("image_urls") or "[]"))
    except ValueError:
        imgs = 0
    badges = (r.get("supplier_badges") or "").lower()
    sup = 0
    if "diamond" in badges:
        sup = 4
    elif "gold" in badges:
        sup = 3
    if "audited" in badges:
        sup += 2
    if not badges and r.get("seller_phones"):
        sup = 5
    parts = {
        "ارتباط": 30 * _f(r.get("relevance_confidence"), 0.6),
        "قیمت": 20 if str(r.get("price_toman") or "").strip() else 0,
        "چند فروشنده": 10 * min(_f(r.get("n_sources"), 1), 3) / 3,
        "مدل": 10 if str(r.get("model") or "").strip() else 0,
        "مشخصات": 15 * min(specs, 8) / 8,
        "عکس": 10 * min(imgs, 3) / 3,
        "تأمین‌کننده": min(sup, 5),
    }
    total = round(sum(parts.values()), 1)
    return total, " · ".join(f"{k} {round(v)}" for k, v in parts.items() if v)


def _foreign(r) -> bool:
    return "made-in-china" in str(r.get("source_domain") or r.get("url") or "")


def select_top(df: pd.DataFrame, n: int, max_per_seller: int = 3, max_foreign: int = 0,
               foreign_skip: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (kept, dropped); both carry quality_score and quality_why.
    max_foreign: at most this many Made-in-China products per category (0 = no cap); foreign_skip: a
    regex of category words whose Made-in-China products are not taken at all (cheap consumables a
    factory sells by the thousand: batch 7 brought 290 brushes, spacers and clips, all dropped)."""
    if df.empty:
        return df, df
    df = df.copy()
    if foreign_skip:
        leaf = df.get("breadcrumb", pd.Series("", index=df.index)).astype(str).str.split(" > ").str[-1]
        skip = df.apply(_foreign, axis=1) & leaf.str.contains(foreign_skip, regex=True)
        if skip.any():
            out = df[skip].copy()
            out["quality_score"], out["quality_why"] = 0, "foreign consumable"
            kept, dropped = select_top(df[~skip], n, max_per_seller, max_foreign, "")
            return kept, pd.concat([dropped, out])
    sc = [score(r) for r in df.to_dict("records")]
    df["quality_score"] = [s for s, _ in sc]
    df["quality_why"] = [w for _, w in sc]
    if not n:
        return df.sort_values("quality_score", ascending=False), df.iloc[0:0]
    keep_idx = []
    for _, part in df.groupby("taxonomy_id", sort=False):
        part = part.sort_values("quality_score", ascending=False, kind="stable")
        if max_foreign:
            fo = part.apply(_foreign, axis=1)
            part = pd.concat([part[~fo], part[fo].head(max_foreign)]).sort_values(
                "quality_score", ascending=False, kind="stable")
        chosen, per = [], {}
        for i, r in part.iterrows():                    # first pass: respect the per-seller cap
            s = (r.get("sellers") or r.get("seller_name") or r.get("source_domain") or "").split(" | ")[0]
            if per.get(s, 0) < max_per_seller:
                chosen.append(i)
                per[s] = per.get(s, 0) + 1
            if len(chosen) >= n:
                break
        if len(chosen) < n:                             # not enough sellers: fill with the best of the rest
            chosen += [i for i in part.index if i not in chosen][: n - len(chosen)]
        keep_idx += chosen
    kept = df.loc[keep_idx]
    return kept, df.drop(index=keep_idx)
