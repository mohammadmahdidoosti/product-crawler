"""(28 Nov) openrouter.approved: the cheapest tested endpoint; pins and models without a list stay as they were."""
from crawler import describe, pricehunt


def _set(monkeypatch, approved):
    monkeypatch.setattr(pricehunt, "_approved_all", lambda: approved)


def test_provider_object(monkeypatch):
    _set(monkeypatch, {"m/a": ["baidu/fp8", "deepinfra/fp8/flex"]})
    assert pricehunt.provider("m/a") == {"only": ["baidu/fp8", "deepinfra/fp8/flex"], "sort": "price",
                                       "allow_fallbacks": True}
    assert pricehunt.provider("m/other") is None


def test_writer_uses_it_unless_pinned(monkeypatch):
    _set(monkeypatch, {"deepseek/deepseek-v4.1-flash": ["baidu/fp8"]})
    sent = []

    class R:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}

    w = describe.Writer({"describe": {"providers": ["deepinfra/fp8"]}})
    w.key = "k"
    w.s.post = lambda *a, **k: (sent.append(k["json"].get("provider")), R())[1]
    w.write({"name": "x", "url": "u"})
    assert sent[0]["only"] == ["baidu/fp8"]
    p = describe.Writer({"describe": {}}, "deepseek/deepseek-v4.1-flash@river")
    p.key = "k"
    p.s.post = lambda *a, **k: (sent.append(k["json"].get("provider")), R())[1]
    p.write({"name": "x", "url": "u"})
    assert sent[-1] == {"order": ["river"], "allow_fallbacks": False}
