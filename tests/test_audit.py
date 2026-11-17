"""Tests for auditor.audit, run against local fixture pages only."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

import pytest

from auditor.audit import (
    AuditError,
    Auditor,
    AuditResult,
    _selector_from_target,
    audit_page,
)


def _violation(result: AuditResult, rule_id: str):
    matches = [v for v in result.violations if v.rule_id == rule_id]
    assert matches, f"expected {rule_id!r}, got {[v.rule_id for v in result.violations]}"
    return matches[0]


def test_clean_page_has_no_findings(fixture_server):
    result = audit_page(f"{fixture_server}/clean.html")
    assert result.url == f"{fixture_server}/clean.html"
    assert result.title == "Clean page"
    assert result.violations == []
    assert result.needs_review == []


@pytest.mark.parametrize(
    "page, rule_id, selector, snippet",
    [
        ("missing_alt.html", "image-alt", "#no-alt", '<img id="no-alt"'),
        ("low_contrast.html", "color-contrast", "#faint-text", '<p id="faint-text"'),
        ("missing_label.html", "label", "#unlabelled", '<input id="unlabelled"'),
        # Problems inside iframes; selector is "<iframe> | <element in frame>".
        ("iframe_same_origin.html", "image-alt", "#widget | #framed-no-alt",
         '<img id="framed-no-alt"'),
        ("iframe_cross_origin.html", "image-alt", "#widget | #framed-no-alt",
         '<img id="framed-no-alt"'),
    ],
)
def test_planted_problem_is_detected(fixture_server, page, rule_id, selector, snippet):
    result = audit_page(f"{fixture_server}/{page}")

    # Each fixture plants exactly one problem, so nothing else should be reported.
    assert [v.rule_id for v in result.violations] == [rule_id]
    assert result.needs_review == []

    v = _violation(result, rule_id)
    assert v.impact in {"minor", "moderate", "serious", "critical"}
    assert v.description
    assert v.help_url.startswith("https://dequeuniversity.com/rules/axe/")
    assert len(v.nodes) == 1
    assert v.nodes[0].selector == selector
    assert v.nodes[0].html.startswith(snippet)


def test_undecidable_problem_goes_to_needs_review(fixture_server):
    # Text over a gradient: axe can't compute contrast, so it's "incomplete".
    result = audit_page(f"{fixture_server}/needs_review.html")

    assert result.violations == []
    assert [f.rule_id for f in result.needs_review] == ["color-contrast"]

    f = result.needs_review[0]
    assert f.impact in {"minor", "moderate", "serious", "critical"}
    assert f.description
    assert f.help_url.startswith("https://dequeuniversity.com/rules/axe/")
    assert len(f.nodes) == 1
    assert f.nodes[0].selector == "#hero-text"
    assert f.nodes[0].html.startswith('<p id="hero-text"')


@pytest.mark.parametrize(
    "page, key", [("missing_alt.html", "violations"), ("needs_review.html", "needs_review")]
)
def test_result_shape_is_json_serialisable(fixture_server, page, key):
    result = audit_page(f"{fixture_server}/{page}")
    data = json.loads(json.dumps(result.to_dict()))
    assert set(data) == {"url", "title", "violations", "needs_review", "audited_at"}
    assert datetime.fromisoformat(data["audited_at"]).tzinfo is not None  # UTC timestamp
    assert set(data[key][0]) == {"rule_id", "impact", "description", "help_url", "nodes"}
    assert set(data[key][0]["nodes"][0]) == {"selector", "html"}


def test_http_error_raises_audit_error(fixture_server):
    with pytest.raises(AuditError, match="404"):
        audit_page(f"{fixture_server}/does-not-exist.html")


def test_unreachable_url_raises_audit_error():
    # Port 9 on localhost (discard) is essentially never listening; fails fast.
    with pytest.raises(AuditError):
        audit_page("http://127.0.0.1:9/", timeout_ms=5_000)


def test_selector_from_target_handles_frames_and_shadow_dom():
    assert _selector_from_target(["#a"]) == "#a"
    assert _selector_from_target(["iframe", "#a"]) == "iframe | #a"
    assert _selector_from_target([["my-el", "#a"]]) == "my-el >>> #a"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "auditor.audit", *args],
        capture_output=True, text=True, timeout=120,
    )


def test_cli_prints_json(fixture_server):
    url = f"{fixture_server}/missing_label.html"
    proc = _run_cli(url)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["url"] == url
    assert data["title"] == "Form input without a label"
    assert [v["rule_id"] for v in data["violations"]] == ["label"]
    assert data["needs_review"] == []


def test_cli_reports_errors_on_stderr(fixture_server):
    proc = _run_cli(f"{fixture_server}/does-not-exist.html")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "404" in proc.stderr


# --- Auditor (reusable browser) ----------------------------------------------


def test_auditor_audits_several_pages(fixture_server):
    with Auditor() as auditor:
        clean = auditor.audit(f"{fixture_server}/clean.html")
        bad = auditor.audit(f"{fixture_server}/missing_alt.html")
    assert clean.violations == []
    assert [v.rule_id for v in bad.violations] == ["image-alt"]


def test_auditor_fetch_text(fixture_server):
    with Auditor() as auditor:
        status, body = auditor.fetch_text(f"{fixture_server}/clean.html")
        missing_status, _ = auditor.fetch_text(f"{fixture_server}/robots.txt")
    assert status == 200
    assert "<title>Clean page</title>" in body
    assert missing_status == 404


def test_auditor_fetch_text_unreachable_raises():
    with Auditor(timeout_ms=5_000) as auditor:
        with pytest.raises(AuditError):
            auditor.fetch_text("http://127.0.0.1:9/robots.txt")
