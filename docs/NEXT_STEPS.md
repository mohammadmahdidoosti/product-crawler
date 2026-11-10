
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
