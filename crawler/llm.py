"""Step 4 (and helpers): language-model calls.

  relevance   does each crawled product belong to the category it was
              searched for? Also returns brand / model when the page lacked
              them, which the dedupe step needs.
  translate   Persian category -> English search phrase (foreign route)
  extract     last-resort product extraction from page text

Providers (config llm.provider):
  openai_compatible  any server speaking the OpenAI chat API: a local Ollama
                     (http://localhost:11434/v1, no key), or a hosted
                     open-model service (base_url + key in LLM_API_KEY)
  anthropic          the Claude Messages API
The free rule-based filter that needs no model at all is in rules.py.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .config import secret

log = logging.getLogger(__name__)
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


class LLM:
    def __init__(self, cfg: dict):
        c = cfg.get("llm", {})
        self.provider = c.get("provider", "rules")
        if self.provider == "rules":
            raise RuntimeError("no language model configured (llm.provider is 'rules')")
        self.model = c.get("model") or ("claude-haiku-5-5" if self.provider == "anthropic" else "qwen2.5:7b")
        self.max_tokens = int(c.get("max_tokens", 4000))
        self.base_url = (c.get("base_url") or "http://localhost:11434/v1").rstrip("/")
        env = c.get("api_key_env") or ("ANTHROPIC_API_KEY" if self.provider == "anthropic" else "LLM_API_KEY")
        self.key = secret(env)
        if self.provider == "anthropic" and not self.key:
            raise RuntimeError(f"{env} is not set (.env locally, Environment on Render)")
        self.s = requests.Session()
        proxy = (cfg.get("http", {}).get("proxy") or {}).get("foreign")
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        for attempt in range(4):
            try:
                r = self.s.post(url, json=body, headers=headers, timeout=300)
            except requests.RequestException as e:
                if attempt == 3:
                    raise RuntimeError(f"model server not reachable at {url}: {e}")
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"model server HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError("model server kept failing; try again later")

    def ask(self, system: str, user: str) -> str:
        if self.provider == "anthropic":
            data = self._post(ANTHROPIC_API,
                              {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                               "messages": [{"role": "user", "content": user}]},
                              {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"})
            u = data.get("usage", {})
            self.usage["input_tokens"] += u.get("input_tokens", 0)
            self.usage["output_tokens"] += u.get("output_tokens", 0)
            self.usage["calls"] += 1
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        data = self._post(f"{self.base_url}/chat/completions",
                          {"model": self.model, "max_tokens": self.max_tokens, "temperature": 0,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}, headers)
        u = data.get("usage") or {}
        self.usage["input_tokens"] += u.get("prompt_tokens", 0)
        self.usage["output_tokens"] += u.get("completion_tokens", 0)
        self.usage["calls"] += 1
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    def ask_json(self, system: str, user: str):
        txt = self.ask(system, user)
        txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)   # reasoning models
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
