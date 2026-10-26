"""Price detective: a doubtful price (held back by price_sanity) settled from evidence, not by a person.

For each product whose price looks wrong:
  1. its own page read again, fresh (a sale price, rial for toman, a part's price read as the set's)
  2. the same model at other Iranian shops: a web search for brand + model, the result pages read
     and kept only when they are the same model (a shared model code, or nearly the same name)
     and the same kind of thing (same first word)
  3. the crawl pool and this run's crawl (other shops already read)
Then the evidence decides:
  - 2+ other shops agree with our price (within tolerance)        -> our price, confirmed
  - our price is a 10x slip of what they ask                       -> corrected (/10 or x10)
  - the fresh read of our page agrees with them                    -> the fresh price
  - 2+ other shops, our price far from them                        -> their median
  - 1 other shop agreeing (with our price or its 10x slip)         -> as above, weaker
  - nothing                                                        -> left for a person
A settled price is saved as a review decision (the export trusts it); the evidence is in
05_price_detective.csv.
"""
from __future__ import annotations

import logging
import re
from statistics import median

import pandas as pd

from .io import read_csv, write_csv
from .normalize import norm_text

log = logging.getLogger(__name__)
COLS = ["url", "name", "old_price", "new_price", "decision", "shops", "evidence", "fresh_price"]
SKIP_DOMAINS = ("caravansanat.com", "torob.com", "emalls.ir", "made-in-china.com", "alibaba.com", "aparat.com",
                "instagram.com", "youtube.com", "wikipedia.org")


def _num(x) -> float | None:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _same_model(ours: dict, name: str) -> bool:
    from .names import clean_name
    from .pipeline import _codes
    from .rules import code_in
    a, b = norm_text(clean_name(str(ours.get("name") or ""))), norm_text(clean_name(name))
    if not a or not b or a.split()[0] != b.split()[0]:
        return False
    import re as _re
    variant = r"بدون باتری|بدون باطری|تک باتری|دو باتری|بدون شارژر|کیت|ست |پک |مجموعه|بدنه|\bbare\b|\bkit\b"
    if bool(_re.search(variant, a)) != bool(_re.search(variant, b)):   # a kit is not the bare tool
        return False
    ca, cb = _codes(a), _codes(b)
    if ca:
        return any(code_in(x, y) or code_in(y, x) for x in ca for y in cb if min(len(x), len(y)) >= 3)
    wa = {w for w in a.split() if len(w) > 2}
    wb = set(b.split())
    return bool(wa) and len(wa & wb) / len(wa) >= 0.8


