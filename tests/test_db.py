"""Tests for auditor.db, each using a fresh temporary database."""

from __future__ import annotations

import sqlite3

import pytest

from auditor import db
from auditor.audit import AuditResult, Finding, FindingNode
from auditor.crawler import PageError

T0 = "2026-01-01T10:00:00.000+00:00"
T1 = "2026-01-01T10:00:05.000+00:00"
T2 = "2026-01-02T10:00:00.000+00:00"
T3 = "2026-01-02T10:00:05.000+00:00"


def _finding(rule_id, *selectors, impact="serious"):
    return Finding(
        rule_id=rule_id,
        impact=impact,
        description=f"{rule_id} description",
        help_url=f"https://dequeuniversity.com/rules/axe/4.10/{rule_id}",
        nodes=[FindingNode(selector=s, html=f"<x id='{s}'>") for s in selectors],
    )


def _crawl_results():
    """A small crawl: two audited pages and one that failed."""
    return [
        AuditResult(
            url="https://example.com/",
            title="Home",
            violations=[_finding("image-alt", "#logo", "#hero"),
                        _finding("label", "#email", impact="critical")],
            needs_review=[_finding("color-contrast", ".banner", impact=None)],
            audited_at="2026-01-01T10:00:01.000+00:00",
        ),
        AuditResult(
            url="https://example.com/about",
            title="About",
            violations=[_finding("image-alt", "#team")],
            audited_at="2026-01-01T10:00:02.000+00:00",
        ),
        PageError(url="https://example.com/missing", error="returned HTTP 404",
                  audited_at="2026-01-01T10:00:03.000+00:00"),
    ]


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def _save(conn, results=None, *, domain="example.com", started=T0, finished=T1, **kw):
    return db.save_run(conn, domain=domain,
                       results=_crawl_results() if results is None else results,
                       started_at=started, finished_at=finished,
                       max_pages=kw.pop("max_pages", 10), **kw)


def _table_counts(conn):
    tables = ["sites", "audit_runs", "pages", "findings", "finding_nodes"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# --- schema ------------------------------------------------------------------


def test_init_db_creates_tables_and_indexes(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    assert {"sites", "audit_runs", "pages", "findings", "finding_nodes"} <= names
    assert {"idx_pages_run", "idx_findings_page", "idx_finding_nodes_finding",
            "idx_audit_runs_site"} <= names
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_db_is_idempotent_and_keeps_data(tmp_path):
    path = tmp_path / "test.db"
    conn = db.init_db(path)
    _save(conn)
    conn.close()

    conn = db.init_db(path)
    try:
        assert len(db.get_runs(conn)) == 1
    finally:
        conn.close()


def test_init_db_refuses_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="schema version"):
        db.init_db(path)


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pages (run_id, url, status, audited_at)"
                     " VALUES (999, 'https://x/', 'ok', ?)", (T0,))


def test_deleting_a_run_deletes_its_pages_and_findings(conn):
    run_id = _save(conn)
    conn.execute("DELETE FROM audit_runs WHERE id = ?", (run_id,))
    counts = _table_counts(conn)
    assert counts == {"sites": 1, "audit_runs": 0, "pages": 0,
                      "findings": 0, "finding_nodes": 0}


# --- saving ------------------------------------------------------------------


def test_save_run_stores_whole_crawl(conn):
    run_id = _save(conn)

    [run] = db.get_runs(conn)
    assert run == {
        "id": run_id, "site_id": run["site_id"], "domain": "example.com",
        "site_name": "example.com", "started_at": T0, "finished_at": T1,
        "max_pages": 10, "status": "completed", "page_count": 3, "error_count": 1,
        "start_url": None, "site_score": 84.5,
    }
    assert _table_counts(conn) == {"sites": 1, "audit_runs": 1, "pages": 3,
                                   "findings": 4, "finding_nodes": 5}


def test_error_pages_are_stored_with_message(conn):
    run_id = _save(conn)
    pages = db.get_pages(conn, run_id)
    error = pages[2]
    assert error["url"] == "https://example.com/missing"
    assert error["status"] == "error"
    assert error["error_message"] == "returned HTTP 404"
    assert error["title"] is None
    assert error["audited_at"] == "2026-01-01T10:00:03.000+00:00"
    assert error["violation_count"] == error["needs_review_count"] == 0
    assert db.get_findings(conn, error["id"]) == []


