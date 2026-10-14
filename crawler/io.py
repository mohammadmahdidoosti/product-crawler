"""CSV helpers and the column layout of every step's output.

All files are written as UTF-8 with BOM so Persian text opens correctly in
Excel. Each step reads the previous step's file from the same run folder.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

import pandas as pd

ENCODING = "utf-8-sig"

FILES = {
    "queue": "01_queue.csv",
    "hits": "02_search_hits.csv",
    "crawled": "03_crawled.csv",
    "filtered": "04_filtered.csv",
    "rejected": "04_rejected.csv",
    "new": "05_new_products.csv",
    "updates": "05_updates_existing.csv",
    "review": "05_review.csv",
    "woo": "06_woo_import.csv",
}

QUEUE_COLS = [
    "taxonomy_id", "query", "breadcrumb", "leaf", "level1", "route",
    "site_product_count", "priority",
]

HITS_COLS = [
    "taxonomy_id", "query", "route", "source_domain", "source_type", "url",
    "title", "found_by", "prefilled_json",
]

# Step 3 output — the columns the tech team listed, plus the ones we added.
CRAWLED_COLS = [
    "taxonomy_id", "breadcrumb", "query", "route",
    "name", "brand", "model", "specs_json", "short_description",
    "price_raw", "price_unit_detected", "price_toman", "price_usd", "price_flags",
    "sale_unit", "in_stock",
    "usd_rate_toman", "usd_rate_date", "usd_rate_type",
    "seller_name", "seller_phones", "claims_representative",
    "source_domain", "source_type", "url", "image_urls",
    "extracted_by", "crawled_at",
]

FILTER_COLS = CRAWLED_COLS + ["relevant", "relevance_confidence", "relevance_reason"]


def read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding=ENCODING, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def write_csv(df: pd.DataFrame, path: Path, columns: list[str] | None = None) -> Path:
    if columns:
        for c in columns:
            if c not in df.columns:
                df[c] = ""
        extra = [c for c in df.columns if c not in columns]
        df = df[columns + extra]
    df.to_csv(path, index=False, encoding=ENCODING, quoting=csv.QUOTE_MINIMAL)
    return path


class Appender:
    """Append rows one by one so a crash never loses finished work."""

    def __init__(self, path: Path, columns: list[str]):
        self.path, self.columns = path, columns
        new = not path.exists() or path.stat().st_size == 0
        self._f = path.open("a", encoding=ENCODING if new else "utf-8", newline="")
        self._w = csv.DictWriter(self._f, fieldnames=columns, extrasaction="ignore")
        if new:
            self._w.writeheader()

    def write(self, row: dict) -> None:
        self._w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in self.columns})
        self._f.flush()

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def done_values(path: Path, column: str) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    try:
        return set(read_csv(path)[column])
    except Exception:
        return set()


def iter_rows(df: pd.DataFrame) -> Iterable[dict]:
    for _, r in df.iterrows():
        yield r.to_dict()