def _search_prices(cfg: dict, f, rec: dict, rate: dict, max_pages: int = 6) -> list[tuple[str, float]]:
    from .config import secret
    from .extract import extract_page
    from .fetch import domain_of
    from .pipeline import _codes, price_fields
    key = secret("SERPER_API_KEY")
    if not key:
        return []
    from .names import clean_name
    name = clean_name(str(rec.get("name") or ""))           # 'قیمت و خرید … - ابزار بیات' -> the product
    codes = sorted(_codes(name), key=len, reverse=True)
    brand = str(rec.get("brand") or "").strip()
    if codes:                                               # the model is the best key: type + brand + code
        head = " ".join(w for w in name.split()[:2] if not any(c in w.lower() for c in codes))
        q = f"{head} {brand if brand and brand not in name else ''} {codes[0]} قیمت"
    else:
        q = f"{name} قیمت"
    q = re.sub(r"\s+", " ", q).strip()
    res = f.post_json("https://google.serper.dev/search", {"q": q, "gl": "ir", "hl": "fa", "num": 10},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    out, seen = [], set()
    own = domain_of(rec.get("url", ""))
    for o in ((res or {}).get("json") or {}).get("organic") or []:
        u = o.get("link") or ""
        dom = domain_of(u)
        if not u or dom in seen or dom == own or any(s in dom for s in SKIP_DOMAINS):
            continue
        seen.add(dom)
        if len(seen) > max_pages:
            break
        page = f.get(u)
        if not page or page.get("status") != 200:
            continue
        try:
            got = extract_page(page.get("text", ""), u)
        except Exception:                                    # noqa: BLE001
            got = None
        if not got or not got.get("name") or not got.get("price_amount") or not _same_model(rec, got["name"]):
            continue
        v = _num(price_fields(got, rate, cfg).get("price_toman"))
        if v:
            out.append((dom, v))
    return out


def _fresh_price(cfg: dict, f, rec: dict, rate: dict) -> float | None:
    from .extract import extract_page
    from .pipeline import price_fields
    page = f.get(rec["url"], use_cache=False)
    if not page or page.get("status") != 200:
        return None
    try:
        got = extract_page(page.get("text", ""), rec["url"])
    except Exception:                                        # noqa: BLE001
        return None
    return _num(price_fields(got, rate, cfg).get("price_toman")) if got and got.get("price_amount") else None


def decide(v: float, others: list[tuple[str, float]], fresh: float | None, tol: float = 0.3) -> tuple[float | None, str]:
    """Each way our price could really read — as it is, a 10x or 100x slip, or what the page shows
    today — is checked against the other shops for the same model; the reading most shops back
    wins. A price is only invented (their median) when 3+ shops agree among themselves and none
    of our readings fits."""
    by_shop: dict[str, list[float]] = {}
    for dom, p in others:
        by_shop.setdefault(dom, []).append(p)
    if not by_shop:
        return None, "منبع دیگری پیدا نشد" + (f" (قیمت تازه‌ی صفحه {fresh:,.0f})" if fresh and fresh != v else "")
    near = lambda a, b: abs(a / b - 1) <= tol                 # noqa: E731
    readings = [(v, "قیمت ما"), (v / 10, "قیمت ما ÷۱۰"), (v * 10, "قیمت ما ×۱۰"), (v / 100, "قیمت ما ÷۱۰۰")]
    if fresh and fresh != v:
        readings.insert(1, (fresh, "قیمت تازه‌ی صفحه"))
    support = []
    for cand, how in readings:
        shops = [dom for dom, ps in by_shop.items() if any(near(cand, p) for p in ps)]
        support.append((len(shops), cand, how, shops))
    best = max(support, key=lambda x: x[0])
    rivals = [x for x in support if x[0] and x is not best and x[0] >= best[0]]
    n = len(by_shop)
    # enough: two shops back the reading, or the only one or two shops found both back it alone
    if best[0] and not rivals and (best[0] >= 2 or n <= 2):
        return round(best[1]), f"{best[2]} با {best[0]} فروشگاه از {n} می‌خواند ({', '.join(best[3][:3])})"
    vals = sorted(median(ps) for ps in by_shop.values())
    if n >= 3 and vals[-1] / vals[0] <= 2.5:                  # several shops agree, our price fits none
        return round(median(vals)), f"میانه‌ی {n} فروشگاه ({vals[0]:,.0f} تا {vals[-1]:,.0f})؛ قیمت ما {v:,.0f} نمی‌خواند"
    hint = f"؛ پیشنهاد: {best[1]:,.0f} ({best[2]})" if best[0] else ""
    return None, f"قیمت ما ({v:,.0f}) با {n} فروشگاه ({vals[0]:,.0f} تا {vals[-1]:,.0f}) روشن نشد{hint}؛ برای بررسی"


def run_detective(cfg: dict, run: str, apply: bool = True, progress=None, redo: bool = False) -> dict:
    from . import fx as fxmod
    from .config import run_dir
    from .pipeline import _fetchers, export_rows, price_peers
    from .review import save_decision
    d = run_dir(cfg, run)
    if redo:                                                 # decide again under the current rules
        from .review import DEC_COLS, _dec_path, decisions
        dec = {u: x for u, x in decisions(d).items() if not str(x.get("note") or "").startswith("کارآگاه")}
        write_csv(pd.DataFrame(list(dec.values()), columns=DEC_COLS), _dec_path(d))
    recs = [rec for rec, _ in export_rows(cfg, run) if str(rec.get("price_check") or "").startswith("suspicious")]
    log.info("price detective %s: %d doubtful price(s)", run, len(recs))
    rate = fxmod.today_rate(cfg)
    f = _fetchers(cfg)["iran"]
    pool = price_peers(cfg, d)
    from .fetch import domain_of
    from .pricemem import habit, learn_habit, recall, remember
    budget = int(cfg.get("price", {}).get("detective_searches", 2)) * max(1, len(recs))
    budget = min(budget, int(cfg.get("price", {}).get("detective_max_searches", 150)))
    rows, settled, searched, from_memory, with_search = [], 0, 0, 0, 0
    for i, rec in enumerate(recs):
        if progress:
            progress(i, len(recs))
        v = _num(rec.get("price_toman"))
        if not v:
            continue
        dom_own = domain_of(rec["url"])
        # 1. free evidence first: shops already crawled, and what earlier checks remembered
        others = [(p["dom"] or p["url"], p["price"]) for p in pool
                  if p["url"] != rec["url"] and p["dom"] != dom_own and _same_model(rec, p["name"])]
        mem = [(dm, pr) for dm, pr, nm in recall(cfg, rec.get("name", "")) if dm != dom_own and _same_model(rec, nm)]
        others += [x for x in mem if x[0] not in {o[0] for o in others}]
        h = habit(cfg, dom_own)
        how = "free"
        # 2. Torob's offers for the same model (free, no search credit)
        if len({o[0] for o in others}) < 2 and cfg.get("price", {}).get("detective_torob", True):
            try:
                from .aggregators import torob_offers
                tb = torob_offers(f, rec, _same_model)
                others += tb
                remember(cfg, [{"name": rec.get("name", ""), "dom": dm, "price": pr, "source": "torob"} for dm, pr in tb])
            except Exception as e:                            # noqa: BLE001
                log.warning("torob lookup failed for %s: %s", rec["url"][-50:], e)
        # 3. a search only when the free evidence is still thin (fewer than 2 shops), within the budget
        if len({o[0] for o in others}) < 2 and searched < budget:
            try:
                found = _search_prices(cfg, f, rec, rate)
                searched += 1
                if len({o[0] for o in others + found}) < 2:      # Emalls' price line, one more search
                    from .aggregators import emalls_range
                    found += emalls_range(cfg, f, rec)
                    searched += 1
                others += found
                remember(cfg, [{"name": rec.get("name", ""), "dom": dm, "price": pr, "source": "search"}
                               for dm, pr in found])
                how = "search"
                with_search += 1
            except Exception as e:                            # noqa: BLE001
                log.warning("detective search failed for %s: %s", rec["url"][-50:], e)
        elif mem:
            from_memory += 1
        fresh = _fresh_price(cfg, f, rec, rate) if len({o[0] for o in others}) < 2 else None
        new, why = decide(v, others, fresh)
        if not new and h:                                    # the shop's known slip, nothing against it
            new, why = round(v / 10 if h == "d10" else v * 10), f"عادت این فروشگاه: قیمت‌هایش {'۱۰ برابر' if h == 'd10' else 'یک‌دهم'} نوشته می‌شود"
        if new and len({o[0] for o in others}) >= 2:         # what this teaches (only from 2+ shops)
            ratio = v / new
            learn_habit(cfg, dom_own, "ok" if abs(ratio - 1) <= 0.25 else "d10" if abs(ratio / 10 - 1) <= 0.25
                        else "x10" if abs(ratio * 10 - 1) <= 0.25 else "other")
            remember(cfg, [{"name": rec.get("name", ""), "dom": dom_own, "price": new, "source": "settled"}])
        rows.append({"url": rec["url"], "name": rec.get("name", ""), "old_price": round(v),
                     "new_price": new or "", "decision": why + ("" if how == "search" else " (بدون جستجو)"),
                     "shops": len({o[0] for o in others}),
                     "evidence": " | ".join(f"{dom}: {p:,.0f}" for dom, p in others[:8]),
                     "fresh_price": round(fresh) if fresh else ""})
        if new and apply:
            from .review import decisions
            old = decisions(d).get(rec["url"], {})               # a photo choice made earlier is kept
            save_decision(cfg, run, {"url": rec["url"], "action": "ok", "images": old.get("images", ""),
                                     "price": str(new), "note": "کارآگاه قیمت: " + why})
            settled += 1
        log.info("detective %s: %s", rec.get("name", "")[:40], why)
    write_csv(pd.DataFrame(rows, columns=COLS), d / "05_price_detective.csv")
    res = {"doubtful": len(recs), "settled": settled, "left": len(recs) - settled, "searches": searched,
           "decided_without_search": len(rows) - with_search}
    log.info("price detective %s: %s", run, res)
    return res
