"""(28 Nov, owner) Price watch: once a day, the OpenRouter prices of the endpoints of the models we use.

Reports two things (one line each in the log, the last ones shown by the ops mailbox 'status'):
  CHEAPER  an endpoint NOT in openrouter.approved is cheaper than the cheapest approved one: test it
           (tools/provider_test.py for texts, tools/vision_test.py for photo checks) before approving it;
  CHANGED  an approved endpoint's price moved since the last run (a discount began or ended).
Nothing is switched here: crawler/pricehunt.py already sends each call to the cheapest approved endpoint.
Price = what one of our calls costs: input + 0.65 x output per token (our texts: ~27k in, ~17.5k out).

    python tools/ops/price_watch.py            # in the app container; state in /data/price_watch.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from crawler.config import load_config  # noqa: E402

OUT_SHARE = 0.65
STATE = Path("/data/price_watch.json")


def blended(p: dict) -> float:
    return float(p.get("prompt") or 0) + OUT_SHARE * float(p.get("completion") or 0)


def main() -> None:
    cfg = load_config()
    approved = (cfg.get("openrouter") or {}).get("approved") or {}
    models = set(approved) | {cfg.get("describe", {}).get("model", "deepseek/deepseek-v4.1-flash")}
    models |= set(cfg.get("describe", {}).get("fallback_models") or [])
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    new, lines = {}, []
    now = time.strftime("%Y-%m-%d %H:%M")
    for m in sorted(models):
        try:
            eps = requests.get(f"https://openrouter.ai/api/v1/models/{m}/endpoints", timeout=30).json()["data"]["endpoints"]
        except Exception as e:                                       # noqa: BLE001
            lines.append(f"{now} ERROR {m}: {e}")
            continue
        prices = {str(e.get("tag")): {"in": float(e["pricing"].get("prompt") or 0) * 1e6,
                                      "out": float(e["pricing"].get("completion") or 0) * 1e6,
                                      "b": blended(e["pricing"]) * 1e6,
                                      "img": "image" in (e.get("supported_parameters") or []) or None}
                  for e in eps if e.get("pricing") and e.get("status", 0) == 0}
        new[m] = prices
        ok = [t for t in (approved.get(m) or []) if t in prices]
        best = min((prices[t]["b"] for t in ok), default=None)
        for t, p in sorted(prices.items(), key=lambda x: x[1]["b"]):
            if t not in ok and best is not None and p["b"] < best * 0.95:
                lines.append(f"{now} CHEAPER {m} {t}: {p['in']:.4f}/{p['out']:.4f} $/M "
                             f"(cheapest approved {best:.4f} blended) -> test it before approving")
        for t in ok:
            was = (old.get(m) or {}).get(t)
            if was and abs(was["b"] - prices[t]["b"]) > 0.001:
                lines.append(f"{now} CHANGED {m} {t}: {was['in']:.4f}/{was['out']:.4f} -> "
                             f"{prices[t]['in']:.4f}/{prices[t]['out']:.4f} $/M")
        if best is not None:
            cheapest = min(ok, key=lambda t: prices[t]["b"])
            lines.append(f"{now} NOW {m}: cheapest approved {cheapest} {prices[cheapest]['in']:.4f}/"
                         f"{prices[cheapest]['out']:.4f} $/M")
    STATE.write_text(json.dumps(new, indent=1))
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
