"""Tests for the auditor.report command line, against a temporary database."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from auditor import db, report
from auditor.audit import AuditResult, Finding, FindingNode
from auditor.crawler import PageError

T0 = "2026-01-01T10:00:00.000+00:00"
T1 = "2026-01-01T10:00:05.000+00:00"


def _finding(rule_id, impact="serious", nodes=1):
    return Finding(rule_id=rule_id, impact=impact, description=f"axe says: {rule_id}",
                   help_url=f"https://dequeuniversity.com/rules/axe/4.10/{rule_id}",
                   nodes=[FindingNode(selector=f"#{rule_id}{i}", html="<x>")
                          for i in range(nodes)])


def _page(n, *violations, needs_review=()):
    return AuditResult(url=f"https://example.com/p{n}", title=f"Page {n}",
                       violations=list(violations), needs_review=list(needs_review))


@pytest.fixture
def db_path(tmp_path):
    """A database with one run of 7 pages (6 audited, 1 failed)."""
    path = tmp_path / "audits.db"
    results = [
        _page(1),                                                       # 100
        _page(2, _finding("image-alt", "critical", nodes=30)),          # 80
        _page(3, _finding("image-alt", "critical"),
              _finding("label", "critical")),                           # 70
        _page(4, _finding("image-alt", "critical"),
              _finding("scrollable-region-focusable", "serious")),      # 77
        _page(5, _finding("label", "critical")),                        # 85
        _page(6, needs_review=[_finding("color-contrast", nodes=9)]),   # 100
        PageError(url="https://example.com/broken", error="returned HTTP 500"),
    ]
    conn = db.init_db(path)
    db.save_run(conn, domain="example.com", results=results, started_at=T0,
                finished_at=T1, max_pages=10, start_url="https://example.com/p1")
    conn.close()
    return path


def _report(*args):
    out = io.StringIO()
    code = report.main([*map(str, args)], out=out)
    return code, out.getvalue()


def test_report_shows_site_score(db_path):
    code, text = _report(1, "--db", db_path)
    assert code == 0
    # (100 + 80 + 70 + 77 + 85 + 100) / 6 = 85.33...
    assert "Site score: 85.3/100" in text
    assert "Started from https://example.com/p1" in text
    assert "7 page(s), 1 failed to load" in text


def test_report_lists_five_worst_pages_in_order(db_path):
    _, text = _report(1, "--db", db_path)
    worst = text.split("Worst pages")[1].split("Top rules")[0]
    lines = [l.split() for l in worst.strip().splitlines()[1:]]
    assert lines == [
        ["70/100", "https://example.com/p3"],
        ["77/100", "https://example.com/p4"],
        ["80/100", "https://example.com/p2"],
        ["85/100", "https://example.com/p5"],
        ["100/100", "https://example.com/p1"],
    ]
    assert "broken" not in worst  # failed pages have no score


def test_report_lists_rules_by_pages_affected_with_fixes(db_path):
    _, text = _report(1, "--db", db_path)
    rules = text.split("Top rules")[1]
    assert rules.index("1. image-alt: 3 page(s), 32 element(s)") \
        < rules.index("2. label: 2 page(s), 2 element(s)") \
        < rules.index("3. scrollable-region-focusable: 1 page(s), 1 element(s)")
    # Curated fix with before/after for a known rule...
    assert "Give every image an alt attribute" in rules
    assert '<img src="logo.png" alt="Acme Inc. home">' in rules
    # ...axe's description for an unknown one...
    assert "Fix: axe says: scrollable-region-focusable" in rules
    assert "More: https://dequeuniversity.com/rules/axe/4.10/scrollable-region-focusable" in rules
    # ...and needs-review items are not listed as rules to fix.
    assert "color-contrast" not in rules


def test_report_top_option_limits_rules(db_path):
    _, text = _report(1, "--db", db_path, "--top", "1")
    assert "1. image-alt" in text
    assert "2. label" not in text


def test_report_run_without_violations(tmp_path):
    path = tmp_path / "clean.db"
    conn = db.init_db(path)
    db.save_run(conn, domain="example.com", results=[_page(1)], started_at=T0,
                finished_at=T1, max_pages=1)
    conn.close()
    code, text = _report(1, "--db", path)
    assert code == 0
    assert "Site score: 100/100" in text
    assert "(no violations found)" in text


def test_report_unknown_run(db_path, capsys):
    code, text = _report(99, "--db", db_path)
    assert code == 1
    assert text == ""
    assert "no audit run 99" in capsys.readouterr().err


def test_report_missing_database_is_not_created(tmp_path, capsys):
    path = tmp_path / "nope.db"
    code, _ = _report(1, "--db", path)
    assert code == 1
    assert "not found" in capsys.readouterr().err
    assert not path.exists()


def test_report_runs_as_module(db_path):
    proc = subprocess.run([sys.executable, "-m", "auditor.report", "1", "--db", str(db_path)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Site score: 85.3/100" in proc.stdout
