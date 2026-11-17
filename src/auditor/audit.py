"""Single-page accessibility audit: load a URL in headless Chromium and run axe-core.

Usage from the command line::

    python -m auditor.audit <url>

prints the audit result as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field

from axe_playwright_python.base import AXE_SCRIPT
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

# axe-core tags for WCAG 2.0 + 2.1, levels A and AA.
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]

AXE_OPTIONS = {
    "runOnly": {"type": "tag", "values": WCAG_TAGS},
    # Only collect node details for these; passes/inapplicable stay summarised.
    "resultTypes": ["violations", "incomplete"],
    "iframes": True,
}

DEFAULT_TIMEOUT_MS = 30_000
# After the "load" event, give late network activity (XHR, lazy assets) a short
# chance to settle. Pages that never go idle (polling, analytics) still get audited.
NETWORK_IDLE_TIMEOUT_MS = 5_000


class AuditError(Exception):
    """Raised when a page cannot be loaded or audited."""


@dataclass
class FindingNode:
    """One element on the page that a rule flagged."""

    selector: str
    html: str


@dataclass
class Finding:
    """An axe rule that flagged one or more elements."""

    rule_id: str
    impact: str | None
    description: str
    help_url: str
    nodes: list[FindingNode] = field(default_factory=list)


@dataclass
class AuditResult:
    url: str
    title: str
    # Definite failures.
    violations: list[Finding] = field(default_factory=list)
    # axe's "incomplete" results: possible failures axe could not decide
    # automatically (e.g. text over a background image). Need a human to check.
    needs_review: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _selector_from_target(target: list) -> str:
    """Flatten an axe `target` into a single selector string.

    axe gives a list of selectors; more than one entry means the element is inside
    iframes (outer -> inner, joined with " | "). Shadow-DOM entries are themselves
    lists (host -> inner, joined with " >>> ").
    """
    parts = [" >>> ".join(t) if isinstance(t, list) else str(t) for t in target]
    return " | ".join(parts)


def _parse_findings(axe_items: list[dict]) -> list[Finding]:
    findings = []
    for item in axe_items:
        nodes = [
            FindingNode(
                selector=_selector_from_target(n.get("target", [])),
                html=n.get("html", ""),
            )
            for n in item.get("nodes", [])
        ]
        findings.append(
            Finding(
                rule_id=item["id"],
                impact=item.get("impact"),
                description=item.get("description", ""),
                help_url=item.get("helpUrl", ""),
                nodes=nodes,
            )
        )
    return findings


# By default axe only exchanges results with same-origin frames. We inject axe into
# every frame ourselves, so let it talk to cross-origin ones (third-party embeds).
_AXE_INIT = AXE_SCRIPT + "\n;axe.configure({allowedOrigins: ['<unsafe_all_origins>']});"


def _run_axe(page: Page) -> dict:
    """Inject axe-core into every frame, then run it from the top-level page.

    axe-core audits iframes by messaging a copy of itself inside each frame, so it
    must be present in all of them (including cross-origin ones) before running.
    """
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            frame.evaluate(_AXE_INIT)
        except PlaywrightError:
            # Frame detached or navigated away mid-injection; skip it rather than
            # failing the whole audit.
            pass
    page.evaluate(_AXE_INIT)
    return page.evaluate("options => axe.run(document, options)", AXE_OPTIONS)


class Auditor:
    """A reusable headless Chromium for auditing many pages with one browser.

    Use as a context manager::

        with Auditor() as auditor:
            for url in urls:
                result = auditor.audit(url)

    Each page gets a fresh browser context (no cookies or storage carried over).
    """

    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._request_context = None

    def __enter__(self) -> "Auditor":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def start(self) -> None:
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._launch()
        except PlaywrightError as exc:
            self.close()
            raise AuditError(f"Failed to start browser: {exc}") from exc

    def close(self) -> None:
        if self._request_context is not None:
            try:
                self._request_context.dispose()
            except PlaywrightError:
                pass
            self._request_context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _launch(self):
        return self._playwright.chromium.launch(headless=True)

    def fetch_text(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        """Plain HTTP GET (no page rendering); returns (status, body text).

        Goes through Playwright's networking rather than Python's urllib, so TLS
        verification doesn't depend on how Python's certificates are set up.
        Follows redirects. Raises AuditError if the server can't be reached.
        """
        if self._playwright is None:
            raise RuntimeError("Auditor is not started; use it as a context manager")
        try:
            if self._request_context is None:
                self._request_context = self._playwright.request.new_context()
            response = self._request_context.get(
                url, headers=headers, timeout=self.timeout_ms, fail_on_status_code=False
            )
            try:
                return response.status, response.text()
            finally:
                response.dispose()
        except PlaywrightError as exc:
            raise AuditError(f"Failed to fetch {url}: {exc}") from exc

    def audit(self, url: str) -> AuditResult:
        """Audit one page. Raises AuditError if it can't be loaded or audited."""
        return self.audit_with_links(url)[0]

    def audit_with_links(self, url: str) -> tuple[AuditResult, list[str]]:
        """Audit one page and also return the absolute URLs of its links.

        Links are read from the rendered DOM (after JavaScript has run), so links
        added by scripts are included.
        """
        if self._playwright is None:
            raise RuntimeError("Auditor is not started; use it as a context manager")
        try:
            if self._browser is None or not self._browser.is_connected():
                self._browser = self._launch()  # recover from a browser crash
            context = self._browser.new_context()
            try:
                page = context.new_page()
                response = page.goto(url, wait_until="load", timeout=self.timeout_ms)
                if response is not None and response.status >= 400:
                    raise AuditError(f"{url} returned HTTP {response.status}")
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
                except PlaywrightError:
                    pass  # never went idle; audit what's there
                title = page.title()
                final_url = page.url
                axe_response = _run_axe(page)
                links = page.eval_on_selector_all(
                    "a[href], area[href]",
                    # SVG <a> elements have a non-string .href; skip them.
                    "els => els.map(e => e.href).filter(h => typeof h === 'string')",
                )
            finally:
                context.close()
        except PlaywrightError as exc:
            raise AuditError(f"Failed to audit {url}: {exc}") from exc

        result = AuditResult(
            url=final_url,
            title=title,
            violations=_parse_findings(axe_response.get("violations", [])),
            needs_review=_parse_findings(axe_response.get("incomplete", [])),
        )
        return result, links


def audit_page(url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> AuditResult:
    """Load `url` in headless Chromium, run axe-core (WCAG 2.1 A/AA), return the result.

    Content inside iframes is audited too. Raises AuditError if the page cannot be
    loaded or returns an HTTP error status. Launches its own browser; to audit many
    pages, use an `Auditor` instead.
    """
    with Auditor(timeout_ms=timeout_ms) as auditor:
        return auditor.audit(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m auditor.audit",
        description="Run a WCAG 2.1 A/AA accessibility audit on a single page.",
    )
    parser.add_argument("url", help="URL of the page to audit")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=f"page load timeout in milliseconds (default {DEFAULT_TIMEOUT_MS})",
    )
    args = parser.parse_args(argv)

    try:
        result = audit_page(args.url, timeout_ms=args.timeout)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