def test_run_with_only_failed_pages(conn):
    run_id = _save(conn, [PageError(url="https://example.com/", error="timeout")])
    [run] = db.get_runs(conn)
    assert (run["page_count"], run["error_count"]) == (1, 1)
    assert [p["status"] for p in db.get_pages(conn, run_id)] == ["error"]
    assert db.rule_counts(conn, run_id) == []


def test_empty_run_is_saved(conn):
    run_id = _save(conn, [])
    [run] = db.get_runs(conn)
    assert run["id"] == run_id
    assert (run["page_count"], run["error_count"]) == (0, 0)
    assert db.get_pages(conn, run_id) == []


def test_missing_audited_at_falls_back_to_finished_at(conn):
    class LegacyError:  # a result without an audited_at attribute
        url = "https://example.com/"
        error = "boom"

    run_id = _save(conn, [LegacyError()])
    assert db.get_pages(conn, run_id)[0]["audited_at"] == T1


def test_recrawl_creates_new_run_and_keeps_old_one(conn):
    first = _save(conn)
    second_results = _crawl_results()[:1]  # site changed: fewer pages now
    second_results[0].violations.pop()  # and the label problem was fixed
    second = _save(conn, second_results, started=T2, finished=T3)

    assert second != first
    runs = db.get_runs(conn)
    assert [r["id"] for r in runs] == [second, first]  # newest first
    assert runs[0]["site_id"] == runs[1]["site_id"]  # same site, not duplicated
    assert _table_counts(conn)["sites"] == 1

    # The first run's results are untouched.
    assert len(db.get_pages(conn, first)) == 3
    assert len(db.get_pages(conn, second)) == 1
    first_rules = {r["rule_id"] for r in db.rule_counts(conn, first)}
    second_rules = {r["rule_id"] for r in db.rule_counts(conn, second)}
    assert first_rules == {"image-alt", "label", "color-contrast"}
    assert second_rules == {"image-alt", "color-contrast"}


def test_runs_are_kept_per_site(conn):
    _save(conn, domain="example.com")
    _save(conn, domain="other.org", started=T2, finished=T3, name="Other")
    assert [r["domain"] for r in db.get_runs(conn)] == ["other.org", "example.com"]
    assert [r["site_name"] for r in db.get_runs(conn, domain="other.org")] == ["Other"]
    assert len(db.get_runs(conn, domain="example.com")) == 1
    assert db.get_runs(conn, domain="nowhere.net") == []


def test_site_name_is_updated_only_when_given(conn):
    _save(conn, name="Example Inc.")
    _save(conn, started=T2, finished=T3)  # no name: keep the old one
    assert {r["site_name"] for r in db.get_runs(conn)} == {"Example Inc."}
    _save(conn, started=T3, finished=T3, name="Example Ltd.")
    assert {r["site_name"] for r in db.get_runs(conn)} == {"Example Ltd."}


def test_failed_status_is_allowed_but_unknown_status_rejected(conn):
    _save(conn, status="failed")
    assert db.get_runs(conn)[0]["status"] == "failed"
    with pytest.raises(ValueError, match="status"):
        _save(conn, status="done")
    assert len(db.get_runs(conn)) == 1


# --- rollback ----------------------------------------------------------------


def test_failed_save_rolls_back_everything(conn):
    _save(conn)
    before = _table_counts(conn)

    bad = _crawl_results()
    # The last page breaks a NOT NULL constraint, after the new site, run,
    # earlier pages and findings have already been inserted.
    bad.append(AuditResult(url="https://new.example/", title="x",
                           violations=[_finding("image-alt", None)]))
    with pytest.raises(sqlite3.IntegrityError):
        _save(conn, bad, domain="new.example", started=T2, finished=T3)

    assert _table_counts(conn) == before
    assert not conn.in_transaction
    assert [r["domain"] for r in db.get_runs(conn)] == ["example.com"]

    # The connection is still usable afterwards.
    _save(conn, domain="new.example", started=T2, finished=T3)
    assert len(db.get_runs(conn)) == 2


