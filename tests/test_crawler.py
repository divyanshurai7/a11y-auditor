"""Tests for auditor.crawler, run against the local fixture site only."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter

import pytest

from auditor import crawler, db
from auditor.audit import AuditError, Auditor, AuditResult
from auditor.crawler import (
    PageError,
    clean_url,
    crawl,
    is_page_link,
    normalize_url,
    same_site,
    site_domain,
)

# Visit order for a full crawl of tests/fixtures/site/ starting at index.html.
# Breadth-first, links in page order. Not visited: report.pdf (a file),
# private/secret.html (robots.txt), the external link, and mailto:/tel:/javascript:.
EXPECTED_PATHS = [
    "/index.html",
    "/about.html",
    "/products/",
    "/missing.html",   # broken link -> recorded as an error
    "/contact.html",   # only linked by JavaScript on /products/
]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record crawler waits instead of sleeping (the fixture robots.txt sets
    Crawl-delay: 1, which would otherwise slow every test down)."""
    calls: list[float] = []
    monkeypatch.setattr(crawler, "_sleep", calls.append)
    return calls


def _page_requests(requests: list[str]) -> list[str]:
    """Requests for pages/files, ignoring robots.txt and the browser's favicon."""
    return [p for p in requests if p not in ("/robots.txt", "/favicon.ico")]


# --- full crawl of the fixture site -----------------------------------------


def test_crawl_visits_each_reachable_page_once(site_server):
    results = crawl(f"{site_server.url}/index.html", delay=0)

    assert [r.url for r in results] == [site_server.url + p for p in EXPECTED_PATHS]

    # The server saw each page requested exactly once: the #fragment link, the
    # utm_* link and the no-trailing-slash link were all recognised as duplicates.
    counts = Counter(_page_requests(site_server.requests))
    assert counts == Counter(EXPECTED_PATHS)


def test_crawl_returns_audit_results_and_survives_broken_link(site_server):
    results = crawl(f"{site_server.url}/index.html", delay=0)

    errors = [r for r in results if isinstance(r, PageError)]
    assert len(errors) == 1
    assert errors[0].url == f"{site_server.url}/missing.html"
    assert "404" in errors[0].error

    # Pages after the broken one were still crawled and audited.
    audited = [r for r in results if isinstance(r, AuditResult)]
    assert [r.title for r in audited] == [
        "Test site: home", "Test site: about", "Test site: products", "Test site: contact",
    ]


def test_crawl_obeys_robots_txt(site_server):
    results = crawl(f"{site_server.url}/index.html", delay=0)

    assert "/robots.txt" in site_server.requests
    assert not any("/private/" in p for p in site_server.requests)
    assert not any("/private/" in r.url for r in results)


def test_crawl_never_requests_non_page_links(site_server):
    results = crawl(f"{site_server.url}/index.html", delay=0)

    assert "/report.pdf" not in site_server.requests
    assert all(r.url.startswith(site_server.url) for r in results)  # no external


def test_crawl_finds_links_added_by_javascript(site_server):
    results = crawl(f"{site_server.url}/products/", delay=0)
    assert f"{site_server.url}/contact.html" in [r.url for r in results]


@pytest.mark.parametrize("max_pages", [1, 3])
def test_crawl_stops_at_max_pages(site_server, max_pages):
    results = crawl(f"{site_server.url}/index.html", max_pages=max_pages, delay=0)

    expected = EXPECTED_PATHS[:max_pages]
    assert [r.url for r in results] == [site_server.url + p for p in expected]
    assert Counter(_page_requests(site_server.requests)) == Counter(expected)


def test_start_url_blocked_by_robots_txt(site_server):
    assert crawl(f"{site_server.url}/private/secret.html", delay=0) == []
    assert _page_requests(site_server.requests) == []


def test_crawl_without_robots_txt_is_allowed(fixture_server):
    # tests/fixtures/ has no robots.txt (404), which means everything is allowed.
    results = crawl(f"{fixture_server}/clean.html", delay=0)
    assert [r.url for r in results] == [f"{fixture_server}/clean.html"]
    assert isinstance(results[0], AuditResult)


# --- delays -------------------------------------------------------------------


def test_robots_crawl_delay_is_honoured(site_server, sleeps):
    results = crawl(f"{site_server.url}/index.html", delay=0)
    # One wait between each pair of requests, at the robots.txt Crawl-delay.
    assert sleeps == [1.0] * (len(results) - 1)


def test_longer_delay_overrides_crawl_delay(site_server, sleeps):
    crawl(f"{site_server.url}/index.html", max_pages=3, delay=2.5)
    assert sleeps == [2.5, 2.5]


