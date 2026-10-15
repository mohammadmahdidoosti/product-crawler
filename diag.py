"""Startup diagnostics: fetch the URLs in $DIAG_URLS and log their structure (for debugging sites remotely).

DIAG_URLS="https://a.com/ https://a.com/robots.txt"   -> lines prefixed with "DIAG" in the service log.
"""
import json
import logging
import os
import re
import threading

import requests
from bs4 import BeautifulSoup

import crawler

log = logging.getLogger("diag")
CHUNK = 1800


def _chunks(label: str, text: str, limit: int):
    text = text[:limit]
    for i in range(0, len(text), CHUNK):
        log.info("DIAG %s [%d] %s", label, i // CHUNK, text[i:i + CHUNK].replace("\n", "⏎"))


def diagnose(url: str, html_limit: int):
    s = requests.Session()
    s.headers.update({"User-Agent": crawler.DEFAULT_USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9"})
    try:
        r = s.get(url, timeout=30)
    except Exception as e:
        log.info("DIAG %s ERROR %s", url, e)
        return
    ct = r.headers.get("content-type", "")
    log.info("DIAG %s status=%s final=%s type=%s bytes=%d server=%s", url, r.status_code, r.url, ct,
             len(r.content), r.headers.get("server", ""))
    if "html" not in ct:
        _chunks(f"{url} BODY", r.text, html_limit)
        return
    r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    gen = soup.find("meta", attrs={"name": "generator"})
    log.info("DIAG %s generator=%s title=%s body_class=%s", url, gen.get("content") if gen else "",
             (soup.title.get_text() if soup.title else "")[:120],
             " ".join(soup.body.get("class") or [])[:300] if soup.body else "")
    scripts = [t.get("src") for t in soup.find_all("script", src=True)]
    log.info("DIAG %s scripts=%s", url, " ".join(scripts)[:CHUNK])
    inline = " ".join(t.get_text() for t in soup.find_all("script", src=False))
    endpoints = sorted(set(re.findall(r"""["'](https?://[^"']+|/[^"'\s]*(?:api|ajax|json|wp-json|graphql|sheet|csv|xlsx?)[^"'\s]*)["']""", inline, re.I)))
    log.info("DIAG %s inline_script_bytes=%d endpoints=%s", url, len(inline), " ".join(endpoints)[:CHUNK])
    if os.environ.get("DIAG_SCRIPTS") == "1" and inline:
        _chunks(f"{url} SCRIPT_HEAD", inline, 7200)
        for kw in ("[{", "JSON.parse", "const data", "var data", "prices", "versions"):
            i = inline.find(kw)
            if i >= 0:
                _chunks(f"{url} SCRIPT@{kw}", inline[max(0, i - 300):i + 1500], 1800)
    ld = [t.get_text()[:300] for t in soup.find_all("script", type=re.compile("ld\\+json"))]
    log.info("DIAG %s jsonld=%s", url, " || ".join(ld)[:CHUNK])
    links = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if not h.startswith(("#", "tel:", "mailto:", "javascript:")):
            links.append(h)
    links = list(dict.fromkeys(links))
    log.info("DIAG %s links=%d", url, len(links))
    _chunks(f"{url} LINKS", " ".join(links), 9000)
    for i, t in enumerate(soup.find_all("table")[:3]):
        rows = t.find_all("tr")
        log.info("DIAG %s table%d rows=%d attrs=%s", url, i, len(rows), dict(t.attrs))
        _chunks(f"{url} TABLE{i}", str(t), 3600)
    for tag in soup(["script", "style", "svg", "noscript"]):
        tag.decompose()
    body = str(soup.body) if soup.body else r.text
    _chunks(f"{url} HTML", re.sub(r"\s+", " ", body), html_limit)


def parse_check(url: str):
    """Run the real parsers on a live page and log what they extract."""
    s = requests.Session()
    s.headers.update({"User-Agent": crawler.DEFAULT_USER_AGENT})
    try:
        r = s.get(url, timeout=30)
        r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
    except Exception as e:
        log.info("DIAG PARSE %s ERROR %s", url, e)
        return
    host = crawler.norm_host(requests.utils.urlparse(url).netloc)
    rec = crawler.parse_product_page(r.text, r.url, host)
    if rec:
        specs = crawler._specs(rec)
        log.info("DIAG PARSE %s PRODUCT name=%r sku=%r brand=%r cats=%r price=%r cur=%r images=%s specs=%d %s desc=%d files=%d",
                 url, rec["name"], rec["sku"], rec["brand"], rec["categories"], rec["price"], rec["currency"],
                 rec["image_count"], len(specs), list(specs.items())[:4], len(rec["description"]),
                 len(crawler._split(rec["files"])))
        return
    recs = crawler.extract_listing_records(r.text, r.url, host)
    log.info("DIAG PARSE %s LISTING %d items", url, len(recs))
    cats = {}
    for x in recs:
        cats[x["categories"]] = cats.get(x["categories"], 0) + 1
    log.info("DIAG PARSE %s categories=%s", url, json.dumps(cats, ensure_ascii=False)[:CHUNK])
    for x in recs[:3] + recs[-2:]:
        log.info("DIAG PARSE %s item name=%r price=%r text=%r cat=%r specs=%s", url, x["name"], x["price"],
                 x["price_text"], x["categories"], x["specs"][:200])


def start():
    urls = os.environ.get("DIAG_URLS", "").split()
    parse_urls = os.environ.get("DIAG_PARSE_URLS", "").split()
    if not urls and not parse_urls:
        return
    limit = int(os.environ.get("DIAG_HTML_LIMIT", "12000"))

    def run():
        for u in urls:
            diagnose(u, limit)
        for u in parse_urls:
            parse_check(u)
        log.info("DIAG done")

    threading.Thread(target=run, daemon=True, name="diag").start()
