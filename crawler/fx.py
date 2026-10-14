"""Dollar rate: fetched once per day and logged to data/fx_rates.csv.

Providers:
  manual    rate typed into config.yaml (good for the first manual run)
  json_api  any JSON endpoint; `json_path` points at the number, e.g.
            "usd_sell.value". This is where your existing dollar API goes.
The daily log is what the later "dollar update" (P_now = P_then × …) reads.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import pandas as pd
import requests

from .io import ENCODING
from .normalize import parse_number

log = logging.getLogger(__name__)
COLS = ["date", "usd_rate_toman", "rate_type", "provider"]


def _log_path(cfg: dict) -> Path:
    return cfg["_data_dir"] / "fx_rates.csv"


def _read_log(cfg: dict) -> pd.DataFrame:
    p = _log_path(cfg)
    if p.exists():
        return pd.read_csv(p, encoding=ENCODING, dtype=str)
    return pd.DataFrame(columns=COLS)


def _dig(data, path: str):
    for part in path.split("."):
        if isinstance(data, list):
            data = data[int(part)]
        else:
            data = data[part]
    return data


def fetch_rate(cfg: dict) -> float:
    fx = cfg.get("fx", {})
    provider = fx.get("provider", "manual")
    if provider == "manual":
        if not fx.get("manual_rate_toman"):
            raise ValueError("set fx.manual_rate_toman in config.yaml (today's dollar rate in toman) "
                             "or switch fx.provider to json_api")
        rate = float(fx["manual_rate_toman"])
    elif provider == "json_api":
        url = fx["url"].format(**os.environ)
        headers = {k: str(v).format(**os.environ) for k, v in (fx.get("headers") or {}).items()}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        rate = parse_number(_dig(r.json(), fx["json_path"]))
        if rate is None:
            raise ValueError(f"no number at json_path {fx['json_path']}")
        if fx.get("unit", "toman") == "rial":
            rate /= 10
    else:
        raise ValueError(f"unknown fx provider {provider}")
    lo, hi = fx.get("sanity_range_toman", [10_000, 10_000_000])
    if not lo <= rate <= hi:
        raise ValueError(f"dollar rate {rate:,.0f} outside sanity range {lo:,}-{hi:,}; check unit/json_path")
    return rate


def today_rate(cfg: dict) -> dict:
    """Rate for today, fetched at most once per day."""
    today = dt.date.today().isoformat()
    df = _read_log(cfg)
    hit = df[df["date"] == today]
    if len(hit):
        r = hit.iloc[-1]
        return {"usd_rate_toman": float(r["usd_rate_toman"]), "usd_rate_date": today,
                "usd_rate_type": r["rate_type"]}
    rate = fetch_rate(cfg)
    fx = cfg.get("fx", {})
    row = {"date": today, "usd_rate_toman": rate, "rate_type": fx.get("rate_type", "free"),
           "provider": fx.get("provider", "manual")}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(_log_path(cfg), index=False, encoding=ENCODING)
    log.info("dollar rate %s: %s toman", today, f"{rate:,.0f}")
    return {"usd_rate_toman": rate, "usd_rate_date": today, "usd_rate_type": row["rate_type"]}


def rate_on(cfg: dict, date: str) -> float | None:
    """Logged rate on or just before `date` (for re-pricing old observations)."""
    df = _read_log(cfg)
    df = df[df["date"] <= date].sort_values("date")
    return float(df.iloc[-1]["usd_rate_toman"]) if len(df) else None
