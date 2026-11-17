# Website Accessibility Auditor

Crawls a website, runs axe-core accessibility checks on each page in headless Chromium, scores the results, stores them in SQLite, and shows reports through a FastAPI web UI.

## Stack

- Python, using the project virtualenv at `.venv` (always run tools via `.venv/bin/python -m ...`)
- Playwright (headless Chromium) for page loading
- axe-core via `axe-playwright-python` for accessibility checks
- FastAPI + Jinja2 templates for the web UI / API
- SQLite (stdlib `sqlite3`) for storage
- pytest for tests
- GitHub Actions for CI

## Layout

```
src/auditor/
  crawler.py   # discover pages on a site (robots.txt, same-domain, page cap, rate limit)
  audit.py     # load a page in Playwright and run axe-core
  scoring.py   # turn axe results into scores
  db.py        # SQLite persistence
  api.py       # FastAPI app + Jinja2 templates
tests/
  pages/       # local static HTML fixtures used by tests
  test_*.py
```

## Commands

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest
```

## Rules

- **Every feature gets pytest tests.** No feature is done without tests covering it.
- **Tests never hit live websites.** Use the HTML fixtures in `tests/pages/`, served locally (e.g. a local HTTP server fixture) or loaded from `file://`. Mock any network access that would otherwise go external.
- **Crawler etiquette:**
  - Respect `robots.txt` (stdlib `urllib.robotparser`).
  - Stay on the same domain as the start URL.
  - Enforce a page cap (configurable, with a sensible default).
  - Wait about 1 second between requests (make the delay configurable so tests can set it to 0).
- **Keep dependencies minimal.** Prefer the standard library; add a package to `requirements.txt` only when clearly justified, and pin its version.
