// ==UserScript==
// @name         Caravan Torob price check
// @namespace    https://caravansanat.com
// @version      1.0
// @description  آرام‌آرام قیمت محصولات کاروان صنعت را در ترب نگاه می‌کند و نتیجه را به کراولر می‌فرستد. فقط در تب torob.com/#caravan-check کار می‌کند.
// @match        https://torob.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      crawler.caravansanat.com
// ==/UserScript==

(function () {
  "use strict";
  if (location.hash !== "#caravan-check") return;          // only the tab opened for this, never normal browsing

  const CRAWLER = GM_getValue("crawler", "https://crawler.caravansanat.com");
  let TOKEN = GM_getValue("token", "");
  if (!TOKEN) {
    TOKEN = (prompt("کلید ترب را از کراولر بگذارید (crawler.caravansanat.com/api/torob/token):") || "").trim();
    if (!TOKEN) return;
    GM_setValue("token", TOKEN);
  }
  const MIN_WAIT = 60, MAX_WAIT = 150;                     // seconds between products: slow on purpose

  const box = document.createElement("div");
  box.style.cssText = "position:fixed;bottom:12px;left:12px;z-index:99999;background:#111;color:#eee;" +
    "font:13px Tahoma,sans-serif;padding:10px 14px;border-radius:8px;max-width:360px;direction:rtl;opacity:.92";
  document.body.appendChild(box);
  const say = (t) => { box.textContent = "بررسی قیمت ترب: " + t; };
  let stopped = false;
  box.onclick = () => { stopped = !stopped; say(stopped ? "متوقف شد (برای ادامه کلیک کنید)" : "ادامه…"); if (!stopped) loop(); };

  function crawler(method, path, body) {
    return new Promise((ok, fail) => GM_xmlhttpRequest({
      method, url: CRAWLER + path, data: body ? JSON.stringify(body) : undefined,
      headers: { "Content-Type": "application/json", "X-Torob-Token": TOKEN },
      onload: (r) => {
        if (r.status === 401) { GM_setValue("token", ""); fail(new Error("کلید نادرست است؛ صفحه را دوباره باز کنید")); return; }
        try { ok(JSON.parse(r.responseText)); } catch (e) { fail(e); }
      },
      onerror: () => fail(new Error("کراولر در دسترس نیست")),
    }));
  }

  async function torob(path, params) {
    const u = new URL("https://api.torob.com/v4/base-product/" + path);
    Object.entries(params).forEach(([k, v]) => u.searchParams.set(k, v));
    const r = await fetch(u, { credentials: "include" });
    if (r.status === 429 || r.status === 403) throw new Error("ترب فعلاً جواب نمی‌دهد (" + r.status + ")");
    return r.json();
  }

  const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
  const jitter = () => MIN_WAIT + Math.random() * (MAX_WAIT - MIN_WAIT);

  async function one() {
    const job = await crawler("GET", "/api/torob/next");
    if (job.wait) { say((job.why || "صبر") + " — ادامه بعد از " + Math.round(job.wait / 60) + " دقیقه"); return job.wait; }
    say("جست‌وجو: " + job.q);
    const s = await torob("search/", { q: job.q, page: 0, size: 8 });
    const items = (s.results || []).map((x) => ({ name1: x.name1 || x.name || "", price: x.price, random_key: x.random_key }));
    let res = await crawler("POST", "/api/torob/result", { id: job.id, items });
    if (res.details) {
      await sleep(8 + Math.random() * 10);                 // the second request is spaced too
      const d = await torob("details/", { prk: res.details });
      const offers = ((d.products_info || {}).result || []).map((o) => ({
        shop: o.shop_text || o.shop_name || "", price: o.price, available: o.availability !== false,
      }));
      res = await crawler("POST", "/api/torob/result", { id: job.id, prk: res.details, torob_name: res.torob_name, offers });
    }
    say((res.verdict || "انجام شد") + " — " + job.q.slice(0, 40));
    return jitter();
  }

  async function loop() {
    while (!stopped) {
      let wait;
      try { wait = await one(); } catch (e) { say("خطا: " + e.message + " — ۱۰ دقیقه بعد دوباره"); wait = 600; }
      for (let left = Math.round(wait); left > 0 && !stopped; left -= 5) {
        if (left % 30 === 0) box.title = "بعدی تا " + left + " ثانیه";
        await sleep(5);
      }
    }
  }
  say("شروع…");
  loop();
})();
