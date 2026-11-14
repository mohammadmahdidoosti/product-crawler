## 14 Nov 2026 — published; lessons now in code (cda3517, deployed 06:50 UTC)
- Published 16,985 new products (batches 51-100, top-30 placed, 18 Zimberg; 4 parallel set_status, 0 failed);
  119 new without a photo back to draft; then 82 held drafts/pending WITH a photo published (owner), 4 with absurd
  prices kept (Dana Kod Negar lasers/CIJ 5-8 billion toman, a check valve: toman stored as USD — fix later).
  Left: ~1,100 products without a photo (price-list creations of 4/8 Nov, pending of 10/11 Nov) -> photo crawl last.
- Active applied (owner: catalog priority): 44 prices, 29 out of stock (all 25 not in the catalog); 32 big/x10
  changes held (image-only catalog misread x8-10); 299 new Active models not made until their prices are sure.
- Zimberg: only 18 of the 51 '-new' products were catalog models -> sent at catalog prices; 78 more queued in
  pl-zimberg-1405-08-new2 (LLM categories), not crawled yet.
- In code now: 3 send lanes in the app (publish/status of batch-*/pl-*-new); tools/batch_lanes.py (shared queue,
  in-process prepare+send with retries, N send slots, no re-crawl/top-up of crawled batches, failed/ with reason,
  cost since start); set_status 4 requests side by side + never publishes above publish.max_publish_toman (2e9);
  BRANDS_LOCK across processes; costs.summary(since=); pricelist.text_check (PDF text beats the picture reading);
  queue_new LLM category fallback; lock_new_to_list at export of a -new run.
- Cost: take it from the OpenRouter balance (owner: ~$6 for the ~900 drafts of 13 Nov), never from summed
  lifetime 'usd' of batch logs.

## 13 Nov 2026 — start here on 14 Nov (owner: first publish the drafts, then new batches)
- Top 30 batches (placed products into new / near-empty categories, plf.py kept only sites with <10 products):
  27 sent as drafts (847 created, 169 updated); 60, 52, 100 were never sent (their lane's sender thread died on an
  app timeout after images+texts) -> tmp/pp.py prepares + sends them (pp-60/52/100.log). Cost ~$33 (images+texts
  of 6 batches paid twice after the 15:40 restart from 4 to 6 lanes). Drafts of batches 51-100 + these: publish first.
- Speed: the app's single site lane was the bottleneck. tmp/i25.py now sends directly via publish() holding one of
  3 slot files (/data/tmp/sendslot-N) instead of the site lane (backup i25.py.bak-direct); tmp/q25.sh = shared queue
  lanes. 6 lanes OOM'd once with 1 GB swap; crawler host got /swapfile2 (8 GB, in fstab). CPU (8 cores) and the
  app's 6 prep lanes are the next limits; smaller batches avoid a long tail.
- Live changes (owner): Crown CT15205 #107675 = 41,400,000 (prices are stored in USD: regular price = toman / rate
  273,000, display rounds down to 1,000); out of stock: Endress+Hauser 34, Minco 4, Sens 40, AWLOP 7 (backup
  /tmp/ap1_backup_*.csv on the shop); Saba INV-250 #190437 -> 74.1M (abzarsara; other Saba untouched, owner);
  Zimberg #100076 39.95M, #218731 22.95M, #102800 17.95M (catalog).
- Zimberg catalog (pl-zimberg-1405-08): the vision read had 60 of 151 prices wrong (x10 both ways, digit slips);
  tmp/zv.py re-read every price from the PDF text (all pages rial) — items.csv fixed (backup .bak-zv). 4 models not
  in the text skipped. New: pl-zimberg-1405-08-new (66, crawl started 16:05 UTC) and -new2 (78, LLM categories,
  tmp/zc.py, not started). Owner wants them built with photos and PUBLISHED.
