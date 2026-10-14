"""Load config.yaml, sources.yaml and .env (no extra dependencies)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    _load_env(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    src_path = ROOT / cfg.get("sources_file", "sources.yaml")
    cfg["sources"] = yaml.safe_load(src_path.read_text(encoding="utf-8")) or {}
    data_dir = ROOT / cfg.get("data_dir", "data")
    cfg["_data_dir"] = data_dir
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    return cfg


def secret(name: str) -> str | None:
    v = os.environ.get(name)
    return v or None


def run_dir(cfg: dict, run: str) -> Path:
    d = cfg["_data_dir"] / "runs" / run
    d.mkdir(parents=True, exist_ok=True)
    return d
