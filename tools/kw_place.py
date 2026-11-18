"""(18 Nov, owner) Place each keyword of the keyword-discovery list under a parent category of the site's tree.

The list's subgroups came from another (foreign) taxonomy and are mixed: a sample showed 15-30% of keywords whose
subgroup's parent did not fit them. So every keyword gets its own parent: DeepSeek (OpenRouter, the describe
model and endpoint) picks one of the allowed parents (the site's level-2/3 categories plus the agreed new ones),
the subgroup's mapped parent being the default. Rows that already have a 'target' are kept as they are.
The keyword then becomes its own category under that parent (kw_tax_import.py).

python3 tools/kw_place.py LIST TARGETS DEFAULTS OUT [batches=1-50] [workers=4]
  LIST      csv: batch, path ('subgroup > keyword'), query, note [, target]
  TARGETS   txt: one allowed parent path per line ('  [NEW]' at the end marks a parent the site does not have yet)
  DEFAULTS  csv: subgroup, target (the subgroup's parent)
  OUT       csv: batch, path ('parent > keyword'), query, note, subgroup, how ('kept'/'model'/'default'/'drop')
The cost goes to /data/runs/kw-place/09_costs.csv."""
import sys, os, json, re, time
sys.path.insert(0, "/app")
os.environ.setdefault("DATA_DIR", "/data")
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import requests
from crawler.config import load_config, secret
from crawler import costs

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
CHUNK = 60

SYSTEM = """You file products for an Iranian online shop (industrial and general goods). Each item is a Persian
keyword that becomes its own product category; pick its PARENT category from the numbered list below.
- Use the item's default parent ("d") when it fits the keyword; otherwise the number of the best-fitting parent.
- The keyword is the product; "src" are the shop keywords it came from (context only).
- Use -1 only when the keyword is not something a shop sells (a service, a question, a shop or place name).
Reply with ONLY JSON: {"items": [{"i": 0, "t": 12}, ...]}, one entry per item.
Parents:
"""


def parse_batches(spec: str) -> set:
    out = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def main() -> None:
    lst_p, tgt_p, def_p, out_p = sys.argv[1:5]
    opts = dict(a.split("=", 1) for a in sys.argv[5:] if "=" in a)
    cfg = load_config()
    dc = cfg.get("describe", {})
    model, providers = dc.get("model") or "deepseek/deepseek-v4.1-flash", dc.get("providers") or []
    key = secret("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set")
    targets = [l.rstrip("\n").replace("  [NEW]", "").strip() for l in open(tgt_p, encoding="utf-8") if l.strip()]
    targets = list(dict.fromkeys(targets))
    tid = {t: i for i, t in enumerate(targets)}
    defaults = {r.subgroup: r.target for r in pd.read_csv(def_p, dtype=str).fillna("").itertuples()}
    lst = pd.read_csv(lst_p, dtype=str).fillna("")
    if "target" not in lst.columns:
        lst["target"] = ""
    if opts.get("batches"):
        lst = lst[lst["batch"].astype(int).isin(parse_batches(opts["batches"]))]
    rows = []
    for r in lst.itertuples():
        sub, _, kw = r.path.rpartition(" > ")
        rows.append({"batch": r.batch, "kw": kw.strip(), "query": r.query, "note": r.note, "subgroup": sub.strip(),
                     "src": "", "target": r.target.strip(), "default": defaults.get(sub.strip(), "")})
    system = SYSTEM + "\n".join(f"{i}. {t}" for i, t in enumerate(targets))
    s = requests.Session()
    spend = {"in": 0, "out": 0, "usd": 0.0, "calls": 0}

    def ask(chunk):
        items = [{"i": i, "kw": r["kw"], "d": tid.get(r["default"], -2)} for i, r in enumerate(chunk)]
        body = {"model": model, "temperature": 0, "max_tokens": 3000, "usage": {"include": True},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}],
                "response_format": {"type": "json_object"}, "reasoning": {"enabled": False}}
        if providers:
            body["provider"] = {"order": providers, "allow_fallbacks": False}
        for attempt in range(4):
            try:
                res = s.post(OPENROUTER, json=body, timeout=180, headers={
                    "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                    "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan crawler"})
            except requests.RequestException:
                time.sleep(5 * (attempt + 1)); continue
            if res.status_code != 200:
                time.sleep(5 * (attempt + 1)); continue
            j = res.json()
            u = j.get("usage") or {}
            spend["in"] += u.get("prompt_tokens", 0); spend["out"] += u.get("completion_tokens", 0)
            spend["usd"] += float(u.get("cost") or 0); spend["calls"] += 1
            text = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            try:
                got = {int(g["i"]): int(g["t"]) for g in json.loads(text).get("items") or []}
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            return got
        return {}

    todo = [r for r in rows if not r["target"]]
    chunks = [todo[k:k + CHUNK] for k in range(0, len(todo), CHUNK)]
    with ThreadPoolExecutor(int(opts.get("workers", 4))) as ex:
        for n, (chunk, got) in enumerate(zip(chunks, ex.map(ask, chunks)), 1):
            for i, r in enumerate(chunk):
                t = got.get(i)
                if t is None or t == -2 or (t not in (-1,) and not 0 <= t < len(targets)):
                    r["target"], r["how"] = r["default"], "default"
                elif t == -1:
                    r["target"], r["how"] = "", "drop"
                else:
                    r["target"], r["how"] = targets[t], ("kept" if targets[t] == r["default"] else "model")
            if n % 10 == 0:
                print(f"{n}/{len(chunks)} chunks, ${spend['usd']:.3f}", flush=True)
    for r in rows:
        r.setdefault("how", "given")
    out = pd.DataFrame([{"batch": r["batch"], "path": f"{r['target']} > {r['kw']}" if r["target"] else "",
                         "query": r["query"], "note": r["note"], "subgroup": r["subgroup"], "how": r["how"]}
                        for r in rows])
    out.to_csv(out_p, index=False)
    d = Path("/data/runs/kw-place"); d.mkdir(parents=True, exist_ok=True)
    costs.add(d, "kw_place", len(todo), spend["usd"], f"{spend['calls']} calls, {spend['in']} in / {spend['out']} out tokens")
    print("rows", len(out), out["how"].value_counts().to_dict(), f"cost ${spend['usd']:.3f}",
          f"tokens in {spend['in']} out {spend['out']}", flush=True)


if __name__ == "__main__":
    main()
