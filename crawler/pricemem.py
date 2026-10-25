"""What the price checks learn, so the next doubtful price costs no search.

price_memory.csv   every shop price seen for a model (model code + kind of thing), with the date:
                   the detective's search results, the prices it settled. A later doubtful price
                   for the same model is decided from memory first (free); a search is made only
                   when memory and the crawl give fewer than two shops.
shop_habits.csv    per shop: how often its prices turned out right, x10 or /10 (rial shown as
                   toman, or the reverse). A shop with a clear habit has its slip fixed without
                   asking anyone.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pandas as pd

from .io import read_csv, write_csv
from .normalize import norm_text

MEM_COLS = ["key", "name", "dom", "price", "date", "source"]
HAB_COLS = ["dom", "ok", "x10", "d10", "other"]
_LOCK = threading.Lock()


def model_key(name: str) -> str:
    """'اره عمود بر رونیکس مدل 4120' -> 'اره|4120'; '' when the name has no model code."""
    from .pipeline import _codes
    codes = sorted(_codes(name), key=len, reverse=True)
    head = (norm_text(name).split() or [""])[0]
    return f"{head}|{codes[0]}" if codes and head else ""


def _p(cfg: dict, name: str) -> Path:
    return cfg["_data_dir"] / name


def remember(cfg: dict, items: list[dict]) -> None:
    """items: {name, dom, price, source}"""
    rows = []
    today = dt.date.today().isoformat()
    for it in items:
        k = model_key(it.get("name", ""))
        if k and it.get("price"):
            rows.append({"key": k, "name": it["name"][:120], "dom": it.get("dom", ""),
                         "price": str(round(float(it["price"]))), "date": today, "source": it.get("source", "")})
    if not rows:
        return
    with _LOCK:
        p = _p(cfg, "price_memory.csv")
        old = read_csv(p) if p.exists() else pd.DataFrame(columns=MEM_COLS)
        allr = pd.concat([old, pd.DataFrame(rows, columns=MEM_COLS)], ignore_index=True)
        allr = allr.drop_duplicates(["key", "dom"], keep="last")          # one price per shop and model, newest
        write_csv(allr, p)


def recall(cfg: dict, name: str, days: int = 45) -> list[tuple[str, float]]:
    k = model_key(name)
    p = _p(cfg, "price_memory.csv")
    if not k or not p.exists():
        return []
    m = read_csv(p)
    cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    m = m[(m["key"] == k) & (m["date"] >= cut)]
    return [(r["dom"], float(r["price"])) for r in m.to_dict("records") if r["price"]]


def learn_habit(cfg: dict, dom: str, verdict: str) -> None:
    """verdict: ok | x10 | d10 | other (what our price from this shop turned out to be)."""
    if not dom or verdict not in HAB_COLS[1:]:
        return
    with _LOCK:
        p = _p(cfg, "shop_habits.csv")
        h = read_csv(p) if p.exists() else pd.DataFrame(columns=HAB_COLS)
        if dom not in set(h["dom"]):
            h = pd.concat([h, pd.DataFrame([{"dom": dom, "ok": "0", "x10": "0", "d10": "0", "other": "0"}])],
                          ignore_index=True)
        i = h.index[h["dom"] == dom][0]
        h.at[i, verdict] = str(int(h.at[i, verdict] or 0) + 1)
        write_csv(h, p)


def habit(cfg: dict, dom: str) -> str:
    """'d10' / 'x10' when the shop's prices were that slip at least 3 times and more often than right."""
    p = _p(cfg, "shop_habits.csv")
    if not dom or not p.exists():
        return ""
    h = read_csv(p)
    r = h[h["dom"] == dom]
    if r.empty:
        return ""
    r = r.iloc[0]
    ok, x10, d10 = int(r["ok"] or 0), int(r["x10"] or 0), int(r["d10"] or 0)
    for k, n in (("d10", d10), ("x10", x10)):
        if n >= 3 and n > ok:
            return k
    return ""
