# PR Reviewer

Requirements-grounded PR review: maps a PR's stated intent (description + linked
Linear/Jira tickets) onto the actual diff, with machine-validated line anchors.
See `DESIGN.md` for the full design; `mockup/ui-mockup.html` is the UI spec.

## Install as a Mac app

```bash
./packaging/build_app.sh --install
```

Builds `PR Reviewer.app` and copies it to `/Applications` — it then appears in
Launchpad and Spotlight like any other app. Drop `--install` to build into
`build/` without installing.

The app is a native window (WKWebView) wrapping the local server: it starts the
server on launch, picks the first free port from 8712, and stops it on quit.
Requires `uv` and the `claude` CLI; the bundle injects `~/.local/bin` and
`/opt/homebrew/bin` into `PATH` so both resolve when launched from Finder.

- Python env: `~/Library/Application Support/PR Reviewer/venv` (kept outside the
  bundle so `/Applications` stays read-only). First launch creates it.
- Server log: `~/Library/Logs/PR Reviewer.log` — also under **PR Reviewer →
  Open Server Log** (⌘L).
- Data (`config.json`, `reviews/`) stays in `~/.pr-reviewer`, shared with the
  terminal launcher below.

## Run from a terminal instead

Double-click **`start.command`** in Finder (or run it from a terminal). It
starts the server if needed and opens <http://127.0.0.1:8712> in your browser.
Stop it with **`stop.command`**. Server logs: `~/.pr-reviewer/server.log`.

Or run the server in the foreground yourself:

```bash
uv run uvicorn pr_reviewer.app:app --host 127.0.0.1 --port 8712
```

## Setup (in the app's Settings screen)

- **Claude backend** — uses your existing Claude Code subscription login
  (`claude login`); no API key. The card shows install/login state with fix-it
  guidance, plus a model picker and a full schema-check via Re-check.
- **GitHub** — personal access token (`repo` read scope), then add repos to
  populate the Command Center. Public repos work without a token.
- **Bitbucket** — username + app password (pull request read scope).
- **Linear / Jira** — API credentials for ticket-grounded reviews; PR
  description is always used as a source.

Tokens live in `~/.pr-reviewer/config.json`; reviews in `~/.pr-reviewer/reviews/`.

## Use

Pick a PR in the Command Center (or paste any PR URL) — the pipeline runs
fetch → parse → extract → map → validate and opens the review: split diff with
requirement chips, evidence-backed fulfillment cards, gap/unexplained flags,
net-effect summary, and a mark-verified checklist. PRs with no stated
requirements fall back to explain mode (annotated changes + net effect).

## Tests

```bash
uv run pytest
```
