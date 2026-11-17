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

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# axe-core tags for WCAG 2.0 + 2.1, levels A and AA.
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]

DEFAULT_TIMEOUT_MS = 30_000
# After the "load" event, give late network activity (XHR, lazy assets) a short
# chance to settle. Pages that never go idle (polling, analytics) still get audited.
NETWORK_IDLE_TIMEOUT_MS = 5_000


class AuditError(Exception):
    """Raised when a page cannot be loaded or audited."""


@dataclass
class ViolationNode:
    """One element on the page that fails a rule."""

    selector: str
    html: str


@dataclass
class Violation:
    rule_id: str
    impact: str | None
    description: str
    help_url: str
    nodes: list[ViolationNode] = field(default_factory=list)


@dataclass
class AuditResult:
    url: str
    title: str
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _selector_from_target(target: list) -> str:
    """Flatten an axe `target` into a single selector string.

    axe gives a list of selectors; more than one entry means the element is inside
    iframes or shadow roots (outer -> inner). Shadow-DOM entries are themselves lists.
    """
    parts = [" >>> ".join(t) if isinstance(t, list) else str(t) for t in target]
    return " | ".join(parts)


def _parse_violations(axe_response: dict) -> list[Violation]:
    violations = []
    for v in axe_response.get("violations", []):
        nodes = [
            ViolationNode(
                selector=_selector_from_target(n.get("target", [])),
                html=n.get("html", ""),
            )
            for n in v.get("nodes", [])
        ]
        violations.append(
            Violation(
                rule_id=v["id"],
                impact=v.get("impact"),
                description=v.get("description", ""),
                help_url=v.get("helpUrl", ""),
                nodes=nodes,
            )
        )
    return violations


def audit_page(url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> AuditResult:
    """Load `url` in headless Chromium, run axe-core (WCAG 2.1 A/AA), return the result.

    Raises AuditError if the page cannot be loaded or returns an HTTP error status.
    """
    axe = Axe()
    options = {
        "runOnly": {"type": "tag", "values": WCAG_TAGS},
        "resultTypes": ["violations"],
    }
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
                results = axe.run(page, options=options)
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise AuditError(f"Failed to audit {url}: {exc}") from exc

    return AuditResult(
        url=final_url,
        title=title,
        violations=_parse_violations(results.response),
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
