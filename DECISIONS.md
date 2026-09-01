# PR Reviewer — Design Conversation & Decision Log

**Date:** 2026-08-05 · Design session between Daniel and Claude (Cowork)
Companion files: `DESIGN.md` (full design doc), `mockup/ui-mockup.html` (clickable UI spec)

## The idea (Daniel's framing)

With AI tools being so powerful today, build a PR review tool that looks at the PR description and potentially linked tickets, points to specific parts of the changes in the PR, and states which changes align with / point to the PR description and ticket requirements. If there are no preexisting ticket requirements, point to the changes, state what they're doing, and summarize the net effect of the PR.

## How the conversation went

1. **Initial scoping.** Claude proposed form-factor/stack options. Daniel chose: **web app or local app**, ticket requirements from **Linear + Jira + PR description**, built in **Python**, no Linear project tracking — just code. Daniel then paused the build: *"Let's go through a design phase."*

2. **System design.** Claude proposed the pipeline (fetch → parse → extract → map → validate → render), the link-centric data model, and framed three buckets: ✅ fulfilled, ⚠️ requirement with no changes, ❓ changes with no requirement — pitching gap-flagging as the differentiator. Daniel agreed gap-flagging makes sense **but pushed that the value prop is equally about pointing out in very clear terms where requirements ARE fulfilled** — leading to the evidence-backed fulfillment cards (where / how / completeness / confidence) and programmatic anchor validation.

3. **Terseness.** Daniel: *"Nothing too verbose for the explanations."* → Decision: mechanism = one tight sentence max, fragments over prose, net-effect = bullet-length lines; enforced as a hard prompt constraint, with clickable anchors carrying the weight.

4. **UI.** Claude proposed requirements-first vs diff-first layouts and unified vs split diff. **Daniel: side-by-side (split) diff with explanations to the right.** Claude built a clickable mockup (fake "login lockout" PR) rendering entirely from a `Review` JSON matching the data model. Daniel: *"This looks awesome"* → mockup adopted as the UI spec as-is (sticky rail, net-effect in header, verify buttons kept).

## Decisions