def test_delay_used_when_robots_has_no_crawl_delay(monkeypatch, site_server, sleeps):
    monkeypatch.setattr(crawler._RobotsCache, "crawl_delay", lambda self, url: 0.0)
    crawl(f"{site_server.url}/index.html", max_pages=3, delay=0.75)
    assert sleeps == [0.75, 0.75]

    sleeps.clear()
    crawl(f"{site_server.url}/index.html", max_pages=3, delay=0)
    assert sleeps == []


# --- browser reuse and progress -----------------------------------------------


def test_crawl_launches_one_browser(monkeypatch, site_server):
    launches = []
    original = Auditor._launch

    def counting_launch(self):
        launches.append(1)
        return original(self)

    monkeypatch.setattr(Auditor, "_launch", counting_launch)
    results = crawl(f"{site_server.url}/index.html", delay=0)
    assert len(results) == len(EXPECTED_PATHS)
    assert len(launches) == 1


def test_progress_callback(site_server):
    calls = []
    crawl(f"{site_server.url}/index.html", max_pages=2, delay=0,
          progress=lambda n, total, url: calls.append((n, total, url)))
    assert calls == [
        (1, 2, f"{site_server.url}/index.html"),
        (2, 2, f"{site_server.url}/about.html"),
    ]


@pytest.mark.parametrize("kwargs", [{"max_pages": 0}, {"delay": -1}])
def test_crawl_rejects_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        crawl("http://127.0.0.1:9/", **kwargs)


@pytest.mark.parametrize("url", ["mailto:a@b.invalid", "ftp://x.invalid/", "not a url"])
def test_crawl_rejects_non_http_start_url(url):
    with pytest.raises(ValueError):
        crawl(url, delay=0)


# --- robots.txt fetching (network mocked) -------------------------------------


@pytest.mark.parametrize(
    "status, allowed",
    [
        (404, True),
        (403, True),   # RFC 9309: any 4xx means "no rules"
        (410, True),
        (500, False),  # server error: assume everything is off-limits
        (503, False),
    ],
)
def test_robots_status_handling(status, allowed):
    robots = crawler._RobotsCache(lambda url, headers: (status, ""))
    assert robots.allowed("https://example.invalid/page") is allowed


def test_robots_unreachable_disallows_everything():
    def unreachable(url, headers):
        raise AuditError("certificate verify failed")

    robots = crawler._RobotsCache(unreachable)
    assert robots.allowed("https://example.invalid/page") is False


def test_robots_fetched_once_per_origin_with_our_user_agent():
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        return 200, "User-agent: *\nDisallow: /no/\nCrawl-delay: 4\n"

    robots = crawler._RobotsCache(fetch)
    assert robots.allowed("https://x.invalid/yes")
    assert not robots.allowed("https://x.invalid/no/page")
    assert robots.crawl_delay("https://x.invalid/yes") == 4.0
    assert robots.allowed("http://other.invalid/no/page") is False
    assert calls == [
        ("https://x.invalid/robots.txt", {"User-Agent": crawler.USER_AGENT}),
        ("http://other.invalid/robots.txt", {"User-Agent": crawler.USER_AGENT}),
    ]


def test_start_url_blocked_warns(site_server, caplog):
    with caplog.at_level("WARNING", logger="auditor.crawler"):
        crawl(f"{site_server.url}/private/secret.html", delay=0)
    assert "disallowed by robots.txt" in caplog.text


# --- URL helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://x.invalid/a#section", "https://x.invalid/a"),
        ("https://x.invalid/a/", "https://x.invalid/a"),
        ("https://x.invalid", "https://x.invalid/"),
        ("https://x.invalid/a?utm_source=n&utm_medium=e", "https://x.invalid/a"),
        ("https://x.invalid/a?id=1&utm_campaign=c&fbclid=z", "https://x.invalid/a?id=1"),
        ("HTTPS://X.Invalid:443/a", "https://x.invalid/a"),
        ("http://x.invalid:80/a", "http://x.invalid/a"),
        ("https://www.x.invalid/a", "https://x.invalid/a"),
    ],
)
def test_normalize_url_treats_as_same(a, b):
    assert normalize_url(a) == normalize_url(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://x.invalid/a", "https://x.invalid/b"),
        ("https://x.invalid/a?id=1", "https://x.invalid/a?id=2"),
        ("https://x.invalid:8443/a", "https://x.invalid/a"),
        ("https://x.invalid/A", "https://x.invalid/a"),  # paths are case-sensitive
    ],
)
def test_normalize_url_keeps_different_pages_apart(a, b):
    assert normalize_url(a) != normalize_url(b)


def test_clean_url_keeps_path_but_drops_fragment_and_tracking():
    assert (clean_url("https://x.invalid/dir/?q=1&utm_source=n#top")
            == "https://x.invalid/dir/?q=1")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.Example.com/a/b?x=1", "example.com"),
        ("http://example.com:80/", "example.com"),
        ("http://127.0.0.1:8000/index.html", "127.0.0.1:8000"),
        ("http://[::1]:8000/", "[::1]:8000"),
    ],
)
def test_site_domain(url, expected):
    assert site_domain(url) == expected


