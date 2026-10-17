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


def select_top(df: pd.DataFrame, n: int, max_per_seller: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (kept, dropped); both carry quality_score and quality_why."""
    if df.empty:
        return df, df
    df = df.copy()
    sc = [score(r) for r in df.to_dict("records")]
    df["quality_score"] = [s for s, _ in sc]
    df["quality_why"] = [w for _, w in sc]
    if not n:
        return df.sort_values("quality_score", ascending=False), df.iloc[0:0]
    keep_idx = []
    for _, part in df.groupby("taxonomy_id", sort=False):
        part = part.sort_values("quality_score", ascending=False, kind="stable")
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
