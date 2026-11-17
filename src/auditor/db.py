"""SQLite storage for crawl results (stdlib sqlite3, no ORM).

Every crawl is saved as a new `audit_runs` row, so a site's results can be
compared over time; nothing is ever overwritten.

    conn = init_db("auditor.db")
    run_id = save_run(conn, domain="example.com", results=results,
                      started_at=..., finished_at=..., max_pages=50,
                      start_url="https://example.com/")
    get_runs(conn)            # newest first
    get_pages(conn, run_id)   # in visit order
    get_findings(conn, page_id)
    rule_counts(conn, run_id)

Timestamps are ISO 8601 strings in UTC, so they sort correctly as text.
Query helpers return plain dicts.

Scores (see `auditor.scoring`) are computed when a run is saved: each audited
page gets `pages.score` (0–100; NULL for pages that failed to load) and the run
gets `audit_runs.site_score` (average page score; NULL if no page has one).

Schema versions (stored in `PRAGMA user_version`):
  1. initial schema
  2. adds pages.score, audit_runs.site_score and audit_runs.start_url;
     migrating a version 1 database computes scores for its existing runs
     (start_url stays NULL for them, as it was never recorded)
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable, Iterator
from os import PathLike

from auditor import scoring
from auditor.audit import AuditResult, Finding

SCHEMA_VERSION = 2

RUN_STATUSES = ("completed", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id      INTEGER PRIMARY KEY,
    domain  TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_runs (
    id          INTEGER PRIMARY KEY,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    max_pages   INTEGER NOT NULL CHECK (max_pages >= 1),
    status      TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    start_url   TEXT,
    site_score  REAL
);
CREATE INDEX IF NOT EXISTS idx_audit_runs_site ON audit_runs(site_id, started_at);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    title         TEXT,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error_message TEXT,
    audited_at    TEXT NOT NULL,
    score         INTEGER CHECK (score BETWEEN 0 AND 100),
    CHECK ((status = 'ok') = (error_message IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_pages_run ON pages(run_id);
CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('violation', 'needs_review')),
    rule_id     TEXT NOT NULL,
    impact      TEXT,
    description TEXT NOT NULL,
    help_url    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_page ON findings(page_id);
CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule_id);

CREATE TABLE IF NOT EXISTS finding_nodes (
    id         INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    selector   TEXT NOT NULL,
    html       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finding_nodes_finding ON finding_nodes(finding_id);
"""


# --- connection ------------------------------------------------------------


