"""Step 4 (and helpers): Claude API calls.

  relevance   does each crawled product belong to the category it was
              searched for? Also returns brand / model when the page lacked
              them, which the dedupe step needs.
  translate   Persian category -> English search phrase (foreign route)
  extract     last-resort product extraction from page text

Uses the Messages API over plain HTTPS; no SDK needed.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .config import secret

log = logging.getLogger(__name__)
API = "https://api.anthropic.com/v1/messages"


class LLM:
    def __init__(self, cfg: dict):
        c = cfg.get("llm", {})
        self.key = secret(c.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (.env locally, Environment on Render)")
        self.model = c.get("model", "claude-haiku-5-5")
        self.max_tokens = int(c.get("max_tokens", 4000))
        self.s = requests.Session()
        proxy = (cfg.get("http", {}).get("proxy") or {}).get("foreign")
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def ask(self, system: str, user: str) -> str:
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        for attempt in range(4):
            r = self.s.post(API, json=body, headers=headers, timeout=120)
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            u = data.get("usage", {})
            self.usage["input_tokens"] += u.get("input_tokens", 0)
            self.usage["output_tokens"] += u.get("output_tokens", 0)
            self.usage["calls"] += 1
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        raise RuntimeError("Claude API kept failing; try again later")

    def ask_json(self, system: str, user: str):
        txt = self.ask(system, user)
        m = re.search(r"(\[.*\]|\{.*\})", txt, re.S)
        if not m:
            raise ValueError(f"no JSON in model reply: {txt[:200]}")
        return json.loads(m.group(1))


RELEVANCE_SYSTEM = """You check product listings for an Iranian industrial marketplace.
For each product decide if it IS the kind of product named by the category path
(the last part is the most specific). A listing is NOT relevant if it is a
different product type, a spare part or accessory of it (unless the category is
for that part), a service, or a different variant the category excludes
(e.g. category "pneumatic actuator" vs an electric one; "1/2 inch" vs "1 inch").
Also extract brand and model number when visible in the name or specs.
Reply with ONLY a JSON array, one object per product, in input order:
[{"i": 0, "relevant": true, "confidence": 0.9, "reason": "short Persian reason",
  "brand": "", "model": ""}]"""


def relevance_batch(llm: LLM, breadcrumb: str, products: list[dict]) -> list[dict]:
    items = [{"i": i, "name": p.get("name", ""), "brand": p.get("brand", ""),
              "model": p.get("model", ""), "specs": p.get("specs_json", "")[:400],
              "desc": p.get("short_description", "")[:250]} for i, p in enumerate(products)]
    user = f"Category path: {breadcrumb}\n\nProducts:\n{json.dumps(items, ensure_ascii=False)}"
    out = llm.ask_json(RELEVANCE_SYSTEM, user)
    by_i = {int(o.get("i", -1)): o for o in out if isinstance(o, dict)}
    return [by_i.get(i, {"relevant": None, "confidence": 0, "reason": "no answer"}) for i in range(len(products))]


TRANSLATE_SYSTEM = """Translate Iranian industrial product category names into the
English search phrase a buyer would type on Made-in-China or Alibaba.
Use the trade term, not a literal translation (e.g. "کف کش" -> "submersible
drainage pump", "اکچویتور برقی تدریجی" -> "modulating electric actuator").
Reply with ONLY a JSON array of strings, same order as input."""


def translate(llm: LLM, items: list[tuple[str, str]]) -> list[str]:
    user = json.dumps([{"path": p, "query": q} for p, q in items], ensure_ascii=False)
    out = llm.ask_json(TRANSLATE_SYSTEM, user)
    return [str(x) for x in out][: len(items)]


EXTRACT_SYSTEM = """Extract the single main product on this web page.
Reply with ONLY JSON: {"name": "", "brand": "", "model": "", "price": null,
"price_unit": "toman|rial|usd|", "specs": {"key": "value"}}. Use null price if
no price is shown. Do not invent values."""


def extract(llm: LLM, page_text: str) -> dict | None:
    try:
        return llm.ask_json(EXTRACT_SYSTEM, page_text[:6000])
    except Exception as e:
        log.warning("LLM extract failed: %s", e)
        return None
