# Design Doc: Requirements-Grounded PR Review Tool

**Status:** v2 — core design and UI agreed; requirement extraction and big-PR reconciliation still open.
**Owner:** Daniel
**Date:** 2026-08-05

## 1. Problem & Value Proposition

AI PR summarizers tell you *what changed*. They don't tell you whether the PR *does what it was supposed to do*. This tool grounds review in the stated intent — the PR description and linked ticket(s) — and produces an evidence-backed map between requirements and the actual diff.

The two-sided value prop:

1. **Clear fulfillment evidence.** For each requirement, point in very clear terms at the exact changes that fulfill it, with a terse explanation of the mechanism. The reviewer verifies claims instead of reverse-engineering the diff.
2. **Gap flagging.** Requirements with no matching changes (possibly unimplemented) and changes with no matching requirement (scope creep, drive-by refactors, risk) are surfaced explicitly.

When no ticket/requirements exist, the tool falls back to explain-and-summarize mode: annotate each logical group of changes with what it does and produce a net-effect summary of the PR.

This turns review from top-to-bottom diff reading into walking a checklist of verifiable claims.

## 2. Scope (prototype)

- **Input:** a PR URL, pasted into a locally-run web app. Host support behind a `PRProvider` interface (§8): GitHub first; Bitbucket as a second implementation (may start stubbed).
- **Ticket sources:** PR description (always), Linear (first integration), Jira (stubbed behind the same interface).
- **Output:** interactive review view (see §7) — requirements with coverage status, anchored to exact diff hunks/lines, plus net-effect summary.
- **Stack:** Python. Locally-run web app — FastAPI backend + single-page frontend (exact frontend approach TBD in UI design phase). Local tokens for GitHub/Linear/Jira; no deployment or auth infrastructure.
- **Out of scope for prototype:** CI integration / posting review comments to GitHub, multi-repo support, team features. But the output data model should be renderable as GitHub review comments later.

## 3. Pipeline

Explicit stages with structured (JSON) outputs — not one giant LLM call:

```
1. FETCH      PR metadata, description, diff; detect ticket refs
              (branch name, title, description) → fetch ticket bodies
2. PARSE      Split diff into hunks; assign stable hunk IDs and line ranges
3. EXTRACT    LLM call #1: ticket(s) + description → discrete requirement list
4. MAP        LLM call #2: requirements + hunks (by ID) → links
5. FLOW       LLM call #3: hunks + links → interaction graph (nodes/edges)
              for the change-flow diagram (§7.2); topology only, no layout
6. VALIDATE   Programmatically verify every anchor (hunk IDs, line ranges)
              cited by the LLM exists — links AND flow nodes; edges only
              between declared nodes; flow nodes canonicalized to one per
              (file, symbol) — duplicates merged, their edges re-pointed
              and deduped; reject/retry invalid output
7. RENDER     Serve the review view from the validated link graph
```

Stage 5 is what makes the anchors trustworthy: because we parse the diff ourselves and hand the model stable IDs, every citation is machine-checkable. Hallucinated anchors are rejected, not rendered.

**No-requirements fallback:** if EXTRACT yields nothing, MAP runs in explain mode — group hunks into logical changes, annotate each ("what it does"), and emit a net-effect summary.

## 4. Data Model

The **requirement↔hunk link** is the central object; requirements and hunks are just its endpoints.

```python
Requirement:
    id: str
    text: str                # quoted/paraphrased from source
    source: str              # "linear:ENG-123" | "jira:PROJ-45" | "pr-description"

Hunk:
    id: str                  # stable, assigned at parse time
    file: str
    line_range: (int, int)   # in the new file
    patch: str

Link:
    requirement_id: str
    hunk_ids: list[str]
    line_ranges: list[(str, int, int)]   # (file, start, end) — precise anchors
    mechanism: str           # ONE terse sentence max; fragments preferred
    status: fulfilled | partial | not_found
    confidence: high | medium | low

FlowNode:
    id: str
    label: str               # "check_lockout()", "_failures {}"
    file: str; line: int     # anchor into the diff — validated like Link anchors
    kind: new | modified | store | ghost   # ghost = not-found requirement's hole

FlowEdge:
    source: str; target: str # FlowNode ids (validated: must exist)
    label: str               # terse: "gate login", "append stamp"
    requirement_ids: list[str]
    missing: bool            # true → expected interaction absent (partial req)

Review:
    requirements: list[Requirement]
    hunks: list[Hunk]
    links: list[Link]
    flow: {nodes: list[FlowNode], edges: list[FlowEdge]}
    net_effect: str          # few bullet-length lines
```

Derived views — one model, three lenses:

- ✅ **Fulfilled:** requirement with link(s), status `fulfilled`
- ⚠️ **Gap:** requirement with no links or status `not_found` / `partial`
- ❓ **Unexplained:** hunk referenced by zero links

## 5. Output Style: Terse by Contract

Verbosity is a product decision, enforced in prompts as a hard constraint (LLMs drift verbose by default):

