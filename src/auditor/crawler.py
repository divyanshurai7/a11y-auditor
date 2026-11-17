"""Crawl a website breadth-first and audit every page found.

Usage from the command line::

    python -m auditor.crawler <url> [--max-pages N] [--delay S] [--output FILE]

prints progress to stderr and writes the results to a JSON file.

Etiquette: stays on the start URL's site (www. and the bare domain count as the
same), obeys robots.txt (including Crawl-delay), waits between requests, and
stops after a page cap.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from auditor.audit import DEFAULT_TIMEOUT_MS, AuditError, Auditor, AuditResult

logger = logging.getLogger(__name__)

USER_AGENT = "a11y-auditor"
DEFAULT_MAX_PAGES = 50
DEFAULT_DELAY = 1.0

# Query parameters that only track where a visitor came from; they never change
# the page, so they're dropped before comparing URLs.
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset(
    {"gclid", "dclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "_ga", "_gl"}
)

# Links to these are files, not web pages.
NON_PAGE_EXTENSIONS = frozenset(
    {
        # documents
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
        "rtf", "csv", "txt", "epub",
        # images
        "jpg", "jpeg", "png", "gif", "svg", "webp", "avif", "bmp", "ico", "tif", "tiff",
        # audio / video
        "mp3", "wav", "ogg", "m4a", "mp4", "m4v", "mov", "avi", "wmv", "webm", "mkv",
        # archives / binaries
        "zip", "gz", "tgz", "tar", "rar", "7z", "bz2", "xz", "exe", "msi", "dmg",
        "pkg", "deb", "rpm", "apk", "iso", "bin",
        # other non-HTML resources
        "css", "js", "json", "xml", "rss", "atom", "woff", "woff2", "ttf", "otf",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Indirection so tests can record waits instead of actually sleeping.
_sleep = time.sleep


@dataclass
class PageError:
    """A page the crawler tried to audit but couldn't (timeout, HTTP error, ...)."""

    url: str
    error: str

    def to_dict(self) -> dict:
        return asdict(self)


PageResult = AuditResult | PageError

ProgressCallback = Callable[[int, int, str], None]


# --- URL helpers -----------------------------------------------------------


def _site_key(host: str | None) -> str:
    """Hostname used to decide "same site": lowercased, leading www. removed."""
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _is_tracking_param(name: str) -> bool:
    name = name.lower()
    return name in TRACKING_PARAMS or name.startswith(TRACKING_PARAM_PREFIXES)


def clean_url(url: str) -> str:
    """The URL to actually request: no #fragment, no tracking parameters,
    lowercase scheme and host, no default port. Raises ValueError if malformed.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    port = parts.port  # raises ValueError for a bad port
    netloc = f"[{host}]" if ":" in host else host  # IPv6
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc += f":{port}"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _is_tracking_param(k)]
    )
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def normalize_url(url: str) -> str:
    """Key used to detect duplicate pages.

    Like `clean_url`, but also drops trailing slashes and treats www. and the
    bare domain as the same host.
    """
    parts = urlsplit(clean_url(url))
    path = parts.path.rstrip("/") or "/"
    host = _site_key(parts.hostname)
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:  # clean_url already dropped default ports
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def same_site(url: str, site: str) -> bool:
    """True if `url` is on `site` (a `_site_key`), ignoring www. and the port."""
    return _site_key(urlsplit(url).hostname) == site


def is_page_link(url: str) -> bool:
    """True for http(s) links that look like web pages (not mailto:, tel:,
    javascript:, or files like .pdf/.jpg/.zip/.docx)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    suffix = PurePosixPath(parts.path).suffix.lower().lstrip(".")
    return suffix not in NON_PAGE_EXTENSIONS


# --- robots.txt ------------------------------------------------------------


# fetch(url, headers) -> (status, body); raises AuditError if unreachable.
FetchText = Callable[[str, dict[str, str]], tuple[int, str]]

_DISALLOW_ALL = ["User-agent: *", "Disallow: /"]


def _fetch_robots(robots_url: str, fetch: FetchText) -> RobotFileParser:
    """Download and parse a robots.txt, following RFC 9309 on failure:

    - 4xx (e.g. no robots.txt): everything is allowed.
    - 5xx or unreachable: assume everything is disallowed.
    """
    parser = RobotFileParser(robots_url)
    try:
        status, body = fetch(robots_url, {"User-Agent": USER_AGENT})
    except AuditError as exc:
        logger.warning("Could not fetch %s (%s); treating site as off-limits",
                       robots_url, exc)
        parser.parse(_DISALLOW_ALL)
        return parser
    if 200 <= status < 300:
        parser.parse(body.splitlines())
    elif 400 <= status < 500:
        parser.parse([])  # allow all
    else:
        logger.warning("%s returned HTTP %s; treating site as off-limits",
                       robots_url, status)
        parser.parse(_DISALLOW_ALL)
    return parser


