"""(28 Nov, owner) Price hunting among tested OpenRouter endpoints.

config.yaml openrouter.approved lists, per model, the endpoints whose quality we tested side by side with the
current one (tools/provider_test.py for the texts, tools/vision_test.py for the photo checks). A call then asks
OpenRouter for the cheapest of them right now ({"only": [...], "sort": "price"}): while an endpoint runs a
discount it gets the work, and when the discount ends the work goes back to the next cheapest by itself.
An endpoint that was not tested never gets work this way (tools/ops/price_watch.py only reports it).

    openrouter:
      approved:
        deepseek/deepseek-v4.1-flash: [baidu/fp8, deepinfra/fp8/flex, deepinfra/fp8]

A model with no list keeps the endpoints each step already pins. 'model@endpoint' pins (the test tools) win.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _approved_all() -> dict:
    try:
        from .config import load_config
        return dict((load_config().get("openrouter") or {}).get("approved") or {})
    except Exception:                                    # noqa: BLE001 - no list: every step keeps its own endpoints
        return {}


def approved(model: str) -> list[str]:
    return [str(x) for x in (_approved_all().get(model) or []) if str(x).strip()]


def provider(model: str) -> dict | None:
    """The OpenRouter provider object for this model, or None when the model has no approved list."""
    eps = approved(model)
    if not eps:
        return None
    # fallbacks stay inside 'only': one busy endpoint hands the call to the next approved one, never elsewhere
    return {"only": eps, "sort": "price", "allow_fallbacks": True}
