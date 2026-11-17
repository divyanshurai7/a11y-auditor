"""Print a summary report for a saved audit run.

Usage::

    python -m auditor.report <run_id> [--db auditor.db] [--top 10]

Shows the site score, the 5 worst pages, and the rules that failed on the
most pages, each with how to fix it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import TextIO

from auditor import db, scoring
from auditor.fixes import get_fix

DEFAULT_TOP_RULES = 10


def _fmt_score(score) -> str:
    if score is None:
        return "n/a"
    if isinstance(score, float) and not score.is_integer():
        return f"{score:.1f}/100"
    return f"{int(score)}/100"


def _indent(text: str, prefix: str) -> str:
    return textwrap.indent(text, prefix)


def top_rules(conn: sqlite3.Connection, run_id: int, limit: int) -> list[dict]:
    """Violated rules of a run, most pages affected first (then most
    elements, then rule id)."""
    rules = [r for r in db.rule_counts(conn, run_id) if r["kind"] == "violation"]
    rules.sort(key=lambda r: (-r["page_count"], -r["node_count"], r["rule_id"]))
    return rules[:limit]


def write_report(conn: sqlite3.Connection, run: dict, out: TextIO,
                 top: int = DEFAULT_TOP_RULES) -> None:
    pages = db.get_pages(conn, run["id"])
    site = scoring.score_site([(p["url"], p["score"]) for p in pages])

    w = lambda line="": print(line, file=out)  # noqa: E731
    w(f"Audit run {run['id']}: {run['site_name']} ({run['domain']})")
    if run.get("start_url"):
        w(f"Started from {run['start_url']}")
    w(f"Started {run['started_at']}, status {run['status']}, "
      f"{run['page_count']} page(s), {run['error_count']} failed to load")
    w()
    # Prefer the stored score; fall back to recomputing it.
    site_score = run["site_score"] if run["site_score"] is not None else site.score
    w(f"Site score: {_fmt_score(site_score)}")
    w()

    w(f"Worst pages (up to {scoring.SCORING['worst_pages']}):")
    if site.worst_pages:
        for url, score in site.worst_pages:
            w(f"  {score:>3}/100  {url}")
    else:
        w("  (no audited pages)")
    w()

    rules = top_rules(conn, run["id"], top)
    w(f"Top rules by pages affected (up to {top}):")
    if not rules:
        w("  (no violations found)")
    for i, rule in enumerate(rules, 1):
        fix = get_fix(rule["rule_id"], rule["description"], rule["help_url"])
        w()
        w(f"{i}. {rule['rule_id']}: {rule['page_count']} page(s), "
          f"{rule['node_count']} element(s)")
        w(_indent(textwrap.fill(f"Fix: {fix.summary}", width=76), "   "))
        if fix.before:
            w("   Before:")
            w(_indent(fix.before, "     "))
            w("   After:")
            w(_indent(fix.after, "     "))
        if fix.help_url:
            w(f"   More: {fix.help_url}")


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="python -m auditor.report",
        description="Print the scores and top fixes for a saved audit run.",
    )
    parser.add_argument("run_id", type=int, help="id of the audit run to report on")
    parser.add_argument("--db", default="auditor.db",
                        help="SQLite database to read (default auditor.db)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_RULES,
                        help=f"how many rules to list (default {DEFAULT_TOP_RULES})")
    args = parser.parse_args(argv)

    if not Path(args.db).is_file():  # don't create an empty database by accident
        print(f"error: database {args.db} not found", file=sys.stderr)
        return 1
    try:
        conn = db.init_db(args.db)
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"error: could not open {args.db}: {exc}", file=sys.stderr)
        return 1
    try:
        run = next((r for r in db.get_runs(conn) if r["id"] == args.run_id), None)
        if run is None:
            print(f"error: no audit run {args.run_id} in {args.db}", file=sys.stderr)
            return 1
        write_report(conn, run, out, top=args.top)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
