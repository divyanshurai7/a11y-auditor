"""Tests for auditor.fixes."""

from __future__ import annotations

import pytest

from auditor.fixes import FIXES, get_fix

REQUESTED = ["image-alt", "color-contrast", "label", "link-name", "button-name",
             "html-has-lang", "document-title", "heading-order", "region",
             "landmark-one-main", "list", "listitem", "frame-title", "select-name",
             "duplicate-id", "aria-required-attr", "aria-valid-attr",
             "aria-valid-attr-value", "aria-roles", "aria-hidden-focus"]


def test_covers_common_rules():
    assert set(REQUESTED) <= set(FIXES)
    assert len(FIXES) >= 25


@pytest.mark.parametrize("rule_id", sorted(FIXES))
def test_every_entry_has_summary_and_examples(rule_id):
    entry = FIXES[rule_id]
    assert entry["summary"].strip()
    assert entry["before"].strip() and entry["after"].strip()
    assert entry["before"] != entry["after"]


def test_known_rule_returns_curated_fix():
    fix = get_fix("image-alt", "axe text", "https://dequeuniversity.com/rules/axe/4.10/image-alt")
    assert fix.curated
    assert fix.rule_id == "image-alt"
    assert "alt" in fix.summary and fix.summary != "axe text"
    assert 'alt="' in fix.after and "alt" not in fix.before
    assert fix.help_url.endswith("/image-alt")


def test_unknown_rule_falls_back_to_axe_description():
    fix = get_fix("scrollable-region-focusable",
                  "Ensure elements that have scrollable content are accessible by keyboard",
                  "https://dequeuniversity.com/rules/axe/4.10/scrollable-region-focusable")
    assert not fix.curated
    assert fix.summary.startswith("Ensure elements that have scrollable content")
    assert fix.help_url.endswith("/scrollable-region-focusable")
    assert fix.before == fix.after == ""


def test_unknown_rule_without_description():
    fix = get_fix("mystery-rule")
    assert not fix.curated
    assert "mystery-rule" in fix.summary
    assert fix.help_url == ""
