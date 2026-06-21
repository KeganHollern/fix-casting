"""Ad/tracker blocking for the captured Chrome tab.

`@ghostery/adblocker-playwright` is a Node library and can't bind to our
Playwright *Python* browser, so we use the same family of engine it does:
`adblock` (Brave's adblock-rust), driven by uBlock Origin's default filter lists
plus EasyList/EasyPrivacy. Blocking is applied through Playwright's
`context.route()` (network) and per-navigation CSS injection (cosmetic), which is
exactly what the Ghostery library does under the hood.
"""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path

try:
    from adblock import Engine, FilterSet
except ImportError:  # adblock is optional; degrade to no blocking if absent.
    Engine = None  # type: ignore[assignment]
    FilterSet = None  # type: ignore[assignment]


# uBlock Origin's default enabled lists (from uAssets) + EasyList/EasyPrivacy +
# Peter Lowe's — i.e. roughly what uBO ships enabled out of the box.
_UBO = "https://raw.githubusercontent.com/uBlockOrigin/uAssets/master/filters/"
DEFAULT_FILTER_URLS = [
    _UBO + "filters.txt",
    _UBO + "badware.txt",
    _UBO + "privacy.txt",
    _UBO + "quick-fixes.txt",
    _UBO + "unbreak.txt",
    _UBO + "resource-abuse.txt",
    "https://easylist.to/easylist/easylist.txt",
    "https://easylist.to/easylist/easyprivacy.txt",
    "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=adblockplus&showintro=0&mimetype=plaintext",
]

CACHE_DIR = Path.home() / ".cache" / "fix-casting" / "adblock"
_CACHE_TTL_S = 24 * 3600
_FETCH_TIMEOUT_S = 20

# Playwright resource_type -> adblock-rust request type.
_RESOURCE_TYPE = {
    "document": "document",
    "stylesheet": "stylesheet",
    "image": "image",
    "media": "media",
    "font": "font",
    "script": "script",
    "xhr": "xmlhttprequest",
    "fetch": "xmlhttprequest",
    "websocket": "websocket",
    "ping": "ping",
    "manifest": "other",
    "texttrack": "other",
    "eventsource": "other",
    "other": "other",
}


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


def build_engine(urls: list[str] | None = None) -> Engine | None:
    """Build an adblock-rust engine from the default uBO + EasyList lists.
    Returns None if the package is missing or no list could be loaded."""
    if Engine is None or FilterSet is None:
        print(
            "[adblock] 'adblock' package not installed — ad blocking disabled. "
            "Install it with: pip install adblock",
            flush=True,
        )
        return None
    filter_set = FilterSet()
    loaded = 0
    for url in urls or DEFAULT_FILTER_URLS:
        text = _cached_list(url)
        if text:
            filter_set.add_filter_list(text)
            loaded += 1
    if not loaded:
        print("[adblock] no filter lists available — ad blocking disabled.", flush=True)
        return None
    engine = Engine(filter_set)
    print(f"[adblock] loaded {loaded} filter lists.", flush=True)
    return engine


def attach_to_context(context, engine: Engine) -> None:
    """Block ad/tracker network requests and inject cosmetic hide rules on the
    Playwright context. Safe to call once right after the context is created."""
    if engine is None:
        return

    def _route(route):
        req = route.request
        try:
            rtype = _RESOURCE_TYPE.get(req.resource_type, "other")
            source = (req.frame.url if req.frame else "") or req.url
            result = engine.check_network_urls(req.url, source, rtype)
            if result.matched:
                route.abort()
                return
        except Exception:
            pass  # never let the blocker break page loads
        route.continue_()

    context.route("**/*", _route)

    # Cosmetic filtering: on each navigation, hide the site-specific ad
    # selectors the engine knows for that URL (the network block handles the
    # rest). Best-effort; a failure here must never break the page.
    def _inject_cosmetic(frame):
        try:
            if frame.parent_frame is not None:
                return  # main frame only
            cos = engine.url_cosmetic_resources(frame.url)
            selectors = list(cos.hide_selectors) + list(cos.style_selectors)
            if selectors:
                css = ",".join(selectors) + "{display:none!important}"
                frame.add_style_tag(content=css)
            if cos.injected_script:
                frame.evaluate(cos.injected_script)
        except Exception:
            pass

    context.on("framenavigated", _inject_cosmetic)