def test_same_site_ignores_www():
    assert same_site("https://www.x.invalid/a", "x.invalid")
    assert same_site("http://x.invalid:8080/a", "x.invalid")
    assert not same_site("https://other.invalid/", "x.invalid")
    assert not same_site("https://x.invalid.evil.invalid/", "x.invalid")
    assert not same_site("https://sub.x.invalid/", "x.invalid")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://x.invalid/page", True),
        ("https://x.invalid/dir/", True),
        ("https://x.invalid/page.html", True),
        ("https://x.invalid/v1.2/docs", True),
        ("mailto:a@x.invalid", False),
        ("tel:+15550100", False),
        ("javascript:void(0)", False),
        ("https://x.invalid/report.pdf", False),
        ("https://x.invalid/photo.JPG", False),
        ("https://x.invalid/files.zip?dl=1", False),
        ("https://x.invalid/letter.docx", False),
    ],
)
def test_is_page_link(url, expected):
    assert is_page_link(url) is expected


# --- command line -----------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "auditor.crawler", *args],
                          capture_output=True, text=True, timeout=120)


def test_cli_writes_json_and_prints_progress(site_server, tmp_path):
    out = tmp_path / "results.json"
    proc = _run_cli(f"{site_server.url}/index.html", "--max-pages", "2", "--delay", "0",
                    "--output", str(out), "--db", str(tmp_path / "a.db"))
    assert proc.returncode == 0, proc.stderr
    assert f"[1/2] {site_server.url}/index.html" in proc.stderr
    assert f"[2/2] {site_server.url}/about.html" in proc.stderr
    assert proc.stdout == ""

    data = json.loads(out.read_text())
    assert [p["url"] for p in data] == [
        f"{site_server.url}/index.html", f"{site_server.url}/about.html",
    ]
    assert data[0]["title"] == "Test site: home"


def test_cli_records_failed_pages_in_json(fixture_server, tmp_path):
    out = tmp_path / "results.json"
    proc = _run_cli(f"{fixture_server}/does-not-exist.html", "--delay", "0",
                    "--output", str(out), "--db", str(tmp_path / "a.db"))
    assert proc.returncode == 1  # nothing was audited successfully
    data = json.loads(out.read_text())
    assert len(data) == 1
    assert data[0]["url"] == f"{fixture_server}/does-not-exist.html"
    assert set(data[0]) == {"url", "error", "audited_at"}
    assert "404" in data[0]["error"]
    assert "404" in proc.stderr


def test_cli_saves_each_crawl_as_a_new_run(site_server, tmp_path):
    db_path = tmp_path / "audits.db"
    args = [f"{site_server.url}/index.html", "--max-pages", "4", "--delay", "0",
            "--output", str(tmp_path / "r.json"), "--db", str(db_path)]

    first = _run_cli(*args)
    assert first.returncode == 0, first.stderr
    assert f"Saved as run 1 in {db_path}" in first.stderr
    second = _run_cli(*args)
    assert second.returncode == 0, second.stderr
    assert f"Saved as run 2 in {db_path}" in second.stderr

    conn = db.init_db(db_path)
    try:
        runs = db.get_runs(conn)
        assert [r["id"] for r in runs] == [2, 1]
        assert {r["domain"] for r in runs} == {site_domain(site_server.url)}
        assert all(r["status"] == "completed" and r["max_pages"] == 4 for r in runs)
        assert all(r["started_at"] <= r["finished_at"] for r in runs)
        assert {r["start_url"] for r in runs} == {f"{site_server.url}/index.html"}
        assert all(r["site_score"] is not None for r in runs)

        pages = db.get_pages(conn, 1)
        assert [p["url"] for p in pages] == [
            f"{site_server.url}{path}" for path in EXPECTED_PATHS[:4]
        ]
        assert [p["status"] for p in pages] == ["ok", "ok", "ok", "error"]
        assert "404" in pages[3]["error_message"]
        assert pages[0]["title"] == "Test site: home"
        # Same pages and findings both times.
        assert [p["url"] for p in db.get_pages(conn, 2)] == [p["url"] for p in pages]
        assert db.rule_counts(conn, 1) == db.rule_counts(conn, 2)
    finally:
        conn.close()

    # The JSON output is still written alongside the database.
    assert len(json.loads((tmp_path / "r.json").read_text())) == 4


def test_cli_reports_unwritable_db(site_server, tmp_path):
    proc = _run_cli(f"{site_server.url}/index.html", "--max-pages", "1", "--delay", "0",
                    "--output", str(tmp_path / "r.json"),
                    "--db", str(tmp_path / "no-such-dir" / "a.db"))
    assert proc.returncode == 1
    assert "could not save" in proc.stderr
    assert (tmp_path / "r.json").exists()  # JSON results are not lost
