"""What a batch cost, step by step (DATA_DIR/runs/<run>/09_costs.csv).

Each paid step adds one line when it ends: the web searches (Serper), the product descriptions
(DeepSeek through OpenRouter, the real cost OpenRouter reports), the photo check (vision, real
cost) and the photo fixes (free fixes, reused fixes and paid edits; a paid edit is counted at
images.ai_edit_usd, as the image model's bill is not returned per call). summary() adds them up
for the report after a batch. A step that runs again adds a new line, so a re-run is counted too.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from .io import read_csv, write_csv

log = logging.getLogger(__name__)
FILE = "09_costs.csv"
COLS = ["time", "step", "units", "usd", "estimated", "note"]


def add(d: Path, step: str, units: int, usd: float, note: str = "", estimated: bool = False) -> None:
    try:
        p = Path(d) / FILE
        old = read_csv(p) if p.exists() else pd.DataFrame(columns=COLS)
        row = {"time": time.strftime("%Y-%m-%d %H:%M"), "step": step, "units": str(int(units)),
               "usd": f"{float(usd):.4f}", "estimated": "yes" if estimated else "", "note": note}
        write_csv(pd.concat([old, pd.DataFrame([row])], ignore_index=True), p, COLS)
        log.info("COST %s: %d unit(s), $%.4f%s %s", step, units, usd, " (estimated)" if estimated else "", note)
    except Exception as e:                                   # noqa: BLE001 - a missing ledger line never stops a step
        log.warning("cost not written (%s): %s", step, e)


def summary(d: Path) -> dict:
    """{'steps': {step: {'units', 'usd'}}, 'usd': total, 'estimated': share of the total that is estimated}."""
    p = Path(d) / FILE
    if not p.exists():
        return {"steps": {}, "usd": 0.0, "estimated": 0.0}
    x = read_csv(p)
    x["usd_f"] = pd.to_numeric(x["usd"], errors="coerce").fillna(0.0)
    x["units_i"] = pd.to_numeric(x["units"], errors="coerce").fillna(0).astype(int)
    steps = {s: {"units": int(g["units_i"].sum()), "usd": round(float(g["usd_f"].sum()), 4)}
             for s, g in x.groupby("step", sort=False)}
    total = float(x["usd_f"].sum())
    est = float(x.loc[x["estimated"] == "yes", "usd_f"].sum())
    return {"steps": steps, "usd": round(total, 4), "estimated": round(est / total, 3) if total else 0.0}
