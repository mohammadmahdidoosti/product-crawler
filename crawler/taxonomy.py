"""Step 1: turn the taxonomy file into a crawl queue.

The deepest category title (the leaf) is the search clue, as the tech team
proposed. Very short leaves ("ممبران") get their parent title added so the
query is not ambiguous. The route (iran / foreign / both) comes from a
`Route` column in the taxonomy file if present, otherwise from config rules
that are inherited down the tree.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .io import QUEUE_COLS, read_csv
from .normalize import norm_text

ROUTES = {"iran", "foreign", "both"}


def _col(df: pd.DataFrame, *prefixes: str) -> str | None:
    for p in prefixes:
        for c in df.columns:
            if c.strip().lower().startswith(p.lower()):
                return c
    return None


def load_taxonomy(path: Path) -> pd.DataFrame:
    df = read_csv(path)
    cols = {
        "id": _col(df, "ID"),
        "l1": _col(df, "Level 1"), "l2": _col(df, "Level 2"),
        "l3": _col(df, "Level 3"), "l4": _col(df, "Level 4"),
        "path": _col(df, "Full Breadcrumb"),
        "parent": _col(df, "Parent_ID"),
        "inferred": _col(df, "Is_Inferred"),
        "count": _col(df, "Site_Product_Count"),
        "prio": _col(df, "Review_Priority"),
        "route": _col(df, "Route", "مسیر"),
    }
    missing = [k for k in ("id", "l1", "path") if not cols[k]]
    if missing:
        raise ValueError(f"taxonomy file is missing columns: {missing}")
    out = pd.DataFrame({
        "taxonomy_id": df[cols["id"]].str.strip(),
        "level1": df[cols["l1"]].str.strip(),
        "levels": df[[c for c in (cols["l1"], cols["l2"], cols["l3"], cols["l4"]) if c]]
        .apply(lambda r: [x.strip() for x in r if str(x).strip()], axis=1),
        "breadcrumb": df[cols["path"]].str.strip(),
        "parent_id": df[cols["parent"]].str.replace(r"\.0$", "", regex=True).str.strip() if cols["parent"] else "",
        "inferred": df[cols["inferred"]].str.lower().eq("true") if cols["inferred"] else False,
        "site_product_count": pd.to_numeric(df[cols["count"]], errors="coerce").fillna(0).astype(int)
        if cols["count"] else 0,
        "review_priority": df[cols["prio"]].str.strip() if cols["prio"] else "",
        "route_col": df[cols["route"]].str.strip().str.lower() if cols["route"] else "",
    })
    parents = set(out["parent_id"]) - {""}
    out["is_leaf"] = ~out["taxonomy_id"].isin(parents)
    out["leaf"] = out["levels"].apply(lambda l: l[-1] if l else "")
    return out


def build_query(levels: list[str]) -> str:
    leaf = levels[-1]
    if len(norm_text(leaf).split()) <= 1 and len(levels) > 1:
        return f"{leaf} {levels[-2]}"
    return leaf


def resolve_route(row: pd.Series, rules: dict) -> str:
    if row["route_col"] in ROUTES:
        return row["route_col"]
    by_id = {str(k): v for k, v in (rules.get("by_id") or {}).items()}
    if row["taxonomy_id"] in by_id:
        return by_id[row["taxonomy_id"]]
    # longest matching breadcrumb prefix wins, so a rule on a level-2 node
    # overrides a rule on its level-1 parent
    best, best_len = None, -1
    path = norm_text(row["breadcrumb"])
    for prefix, route in (rules.get("by_path_prefix") or {}).items():
        p = norm_text(prefix)
        if path.startswith(p) and len(p) > best_len:
            best, best_len = route, len(p)
    return best or rules.get("default", "iran")


def build_queue(tax: pd.DataFrame, cfg: dict, *, mode: str = "gap", level1: str | None = None,
                path_contains: str | None = None, ids: list[str] | None = None,
                include_flagged: bool = False, limit: int | None = None) -> pd.DataFrame:
    q = tax[tax["is_leaf"] & ~tax["inferred"]].copy()
    excl = [norm_text(x) for x in (cfg.get("taxonomy", {}).get("exclude_level1") or [])]
    q = q[~q["level1"].map(norm_text).isin(excl)]
    if not include_flagged:
        q = q[q["review_priority"] != "بالا"]
    if mode == "gap":
        q = q[q["site_product_count"] == 0]
    elif mode == "enrich":
        q = q[q["site_product_count"] > 0]
    if level1:
        q = q[q["level1"].map(norm_text).str.contains(norm_text(level1), regex=False)]
    if path_contains:
        q = q[q["breadcrumb"].map(norm_text).str.contains(norm_text(path_contains), regex=False)]
    if ids:
        q = q[q["taxonomy_id"].isin([str(i) for i in ids])]

    rules = cfg.get("routes") or {}
    q["route"] = q.apply(lambda r: resolve_route(r, rules), axis=1)
    q["query"] = q["levels"].apply(build_query)
    # gap mode: leaves with no product first; enrich mode: biggest first
    q["priority"] = range(len(q))
    if mode == "enrich":
        q = q.sort_values("site_product_count", ascending=False)
    q = q.drop_duplicates("query")
    if limit:
        q = q.head(limit)
    return q[QUEUE_COLS].reset_index(drop=True)
