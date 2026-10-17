"""Persian name, specs and description for foreign (English) products — no model, no tokens.

Name    = the Caravan category name + model + up to 3 key specs, e.g.
          «اکچویتور برقی تدریجی مدل HEQ، AC220V، اتصال فلنجی، آلیاژ آلومینیوم»
Specs   = keys and values through glossary/specs_fa.csv and values_fa.csv.
          Numbers, units and codes (AC220V, DN50, IP67) pass unchanged.
          Anything else not in the dictionary keeps its English text and is
          counted in data/untranslated.csv, to be added to the dictionary later.
The English original stays in the data (source_name) for the team.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .config import ROOT

FA = re.compile(r"[؀-ۿ]")
CODE = re.compile(r"^[A-Za-z]{0,4}[\s\-]?\d[\w\s.,/~\-+°%()*:x×]*$|^[A-Z0-9][A-Z0-9\-/+.]{1,14}$")
TITLE_KEYS = [  # spec keys worth putting in the name, in order
    ("ولتاژ", ("Power Supply", "Voltage", "Rated Voltage", "Operating Voltage", "Input Voltage")),
    ("توان", ("Power", "Rated Power", "Output Power", "Motor Power")),
    ("گشتاور", ("Max Torque", "Output Torque", "Torque", "Rated Torque")),
    ("سایز", ("Nominal Diameter", "Size")),
    ("اتصال", ("Connection", "Connection Form", "Connection Type")),
    ("جنس", ("Body Material", "Material", "Housing Material")),
]
UNITS = {"piece": "عدد", "set": "دستگاه", "unit": "عدد", "pair": "جفت", "meter": "متر", "ton": "تن", "kg": "کیلوگرم",
         "roll": "رول", "boxe": "جعبه", "box": "جعبه"}
SKIP_KEYS = {"customization", "after-sales service", "warranty", "package size", "package gross weight",
             "transport package", "hs code", "production capacity", "trademark", "specification"}


def is_foreign(name: str) -> bool:
    return bool(name) and not FA.search(name)


@lru_cache(maxsize=None)
def _dict(name: str) -> dict[str, str]:
    p = ROOT / "glossary" / name
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig") as f:
        return {r["en"].strip().lower(): r["fa"].strip() for r in csv.DictReader(f) if r.get("fa")}


def tr_key(k: str) -> str | None:
    return _dict("specs_fa.csv").get(k.strip().rstrip(":").lower())


def tr_value(v: str) -> tuple[str, bool]:
    """(text, translated?) — codes and numbers count as translated."""
    v = v.strip()
    d = _dict("values_fa.csv")
    if v.lower() in d:
        return d[v.lower()], True
    m = re.match(r"^([\d,.]+)\s*(Pieces?|Pcs|Sets?|Units?|Pairs?|Meters?|Tons?|Kg|Rolls?|Boxes?)$", v, re.I)
    if m:
        return f"{m.group(1)} {UNITS[m.group(2).lower().rstrip('s') if m.group(2).lower() != 'pcs' else 'piece']}", True
    if CODE.match(v) or FA.search(v) or (" " not in v and sum(c.isupper() for c in v) >= 2):
        return v, True
    parts = [p.strip() for p in re.split(r"[,/;]", v) if p.strip()]
    if len(parts) > 1:
        out = [tr_value(p) for p in parts]
        return "، ".join(t for t, _ in out), all(ok for _, ok in out)
    return v, False


def translate_specs(specs: dict, missing: Counter | None = None) -> dict[str, str]:
    out = {}
    for k, v in specs.items():
        if not v or k.strip().lower() in SKIP_KEYS:
            continue
        fk = tr_key(k)
        fv, ok = tr_value(str(v))
        if missing is not None:
            if not fk:
                missing[("key", k.strip())] += 1
            if not ok:
                missing[("value", str(v).strip())] += 1
        out[fk or k.strip()] = fv
    return out


def persian_name(leaf: str, model: str, specs: dict) -> str:
    bits = []
    for _, keys in TITLE_KEYS:
        for k in keys:
            v = next((val for kk, val in specs.items() if kk.lower() == k.lower() and val), None)
            if v:
                fv, ok = tr_value(str(v))
                if ok and len(fv) <= 22:
                    label = tr_key(k) or ""
                    bits.append(f"{label} {fv}" if label.startswith(("اتصال", "نوع اتصال")) else fv)
                break
        if len(bits) >= 3:
            break
    bits = [b.replace("نوع اتصال ", "اتصال ") for b in bits]
    name = leaf.strip()
    if model:
        name += f" مدل {model}"
    return name + (f"، {'، '.join(bits)}" if bits else "")


def save_missing(path: Path, missing: Counter) -> None:
    old = Counter()
    if path.exists():
        with path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                old[(r["kind"], r["text"])] = int(r["count"])
    old.update(missing)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "text", "count", "fa"])
        for (kind, text), n in old.most_common():
            w.writerow([kind, text, n, ""])
