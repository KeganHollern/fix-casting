"""Ad/tracker blocking for the captured Chrome tab — native, via CDP.

`@ghostery/adblocker-playwright` is a Node library and can't bind to our
Playwright *Python* browser. An earlier version used Playwright `context.route`
with an adblock-rust engine, but that round-trips *every* network request
through Python — on a live HLS stream (many requests/sec) that steals CPU from
ffmpeg and stutters the video.

So block natively instead: derive an ad/tracker *domain* list from uBlock
Origin's network lists + Peter Lowe's server list, and hand it to Chrome via CDP
`Network.setBlockedURLs`. Chrome blocks matching requests in-process with no
per-request Python work. We only take the safe blanket-domain rules (`||domain^`,
host-file entries), skipping exceptions, element-hiding, regex and site-scoped
(`$domain=`) rules — so it won't wrongly block a needed resource (verified:
legit CDNs stay allowed).
"""

from __future__ import annotations

import re
import time
import urllib.request
from pathlib import Path

# uBlock Origin's default network lists + Peter Lowe's ad/tracking-server list.
# Deliberately NOT the full EasyList/EasyPrivacy: those carry ~50k blanket
# domains, and setBlockedURLs matching cost scales with pattern count (~+1s per
# page vs ~+0.1s here) — enough to contend for CPU and stutter the encoder. This
# focused set (~6.5k domains) blocks the major ad/tracker servers with
# negligible per-request overhead. Measured: blocks googlesyndication, doublick,
# analytics, GTM, scorecard, taboola; leaves Cloudflare/jsDelivr/Fonts/YT alone.
_UBO = "https://raw.githubusercontent.com/uBlockOrigin/uAssets/master/filters/"
DEFAULT_FILTER_URLS = [
    _UBO + "filters.txt",
    _UBO + "badware.txt",
    _UBO + "privacy.txt",
    _UBO + "quick-fixes.txt",
    _UBO + "unbreak.txt",
    _UBO + "resource-abuse.txt",
    "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=adblockplus&showintro=0&mimetype=plaintext",
]

CACHE_DIR = Path.home() / ".cache" / "fix-casting" / "adblock"
_CACHE_TTL_S = 24 * 3600
_FETCH_TIMEOUT_S = 20

# Blanket domain blocks only: ||domain^ optionally with non-scoping options (no
# '=', so no $domain=site rules), and 0.0.0.0/127.0.0.1 host-file lines. Anything
# with @@/##/#@#/regex/$domain= is skipped by the callers below.
_DOMAIN_RULE = re.compile(r"^\|\|([a-z0-9.-]+\.[a-z]{2,})\^?(\$[^=]*)?$")
_HOST_RULE = re.compile(r"^(?:0\.0\.0\.0|127\.0\.0\.1)\s+([a-z0-9.-]+\.[a-z]{2,})\s*$")


def _cached_list(url: str) -> str | None:
    """Fetch a filter list, caching to disk with a 1-day TTL. Falls back to a
    stale cache on network failure; returns None only if we have nothing."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in url)[-150:]
    path = CACHE_DIR / f"{safe}.txt"
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < _CACHE_TTL_S
    if fresh:
        return path.read_text(encoding="utf-8", errors="ignore")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fix-casting-adblock"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        path.write_text(text, encoding="utf-8")
        return text
    except Exception as exc:
        if path.exists():
            print(f"[adblock] using cached {url} (refresh failed: {exc})", flush=True)
            return path.read_text(encoding="utf-8", errors="ignore")
        print(f"[adblock] skipping {url}: {exc}", flush=True)
        return None


def build_block_patterns(urls: list[str] | None = None) -> list[str]:
    """Build CDP Network.setBlockedURLs patterns from the default uBO + EasyList
    domain rules. Returns [] if no list could be loaded."""
    domains: set[str] = set()
    loaded = 0
    for url in urls or DEFAULT_FILTER_URLS:
        text = _cached_list(url)
        if not text:
            continue
        loaded += 1
        for line in text.splitlines():
            s = line.strip().lower()
            if not s or s[0] in "!#[" or s.startswith("@@") or "##" in s or "#@#" in s:
                continue
            m = _DOMAIN_RULE.match(s) or _HOST_RULE.match(s)
            if m:
                domains.add(m.group(1))
    if not domains:
        print("[adblock] no filter domains loaded — ad blocking disabled.", flush=True)
        return []
    patterns: list[str] = []
    for d in domains:
        patterns.append(f"*://*.{d}/*")
        patterns.append(f"*://{d}/*")
    print(
        f"[adblock] {len(domains)} ad/tracker domains from {loaded} lists "
        f"(native CDP blocking).",
        flush=True,
    )
    return patterns


def apply_to_page(cdp_session, patterns: list[str]) -> None:
    """Block the ad/tracker patterns on a page's CDP session. Best-effort: a
    failure here must never break the page load."""
    if not patterns:
        return
    try:
        cdp_session.send("Network.enable")
        cdp_session.send("Network.setBlockedURLs", {"urls": patterns})
    except Exception as exc:
        print(f"[adblock] failed to apply CDP blocklist: {exc}", flush=True)
