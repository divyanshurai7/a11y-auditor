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


def audit_page(url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> AuditResult:
    """Load `url` in headless Chromium, run axe-core (WCAG 2.1 A/AA), return the result.

    Content inside iframes is audited too. Raises AuditError if the page cannot be
    loaded or returns an HTTP error status.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                response = page.goto(url, wait_until="load", timeout=timeout_ms)
                if response is not None and response.status >= 400:
                    raise AuditError(f"{url} returned HTTP {response.status}")
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
                except PlaywrightError:
                    pass  # never went idle; audit what's there
                title = page.title()
                final_url = page.url
                axe_response = _run_axe(page)
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise AuditError(f"Failed to audit {url}: {exc}") from exc

    return AuditResult(
        url=final_url,
        title=title,
        violations=_parse_findings(axe_response.get("violations", [])),
        needs_review=_parse_findings(axe_response.get("incomplete", [])),
    )


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