def test_rollback_on_non_database_error(conn):
    def results():
        yield from _crawl_results()
        raise KeyboardInterrupt  # e.g. Ctrl-C half way through saving

    with pytest.raises(KeyboardInterrupt):
        _save(conn, results())
    assert _table_counts(conn) == dict.fromkeys(_table_counts(conn), 0)
    assert not conn.in_transaction


def test_failed_save_leaves_data_visible_to_other_connections_unchanged(tmp_path):
    path = tmp_path / "test.db"
    conn = db.init_db(path)
    _save(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _save(conn, [PageError(url=None, error="x")], started=T2, finished=T3)
    conn.close()

    other = db.connect(path)
    try:
        assert len(db.get_runs(other)) == 1
    finally:
        other.close()


# --- query helpers -----------------------------------------------------------


def test_get_pages_in_visit_order_with_counts(conn):
    run_id = _save(conn)
    pages = db.get_pages(conn, run_id)
    assert [(p["url"], p["title"], p["status"], p["violation_count"],
             p["needs_review_count"]) for p in pages] == [
        ("https://example.com/", "Home", "ok", 2, 1),
        ("https://example.com/about", "About", "ok", 1, 0),
        ("https://example.com/missing", None, "error", 0, 0),
    ]
    assert pages[0]["error_message"] is None
    assert pages[0]["audited_at"] == "2026-01-01T10:00:01.000+00:00"
    assert db.get_pages(conn, run_id + 100) == []


def test_get_findings_with_nodes(conn):
    run_id = _save(conn)
    home = db.get_pages(conn, run_id)[0]
    findings = db.get_findings(conn, home["id"])

    assert [(f["kind"], f["rule_id"], f["impact"]) for f in findings] == [
        ("violation", "image-alt", "serious"),
        ("violation", "label", "critical"),
        ("needs_review", "color-contrast", None),
    ]
    image_alt = findings[0]
    assert image_alt["page_id"] == home["id"]
    assert image_alt["description"] == "image-alt description"
    assert image_alt["help_url"].endswith("/image-alt")
    assert image_alt["nodes"] == [
        {"selector": "#logo", "html": "<x id='#logo'>"},
        {"selector": "#hero", "html": "<x id='#hero'>"},
    ]
    assert db.get_findings(conn, 12345) == []


def test_rule_counts(conn):
    run_id = _save(conn)
    _save(conn, started=T2, finished=T3)  # another run must not be counted
    counts = db.rule_counts(conn, run_id)
    assert [(r["kind"], r["rule_id"], r["page_count"], r["node_count"])
            for r in counts] == [
        ("violation", "image-alt", 2, 3),
        ("violation", "label", 1, 1),
        ("needs_review", "color-contrast", 1, 1),
    ]
    assert counts[0]["help_url"].endswith("/image-alt")
    assert db.rule_counts(conn, run_id + 100) == []


def test_finding_without_nodes_is_counted_with_zero_nodes(conn):
    result = AuditResult(url="https://example.com/", title="Home",
                         violations=[_finding("document-title")])
    run_id = _save(conn, [result])
    [row] = db.rule_counts(conn, run_id)
    assert (row["rule_id"], row["page_count"], row["node_count"]) == ("document-title", 1, 0)


# --- scores and start_url (schema version 2) ---------------------------------


def test_save_run_stores_page_and_site_scores(conn):
    run_id = _save(conn)
    pages = db.get_pages(conn, run_id)
    # Home: image-alt serious x2 (8 + 0.5) + label critical (15) = 23.5 off -> 77.
    # About: image-alt serious x1 = 8 off -> 92. The 404 page has no score.
    assert [p["score"] for p in pages] == [77, 92, None]
    assert db.get_runs(conn)[0]["site_score"] == 84.5


def test_site_score_is_null_without_audited_pages(conn):
    _save(conn, [PageError(url="https://example.com/", error="timeout")])
    assert db.get_runs(conn)[0]["site_score"] is None


def test_save_run_stores_start_url(conn):
    _save(conn, start_url="https://example.com/start")
    assert db.get_runs(conn)[0]["start_url"] == "https://example.com/start"


def test_score_must_be_between_0_and_100(conn):
    run_id = _save(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pages SET score = 101 WHERE run_id = ?", (run_id,))


# The schema exactly as version 1 created it.
_V1_SCHEMA = """
CREATE TABLE sites (id INTEGER PRIMARY KEY, domain TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE audit_runs (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL, finished_at TEXT,
    max_pages INTEGER NOT NULL CHECK (max_pages >= 1),
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')));
CREATE INDEX idx_audit_runs_site ON audit_runs(site_id, started_at);
CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
    url TEXT NOT NULL, title TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error_message TEXT, audited_at TEXT NOT NULL,
    CHECK ((status = 'ok') = (error_message IS NULL)));
CREATE INDEX idx_pages_run ON pages(run_id);
CREATE INDEX idx_pages_url ON pages(url);
CREATE TABLE findings (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('violation', 'needs_review')),
    rule_id TEXT NOT NULL, impact TEXT, description TEXT NOT NULL, help_url TEXT NOT NULL);
CREATE INDEX idx_findings_page ON findings(page_id);
CREATE INDEX idx_findings_rule ON findings(rule_id);
CREATE TABLE finding_nodes (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    selector TEXT NOT NULL, html TEXT NOT NULL);
CREATE INDEX idx_finding_nodes_finding ON finding_nodes(finding_id);

INSERT INTO sites VALUES (1, 'example.com', 'example.com');
INSERT INTO audit_runs VALUES (1, 1, '2026-01-01T10:00:00Z', '2026-01-01T10:00:05Z', 10, 'completed');
INSERT INTO audit_runs VALUES (2, 1, '2026-01-02T10:00:00Z', '2026-01-02T10:00:05Z', 10, 'failed');
INSERT INTO pages VALUES (1, 1, 'https://example.com/', 'Home', 'ok', NULL, '2026-01-01T10:00:01Z');
INSERT INTO pages VALUES (2, 1, 'https://example.com/about', 'About', 'ok', NULL, '2026-01-01T10:00:02Z');
INSERT INTO pages VALUES (3, 1, 'https://example.com/x', NULL, 'error', '404', '2026-01-01T10:00:03Z');
INSERT INTO pages VALUES (4, 2, 'https://example.com/', NULL, 'error', 'timeout', '2026-01-02T10:00:01Z');
INSERT INTO findings VALUES (1, 1, 'violation', 'image-alt', 'serious', 'd', 'u');
INSERT INTO findings VALUES (2, 1, 'violation', 'label', 'critical', 'd', 'u');
INSERT INTO findings VALUES (3, 1, 'needs_review', 'color-contrast', 'serious', 'd', 'u');
INSERT INTO finding_nodes VALUES (1, 1, '#logo', '<img>');
INSERT INTO finding_nodes VALUES (2, 1, '#hero', '<img>');
INSERT INTO finding_nodes VALUES (3, 2, '#email', '<input>');
INSERT INTO finding_nodes VALUES (4, 3, '.banner', '<p>');
PRAGMA user_version = 1;
"""


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_migrates_version_1_database(tmp_path):
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript(_V1_SCHEMA)
    raw.close()

    conn = db.init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "score" in _columns(conn, "pages")
        assert {"site_score", "start_url"} <= set(_columns(conn, "audit_runs"))

        # Existing data is kept and scored.
        assert [p["score"] for p in db.get_pages(conn, 1)] == [77, 100, None]
        runs = {r["id"]: r for r in db.get_runs(conn)}
        assert runs[1]["site_score"] == 88.5
        assert runs[2]["site_score"] is None  # no audited pages
        assert runs[1]["start_url"] is None  # never recorded in version 1
        assert len(db.get_findings(conn, 1)) == 3

        # New runs save normally into the migrated database.
        run_id = _save(conn, start_url="https://example.com/")
        assert [p["score"] for p in db.get_pages(conn, run_id)] == [77, 92, None]
    finally:
        conn.close()

    # Opening again doesn't try to migrate twice.
    db.init_db(path).close()


def test_failed_migration_leaves_version_1_database_untouched(tmp_path, monkeypatch):
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript(_V1_SCHEMA)
    raw.close()

    def boom(findings):
        raise RuntimeError("scoring failed")

    monkeypatch.setattr(db.scoring, "score_page", boom)
    with pytest.raises(RuntimeError, match="scoring failed"):
        db.init_db(path)

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "score" not in [r[1] for r in raw.execute("PRAGMA table_info(pages)")]
    finally:
        raw.close()
