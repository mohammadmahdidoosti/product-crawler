"""What the price checks learn, so the next doubtful price costs no search.

price_memory.csv   every shop price seen for a model (model code + kind of thing), with the date:
                   the detective's search results, the prices it settled. A later doubtful price
                   for the same model is decided from memory first (free); a search is made only
                   when memory and the crawl give fewer than two shops.
                   'checked' rows are prices a person confirmed (market checked by hand, e.g. on Torob
                   through the person's browser): trusted first by the next check of the same model.
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
        if k and it.get("price") and it.get("source") != "settled":   # only prices shops really ask
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


def recall(cfg: dict, name: str, days: int = 45) -> list[tuple[str, float, str]]:
    k = model_key(name)
    p = _p(cfg, "price_memory.csv")
    if not k or not p.exists():
        return []
    m = read_csv(p)
    cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    m = m[(m["key"] == k) & (m["date"] >= cut)]
    return [(r["dom"], float(r["price"]), r["name"]) for r in m.to_dict("records") if r["price"]]


def checked(cfg: dict, name: str, days: int = 120) -> float | None:
    """The newest price a person confirmed for this model, if any."""
    k = model_key(name)
    p = _p(cfg, "price_memory.csv")
    if not k or not p.exists():
        return None
    m = read_csv(p)
    cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    m = m[(m["key"] == k) & (m["source"] == "checked") & (m["date"] >= cut) & (m["price"] != "")]
    return float(m.sort_values("date").iloc[-1]["price"]) if len(m) else None


def verdict(old: float, new: float, tol: float = 0.25) -> str:
    """What our price turned out to be next to the right one: ok | d10 (10x too high) | x10 | other."""
    if not old or not new:
        return ""
    r = old / new
    return ("ok" if abs(r - 1) <= tol else "d10" if abs(r / 10 - 1) <= tol
            else "x10" if abs(r * 10 - 1) <= tol else "other")


def import_checked(cfg: dict, rows: list[dict]) -> dict:
    """rows: {name, url, old, new}: prices a person confirmed. Each is remembered as 'checked' for
    its model (the next doubtful price of that model is settled from it, no search), and teaches
    the habit of the shop it came from (rial written as toman and the like)."""
    from .fetch import domain_of
    mem, habits = [], 0
    for r in rows:
        try:
            new = float(str(r.get("new") or "").replace(",", ""))
        except ValueError:
            continue
        if new <= 0:
            continue
        mem.append({"name": str(r.get("name") or ""), "dom": "checked", "price": new, "source": "checked"})
        try:
            old = float(str(r.get("old") or "").replace(",", ""))
        except ValueError:
            old = 0
        v = verdict(old, new)
        if v and r.get("url"):
            learn_habit(cfg, domain_of(str(r["url"])), v)
            habits += 1
    remember(cfg, mem)
    return {"remembered": sum(bool(model_key(x["name"])) for x in mem), "habits": habits, "rows": len(rows)}


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


def forget_learning(cfg: dict) -> None:
    """Start the shop habits and the settled prices again (after the decision rules changed)."""
    for n in ("shop_habits.csv",):
        p = _p(cfg, n)
        if p.exists():
            p.unlink()
    p = _p(cfg, "price_memory.csv")
    if p.exists():
        m = read_csv(p)
        write_csv(m[m["source"] != "settled"], p)
