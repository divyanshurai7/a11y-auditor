"""Tests for auditor.scoring."""

from __future__ import annotations

import pytest

from auditor.audit import AuditResult, Finding, FindingNode
from auditor.scoring import SCORING, SiteScore, rule_deduction, score_page, score_site

W = SCORING["impact_weights"]
PER = SCORING["per_extra_element"]
CAP = SCORING["max_element_deduction_per_rule"]


def _f(rule_id, impact="serious", nodes=1):
    return Finding(rule_id=rule_id, impact=impact, description="d", help_url="u",
                   nodes=[FindingNode(selector=f"#n{i}", html="<x>") for i in range(nodes)])


def test_config_is_as_documented():
    assert W == {"critical": 15, "serious": 8, "moderate": 4, "minor": 2}


def test_clean_page_scores_100():
    assert score_page([]) == 100
    assert score_page(AuditResult(url="u", title="t")) == 100


@pytest.mark.parametrize("impact", ["critical", "serious", "moderate", "minor"])
def test_single_violation_costs_its_impact_weight(impact):
    assert score_page([_f("r", impact)]) == 100 - W[impact]


def test_impact_weighting_orders_pages():
    scores = [score_page([_f("r", i)]) for i in ("critical", "serious", "moderate", "minor")]
    assert scores == sorted(scores)
    assert score_page([_f("a", "critical"), _f("b", "minor")]) == 100 - 15 - 2


def test_unknown_impact_uses_fallback_weight():
    assert score_page([_f("r", None)]) == 100 - SCORING["unknown_impact_weight"]


def test_extra_elements_add_small_deduction():
    # serious, 3 elements: 8 + 2 * 0.5 = 9
    assert rule_deduction("serious", 3) == W["serious"] + 2 * PER
    assert score_page([_f("r", "serious", nodes=3)]) == 91


def test_element_deduction_is_capped_per_rule():
    # 30 spacer images with no alt cost no more than weight + cap...
    assert rule_deduction("critical", 30) == W["critical"] + CAP
    assert score_page([_f("image-alt", "critical", nodes=30)]) == 100 - 15 - CAP
    # ...the same as 1000 of them.
    assert score_page([_f("image-alt", "critical", nodes=1000)]) == 100 - 15 - CAP


def test_cap_applies_per_rule_not_per_page():
    findings = [_f("a", "minor", nodes=50), _f("b", "minor", nodes=50)]
    assert score_page(findings) == 100 - 2 * (2 + CAP)


def test_finding_without_nodes_costs_its_weight():
    assert score_page([_f("document-title", "serious", nodes=0)]) == 92


def test_same_rule_twice_is_charged_once():
    # 3 elements in total: 8 + 2 * 0.5 = 9 off, not 8 + 8.5.
    assert score_page([_f("r", "serious", nodes=2), _f("r", "serious")]) == 91


def test_score_is_clamped_at_zero():
    findings = [_f(f"rule-{i}", "critical", nodes=40) for i in range(10)]
    assert score_page(findings) == 0


def test_score_rounds_half_up():
    # serious with 2 elements: 8.5 off -> 91.5 -> 92
    assert score_page([_f("r", "serious", nodes=2)]) == 92


def test_needs_review_is_ignored():
    result = AuditResult(url="u", title="t",
                         needs_review=[_f("color-contrast", "serious", nodes=20)])
    assert score_page(result) == 100
    result.violations.append(_f("label", "critical"))
    assert score_page(result) == 85


def test_accepts_db_finding_dicts_and_skips_needs_review():
    findings = [
        {"kind": "violation", "rule_id": "label", "impact": "critical",
         "nodes": [{"selector": "#a", "html": ""}]},
        {"kind": "needs_review", "rule_id": "color-contrast", "impact": "serious",
         "nodes": [{"selector": "#b", "html": ""}]},
    ]
    assert score_page(findings) == 85


def test_custom_config():
    config = {**SCORING, "impact_weights": {**W, "minor": 10}}
    assert score_page([_f("r", "minor")], config) == 90


# --- site ---


def test_site_score_is_average_with_worst_five():
    scores = {f"https://x/{i}": s for i, s in enumerate([100, 90, 40, 70, 85, 55, 95])}
    site = score_site(scores)
    assert site.score == round(sum(scores.values()) / 7, 1)
    assert site.worst_pages == [("https://x/2", 40), ("https://x/5", 55),
                                ("https://x/3", 70), ("https://x/4", 85),
                                ("https://x/1", 90)]


def test_site_score_accepts_pairs_and_skips_unscored_pages():
    site = score_site([("a", 80), ("b", None), ("c", 91)])
    assert site == SiteScore(score=85.5, worst_pages=[("a", 80), ("c", 91)])


def test_site_with_no_scored_pages():
    assert score_site({}) == SiteScore(score=None, worst_pages=[])
    assert score_site([("a", None)]).score is None


def test_worst_pages_ties_keep_order():
    assert score_site([("a", 50), ("b", 50), ("c", 10)]).worst_pages == [
        ("c", 10), ("a", 50), ("b", 50)]