def connect(path: str | PathLike) -> sqlite3.Connection:
    """Open a database with foreign keys enforced and rows readable by name.

    The connection is in autocommit mode; writes that must be atomic go
    through `_transaction`.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | PathLike) -> sqlite3.Connection:
    """Open (creating if needed) the database at `path` and ensure the schema
    exists. Safe to call on an existing database. Returns the connection."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} uses schema version {version}; this code supports "
                f"up to {SCHEMA_VERSION}"
            )
        with _transaction(conn):
            # Upgrade existing databases first, one version at a time; a new
            # (version 0) database gets the current schema directly below.
            if version >= 1:
                for v in range(version, SCHEMA_VERSION):
                    _MIGRATIONS[v](conn)
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        conn.close()
        raise
    return conn


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Add score columns and start_url, then score the runs already stored."""
    conn.execute("ALTER TABLE pages ADD COLUMN score INTEGER"
                 " CHECK (score BETWEEN 0 AND 100)")
    conn.execute("ALTER TABLE audit_runs ADD COLUMN start_url TEXT")
    conn.execute("ALTER TABLE audit_runs ADD COLUMN site_score REAL")
    for (page_id,) in conn.execute("SELECT id FROM pages WHERE status = 'ok'").fetchall():
        conn.execute("UPDATE pages SET score = ? WHERE id = ?",
                     (scoring.score_page(get_findings(conn, page_id)), page_id))
    for (run_id,) in conn.execute("SELECT id FROM audit_runs").fetchall():
        page_scores = conn.execute(
            "SELECT url, score FROM pages WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        conn.execute("UPDATE audit_runs SET site_score = ? WHERE id = ?",
                     (scoring.score_site(page_scores).score, run_id))


# _MIGRATIONS[v] upgrades a database from schema version v to v + 1.
_MIGRATIONS = {1: _migrate_1_to_2}


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN ... COMMIT, or ROLLBACK if anything inside raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- saving ----------------------------------------------------------------


def _site_id(conn: sqlite3.Connection, domain: str, name: str | None) -> int:
    """Id of the site for `domain`, creating it if new. A given `name`
    replaces the stored one; otherwise a new site is named after its domain."""
    row = conn.execute("SELECT id FROM sites WHERE domain = ?", (domain,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO sites (domain, name) VALUES (?, ?)",
                           (domain, name or domain))
        return cur.lastrowid
    if name:
        conn.execute("UPDATE sites SET name = ? WHERE id = ?", (name, row["id"]))
    return row["id"]


def _save_findings(conn: sqlite3.Connection, page_id: int, kind: str,
                   findings: Iterable[Finding]) -> None:
    for f in findings:
        cur = conn.execute(
            "INSERT INTO findings (page_id, kind, rule_id, impact, description, help_url)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (page_id, kind, f.rule_id, f.impact, f.description, f.help_url),
        )
        conn.executemany(
            "INSERT INTO finding_nodes (finding_id, selector, html) VALUES (?, ?, ?)",
            [(cur.lastrowid, n.selector, n.html) for n in f.nodes],
        )


def save_run(
    conn: sqlite3.Connection,
    *,
    domain: str,
    results: Iterable,
    started_at: str,
    finished_at: str | None,
    max_pages: int,
    status: str = "completed",
    name: str | None = None,
    start_url: str | None = None,
) -> int:
    """Save one crawl as a new audit run, all in a single transaction.

    `results` are the crawler's per-page results, in visit order: an
    `AuditResult` for an audited page, or anything with `.url` and `.error`
    (a `crawler.PageError`) for a page that failed. Each may carry an
    `audited_at` timestamp; otherwise `finished_at` is used. `start_url` is
    the URL the crawl started from.

    Each audited page is scored with `scoring.score_page`, and the run with
    `scoring.score_site`.

    If anything fails, nothing is saved (not even a new site) and the error
    is re-raised. Returns the new run's id.
    """
    if status not in RUN_STATUSES:
        raise ValueError(f"status must be one of {RUN_STATUSES}, not {status!r}")

    with _transaction(conn):
        site_id = _site_id(conn, domain, name)
        run_id = conn.execute(
            "INSERT INTO audit_runs"
            " (site_id, started_at, finished_at, max_pages, status, start_url)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (site_id, started_at, finished_at, max_pages, status, start_url),
        ).lastrowid

        page_scores = []
        for result in results:
            audited_at = getattr(result, "audited_at", None) or finished_at or started_at
            if isinstance(result, AuditResult):
                score = scoring.score_page(result)
                page_scores.append((result.url, score))
                page_id = conn.execute(
                    "INSERT INTO pages (run_id, url, title, status, audited_at, score)"
                    " VALUES (?, ?, ?, 'ok', ?, ?)",
                    (run_id, result.url, result.title, audited_at, score),
                ).lastrowid
                _save_findings(conn, page_id, "violation", result.violations)
                _save_findings(conn, page_id, "needs_review", result.needs_review)
            else:
                conn.execute(
                    "INSERT INTO pages (run_id, url, status, error_message, audited_at)"
                    " VALUES (?, ?, 'error', ?, ?)",
                    (run_id, result.url, result.error, audited_at),
                )

        conn.execute("UPDATE audit_runs SET site_score = ? WHERE id = ?",
                     (scoring.score_site(page_scores).score, run_id))
    return run_id


# --- queries ---------------------------------------------------------------


def _dicts(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor]


def get_runs(conn: sqlite3.Connection, domain: str | None = None) -> list[dict]:
    """Audit runs, newest first (optionally only for one site), each with its
    site's domain and name and its page / error page counts."""
    sql = """
        SELECT r.id, r.site_id, s.domain, s.name AS site_name,
               r.started_at, r.finished_at, r.max_pages, r.status,
               r.start_url, r.site_score,
               COUNT(p.id) AS page_count,
               COALESCE(SUM(p.status = 'error'), 0) AS error_count
        FROM audit_runs r
        JOIN sites s ON s.id = r.site_id
        LEFT JOIN pages p ON p.run_id = r.id
        {where}
        GROUP BY r.id
        ORDER BY r.started_at DESC, r.id DESC
    """
    if domain is None:
        return _dicts(conn.execute(sql.format(where="")))
    return _dicts(conn.execute(sql.format(where="WHERE s.domain = ?"), (domain,)))


def get_pages(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Pages of a run in visit order, with their score and how many
    violations and needs-review findings (rules, not elements) each has."""
    return _dicts(conn.execute(
        """
        SELECT p.id, p.run_id, p.url, p.title, p.status, p.error_message, p.audited_at,
               p.score,
               COALESCE(SUM(f.kind = 'violation'), 0) AS violation_count,
               COALESCE(SUM(f.kind = 'needs_review'), 0) AS needs_review_count
        FROM pages p
        LEFT JOIN findings f ON f.page_id = p.id
        WHERE p.run_id = ?
        GROUP BY p.id
        ORDER BY p.id
        """,
        (run_id,),
    ))


def get_findings(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """Findings for a page (violations first, then needs-review, each in
    axe's order), each with a `nodes` list of {selector, html}."""
    findings = _dicts(conn.execute(
        """
        SELECT id, page_id, kind, rule_id, impact, description, help_url
        FROM findings
        WHERE page_id = ?
        ORDER BY kind = 'needs_review', id
        """,
        (page_id,),
    ))
    by_id = {f["id"]: f for f in findings}
    for f in findings:
        f["nodes"] = []
    for node in conn.execute(
        """
        SELECT n.finding_id, n.selector, n.html
        FROM finding_nodes n
        JOIN findings f ON f.id = n.finding_id
        WHERE f.page_id = ?
        ORDER BY n.id
        """,
        (page_id,),
    ):
        by_id[node["finding_id"]]["nodes"].append(
            {"selector": node["selector"], "html": node["html"]}
        )
    return findings


def rule_counts(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """How often each rule fired across a run: one row per (kind, rule_id)
    with `page_count` (pages it fired on) and `node_count` (elements flagged).
    Violations first, then most elements flagged."""
    return _dicts(conn.execute(
        """
        SELECT f.kind, f.rule_id,
               MAX(f.description) AS description, MAX(f.help_url) AS help_url,
               COUNT(DISTINCT f.page_id) AS page_count,
               COUNT(n.id) AS node_count
        FROM findings f
        JOIN pages p ON p.id = f.page_id
        LEFT JOIN finding_nodes n ON n.finding_id = f.id
        WHERE p.run_id = ?
        GROUP BY f.kind, f.rule_id
        ORDER BY f.kind = 'needs_review', node_count DESC, f.rule_id
        """,
        (run_id,),
    ))