class _RobotsCache:
    """One parsed robots.txt per origin (scheme + host + port)."""

    def __init__(self, fetch: FetchText, user_agent: str = USER_AGENT) -> None:
        self.user_agent = user_agent
        self._fetch = fetch
        self._parsers: dict[str, RobotFileParser] = {}

    def _parser(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            self._parsers[origin] = _fetch_robots(origin + "/robots.txt", self._fetch)
        return self._parsers[origin]

    def allowed(self, url: str) -> bool:
        return self._parser(url).can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float:
        delay = self._parser(url).crawl_delay(self.user_agent)
        return float(delay) if delay else 0.0


# --- crawler ---------------------------------------------------------------


def crawl(
    start_url: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    delay: float = DEFAULT_DELAY,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    progress: ProgressCallback | None = None,
) -> list[PageResult]:
    """Breadth-first crawl from `start_url`, auditing each page visited.

    Returns one entry per page visited, in visit order: an `AuditResult`, or a
    `PageError` if that page failed. At most `max_pages` pages are requested.
    Between requests the crawler waits `delay` seconds, or longer if robots.txt
    sets a Crawl-delay. `progress(n, max_pages, url)` is called before each page.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if delay < 0:
        raise ValueError("delay must not be negative")
    if not is_page_link(start_url):
        raise ValueError(f"not a crawlable http(s) page URL: {start_url!r}")

    start = clean_url(start_url)
    site = _site_key(urlsplit(start).hostname)
    queue: deque[str] = deque([start])
    queued = {normalize_url(start)}  # everything ever queued (dedupe links)
    visited: set[str] = set()  # final URLs of audited pages (catch redirects)
    results: list[PageResult] = []

    with Auditor(timeout_ms=timeout_ms) as auditor:
        # Fetch robots.txt through the browser's network stack, like the pages.
        robots = _RobotsCache(auditor.fetch_text)
        while queue and len(results) < max_pages:
            url = queue.popleft()
            if normalize_url(url) in visited:
                continue  # an earlier page redirected here
            if not robots.allowed(url):
                if url == start:
                    logger.warning("%s is disallowed by robots.txt; nothing to crawl", url)
                else:
                    logger.info("Skipping %s (disallowed by robots.txt)", url)
                continue

            if results:
                wait = max(delay, robots.crawl_delay(url))
                if wait > 0:
                    _sleep(wait)

            if progress is not None:
                progress(len(results) + 1, max_pages, url)

            visited.add(normalize_url(url))
            try:
                result, links = auditor.audit_with_links(url)
            except AuditError as exc:
                logger.warning("Failed: %s", exc)
                results.append(PageError(url=url, error=str(exc)))
                continue

            results.append(result)
            visited.add(normalize_url(result.url))
            if not same_site(result.url, site):
                continue  # redirected off-site: audit it, but don't follow its links

            for link in links:
                if not is_page_link(link) or not same_site(link, site):
                    continue
                try:
                    link = clean_url(link)
                except ValueError:
                    continue
                key = normalize_url(link)
                if key not in queued:
                    queued.add(key)
                    queue.append(link)

    return results


# --- command line ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m auditor.crawler",
        description="Crawl a website and run a WCAG 2.1 A/AA audit on each page.",
    )
    parser.add_argument("url", help="URL to start crawling from")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"maximum number of pages to audit (default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"seconds to wait between requests (default {DEFAULT_DELAY})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS,
                        help=f"page load timeout in milliseconds (default {DEFAULT_TIMEOUT_MS})")
    parser.add_argument("-o", "--output", default="crawl_results.json",
                        help="JSON file to write results to (default crawl_results.json)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s",
                        stream=sys.stderr)

    def show_progress(n: int, total: int, url: str) -> None:
        print(f"[{n}/{total}] {url}", file=sys.stderr, flush=True)

    try:
        results = crawl(args.url, max_pages=args.max_pages, delay=args.delay,
                        timeout_ms=args.timeout, progress=show_progress)
    except (ValueError, AuditError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)

    failed = sum(isinstance(r, PageError) for r in results)
    print(f"Visited {len(results)} page(s), {failed} failed. Results written to {args.output}",
          file=sys.stderr)
    return 0 if len(results) > failed else 1


if __name__ == "__main__":
    sys.exit(main())