| # | Decision | Notes |
|---|---|---|
| 1 | Locally-run web app | FastAPI backend + single-page frontend; paste a GitHub PR URL; local tokens, no deployment/auth infra |
| 2 | Python | Daniel's preference |
| 3 | Ticket sources: PR description + Linear + Jira | Behind a `RequirementsSource` interface, auto-detected from refs. Build order: PR-desc → Linear → Jira (stub) |
| 4 | Staged pipeline, not one big LLM call | fetch → parse → extract (LLM #1) → map (LLM #2) → validate → render; structured JSON at each stage |
| 5 | Link-centric data model | `Link {requirement_id, hunk_ids, line_ranges, mechanism, status, confidence}`; fulfilled/gap/unexplained are three views of one model |
| 6 | Fulfillment is evidence-backed, not a bare checkmark | Cards: requirement + precise anchors + one-sentence mechanism + fulfilled/partial/not-found + honest confidence |
| 7 | Programmatic anchor validation | Every LLM-cited hunk/line is machine-checked against the parsed diff; invalid links rejected/retried |
| 8 | Terse output as a hard contract | One-sentence mechanisms, fragments over prose, bullet-length net effect |
| 9 | No-ticket fallback | Explain mode: annotate logical change groups + net-effect summary |
| 10 | UI: split diff + right explanation rail | Per mockup: sticky card rail, bidirectional card↔diff linking, file-header chips, amber gutter for unexplained, header verify counter, "Mark verified" checklist workflow |
| 11 | Review-as-checklist UX | The reviewer walks down claims and verifies them, instead of reading the diff top-to-bottom |
| 12 | No Linear project for tracking this build | Code only |
| 13 | Claude Code as LLM backend, behind an `LLMBackend` interface | Daniel asked whether we can link Claude Code and piggyback its review function. Investigated: built-in reviews (`/code-review`, `ultrareview`) are bug-hunters, not requirements-mappers — can't replace our pipeline, though `ultrareview --json` could feed a future "bugs" section. Chosen path: subprocess to `claude -p --output-format json --json-schema` for EXTRACT/MAP — schema-enforced output on Daniel's existing subscription auth, no API key. Agent SDK (API-key only) kept as drop-in alternative; optional custom skill makes it runnable inside Claude Code as `/requirements-review`. See DESIGN.md §10 |
| 14 | Claude login integration is a first-class interface | App detects `claude` CLI install + login state, surfaces it in the UI with fix-it guidance instead of failing mid-review; auth state reported uniformly through the `LLMBackend` interface; credentials never handled by us — deferred to Claude Code's own login flow. See DESIGN.md §10 |
| 15 | PR hosts behind a `PRProvider` interface: GitHub + Bitbucket | Daniel flagged that Bitbucket wasn't in the requirements. Added a `PRProvider` abstraction mirroring `RequirementsSource` — selected from the pasted URL, everything downstream is host-agnostic. Build order: GitHub first, Bitbucket second (may start stubbed). See DESIGN.md §8 |
| 16 | App is three screens, not one | The review view (mockup) is the main product only. Added: a **command center** entry screen laying out all open PRs across connected repos for selection (with review-status at a glance; paste-a-URL still works), and a **settings page** for integration items — PR host tokens, Linear/Jira credentials, and the Claude backend login status with fix-it guidance. See DESIGN.md §7 |
| 17 | Requirement identity system in the review view | Daniel's feedback: diff labeling wasn't intuitive and the comments didn't make it obvious which requirement they point to. Fix: stable ID + color per requirement, shown identically on the rail card (badge + colored border), as always-visible line tags in a diff gutter column, and on file-header chips; all cross-clickable. See DESIGN.md §7.2 |
| 18 | Command center shows assigned PRs, not just the user's own | PRs where the user is a requested reviewer appear with a "review requested" badge; filter tabs All / Needs my review / My PRs. `PRProvider` must expose author + requested reviewers. See DESIGN.md §7.1 |
| 19 | Requirements shown in text form in three reference spots | Daniel's feedback: requirement text wasn't visible anywhere for easy reference. Added: color-coded legend strip in the sticky review header (clickable), hover tooltips on diff gutter tags, and a collapsible "Source requirements" panel showing the original ticket/PR text with each extracted phrase underlined in its requirement's color (provenance for the extraction step). See DESIGN.md §7.2 |
| 20 | Change-flow diagram at the bottom of the review view | Daniel's idea: a flowchart showing how the new code interacts and how it fulfills the requirements. Design: interaction graph of changed symbols, edges colored by requirement; dashed amber edges = missing interactions (partials), dashed red ghost nodes = not-found requirements; nodes click through to diff lines. New FLOW pipeline stage (LLM call #3, topology only, anchors validated); client-side auto-layout. See DESIGN.md §3, §4, §7.2 |
| 21 | Flow diagram: deterministic auto-layout + node canonicalization | Daniel's feedback: graph wasn't visually arranged properly and the same node type could appear multiple times. Fix: mockup now renders from `{nodes, edges}` data through the real layout algorithm (longest-path layering, barycenter crossing-minimization, centered columns, label collision-staggering, ghosts in last column); VALIDATE canonicalizes nodes on (file, symbol) so duplicates are merged and their edges re-pointed. See DESIGN.md §3, §7.2 |
| 22 | Product-level flow layout: ports + search-based label placement | Daniel: overlaps still unacceptable, needs polish. Fix: edges get distributed ports along node sides (no shared endpoints), labels placed by candidate search along their own curve rejecting any collision with nodes/labels; verified via rendered screenshot. See DESIGN.md §7.2 |
| 23 | Labels must also clear edge paths; measured not estimated | Daniel: still overlapping — labels were landing on crossing edges, and estimated text widths could spill on other fonts. Fix: label sizes come from real `getBBox` measurement; placement search treats every sampled edge curve as an obstacle; white casing under edges makes remaining crossings read as bridges. Screenshot-verified at two widths. See DESIGN.md §7.2 |
| 24 | Flow layout hardened against real-PR density (fixed in the live app) | Root cause of the persistent overlap: fixes were landing in the mockup while the live app was in use, whose graphs (11 nodes/10 edges on a real PR) are far denser than the mockup sample. Reproduced with the app's actual code + real graph data, then fixed in `static/app.js`: (a) node internals rebuilt — kind tag top-right, name/loc clipped to actual pixel budget with full text in tooltips, requirement dots moved to their own bottom row (they previously collided with the node name); (b) label search widened (denser t-grid, deeper dy range, widest-labels-first ordering); (c) the silent "place anyway" fallback replaced with a spiral search over guaranteed-free canvas space plus a dashed leader line back to the edge — overlap is now impossible, not just unlikely; (d) taller/wider canvas constants. Verified in the browser on the live app. The app is now the source of truth for the layout algorithm; the mockup is a historical design artifact. |

## Open questions (deferred to implementation)

1. **Requirement extraction** — hardest prompt: splitting a rambling ticket into discrete, checkable requirements at the right granularity.
2. **Big-PR handling** — chunk by file, map per chunk, reconciliation pass; merge details TBD.
3. **Later:** GitHub Action / CI mode posting the link graph as inline review comments (data model already renderable that way); complementary "bugs" section fed by `claude ultrareview --json`.

## Agreed next step

First build milestone: pipeline end-to-end on a real GitHub PR (fetch → parse → extract → map → validate) dumping `Review` JSON, then wire the mockup UI on top via FastAPI.