- `mechanism`: one tight sentence max, fragments over prose.
  Good: "Adds `failed_attempts` counter; `check_lockout()` rejects after 5."
  Bad: a paragraph restating the diff.
- `net_effect`: a few bullet-length lines, not paragraphs.
- Anchors carry the weight — click-through shows the actual code, so the text doesn't have to describe it.

## 6. Fulfillment Cards

Each requirement renders as a card that reads like a mini proof:

| Element | Content |
|---|---|
| Requirement | Terse quote/paraphrase + source badge (Linear/Jira/PR desc) |
| Where | File + line anchors; clickable, highlights exact lines in diff |
| How | The one-sentence `mechanism` |
| Completeness | fulfilled / partial / not found — partial includes what's missing |
| Confidence | Surfaced honestly; "verify the migration" beats false certainty |

Reviewer can check off cards as verified → review-workflow tool, not just a report.

## 7. UI

The app has three screens. The review view is the main product; it's flanked by a command center for choosing what to review and a settings page for integrations.

### 7.1 Command Center (entry screen)

All PRs laid out for selection:

- Lists open PRs across the user's connected repos/hosts (via `PRProvider`), with title, repo, branch, author, linked-ticket badges, and freshness.
- Covers PRs the user **authored and PRs assigned to them for review** (review-requested), not just their own — assigned ones carry a "review requested" badge. Filter tabs: All / Needs my review / My PRs. `PRProvider` must therefore expose author + requested-reviewer info per PR.
- Selecting a PR launches the review pipeline and opens the review view; PRs already reviewed show their status at a glance (e.g., 3/4 fulfilled, 1 unexplained, n claims verified).
- Paste-a-URL remains available as a direct entry path for PRs outside the listed repos.

### 7.2 Review View (main product — settled, see `mockup/ui-mockup.html`)

Agreed layout, captured as a clickable mockup that renders entirely from a fake `Review` JSON (doubling as a proof of the §4 data model):

- **Split diff** (old | new side by side) as the main pane; **explanation rail on the right** with the fulfillment/gap/unexplained cards. Rail is sticky, one scrollable list.
- **Header:** PR title, ticket badges, net-effect summary (bullet-length lines), and a "claims verified" counter.
- **Bidirectional linking:** clicking a card or a `file:line` anchor highlights the exact lines in the diff; chips on each file header (R1, ❓) jump from diff → card.
- **Cards:** status pill (✓ Fulfilled / ◐ Partial / ✗ Not found / ❓ Unexplained), terse requirement text with source badge, one-sentence mechanism, "what's missing" line for partials/gaps, clickable anchors, confidence dot, "Mark verified" button feeding the header counter.
- **Gutter markers:** amber left-border on unexplained lines.
- **Requirement identity system** (added after review feedback — the linking must be obvious without clicking): every requirement/unexplained group has a stable ID + color used identically in three places — the ID badge and colored left-border on its rail card, always-visible colored line tags in a dedicated diff gutter column (e.g. `R1 R4` on lines both cover), and the file-header chips. Tags/chips click through to the matching card and vice versa.
- **Change-flow diagram** (added after review feedback): panel at the bottom of the review view — an interaction graph of the symbols this PR touches. Nodes = changed functions/stores/endpoints (green stripe new, amber modified), edges = calls/reads/writes, colored by the requirement they serve, so the diagram is a traceable fulfillment story. Two gap elements: **dashed amber "missing" edges** render a partial requirement's absent interaction (R2's uncalled `record_success()` is a visibly broken wire), and **dashed red ghost nodes** render not-found requirements as the hole where the implementation should be. Clicking a node highlights its lines in the diff; ghost/missing elements open their gap card. Generated by the FLOW stage (§3): the LLM returns topology only against real anchors (validated); it never positions anything. Layout is deterministic and client-side (the mockup implements the actual algorithm): longest-path layering puts entry points in the left column and downstream effects to the right; barycenter ordering within each column minimizes edge crossings; columns are vertically centered; **port distribution** spreads each node's edge endpoints along its side (sorted by far-end position) so no two edges share a start/end point; **edge labels are placed by search** — labels are measured at their real rendered size (`getBBox`, not estimated widths), then candidate positions along the label's own curve are accepted only when clear of every node, every placed label, and **every edge path** (each curve is sampled and treated as an obstacle); edges are drawn over a white casing so unavoidable crossings read as bridges; ghost nodes sink to the final column. When the search finds no clear spot (dense real-PR graphs), the label is **spiral-placed in guaranteed-free canvas space with a dashed leader line** back to its curve — overlap is impossible by construction, not just unlikely. Node internals budget actual pixels: kind tag top-right, name/loc clipped to the space they really have (full text in hover tooltips), requirement dots on their own bottom row. Layout quality is verified by rendered screenshot against real PR graphs before shipping changes. The implementation in the app's `static/app.js` is the source of truth for this algorithm. Duplicate nodes for the same symbol are impossible by construction — VALIDATE canonicalizes on (file, symbol) and merges.
- **Requirements in text form** (added after review feedback — full text must be referencable without hunting): (a) a compact color-coded **legend strip in the sticky header** listing every requirement's ID, status glyph, and text, clickable through to its card; (b) **hover tooltips on diff gutter tags** showing the full requirement text in place; (c) a collapsible **"Source requirements" panel** in the header showing the original, unprocessed ticket + PR-description text, with the phrase each requirement was extracted from underlined in that requirement's color — doubles as a provenance check on the extraction step.

