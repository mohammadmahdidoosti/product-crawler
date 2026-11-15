<?php
/**
 * (15 Nov) The shop side of the SSH bridge: the crawler's REST calls run inside ONE WordPress process
 * on the shop server (WP-CLI), not as HTTP requests through nginx and php-fpm. The calls are the same
 * REST calls (rest_do_request), so products come out exactly as from the REST API: the same checks,
 * the same hooks, the same plugins - only the way in is different, and the site's visitors' PHP
 * workers and memory are not used for it.
 *
 * Run (on the shop server, by bridge_proxy.py on the crawler host over ssh):
 *   cd /home/caravan/htdocs/caravansanat.com && sudo -u caravan wp eval-file shop_bridge.php --user=caravancrawler
 *
 * Protocol, one JSON object per line both ways:
 *   in:  {"api": "wc"|"wp", "method": "GET", "path": "products", "params": {...}, "json": {...},
 *         "body_b64": "...", "headers": {...}}
 *   out: "@@R " + {"status": 201, "headers": {...}, "body": <the REST response data>}
 * Anything a plugin prints is dropped; only lines starting with "@@R " are answers.
 */
if (!defined('ABSPATH')) {
    fwrite(STDERR, "run with wp eval-file\n");
    exit(1);
}
if (!get_current_user_id()) {
    fwrite(STDERR, "no user: run with --user=<the crawler's user>\n");
    exit(1);
}
@ini_set('display_errors', '0');                 // notices must not land between the answers
rest_get_server();                                // registers the REST routes (rest_api_init)
fwrite(STDOUT, "@@R " . json_encode(['status' => 200, 'ready' => true, 'user' => get_current_user_id()]) . "\n");

$served = 0;
while (($line = fgets(STDIN)) !== false) {
    $line = trim($line);
    if ($line === '') {
        continue;
    }
    if ($line === 'QUIT') {
        break;
    }
    ob_start();
    $out = ['status' => 500, 'headers' => [], 'body' => null];
    try {
        $q = json_decode($line, true);
        if (!is_array($q)) {
            throw new Exception('bad request line');
        }
        $ns = ($q['api'] ?? 'wc') === 'wp' ? '/wp/v2/' : '/wc/v3/';
        $req = new WP_REST_Request(strtoupper($q['method'] ?? 'GET'), $ns . ltrim($q['path'] ?? '', '/'));
        if (!empty($q['params'])) {
            $req->set_query_params($q['params']);
        }
        foreach (($q['headers'] ?? []) as $k => $v) {
            $req->set_header($k, $v);
        }
        if (isset($q['json'])) {
            $req->set_header('Content-Type', 'application/json');
            $req->set_body(json_encode($q['json']));     // read as the JSON body, as over HTTP
        } elseif (isset($q['body_b64'])) {
            $req->set_body(base64_decode($q['body_b64']));
        }
        $res = rest_do_request($req);
        $server = rest_get_server();
        $out['status'] = $res->get_status();
        $out['headers'] = $res->get_headers();
        $out['body'] = $server->response_to_data($res, false);
    } catch (Throwable $e) {
        $out['body'] = ['code' => 'bridge_error', 'message' => $e->getMessage()];
    }
    ob_end_clean();
    fwrite(STDOUT, "@@R " . json_encode($out, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE) . "\n");
    fflush(STDOUT);
    $served++;
    if (function_exists('wp_cache_flush_runtime')) {     // the in-process cache would grow with every product
        wp_cache_flush_runtime();
    }
    if ($served % 50 === 0) {
        gc_collect_cycles();
    }
}
