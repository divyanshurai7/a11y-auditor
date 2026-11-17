"""Turn axe-core findings into 0–100 accessibility scores.

How a page is scored
--------------------
A page starts at 100. Each *violated rule* costs points by its axe impact
(see ``SCORING["impact_weights"]``), so one critical problem (say, a form field
with no label) weighs about as much as two serious ones or seven minor ones.
The weights roughly double at each step, as axe's impact levels are meant to
say how badly a problem blocks users, not just how often it shows up.

The rule is what gets charged, not each element. A page with 30 unlabelled
spacer images has *one* problem with one fix (usually one template). Each
affected element after the first adds a small extra deduction
(``per_extra_element``) so widespread problems still rank below isolated ones,
but that extra is capped per rule (``max_element_deduction_per_rule``). So no
single rule can cost more than its weight plus the cap, and a page's score
reflects how many *different* problems it has.

Needs-review items (axe's "incomplete" results) never lower the score: axe
could not tell whether they are real failures, and charging for them would
punish pages for content axe can't judge (text over images, for example).

The total is clamped to 0–100 and rounded to a whole number. A site's score
is the plain average of its audited pages' scores. Pages that failed to load
have no score and are left out.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# All the numbers used for scoring, in one place, so they're easy to explain and tune.
SCORING = {
    # Score a page with no violations gets.
    "max_score": 100,
    # Points taken off once per violated rule, by axe impact.
    "impact_weights": {
        "critical": 15,
        "serious": 8,
        "moderate": 4,
        "minor": 2,
    },
    # Used when axe reports no impact for a violation (rare).
    "unknown_impact_weight": 4,
    # Extra points off for each affected element after the first...
    "per_extra_element": 0.5,
    # ...but no more than this many extra points per rule.
    "max_element_deduction_per_rule": 5,
    # How many of the lowest-scoring pages score_site reports.
    "worst_pages": 5,
}


def _get(item, name, default=None):
    """Read a field from a Finding dataclass or a dict (as db.get_findings returns)."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _node_count(finding) -> int:
    nodes = _get(finding, "nodes")
    if nodes is not None:
        return len(nodes)
    return int(_get(finding, "node_count", 0) or 0)


def rule_deduction(impact: str | None, node_count: int, config: Mapping = SCORING) -> float:
    """Points one violated rule costs: its impact weight plus a capped
    extra for each affected element after the first."""
    weight = config["impact_weights"].get(impact, config["unknown_impact_weight"])
    extra = config["per_extra_element"] * max(node_count - 1, 0)
    return weight + min(extra, config["max_element_deduction_per_rule"])


def score_page(findings, config: Mapping = SCORING) -> int:
    """Score one page from 0 to 100.

    `findings` may be an `AuditResult` (only its violations count) or an
    iterable of findings: `audit.Finding` objects (treated as violations) or
    dicts like those from `db.get_findings` (any with
    ``kind == "needs_review"`` are ignored). If the same rule appears more than
    once, it is charged once, with all its elements.
    """
    if hasattr(findings, "violations"):
        findings = findings.violations

    # rule_id -> (impact, element count)
    rules: dict[str, tuple[str | None, int]] = {}
    for f in findings:
        if _get(f, "kind", "violation") != "violation":
            continue
        rule_id = _get(f, "rule_id")
        impact, count = rules.get(rule_id, (_get(f, "impact"), 0))
        rules[rule_id] = (impact, count + _node_count(f))

    score = config["max_score"] - sum(
        rule_deduction(impact, count, config) for impact, count in rules.values()
    )
    score = min(max(score, 0), config["max_score"])
    return math.floor(score + 0.5)  # round half up, not to even


@dataclass
class SiteScore:
    # Average page score (one decimal), or None if no page has a score.
    score: float | None
    # Up to SCORING["worst_pages"] (url, score) pairs, lowest score first.
    worst_pages: list[tuple[str, int]] = field(default_factory=list)


def score_site(page_scores, config: Mapping = SCORING) -> SiteScore:
    """Average page score, plus the lowest-scoring pages.

    `page_scores` is a mapping of url -> score, or an iterable of
    (url, score) pairs. Pages whose score is None (failed to load) are left
    out. Pages with equal scores keep their given order.
    """
    pairs: Iterable = page_scores.items() if isinstance(page_scores, Mapping) else page_scores
    scored = [(url, score) for url, score in pairs if score is not None]
    if not scored:
        return SiteScore(score=None, worst_pages=[])
    average = sum(score for _, score in scored) / len(scored)
    worst = sorted(scored, key=lambda pair: pair[1])[: config["worst_pages"]]
    return SiteScore(score=round(average, 1), worst_pages=worst)
