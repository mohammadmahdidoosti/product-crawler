"""(18 Nov, owner) The keyword-discovery list (the «نداریم» keywords and their new terms with demand) -> crawler
taxonomy (no site change: a site category is made only when a product is sent there) + routing rows, in the
batches planned in the sheet (100 keywords each, a subgroup's keywords together). Same steps as
seed_tax_import.py; the list itself stays on the server (/data/tmp), not in the repo.
In:  a CSV with columns batch (1, 2, ... in the sheet), path ('group > subgroup > keyword'), query, note
python3 tools/kw_tax_import.py FILE          -> dry run (counts only)
python3 tools/kw_tax_import.py FILE go       -> write taxonomy.csv and routing.csv (backups first)
base=N (optional): the routing batch number that the list's batch 0 maps to. The first import prints it; give the
same base to a later import of more of the list so its batches line up (rows already routed are skipped)."""
import sys, os, shutil, time
sys.path.insert(0, "/app")
os.environ.setdefault("DATA_DIR", "/data")
from pathlib import Path
import pandas as pd
from crawler.config import load_config
from crawler import catmake as CM
from crawler import routing_state as RS
from crawler.routing import rule_route
from crawler.terms import leaf_english

SRC = sys.argv[1]
GO = "go" in sys.argv[2:]
cfg = load_config(); cfg["_data_dir"] = Path("/data"); cfg.setdefault("taxonomy", {})["file"] = "/data/taxonomy.csv"
lst = pd.read_csv(SRC, dtype=str).fillna("")
lst = lst[lst["path"].str.strip() != ""]          # kw_place.py drops (not a product) have no path
tree = CM.Tree(cfg)
src_col = next((c for c in tree.raw.columns if c.strip().startswith("Source")), None)
ids, reused, refused, new_nodes, new_groups = [], [], [], 0, set()
for r in lst.itertuples():
    parts = [p.strip() for p in r.path.split(">") if p.strip()]
    why = next((f"{p}: {CM.bad_name(p)}" for p in parts if CM.bad_name(p)), None)
    if why or len(parts) > CM.MAX_LEVELS:
        refused.append((r.path, why or "levels")); continue
    tid = None
    for i in range(1, len(parts) + 1):
        f = tree.find(" > ".join(parts[:i]))
        if f is not None:
            tid = f
            if i == len(parts):
                reused.append(r.path)
            continue
        if i < len(parts):
            new_groups.add(" > ".join(parts[:i]))         # a group level that was not in the taxonomy
        parent = tree.find(" > ".join(parts[:i - 1])) if i > 1 else ""
        tid = tree.add(parts[:i], parent or "", "")
        new_nodes += 1
        if src_col:
            tree.new_rows[-1][src_col] = "kwdisc-1405-08"
    ids.append((tid, r.path, r.query or parts[-1], r.batch, r.note, r.path in reused))
print("rows", len(lst), "new categories", new_nodes, "already in the taxonomy", len(reused), "refused", len(refused),
      "group levels made", len(new_groups), flush=True)
for p, w in refused[:10]:
    print("  REFUSED", p, "|", w)
for g in sorted(new_groups)[:15]:
    print("  NEW GROUP", g)
old = RS.load(cfg)
have = set(old["taxonomy_id"].astype(str))
b0 = int(pd.to_numeric(old["batch"], errors="coerce").max())
b0 = next((int(a.split("=", 1)[1]) for a in sys.argv[2:] if a.startswith("base=")), b0)
gl = RS.load_glossary()
new = []
for tid, path, q, b, note, was in ids:
    if tid in have:
        continue
    rr = rule_route(path)
    g = RS.group_of(path)
    new.append({"batch": str(b0 + int(b)), "taxonomy_id": tid, "breadcrumb": path, "query": q, "group": g,
                "kind": rr["kind"], "brand": rr["brand"], "brand_origin": rr["brand_origin"], "decision": rr["decision"],
                "reason": rr["reason"], "shops": "", "priced": "",
                "english": leaf_english(q, g.split(">")[-1].strip(), RS.english_for(g, gl)),
                "review": "", "final_route": "", "note": "kwdisc-1405-08 " + note, "queued": ""})
nd = pd.DataFrame(new)
print("routing: base", b0, "new rows", len(nd), "batches", nd["batch"].iloc[0], "-", nd["batch"].iloc[-1],
      "skipped (already routed)", len(ids) - len(nd), "decisions", nd["decision"].value_counts().to_dict(), flush=True)
if GO:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy("/data/routing.csv", f"/data/routing.csv.bak-{stamp}")
    tree.save()
    RS.save(cfg, pd.concat([old, nd], ignore_index=True))
    print("SAVED taxonomy + routing", stamp, "batches", nd["batch"].iloc[0], "-", nd["batch"].iloc[-1], flush=True)
