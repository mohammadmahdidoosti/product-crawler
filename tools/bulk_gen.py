"""(14 Nov, owner) The keyword categories the site does not have yet -> /data/tmp/bulkcats.csv, parents first.
Columns: tid, name, parent_tid, parent_wp (parent_wp set when the parent is already on the site)."""
import csv, pandas as pd
t = pd.read_csv("/data/taxonomy.csv", dtype=str).fillna("")
src = next(c for c in t.columns if c.strip().startswith("Source"))
w = pd.read_csv("/data/wp_categories.csv", dtype=str).fillna("")
wp = {a.strip(): b.strip() for a, b in zip(w["ID"], w["wp_id"]) if b.strip()}
seed = t[(t[src] == "seed-1405-08") & ~t["ID"].isin(wp)].copy()
seed["lvl"] = seed["Full Breadcrumb Path"].str.count(">")
seed = seed.sort_values(["lvl", "Full Breadcrumb Path"])
ids = set(seed["ID"])
bad = 0
with open("/data/tmp/bulkcats.csv", "w", newline="", encoding="utf-8") as f:
    out = csv.writer(f)
    out.writerow(["tid", "name", "parent_tid", "parent_wp"])
    for r in seed.itertuples():
        p = str(r.Parent_ID).replace(".0", "").strip()
        pw = wp.get(p, "") if p else "0"
        if p and not pw and p not in ids:
            bad += 1
            continue
        out.writerow([r.ID, r.Category_Name.strip(), p, pw])
print("BULK rows", len(seed) - bad, "orphans", bad, "already on site", int((t[src] == "seed-1405-08").sum()) - len(seed),
      "levels", seed["lvl"].value_counts().sort_index().to_dict(), flush=True)
