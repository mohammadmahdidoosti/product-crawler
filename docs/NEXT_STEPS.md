
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
