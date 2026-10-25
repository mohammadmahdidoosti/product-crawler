"""Products with no category on the site yet: good pages (Persian name, price, photo) that the
filter, the second chance, the category above and the rest of the tree could not place
('کمان اره' — the site has no such category). Nothing is thrown away: each run writes its own
list (05_no_category.csv) and the shared list DATA_DIR/no_category.csv collects every run, with a
suggested group (the name's first two words) and how many products each group has. When a group
is worth a category, the category is made once (site + taxonomy + map) and those products move
there. A product already on the site that lands here goes back to draft (resync), never deleted.
"""
from __future__ import annotations

import logging
import re

import pandas as pd

from .io import read_csv, write_csv
from .normalize import norm_text

log = logging.getLogger(__name__)
COLS = ["group", "group_size", "name", "price_toman", "brand", "on_site", "found_under", "run", "url"]
LEAD = {norm_text(w) for w in ("خرید", "قیمت", "و", "مشخصات", "ست", "پک", "مجموعه", "کیت")}


def group_of(name: str) -> str:
    ws = [w for w in norm_text(name).split() if w not in LEAD and not re.search(r"\d", w)]
    return " ".join(ws[:2])


def write(cfg: dict, d, run: str, df: pd.DataFrame, crawled: pd.DataFrame, on_site: set[str]) -> int:
    from .harvest import FA
    placed = set(df["url"]) if len(df) else set()
    for us in (df.get("urls") if "urls" in df else []):
        placed |= {u.strip() for u in str(us).split("|") if u.strip()}
    pages = crawled[(crawled["price_toman"] != "") & (crawled["image_urls"] != "")
                    & crawled["name"].map(lambda n: bool(FA.search(str(n))))
                    & (crawled.get("source_type", "") != "foreign") & ~crawled["url"].isin(placed)]
    rows = []
    for url, part in pages.groupby("url", sort=False):
        r = part.iloc[0]
        rows.append({"group": group_of(r["name"]), "name": r["name"], "price_toman": r["price_toman"],
                     "brand": r.get("brand", ""), "on_site": "yes" if url in on_site else "",
                     "found_under": " | ".join(dict.fromkeys(part["breadcrumb"].str.split(">").str[-1].str.strip()))[:200],
                     "run": run, "url": url})
    mine = pd.DataFrame(rows, columns=COLS)
    shared_p = cfg["_data_dir"] / "no_category.csv"
    old = read_csv(shared_p) if shared_p.exists() else pd.DataFrame(columns=COLS)
    allr = pd.concat([old[old["run"] != run], mine], ignore_index=True).drop_duplicates("url", keep="last")
    for x in (mine, allr):
        if len(x):
            x["group_size"] = x.groupby("group")["url"].transform("count")
    allr = allr.sort_values(["group_size", "group"], ascending=[False, True]) if len(allr) else allr
    write_csv(mine.reindex(columns=COLS), d / "05_no_category.csv")
    write_csv(allr.reindex(columns=COLS), shared_p)
    log.info("no category yet: %d page(s) in this run (%d on the site, back to draft), %d in the shared list",
             len(mine), int((mine["on_site"] == "yes").sum()) if len(mine) else 0, len(allr))
    return len(mine)