- Active (pl-active-1405-08): 123 image pages, 377 rows (134 corrected by the verify pass). plz.py still matching.
  Owner: catalog has priority — catalog products updated + in stock (>50% changes checked first), every other
  'اکتیو' product (term 15788, 87) out of stock, new models crawled with photos. Not applied yet.
- Lesson: a catalog read by vision must be checked against the PDF text when the PDF has text (pdfplumber).


## 11 Nov 2026 (morning) — done
- Batches 1-50 published (tmp/pub11.py: 28,347 products, 0 failed; doubtful price / no category stay draft).
- Shop names as brands (ابزار ماتریس, فروشگاه فایر ایران, ...): 52 products fixed (tmp/snb.py, brandtools.shop_not_brand).
  37 more brand terms equal a seller name in the crawl data (JBH, محک, شیوا امواج, ...): most are real makers, not touched.
- Flex (#15777) out of stock, 8 products. Kenzax doubtful photos: all approved and published (owner).
- Hyundai 36V drill #101438: photo without the shop's flag, price 9,350,000 locked (Torob same kit).
- Supplier sites as price lists (pricelist.site_as_list, tmp/sal.py -> sal2.py -> sal3.py; owner: current prices):
  Noyan/Morayco + Black Panther (dashtesabz-shop.ir), matinfidar.ir, imen-tamin.ir, geokala.com (prices are TOMAN,
  the parser said IRR: fixed in site/products.jsonl), eramardin.ir. No redirects, no brand-wide out of stock, no spread.
  Rows matched to another brand's product were made as new. Still for the owner: same brand, |change| > 50%,
  in pricelists/pl-*-site/held_owner2.csv (imentamin 26, eramardin 23, matinfidar 8).
  Geokala D510 (#142192 -> 100M) and DW088K (#109143 -> 8M) were applied (guard missed them); old values in backup.jsonl.
- Crawler redeployed with 2eabcfe.
- Batches 51-100: routing probed and approved by the owner (11 Nov 11:53); crawl started 08:24 UTC in three lanes
  (tmp/c3.py: 51-67, 68-84, 85-100; a lane stops on a failed batch or a crawl over $1).
  Owner (12:03): watch the costs. Promised: prepare (photos + texts) at most 2 batches at a time, a batch whose
  prepare passes $2 is stopped and looked at, and the OpenRouter balance is reported every few hours
  (balance 11 Nov 08:00 UTC: $279 credits, $143.4 used).
  Prepare + send: tmp/i22.py, two lanes (waits for 'C3 batch N ok', stops a lane over $2). Sent as drafts.
  Owner (13:44): publish batches 51-100 together only when all 50 are done. Real cost so far $0.37-0.62/batch.
- Shop VPS (11 Nov 08:48 UTC, owner «بلاکش کن»): PerplexityBot made 2,008 of 3,000 requests (load 11). Blocked:
  nginx `if ($http_user_agent ~* "PerplexityBot|Perplexity-User") { return 403; }` in the caravansanat.com 443 server
  block (backup /root/mig/caravansanat.com.conf.bak-20261111; a CloudPanel vhost save may drop it) + Disallow in
  robots.txt (backup /root/mig/robots.txt.bak-20261111). Load back to ~2.
- Price lists read with the wrong unit (11 Nov): PAP (pl-pap-1405-08) pages of toman were read as rial -> 40 products
  x10 (tmp/pap10.py, owner OK), DH-810 x3 set to 17.9M + out of stock, 2 other-brand products restored, index fixed.
  PM/Anchor: 71 prices applied (37 as read + 34 nearest of list/list x10 within 35%), 14 new (Torob ok/x10), its index
  entries removed; 17 updates + ~50 new held. Mahak: 6 x10 applied. Open: Tosan 3 doubtful (100115, 149539, 35877).
  Lesson: a list whose read.json shows mixed units needs the per-page unit checked before apply.
- Owner (11 Nov 11:42): kalasanati.com goes on the promising-shops list for a dedicated full crawl later (its site is
  larger than the 1,500-page cap of a price-list site read; the Salem Ab dosing pump SMA-D-1 was not reached).
  Also from 11 Nov: abzarfarshid.com (Silver dealer; robots.txt blocks crawlers, only its Store API search was read).

## Sends done 10 Nov 2026 — start here on 11 Nov
- All 50 batches sent (last: batch 4 at 16:08 UTC, created 171, updated 64, error 0). Batches 8 and 9 went through
  the app's own job (app log: created 44 / 29). Roughly 1,800 products created today, all drafts except Kenzax.
- Crawler redeployed at 16:12 UTC with 84e2031 (atomic write_csv), so the hot patch is now permanent.
- Owner's order for 11 Nov: first the small fixes, then plan the next 50 batches:
  1. 114 doubtful Kenzax photos (owner reviews on the site-fix page)
  2. Pooka CS9291 photo (owner sends a photo or link)
  3. Hyundai 36V drill (owner says which product)
  4. Noyan / Black Panther comparison against dashtesabz-shop.ir (approved)
  5. Eye-check of the 60 rows flagged x10 (approved)
  6. Publishing today's drafts: only when the owner says so
  Then: plan batches 51-100. Thursday 10:00 reminders are scheduled (backup, old host cleanup, test subdomain,
  server guide, DMARC, Netafraz E1 ticket).

## After batch 50 (owner, 8 Nov 2026)
When every batch up to 50 (and the unseen products 11-50) is done and published, before the next 50 batches:
1. Shop host (DirectAdmin, server192e.irwebspace.com): inodes were 494k / 768k on 8 Nov. Images are ~320k of it
   (53.8k images x 6 files). Check and clean: wp-content/old-plugins-to-delete, upgrade-temp-backup,
   webibackup-backups, ~/backups, webtoffee_export, store-exporter, wflogs, ssbhesabfa-*.txt; consider dropping
   the medium_large (768) size (~50k inodes, needs metadata regeneration); delete drafts that are not published.
   Estimate the inodes the next 50 batches need (~5-6k per batch) and decide: upgrade the host or move the shop
   to its own VPS (not the crawler's).
2. Then prepare and start the crawl of the next 50 batches.
3. Leftovers of batches 1-50 worth loading (counts on 8 Nov), before the next 50 batches:
   - 05_review: 2,028 products held (doubtful price / thin data) -> check and send as doubtful.
   - 03_failed: 23,021 pages that failed to load -> crawl once more.
   - 06_image_issues: 4,109 -> see which went without a good photo or were held.
   - 05_low_categories: 3,654 -> check categories; ~1,000 small groups still waiting for a category.
   - 04_rejected (212,850): sample-check the filter is not too strict. 05_not_selected (13,893): duplicates, skip.
   Remind the owner of all of this when batch 50 is published.

   Status 9 Nov:
   - 05_review: 1,730 unique, compared by the model with their site look-alike (tmp/review_cmp.csv): 1,452 different
     -> sent as new via tmp/i16.py; 259 same -> site price updated when within 40% (tmp/same259.py); 19 unclear.
   - 03_failed: really 5,440 unique pages. Sample of 60: 'blocked/none' ~50% real priced products -> retried by
     i16.py; 'no product data' and anti-bot 403/418 not worth it (owner agreed).
   - Small no-category groups (~1,066 products, groups of 1-2): two rounds of placing them in the closest existing
     category were ~50% wrong. Owner (9 Nov 11:29): leave them waiting; they get a category in the next 50 batches,
     when a group reaches 3+ products.
   - Next 50 batches: owner decides on 10 Nov after every unseen product of 1-50 is published.
   - Host space: after publishing ends; a fresh full backup outside the host first, nothing deleted without the owner.

## Shop move to its own VPS (owner, 9 Nov 2026)
- Bought ParsVDS IR_VPS_05 (4 cores ~2-2.5 GHz shared, 10 GB RAM, 80 GB NVMe, 500 GB traffic), 1 month, Ubuntu 24.04.
  Delivery up to 7 days. Upgrade keeps IP and data, only the price difference is paid.
- No backups from the provider: we set up our own daily backup (DB + uploads) off the VPS.
- Panel: owner chose CloudPanel (free, Nginx, Ubuntu 24.04) on 9 Nov; DirectAdmin dropped (3.2M + 698k/month, AlmaLinux + Apache only).
- Delivered 9 Nov 17:29 with national-only internet: international access needs the owner's CRA identity request
  (done 9 Nov, 24-48h+). Nothing is installed or moved before it works (Google, updates, our crawler need it).
  72h refund window ends ~12 Nov 17:30 Tehran: if still national-only by 11 Nov evening, ask the owner: wait or refund.
- Access: key /root/.ssh/shop_vps on the crawler host; owner adds its .pub to root's authorized_keys (SSH port 9011).
- Until the move: next 50 batches may be crawled/prepared, but sending waits for the new server (host inodes).
- Owner enters all passwords/payments himself.

### Shop VPS status 10 Nov
- International internet active (9 Nov evening). Key added via ssh-copy-id (owner typed the root password); crawler host alias `ssh shop`.
- CloudPanel 2.5.4 installed with DB_ENGINE=MARIADB_11.8 (MYSQL_8.4 needs repo.percona.com, blocked from Iran).
  Owner created the panel admin. ufw: 22, 80, 443, 8443, 9011.
- Site caravansanat.com created (PHP 8.3, WooCommerce vhost, site user caravan; its password only in /root/.caravan_site_pw on the VPS).
- Next: copy files + DB from the DirectAdmin host (needs SSH there or a DA full backup), test on the VPS via hosts file, then DNS (A record only; MX stays on the old host).
- 10 Nov 07:25 UTC (owner): sends to the old host stopped until the move. Runners i16 (leftovers 11-29, 43-50) and i17 (35,37,36,38,39)
  killed. Sent+published: 35, 37. Waiting to send on the new server: 36 (prepare started), 38, 39; leftovers from batch 21 on
  (i71 list 11-25: done up to 21 prepare) and from 43 on (i72 list 26-29,31,43-50: crawling 43); 1-10 leftovers not started.
  Kenzax creation (tmp/kz3.py) allowed to finish on the old host. Held for the owner: 8 big updates, 3 x10 updates, 2 'off'.
- Copy: DB dumped by cron PHP (~/migr/db.sql.gz, 134 MB, via mysqli; cron job '* * * * * php ~/migr/dump.php' must be deleted),
  files by lftp mirror on the VPS (tmux session 'mig', FTP account migr@caravansanat.com = temporary, delete after the move).
- 10 Nov: test copy live at https://test.caravansanat.com (DNS A test -> VPS, Let's Encrypt via certbot webroot,
  nginx /etc/nginx/sites-enabled/zz-test-caravan.conf proxies to Varnish 6081 with Host caravansanat.com + sub_filter, noindex).
  Delete the DNS record + that file + certbot cert on switch day.
- Copy method that works on this host (exec disabled, files stop at 2 GB): cron PHP ~/migr/dump.php (mysqli dump) and
  ~/migr/files.php (PHP tar writer, parts < 1.8 GB), then lftp from the VPS (owner types the FTP password), then
  /root/mig/restore.sh on the VPS. 501,806 files, 9.4 GB; tar in 5 min, download ~20 min, restore 5 min.
- Tuning on the VPS (re-apply after the final DB copy): /root/mig/tune.sh (MariaDB 4G pool, FPM pool 40 dynamic,
  LSCache object cache -> Redis in wp_options AND wp-content/.litespeed_conf.php), .user.ini prepend -> /home/caravan/.prepend.php
  (CloudPanel Varnish controller + Wordfence), memory 512M, no cPanel session path; old /home/caravan2 paths in .user.ini,
  wp-content/maintenance.php, wflogs/config-synced.php; plugin clp-varnish-cache active; Varnish TTL 600 s, excludes cart/checkout/
  my-account/wp-admin/wc-api/wp-json. DISABLE_WP_CRON is already true in wp-config (the host runs wp-cron by wget cron):
  on the VPS add a CloudPanel cron for wp-cron + caravan_core_rec_cron after the switch.
- Load test (on the VPS): cached pages 307 req/s at 50 concurrent, 0 failures; uncached PHP ~13-15 req/s, 40 concurrent ->
  2.7 s mean, 0 failures. CPU (4 x Broadwell 2 GHz) is the limit for uncached work: keep crawler sends to ~6-8 concurrent.
- Backup: crawler host cron 00:30 UTC /root/shop_backup.sh -> /srv/shop-backup (DB 7 days + rsync of the site files).

### Switch done 10 Nov (09:46-10:25 UTC)
- Old host in maintenance (public_html/.maintenance, $upgrading=2000000000) — keep it a day or two so stale-DNS visitors
  cannot order there; then remove. Final copy: ~/migr/final.php (cron deleted) -> VPS /root/mig/final, /root/mig/final_restore.sh
  (+ post.sh, which now flushes Redis FIRST: a stale Redis object cache made wp-cli think plugins were active).
- DNS A caravansanat.com + www -> 87.107.104.175 (TTL 300). MX/mail/ftp/pop/smtp stay on 89.42.44.95.
- SSL: clpctl lets-encrypt (valid to Feb 2027, CloudPanel renews). Site crons: crontab of user caravan (/root/mig/site.cron).
- Mail: WP Mail SMTP via mail.caravansanat.com:465 as info@ (owner typed the password). The old exim refuses
  HELO caravansanat.com from the VPS IP -> mu-plugin wp-content/mu-plugins/caravan-smtp-helo.php sets HELO to the VPS PTR name.
- ionCube: do NOT install. pw-bulk-edit is an unlicensed copy; with the loader on, it fatals every admin-ajax call
  (add to cart broke for ~7 min). Removed again; plugin stays self-disabled as on the old host.
- Sends resumed 10:27 UTC, settings.json publish.workers = 8 (backup settings.json.bak-20261110):
  tmp/i73.log = i17 SEND_FIRST 36,38,39,22,23,24 + batch 25; tmp/i74.log = i16 SEND_FIRST 26-29,31,43-45 + batches 46-50.
  Measured: prepare of batch 36 took 7 min; the site send is CPU-bound (load 7-8 on 4 cores, image subsizes).
  Owner approved: mu-plugin caravan-image-sizes.php drops the 1536/2048 subsizes of new uploads (theme never uses them;
  delete the file to undo). Keep workers at 8. Idea for the next 50 batches: server-side importer over SSH
  (pre-resized images, 100 at a time, nice) — about a day of work, after the current batches. Leftovers of 1-10 not started.
  Measured batch 36 (189 products, 37 min): prepare 7, photo check 7, spec+brand list 9, per-product checks ~10,
  the site upload itself only ~5 min -> an SSH importer would save little. The time went to Python loops:
  pricelist.list_price (one regex per 5,207 keys per product) and brands_fa.from_name (6,623 brands, key() and a
  new regex each time). Fixed in 0fabce2 (substring pre-tests, cached keys/regex, one site-brand index):
  0.2 s vs 246 s and 11 s vs 338 s on real names, identical answers (tools/lp_speed_check.py, brand_speed_check.py).
  Deployed 11:29 UTC; runners again: tmp/i75.log (i17 SEND_FIRST 38,39,22,23,24,25), tmp/i76.log (i16 SEND_FIRST
  26-29,31,43-46 + images 47-50). robots.txt: ClaudeBot Crawl-delay 5 (owner; it was most of the VPS load).
  11:46 UTC (owner: faster, several at a time): i17 runner replaced by two tmp/i18.py runners (i17 + PUB_WORKERS
  -> publish opts workers=10): tmp/i77.log SEND_FIRST 38,22,24 and tmp/i78.log SEND_FIRST 39,23,25; i16 (i76.log) keeps
  27-29,31,43-46 + images 47-50. Three prepares side by side (all three prep lanes), sends one at a time (site lane).
  Note: settings.json has no 'publish' section merge (app.build_cfg) -> publish.workers there is ignored; restored.
- Still to do: remove FTP account migr + ~/migr on the old host, test subdomain (DNS + nginx file + cert) after a few days,
  check the backup run (systemd unit shopbackup-now), Persian server guide for the owner, DMARC record.

## 12 Nov 2026 (owner approved items 2 and 4 of the Thursday list)
- test.caravansanat.com removed on the shop VPS: nginx zz-test-caravan.conf (copy /root/mig/zz-test-caravan.conf.bak-20261112)
  + certbot cert deleted, nginx reloaded, main site 200. The owner removes the DNS record "test" himself.
- Persian server guide for the owner: Docs artifact https://claude.ai/artifact/PnUrfcWyXGJJ7evnVrDVre
- Backup check: DB dump nightly OK (146M); files rsync takes ~8 h (500k files) and logs FILES FAILED on rsync code 24
  (vanished files) -> proposed to treat 24 as ok (waiting for the owner).
- Batches 51-100: all crawled by 22:00 UTC 11 Nov; 27 sent (7,987 drafts) by 04:45 UTC. Lane 1 hung on batch 56
  (100% CPU, no output 2.5 h) -> killed; owner asked for more speed: 4 prepare lanes (i23-2 94-100, i23-3 56-60,
  i23-4 61-65, i23-5 66 67 90 91 92).
- PM/Anchor 11 Nov: pack chisels priced per pack (piece x count, owner OK); 26 unconfirmed new products back to
  pending; PM run removed from the price index (apply() re-indexes the whole run: drop it after any apply on this run).
- Pooka CS9291 (#156824): Arva photos removed, pending; no right photo found on the web.
- Old host (DirectAdmin server192e.irwebspace.com, Netafraz) cleaned 12 Nov with owner OKs: migr, backups, old WP
  public_html, DBs 9FPGo1pd + akhavan (Woodmart demo) + carvandrive, FTP migr/tmpdrive, DNS test. Left: e-mail, DNS
  (DNS zone lives here: never cancel this host), .well-known. Owner downgrades it to plan E1 (ticket via Netafraz
  client area; DirectAdmin ticket system is off).
- Nextcloud drive.caravansanat.com (sales invoices, uploaded by hand; 2 users, 424 files, v32.0.6) moved to the shop
  VPS: CloudPanel site user caravandrive, /home/caravandrive/htdocs/drive.caravansanat.com, DB caravandrive
  (/root/.my.cnf.drive), cron */5 as caravandrive, Let's Encrypt via clpctl. DNS drive -> 87.107.104.175 (TTL 300).
  Full old-host DirectAdmin backup kept on the shop VPS: /root/mig/drive/backup-Nov-12-2026-1.tar.gz (+ x/).
  shop_backup.sh (crawler host) now also dumps caravandrive + mirrors its files to /srv/shop-backup/drive.
- Backups trimmed (owner): crawler keeps only the newest shop dump; Drive vps-backups/caravansanat-db keeps only the
  newest (caravan-db-pull.sh removes the local pulled copy after upload). Backups of scripts: *.bak-20261112*.
- Waiting for the owner's go (12 Nov 10:11, "when I tell you"): dedicated crawl of the promising-shops list, incl.
  kalasanati.com (Saleem SMA-D-1 dosing pump) and abzarfarshid.com. Do NOT start on our own; remind after batches 51-100.
