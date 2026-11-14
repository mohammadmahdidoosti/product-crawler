<?php
// (14 Nov, owner) Make the keyword categories in one go on the shop server: parents first, an existing one with
// the same name under the same parent is reused, the category tree is rebuilt once at the end (not per category).
wp_defer_term_counting(true);
wp_suspend_cache_invalidation(true);
$in = fopen('/tmp/bulkcats.csv', 'r'); fgetcsv($in);
$out = fopen('/tmp/bulkcats_out.csv', 'w'); fputcsv($out, ['ID', 'wp_id']);
$map = []; $made = 0; $reused = 0; $err = 0; $t0 = microtime(true);
while (($r = fgetcsv($in)) !== false) {
    list($tid, $name, $ptid, $pwp) = $r;
    $parent = $pwp !== '' ? (int)$pwp : ($map[$ptid] ?? null);
    if ($parent === null) { $err++; fwrite(STDERR, "NOPARENT $tid $name\n"); continue; }
    $ex = term_exists($name, 'product_cat', $parent);
    if ($ex) { $id = (int)$ex['term_id']; $reused++; }
    else {
        $res = wp_insert_term($name, 'product_cat', ['parent' => $parent]);
        if (is_wp_error($res)) {
            if ($res->get_error_code() === 'term_exists' && $res->get_error_data()) { $id = (int)$res->get_error_data(); $reused++; }
            else { $err++; fwrite(STDERR, "ERR $tid $name " . $res->get_error_message() . "\n"); continue; }
        } else { $id = (int)$res['term_id']; $made++; }
    }
    $map[$tid] = $id;
    fputcsv($out, [$tid, $id]);
    if (($made + $reused) % 500 == 0) { echo "progress ", $made + $reused, " ", round(microtime(true) - $t0), "s\n"; }
}
fclose($out);
wp_suspend_cache_invalidation(false);
wp_defer_term_counting(false);
delete_option('product_cat_children');
_get_term_hierarchy('product_cat');
wp_cache_flush();
echo "DONE made $made reused $reused errors $err in ", round(microtime(true) - $t0), "s\n";
