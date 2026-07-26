"""Unit tests for filter-list parsing (no network: _cached_list is stubbed)."""

import cast_tab.adblocking as ab

FIXTURE = """\
! comment line
[Adblock Plus 2.0]
||ads.example.com^
||tracker.example.net^$third-party
||scoped.example.org^$domain=onlyhere.com
@@||allowed.example.com^
example.com##.ad-banner
##generic-hide
0.0.0.0 hostfile.example.com
127.0.0.1 localhost.example.net
not a rule at all
||UPPER.EXAMPLE.COM^
"""


def test_build_block_patterns_extracts_blanket_domains_only(monkeypatch):
    monkeypatch.setattr(ab, "_cached_list", lambda url: FIXTURE)
    patterns = ab.build_block_patterns(["http://one.test/list.txt"])
    domains = {p for p in patterns}
    # Blanket domain rules and host-file rules are included (two patterns each).
    assert "*://ads.example.com/*" in domains
    assert "*://*.ads.example.com/*" in domains
    assert "*://tracker.example.net/*" in domains  # non-scoping option kept
    assert "*://hostfile.example.com/*" in domains
    assert "*://localhost.example.net/*" in domains
    # Exceptions, element hiding, and site-scoped rules are skipped.
    assert not any("allowed.example.com" in p for p in patterns)
    assert not any("scoped.example.org" in p for p in patterns)
    assert not any("ad-banner" in p for p in patterns)
    # Input is lowercased before matching.
    assert "*://upper.example.com/*" in domains


def test_build_block_patterns_empty_when_no_lists(monkeypatch):
    monkeypatch.setattr(ab, "_cached_list", lambda url: None)
    assert ab.build_block_patterns(["http://one.test/list.txt"]) == []