### 7.3 Settings (integration items)

One page for everything pluggable:

- **PR hosts:** GitHub / Bitbucket token entry, connection test, connected-repo selection for the command center.
- **Ticket sources:** Linear / Jira credentials and connection status; PR-description source needs no config.
- **Claude backend:** the §10 login interface surfaces here — CLI installed?, logged in?, backend ready/not-ready with fix-it guidance.
- Status indicators per integration (connected / error / not configured); tokens stored locally only.

## 8. Pluggable Sources

Both sides of the pipeline's input are behind provider interfaces.

**Ticket sources:**

```python
class RequirementsSource(Protocol):
    def matches(self, ref: str) -> bool          # e.g. "ENG-123" pattern
    def fetch(self, ref: str) -> TicketContent   # title, body, acceptance criteria
```

Auto-detected from ticket references in branch name / title / description.
Build order: PR-description → Linear → Jira (stub).

**PR hosts:**

```python
class PRProvider(Protocol):
    def matches(self, url: str) -> bool          # github.com/... vs bitbucket.org/...
    def fetch(self, url: str) -> PRContent       # metadata, description, branch, diff
```

Selected from the pasted URL. Everything downstream of FETCH (parse, extract,
map, validate, render) is host-agnostic — it only sees `PRContent`.
Build order: GitHub → Bitbucket (may start stubbed). Auth via local tokens
per host, same pattern as ticket sources.

## 9. Big-Diff Handling (design direction, details open)

Large PRs won't fit in one MAP prompt. Plan: chunk by file, map each chunk
against the full requirements list independently, then a final reconciliation
pass to merge links and resolve conflicts (e.g., a requirement fulfilled
across chunks). Designed in from day one — the link-centric data model is
chunking-agnostic, so this only affects the MAP stage internals.

## 10. Claude Code Integration (investigated 2026-08-05)

Question raised: can we link Claude Code to this and piggyback off its review function?

**Finding:** Claude Code's built-in reviews (`/code-review`, `/review <pr>`, cloud `ultrareview`) are bug-hunters — they don't do requirement↔hunk mapping, so they can't replace our pipeline. But `ultrareview --json` emits structured findings that could feed a complementary "bugs" section in our UI later. The real piggyback is using **Claude Code as the LLM backend** for the EXTRACT/MAP stages:

- **Path A — subprocess to `claude -p` (chosen for prototype):** headless mode with `--output-format json` + `--json-schema` enforces our `Review` JSON shape with built-in validation/retries, and uses the existing claude.ai **subscription auth** — no API key, no separate billing. Bonus: ship the review as a custom skill (`.claude/skills/requirements-review/SKILL.md`) so `/requirements-review PR#123` also runs inside Claude Code interactively with repo context + Linear MCP.
- **Path B — Claude Agent SDK (Python):** in-process, Pydantic-typed structured outputs, MCP servers configurable directly. Cleaner FastAPI integration but **API-key only** (no subscription auth).

**Decision:** wrap EXTRACT/MAP behind an `LLMBackend` interface; implement Path A first, keep Path B as a drop-in swap.

**Requirement — Claude login interface.** The app must provide an explicit interface for Claude login integration rather than assuming auth exists:

- On startup (and via a status endpoint), detect whether the `claude` CLI is installed and authenticated with a claude.ai subscription login.
- Surface auth state in the UI (e.g., a status indicator in the header); if not logged in, show clear guidance to run `claude` / `claude login` in a terminal instead of failing mid-review with a cryptic subprocess error.
- Auth concerns live behind the `LLMBackend` interface: the subprocess backend reports subscription-login state; a future SDK backend would report API-key presence instead. The UI consumes one uniform "backend ready / not ready / how to fix" signal.
- Never store or handle Claude credentials ourselves — defer entirely to the Claude Code CLI's own login flow and token storage.

Docs: code.claude.com/docs/en/headless.md, /agent-sdk/python.md, /agent-sdk/structured-outputs.md, /code-review.md.

## 11. Open Questions

1. **Requirement extraction** — hardest prompt in the system: splitting a rambling ticket into discrete, checkable requirements at the right granularity.
2. **Big-PR reconciliation** — merge strategy details for §9.
3. **Frontend approach** — mockup is vanilla JS rendering from `Review` JSON; likely carry that straight into the FastAPI app, revisit only if it gets unwieldy.
4. **Later:** GitHub Action / CI mode posting the same link graph as inline review comments; optional "bugs" section fed by `ultrareview --json`.
