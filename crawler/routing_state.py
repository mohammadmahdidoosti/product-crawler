"""The routing table: every leaf, its decision, the reason, and your review.

Work goes in batches of 100 leaves (siblings kept together). For a batch:
  probe   -> one search per group still open, decisions filled in
  review  -> you mark the wrong rows and pick the right route; the rest count
             as correct; corrections are kept and reused
  queue   -> the approved rows become a crawl run "batch-N"
The table lives in data/routing.csv.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pandas as pd

from .config import ROOT
from .io import FILES, QUEUE_COLS, read_csv, write_csv
from .normalize import norm_text
from .probe import decide, group_query, load_probes, record_usage, run_probe, save_probes
from .routing import rule_route
from .terms import leaf_english
from .taxonomy import build_queue, load_taxonomy

log = logging.getLogger(__name__)
COLS = ["batch", "taxonomy_id", "breadcrumb", "query", "group", "kind", "brand", "brand_origin",
        "decision", "reason", "shops", "priced", "english", "review", "final_route", "note", "queued"]
ROUTES = {"iran", "foreign", "both", "skip"}


def table_file(cfg) -> Path:
    return cfg["_data_dir"] / "routing.csv"


def group_of(breadcrumb: str) -> str:
    p = [x.strip() for x in breadcrumb.split(">")]
    if len(p) > 3:
        return " > ".join(p[:3])
    return " > ".join(p[:-1]) if len(p) > 1 else p[0]


def load_glossary() -> dict[str, str]:
    p = ROOT / "glossary" / "groups_en.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig") as f:
        return {norm_text(r["group_path"]): r["english"] for r in csv.DictReader(f) if r.get("english")}


def english_for(group: str, gl: dict[str, str]) -> str:
    g = norm_text(group)
    if g in gl:
        return gl[g]
    parts = [p.strip() for p in group.split(">")]           # fall back to the parent's term
    while len(parts) > 1:
        parts = parts[:-1]
        k = norm_text(" > ".join(parts))
        if k in gl:
            return gl[k]
    return ""


def load(cfg) -> pd.DataFrame:
    p = table_file(cfg)
    return read_csv(p) if p.exists() else pd.DataFrame(columns=COLS)


def save(cfg, df: pd.DataFrame) -> None:
    write_csv(df, table_file(cfg), COLS)


def build(cfg, *, mode="gap", level1=None, path_contains=None, include_flagged=False, batch_size=100) -> dict:
    """(Re)build the table for a scope. Reviews of leaves already in the table are kept."""
    tax = load_taxonomy(Path(cfg["taxonomy"]["file"]))
    q = build_queue(tax, cfg, mode=mode, level1=level1, path_contains=path_contains,
                    include_flagged=include_flagged)
    old = load(cfg).set_index("taxonomy_id") if table_file(cfg).exists() else None
    gl = load_glossary()
    rows = []
    for r in q.to_dict("records"):
        rr = rule_route(r["breadcrumb"])
        g = group_of(r["breadcrumb"])
        row = {"taxonomy_id": r["taxonomy_id"], "breadcrumb": r["breadcrumb"], "query": r["query"], "group": g,
               "kind": rr["kind"], "brand": rr["brand"], "brand_origin": rr["brand_origin"],
               "decision": rr["decision"], "reason": rr["reason"], "shops": "", "priced": "",
               "english": leaf_english(r["breadcrumb"].split(">")[-1].strip(), g.split(">")[-1].strip(),
                                          english_for(g, gl)), "review": "", "final_route": "", "note": "", "queued": ""}
        if old is not None and r["taxonomy_id"] in old.index:
            o = old.loc[r["taxonomy_id"]]
            for k in ("review", "final_route", "note", "queued"):
                row[k] = o.get(k, "")
            if o.get("decision") not in ("", "probe") and rr["decision"] == "probe":
                for k in ("decision", "reason", "shops", "priced"):
                    row[k] = o.get(k, "")
        rows.append(row)
    df = pd.DataFrame(rows, columns=[c for c in COLS if c != "batch"])
    df = df.sort_values(["group", "taxonomy_id"], kind="stable").reset_index(drop=True)
    df.insert(0, "batch", (df.index // batch_size + 1).astype(str))
    save(cfg, df)
    return summary(cfg)


def summary(cfg) -> dict:
    df = load(cfg)
    if df.empty:
        return {"rows": 0, "batches": []}
    out = []
    for b, part in df.groupby(df["batch"].astype(int)):
        out.append({"batch": int(b), "rows": len(part),
                    "open": int((part["decision"] == "probe").sum()),
                    "reviewed": bool((part["review"] != "").all()),
                    "wrong": int((part["review"] == "wrong").sum()),
                    "queued": bool((part["queued"] == "yes").any())})
    return {"rows": len(df), "groups": int(df["group"].nunique()), "batches": out,
            "by_decision": df["decision"].value_counts().to_dict()}


def batch_rows(cfg, n: int) -> pd.DataFrame:
    df = load(cfg)
    return df[df["batch"] == str(n)]


def probe_batch(cfg, n: int, progress=None, stop=None) -> dict:
    """Search once per still-open group of batch n and fill in the decisions."""
    df = load(cfg)
    mask = df["batch"] == str(n)
    probes = load_probes(cfg)
    want = [g for g in df.loc[mask & (df["decision"] == "probe"), "group"].unique()]
    if cfg.get("routing", {}).get("probe_rule_groups", True):      # also discover shops for branded groups
        want += [g for g in df.loc[mask & (df["decision"] != "probe"), "group"].unique() if g not in want]
    jobs = [(g, None) for g in want if g not in probes]
    if cfg.get("routing", {}).get("brand_probe", True):     # one search per brand inside a group
        from .probe import brand_key, brand_query
        for g, b in df.loc[mask & (df["kind"] == "brand"), ["group", "brand"]].drop_duplicates().itertuples(index=False):
            k = brand_key(g, b)
            q = brand_query(g, b)
            if b and k not in probes and q != group_query(g) and all(k != j[0] for j in jobs):
                jobs.append((k, q))
    todo = [j[0] for j in jobs]
    queries = dict(jobs)
    done = []
    for i, g in enumerate(todo, 1):
        if stop and stop():
            break
        if progress:
            progress(i - 1, len(todo))
        try:
            res = run_probe(cfg, g, query=queries.get(g))
        except Exception as e:
            log.warning("probe failed for %s: %s", g, e)
            if "SERPER_API_KEY" in str(e) or "did not answer" in str(e):
                raise
            continue
        probes[g] = res
        done.append(res)
        used = record_usage(cfg)
        log.info("probe %d/%d: %s -> %s shops, %s priced (searches used: %d)",
                 i, len(todo), g.split(" > ")[-1], res["shops"], res["priced"], used)
    if done:
        save_probes(cfg, done)
    for idx in df.index[mask]:
        g = df.at[idx, "group"]
        if g in probes:
            df.at[idx, "shops"], df.at[idx, "priced"] = str(probes[g]["shops"]), str(probes[g]["priced"])
            if df.at[idx, "decision"] == "probe":
                d, why = decide(probes[g], cfg)
                df.at[idx, "decision"], df.at[idx, "reason"] = d, why
    save(cfg, df)
    return {"searched": len(done), "groups": len(want)}


def review_batch(cfg, n: int, rows: list[dict]) -> dict:
    """rows: [{taxonomy_id, review: ok|wrong, final_route, note}]; unlisted rows count as ok."""
    df = load(cfg)
    mask = df["batch"] == str(n)
    by_id = {str(r["taxonomy_id"]): r for r in rows}
    wrong = []
    for idx in df.index[mask]:
        r = by_id.get(df.at[idx, "taxonomy_id"])
        if r and r.get("review") == "wrong" and r.get("final_route") in ROUTES:
            df.at[idx, "review"], df.at[idx, "final_route"] = "wrong", r["final_route"]
            df.at[idx, "note"] = (r.get("note") or "")[:300]
            wrong.append({k: df.at[idx, k] for k in ("taxonomy_id", "breadcrumb", "decision", "reason",
                                                      "final_route", "note", "shops", "priced")})
        else:
            df.at[idx, "review"] = "ok"
            df.at[idx, "final_route"] = df.at[idx, "decision"] if df.at[idx, "decision"] in ROUTES else "iran"
    save(cfg, df)
    total = int(mask.sum())
    # one line in the server log per review: lets the developer read errors from the Render logs
    log.info("ROUTING_REVIEW %s", json.dumps({"batch": n, "rows": total, "wrong": wrong}, ensure_ascii=False))
    return {"rows": total, "wrong": len(wrong), "error_rate": round(len(wrong) / total, 3) if total else 0}


def make_queue(cfg, n: int, run: str) -> dict:
    """Approved rows of batch n -> data/runs/<run>/01_queue.csv (route = final_route)."""
    from .config import run_dir
    df = load(cfg)
    mask = (df["batch"] == str(n)) & (df["review"] != "")
    part = df[mask & (df["final_route"] != "skip")]
    if part.empty:
        raise ValueError("این دسته هنوز بررسی نشده یا همه‌ی ردیف‌ها رد شده‌اند")
    q = pd.DataFrame({
        "taxonomy_id": part["taxonomy_id"], "query": part["query"], "breadcrumb": part["breadcrumb"],
        "leaf": part["breadcrumb"].str.split(">").str[-1].str.strip(),
        "level1": part["breadcrumb"].str.split(">").str[0].str.strip(),
        "route": part["final_route"], "site_product_count": "0", "priority": range(len(part)),
        "group": part["group"], "english": part["english"],
    })
    out = run_dir(cfg, run) / FILES["queue"]
    write_csv(q, out, QUEUE_COLS)
    df.loc[mask, "queued"] = "yes"
    save(cfg, df)
    log.info("queue for %s from batch %d: %d rows (%s)", run, n, len(q), q["route"].value_counts().to_dict())
    return {"run": run, "rows": len(q)}
