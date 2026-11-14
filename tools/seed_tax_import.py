"""(14 Nov, owner) Seed-keyword taxonomy -> crawler taxonomy (no site change) + routing rows as new batches.
python3 tax_import.py         -> dry run (counts only)
python3 tax_import.py go      -> write taxonomy.csv (backup first) and routing.csv (backup first)"""
import sys, os, json, gzip, base64, shutil, time
sys.path.insert(0, "/app")
os.environ.setdefault("DATA_DIR", "/data")
from pathlib import Path
import pandas as pd
from crawler.config import load_config
from crawler import catmake as CM
from crawler import routing_state as RS
from crawler.routing import rule_route
from crawler.terms import leaf_english

GO = "go" in sys.argv[1:]
cfg = load_config(); cfg["_data_dir"] = Path("/data"); cfg.setdefault("taxonomy", {})["file"] = "/data/taxonomy.csv"
_t = pd.read_csv("/app/assets/seeds/seed_taxonomy_1405-08.csv", dtype=str).fillna("")
rows = [[r.path, r.synonyms, r.n] for r in _t.itertuples()]
tree = CM.Tree(cfg)
src_col = next((c for c in tree.raw.columns if c.strip().startswith("Source")), None)
syn_col = "Synonyms" if "Synonyms" in tree.raw.columns else None
seed_ids, reused, refused, new_nodes = [], [], [], 0
for path, syn, n in rows:
    parts = [p.strip() for p in path.split(">") if p.strip()]
    why = next((f"{p}: {CM.bad_name(p)}" for p in parts if CM.bad_name(p)), None)
    if why or len(parts) > CM.MAX_LEVELS:
        refused.append((path, why or "levels")); continue
    tid = None
    for i in range(1, len(parts) + 1):
        f = tree.find(" > ".join(parts[:i]))
        if f is not None:
            tid = f
            if i == len(parts):
                reused.append(path)
            continue
        parent = tree.find(" > ".join(parts[:i - 1])) if i > 1 else ""
        tid = tree.add(parts[:i], parent or "", "")
        new_nodes += 1
        r = tree.new_rows[-1]
        if src_col:
            r[src_col] = "seed-1405-08"
        if i == len(parts) and syn_col and syn:
            r[syn_col] = syn
    if path not in reused:
        seed_ids.append((tid, path, parts[-1]))
print("rows", len(rows), "new categories", new_nodes, "seed categories to crawl", len(seed_ids),
      "already in the taxonomy", len(reused), "refused", len(refused), flush=True)
for p, w in refused[:15]:
    print("  REFUSED", p, "|", w)
for p in reused[:10]:
    print("  REUSED", p)
old = RS.load(cfg)
have = set(old["taxonomy_id"].astype(str))
b0 = int(pd.to_numeric(old["batch"], errors="coerce").max())
gl = RS.load_glossary()
new = []
for tid, path, leaf in seed_ids:
    if tid in have:
        continue
    rr = rule_route(path)
    g = RS.group_of(path)
    new.append({"taxonomy_id": tid, "breadcrumb": path, "query": leaf, "group": g, "kind": rr["kind"],
                "brand": rr["brand"], "brand_origin": rr["brand_origin"], "decision": rr["decision"],
                "reason": rr["reason"], "shops": "", "priced": "",
                "english": leaf_english(leaf, g.split(">")[-1].strip(), RS.english_for(g, gl)),
                "review": "", "final_route": "", "note": "seed-1405-08", "queued": ""})
nd = pd.DataFrame(new)
nd.insert(0, "batch", (nd.index // 100 + b0 + 1).astype(str))
print("routing: last old batch", b0, "new rows", len(nd), "new batches", nd["batch"].iloc[0], "-", nd["batch"].iloc[-1],
      "decisions", nd["decision"].value_counts().to_dict(), flush=True)
if GO:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy("/data/routing.csv", f"/data/routing.csv.bak-{stamp}")
    tree.save()
    RS.save(cfg, pd.concat([old, nd], ignore_index=True))
    print("SAVED taxonomy + routing", stamp, flush=True)
