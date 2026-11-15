"""(15 Nov) The SSH bridge client: WC calls go through it unchanged (answers, errors, retries)."""
import json
import socket
import threading

import pytest

from crawler import wcbridge
from crawler.wc import WC, WCError


class FakeBridge:
    """Stands for bridge_proxy.py + shop_bridge.php: checks the token, answers one line per request."""

    def __init__(self, tmp_path, answer, drop_first=0):
        self.answer, self.seen, self.drop = answer, [], drop_first
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(4)
        self.port = self.s.getsockname()[1]
        self.token = tmp_path / "bridge.token"
        self.token.write_text("t0k3n-for-the-test-1234567\n")
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        f = c.makefile("rb")
        if f.readline().strip() != b"t0k3n-for-the-test-1234567":
            c.sendall(b"@@E token\n")
            c.close()
            return
        c.sendall(b"PHP Notice: something from a plugin\n@@R " + json.dumps({"status": 200, "ready": True}).encode() + b"\n")
        for line in f:
            if line.strip() == b"QUIT":
                break
            q = json.loads(line)
            self.seen.append(q)
            if self.drop:
                self.drop -= 1
                c.close()
                return
            st, body = self.answer(q)
            c.sendall(b"noise\n@@R " + json.dumps({"status": st, "headers": {"X-WP-Total": "7"}, "body": body},
                                                  ensure_ascii=False).encode() + b"\n")
        c.close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    def start(answer, drop_first=0):
        fb = FakeBridge(tmp_path, answer, drop_first)
        monkeypatch.setenv("WC_BRIDGE", f"127.0.0.1:{fb.port}")
        monkeypatch.setenv("WC_BRIDGE_TOKEN_FILE", str(fb.token))
        monkeypatch.setenv("WC_URL", "https://shop.example")
        monkeypatch.delenv("WC_KEY", raising=False)
        monkeypatch.delenv("WC_SECRET", raising=False)
        wcbridge.close()
        return fb
    yield start
    wcbridge.close()


def test_create_goes_through_the_bridge(env):
    fb = env(lambda q: (201, {"id": 55, "name": q["json"]["name"], "status": "draft"}))
    wc = WC()
    assert wc.ok() and wc.can_upload()
    p = wc.create_product({"name": "پمپ آب", "sku": "X1", "status": "draft"})
    assert p == {"id": 55, "name": "پمپ آب", "status": "draft"}
    q = fb.seen[0]
    assert (q["api"], q["method"], q["path"]) == ("wc", "POST", "products")
    assert q["json"]["sku"] == "X1"


def test_rest_errors_keep_their_status(env):
    env(lambda q: (400, {"code": "product_invalid_sku", "message": "SKU already used"}))
    with pytest.raises(WCError) as e:
        WC().create_product({"name": "a", "sku": "X1"})
    assert e.value.status == 400 and "product_invalid_sku" in str(e.value)


def test_term_exists_answer_is_readable(env):
    env(lambda q: (400, {"code": "term_exists", "message": "exists", "data": {"resource_id": 812}}))
    r = WC()._wc("POST", "products/categories", json={"name": "x", "parent": 3})
    assert not r.ok and r.status_code == 400 and r.json()["data"]["resource_id"] == 812


def test_a_dropped_connection_is_retried(env, monkeypatch):
    monkeypatch.setattr("crawler.wc.time.sleep", lambda s: None)
    fb = env(lambda q: (200, {"id": 9, "sku": "X9"}), drop_first=1)
    assert WC().get_product(9)["id"] == 9
    assert len(fb.seen) == 2                            # the first try was lost, the second answered


def test_media_upload_sends_the_bytes(env, tmp_path):
    fb = env(lambda q: (201, {"id": 700}))
    f = tmp_path / "a.webp"
    f.write_bytes(b"\x00\x01webpdata")
    assert WC().upload_media(f, "x1-1.webp", alt="پمپ")["id"] == 700
    q = fb.seen[0]
    import base64
    assert q["api"] == "wp" and q["path"] == "media" and base64.b64decode(q["body_b64"]) == b"\x00\x01webpdata"
    assert q["headers"]["Content-Disposition"] == 'attachment; filename="x1-1.webp"'
    assert q["params"] == {"alt_text": "پمپ", "title": "پمپ"}


def test_headers_of_a_page_are_kept(env):
    env(lambda q: (200, [{"id": 1}]))
    items, total, pages = WC().products_page(1)
    assert items == [{"id": 1}] and total == 7


def test_a_wrong_token_is_a_connection_error(env, tmp_path, monkeypatch):
    env(lambda q: (200, {}))
    bad = tmp_path / "bad.token"
    bad.write_text("wrong")
    monkeypatch.setenv("WC_BRIDGE_TOKEN_FILE", str(bad))
    monkeypatch.setattr("crawler.wc.time.sleep", lambda s: None)
    with pytest.raises(WCError):
        WC().get_product(1)
