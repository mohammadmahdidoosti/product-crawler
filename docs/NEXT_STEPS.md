
## After batch 50 (owner, 8 Nov 2026)
When every batch up to 50 (and the unseen products 11-50) is done and published, before the next 50 batches:
1. Shop host (DirectAdmin, server192e.irwebspace.com): inodes were 494k / 768k on 8 Nov. Images are ~320k of it
   (53.8k images x 6 files). Check and clean: wp-content/old-plugins-to-delete, upgrade-temp-backup,
   webibackup-backups, ~/backups, webtoffee_export, store-exporter, wflogs, ssbhesabfa-*.txt; consider dropping
   the medium_large (768) size (~50k inodes, needs metadata regeneration); delete drafts that are not published.
   Estimate the inodes the next 50 batches need (~5-6k per batch) and decide: upgrade the host or move the shop
   to its own VPS (not the crawler's).
2. Then prepare and start the crawl of the next 50 batches.
