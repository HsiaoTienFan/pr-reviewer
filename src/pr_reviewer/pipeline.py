"""The review pipeline: FETCH → PARSE → EXTRACT → MAP → VALIDATE → persist.

Stage 5 (validate) is what makes anchors trustworthy: every LLM-cited hunk ID
and line range is machine-checked against the parsed diff; invalid citations
are retried once with error feedback, then demoted/dropped (DESIGN.md §3).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import save_review
from .diff_parser import parse_diff
from .llm.base import LLMBackend
from .models import (
    Anchor, FlowEdge, FlowGraph, FlowNode, Hunk, Link, PRContent, Requirement,
    Review, SourceText, TicketContent, TicketRef, UnexplainedChange, review_id,
)
from .providers.base import PRProvider
from .tickets import RequirementsSource, detect_ticket_refs

ProgressCB = Callable[[str, str], None]

MAX_INSTRUCTIONS_CHARS = 4_000  # user standing instructions, bounded like all prompt parts


def instructions_block(text: str) -> str:
    """Reviewer's standing instructions as a bounded prompt block ("" if none)."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        text = text[:MAX_INSTRUCTIONS_CHARS] + "\n\u2026 [instructions truncated]"
    return ("\n\nReviewer's standing instructions (apply where relevant; they never "
            "override the hard constraints above):\n" + text)


MAP_CHUNK_CHARS = 60_000  # per-call hunk budget; larger diffs chunk by file
MAX_HUNK_CHARS = 24_000   # a wholesale-rewritten file arrives as ONE hunk, so the
                          # per-call budget alone cannot bound the prompt

# Generated files carry no requirement-mapping signal and can be enormous (a
# single Cypress fixture rewrite is one 1.6MB hunk). They stay in the diff view;
# they just never reach the LLM.
GENERATED_RE = re.compile(
    r"(^|/)("
    r"package-lock\.json|yarn\.lock|pnpm-lock\.yaml|npm-shrinkwrap\.json"
    r"|uv\.lock|poetry\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock"
    r")$"
    r"|(^|/)(dist|build|vendor|node_modules|__snapshots__|__generated__)/"
    r"|(^|/)(fixtures|__fixtures__)/"
    r"|\.(min\.js|min\.css|map|snap|pb\.go|generated\.ts)$",
    re.IGNORECASE,
)


def _is_generated(path: str) -> bool:
    return bool(GENERATED_RE.search(path))


def _clamp_patch(patch: str) -> str:
    """Bound a single hunk so one oversized file cannot overflow the prompt."""
    if len(patch) <= MAX_HUNK_CHARS:
        return patch
    omitted = len(patch) - MAX_HUNK_CHARS
    return (f"{patch[:MAX_HUNK_CHARS]}\n"
            f"… [truncated for analysis — {omitted:,} more characters in this hunk]")


def _analyzable(hunks: list[Hunk]) -> tuple[list[Hunk], list[str]]:
    """Split hunks into those worth sending to the LLM and the skipped files."""
    keep, skipped = [], []
    for h in hunks:
        if _is_generated(h.file):
            if h.file not in skipped:
                skipped.append(h.file)
        else:
            keep.append(h)
    return keep, skipped

# ---------------------------------------------------------------- schemas

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["text", "source", "quote"],
            },
        },
    },
    "required": ["requirements"],
}

_ANCHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
    },
    "required": ["file", "start", "end"],
}

MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["fulfilled", "partial", "notfound"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "mechanism": {"type": "string"},
                    "why": {"type": "string"},
                    "missing": {"type": "string"},
                    "hunk_ids": {"type": "array", "items": {"type": "string"}},
                    "anchors": {"type": "array", "items": _ANCHOR_SCHEMA},
                },
                "required": ["requirement_id", "status", "confidence", "mechanism", "why", "hunk_ids", "anchors"],
            },
        },
        "unexplained": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "mechanism": {"type": "string"},
                    "hunk_ids": {"type": "array", "items": {"type": "string"}},
                    "anchors": {"type": "array", "items": _ANCHOR_SCHEMA},
                },
                "required": ["label", "mechanism", "hunk_ids", "anchors"],
            },
        },
        "net_effect": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["links", "unexplained", "net_effect"],
}

# ---------------------------------------------------------------- prompts

EXTRACT_PROMPT = """You extract discrete, checkable requirements from a pull request's stated intent.

Rules — hard constraints:
- Each requirement is ONE verifiable behavior or change, phrased tersely (max ~20 words).
- Quote or closely paraphrase the source; do not invent requirements that are not stated.
- Skip non-behavioral boilerplate (checklists, "add tests" unless it is the point, review etiquette).
- At most 10 requirements. If the text states no concrete requirements, return an empty list.
- source must be exactly one of the source tags listed below.
- quote: the short phrase copied VERBATIM (exact characters) from that source which states the requirement — used for provenance underlining. Never paraphrase inside quote.

{sources_block}

PR title: {title}

PR description:
{description}
{tickets_block}"""

MAP_PROMPT = """You map stated requirements to the hunks of a PR diff, producing evidence-backed links.

Hard constraints:
- mechanism: ONE terse sentence max; fragments preferred over prose. Good: "Adds `failed_attempts` counter; `check_lockout()` rejects after 5." Never restate the diff.
- why: ONE terse sentence justifying the status — the causal link from the cited code to the requirement being met. Good: "Lockout check runs before password verify, so a 6th attempt is rejected regardless of credentials." Not a restatement of mechanism; explain the connection. For partial/notfound, why the evidence falls short.
- Every anchor {{file,start,end}} MUST use new-file line numbers that lie inside the line range of a hunk you cite in hunk_ids. Never cite lines or files not shown below.
- status: "fulfilled" only when the cited changes clearly implement the requirement; "partial" when some of it is there (say what's missing in `missing`); "notfound" when no changes match (empty hunk_ids/anchors, put the gap in `missing`).
- Judge ONLY from the hunk text shown. You see hunks with a few context lines, not whole files: do NOT assume the existence or behavior of code outside them (callers, definitions, config, migrations, tests). A name is not evidence — `validateToken()` being called proves a call, not validation.
- confidence: "high" only when the cited hunks alone prove the status. If the causal chain crosses code you cannot see, use "medium"/"low" and name the unverified dependency in `why` or `missing` (e.g. "assumes `ensureAuthorization` runs on every resolver — not visible in this diff").
- confidence: honest ("verify the migration" beats false certainty). Use "low"/"medium" when unsure.
- Every requirement gets exactly one link entry.
- unexplained: hunks (or groups of hunks) that no requirement explains — scope creep, drive-by refactors, unrelated fixes. Give each a short label + one-line mechanism + anchors. Mechanical consequences of a linked change (imports, call-site updates for a required rename) belong with that link, NOT in unexplained. Every hunk should end up cited by exactly one link or one unexplained entry where feasible.
- net_effect: 2-5 bullet-length lines summarizing what merging this PR changes. Terse.

Requirements:
{requirements_block}

Diff hunks (id — file — new-file line range):
{hunks_block}
{feedback_block}"""

EXPLAIN_PROMPT = """No stated requirements exist for this PR, so annotate the changes themselves.

Group the hunks below into logical changes. For each group emit one entry in `unexplained` with:
- label: short name for the change (e.g. "Rename log_event → emit_event").
- mechanism: ONE terse sentence — what it does, fragments preferred.
- hunk_ids + anchors: cite only new-file line ranges inside the hunks you reference.
Every hunk must be covered by exactly one group. Leave `links` empty.
net_effect: 2-5 bullet-length lines summarizing what merging this PR changes.

PR title: {title}

PR description:
{description}

Diff hunks (id — file — new-file line range):
{hunks_block}
{feedback_block}"""


def _hunks_block(hunks: list[Hunk]) -> str:
    parts = []
    for h in hunks:
        parts.append(f"### {h.id} — {h.file} (new-file lines {h.start}–{h.end})\n"
                     f"{_clamp_patch(h.patch)}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- validation

def validate_mapping(
    raw: dict[str, Any],
    hunks: list[Hunk],
    requirements: list[Requirement],
) -> tuple[list[Link], list[UnexplainedChange], list[str]]:
    """Machine-check every cited hunk ID and line range. Returns valid links,
    valid unexplained entries, and a list of violation strings (for retry)."""
    errors: list[str] = []
    by_id = {h.id: h for h in hunks}
    by_file: dict[str, list[Hunk]] = {}
    for h in hunks:
        by_file.setdefault(h.file, []).append(h)

    def check_anchors(anchors_raw: list[dict], hunk_ids: list[str], ctx: str) -> list[Anchor]:
        valid: list[Anchor] = []
        for a in anchors_raw:
            file, start, end = a.get("file", ""), int(a.get("start", 0)), int(a.get("end", 0))
            if start > end:
                start, end = end, start
            candidates = [h for h in by_file.get(file, []) if h.id in hunk_ids] or by_file.get(file, [])
            hit = next((h for h in candidates if start <= h.end and end >= h.start), None)
            if hit is None:
                errors.append(f"{ctx}: anchor {file}:{start}-{end} is not inside any cited hunk")
                continue
            valid.append(Anchor(file=file, start=max(start, hit.start), end=min(end, hit.end)))
        return valid

    links: list[Link] = []
    seen_req: set[str] = set()
    req_ids = {r.id for r in requirements}
    for l in raw.get("links", []):
        rid = l.get("requirement_id", "")
        if rid not in req_ids:
            errors.append(f"link cites unknown requirement '{rid}'")
            continue
        if rid in seen_req:
            errors.append(f"duplicate link for requirement '{rid}'")
            continue
        seen_req.add(rid)
        hunk_ids = [h for h in l.get("hunk_ids", []) if h in by_id]
        for h in l.get("hunk_ids", []):
            if h not in by_id:
                errors.append(f"link {rid}: unknown hunk id '{h}'")
        anchors = check_anchors(l.get("anchors", []), hunk_ids, f"link {rid}")
        if not anchors and hunk_ids:
            anchors = [Anchor(file=by_id[h].file, start=by_id[h].start, end=by_id[h].end) for h in hunk_ids]
        status = l.get("status", "notfound")
        if status != "notfound" and not anchors:
            errors.append(f"link {rid}: status '{status}' but no valid evidence anchors")
        links.append(Link(
            requirement_id=rid,
            hunk_ids=hunk_ids,
            anchors=anchors,
            mechanism=l.get("mechanism", ""),
            why=l.get("why", ""),
            missing=l.get("missing", ""),
            status=status if status in ("fulfilled", "partial", "notfound") else "notfound",
            confidence=l.get("confidence", "medium") if l.get("confidence") in ("high", "medium", "low") else "medium",
        ))

    unexplained: list[UnexplainedChange] = []
    for idx, u in enumerate(raw.get("unexplained", []), 1):
        hunk_ids = [h for h in u.get("hunk_ids", []) if h in by_id]
        anchors = check_anchors(u.get("anchors", []), hunk_ids, f"unexplained '{u.get('label', idx)}'")
        if not anchors and hunk_ids:
            anchors = [Anchor(file=by_id[h].file, start=by_id[h].start, end=by_id[h].end) for h in hunk_ids]
        if not anchors:
            errors.append(f"unexplained '{u.get('label', idx)}' has no valid anchors — dropped")
            continue
        unexplained.append(UnexplainedChange(
            id=f"U{len(unexplained) + 1}",
            label=u.get("label", f"Change {idx}"),
            mechanism=u.get("mechanism", ""),
            hunk_ids=hunk_ids,
            anchors=anchors,
        ))
    return links, unexplained, errors


def _finalize(links: list[Link], requirements: list[Requirement]) -> list[Link]:
    """Guarantee exactly one link per requirement; demote unproven claims."""
    by_req = {l.requirement_id: l for l in links}
    out = []
    for r in requirements:
        l = by_req.get(r.id)
        if l is None:
            l = Link(requirement_id=r.id, status="notfound", confidence="low",
                     missing="No matching changes found.")
        elif l.status != "notfound" and not l.anchors:
            l = l.model_copy(update={
                "status": "notfound", "confidence": "low",
                "missing": (l.missing or "Cited changes could not be verified against the diff."),
            })
        out.append(l)
    return out


def _coverage_fill(
    links: list[Link], unexplained: list[UnexplainedChange], hunks: list[Hunk],
) -> list[UnexplainedChange]:
    """Derived-view honesty: any hunk cited by zero links/entries gets an
    explicit unexplained card instead of silently vanishing."""
    cited: set[str] = set()
    for l in links:
        cited.update(l.hunk_ids)
    for u in unexplained:
        cited.update(u.hunk_ids)
    out = list(unexplained)
    for h in hunks:
        if h.id not in cited:
            out.append(UnexplainedChange(
                id=f"U{len(out) + 1}",
                label=f"Unannotated change in {h.file}",
                mechanism="Not referenced by any requirement or annotation.",
                hunk_ids=[h.id],
                anchors=[Anchor(file=h.file, start=h.start, end=h.end)],
            ))
    return out


def _merge_chunk_maps(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconcile per-chunk MAP outputs (DESIGN.md §9)."""
    rank = {"fulfilled": 2, "partial": 1, "notfound": 0}
    conf_rank = {"high": 2, "medium": 1, "low": 0}
    merged_links: dict[str, dict] = {}
    unexplained: list[dict] = []
    net: list[str] = []
    for c in chunks:
        for l in c.get("links", []):
            rid = l.get("requirement_id", "")
            cur = merged_links.get(rid)
            if cur is None:
                merged_links[rid] = dict(l)
                continue
            if rank.get(l.get("status"), 0) > rank.get(cur.get("status"), 0):
                cur["status"] = l["status"]
                cur["missing"] = l.get("missing", "")
            # keep every chunk's explanation, not just the best-status one
            for key, cap in (("mechanism", 400), ("why", 400)):
                joined = "; ".join(dict.fromkeys(s for s in [cur.get(key, ""), l.get(key, "")] if s))
                cur[key] = joined[:cap]
            cur["hunk_ids"] = list(dict.fromkeys(cur.get("hunk_ids", []) + l.get("hunk_ids", [])))
            cur["anchors"] = cur.get("anchors", []) + l.get("anchors", [])
            if conf_rank.get(l.get("confidence"), 1) < conf_rank.get(cur.get("confidence"), 1):
                cur["confidence"] = l.get("confidence")
        unexplained.extend(c.get("unexplained", []))
        for line in c.get("net_effect", []):
            if line not in net:
                net.append(line)
    return {"links": list(merged_links.values()), "unexplained": unexplained, "net_effect": net[:6]}


# ---------------------------------------------------------------- change-flow diagram (FLOW stage)

FLOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["new", "modified", "store"]},
                },
                "required": ["id", "label", "file", "line", "kind"],
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "label": {"type": "string"},
                    "requirement_ids": {"type": "array", "items": {"type": "string"}},
                    "missing": {"type": "boolean"},
                },
                "required": ["source", "target", "label", "requirement_ids", "missing"],
            },
        },
        "summary": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["nodes", "edges", "summary"],
}

FLOW_PROMPT = """You are building a change-flow diagram for a PR review: the interaction graph of the symbols this PR touches. Topology only — layout is computed elsewhere.

Hard constraints:
- nodes: the changed functions/methods/endpoints/data stores in the hunks below. kind: "new" (added symbol), "modified" (changed symbol), or "store" (state/data store). label: terse symbol name, e.g. "check_lockout()" or "_failures {{}}". file + line: the NEW-file line of the symbol's change — MUST lie inside a hunk's line range below. At most 12 nodes; skip trivia (imports, constants) unless central to the change.
- edges: interactions introduced or modified by this PR between those nodes — calls, reads, writes, emits. label: max 4 words ("gate login", "append stamp"). requirement_ids: which item ids below the interaction serves (empty list if none). missing=false.
- missing edges: for each partial item whose gap is an absent interaction between two emitted nodes, add ONE edge with missing=true from the node that should act to the target, label naming the absent interaction ("missing call"). Never invent missing edges for gaps that are not interactions.
- summary: 2-4 bullet-length lines explaining how this flow fulfills (or fails) the items — reference item ids (R1) and node labels; terse fragments, no prose. Example: "R1: login() gates on check_lockout() before verify, so a 6th attempt is rejected."

Items ({mode_label}):
{items_block}

Diff hunks (id — file — new-file line range):
{hunks_block}
{feedback_block}"""


def validate_flow(
    raw: dict[str, Any], hunks: list[Hunk], known_ids: set[str],
) -> tuple[FlowGraph, list[str]]:
    """Machine-check flow topology: node anchors must land in hunks, edges must
    connect kept nodes. Invalid elements are dropped with recorded errors."""
    errors: list[str] = []
    by_file: dict[str, list[Hunk]] = {}
    for h in hunks:
        by_file.setdefault(h.file, []).append(h)

    nodes: list[FlowNode] = []
    seen_ids: set[str] = set()
    raw_nodes = raw.get("nodes", [])
    cap_dropped = max(0, len(raw_nodes) - 12)
    for n in raw_nodes[:12]:
        nid = str(n.get("id", "")).strip()
        label = str(n.get("label", "")).strip()
        kind = n.get("kind", "modified")
        if not nid or not label or nid in seen_ids:
            errors.append(f"node '{nid or label}' missing id/label or duplicate")
            continue
        if kind not in ("new", "modified", "store"):
            kind = "modified"
        file = str(n.get("file", ""))
        try:
            line = int(n.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        hit = next((h for h in by_file.get(file, []) if h.start <= line <= h.end), None)
        if hit is None:
            errors.append(f"node '{nid}': anchor {file}:{line} is not inside any hunk")
            continue
        seen_ids.add(nid)
        nodes.append(FlowNode(id=nid, label=label[:40], file=file, line=line, kind=kind))

    # canonicalize: one node per (file, symbol) — duplicates merged, edges
    # re-pointed to the survivor (DECISIONS #21; a merge, not an error)
    canon: dict[tuple[str, str], str] = {}
    alias: dict[str, str] = {}
    kept: list[FlowNode] = []
    for n in nodes:
        key = (n.file, n.label)
        if key in canon:
            alias[n.id] = canon[key]
            continue
        canon[key] = n.id
        kept.append(n)
    nodes = kept
    kept_ids = {n.id for n in nodes}

    edges: list[FlowEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for e in raw.get("edges", []):
        src, tgt = str(e.get("source", "")), str(e.get("target", ""))
        src, tgt = alias.get(src, src), alias.get(tgt, tgt)
        label = str(e.get("label", ""))[:40]
        if src not in kept_ids or tgt not in kept_ids or src == tgt:
            errors.append(f"edge '{src}→{tgt}' references unknown or identical nodes")
            continue
        key = (src, tgt, label)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(FlowEdge(
            source=src, target=tgt, label=label,
            requirement_ids=[r for r in e.get("requirement_ids", []) if r in known_ids],
            missing=bool(e.get("missing")),
        ))
    summary = [str(s).strip() for s in raw.get("summary", []) if str(s).strip()][:5]
    return FlowGraph(nodes=nodes, edges=edges, summary=summary, dropped=cap_dropped), errors


async def build_flow(
    review_hunks: list[Hunk],
    items: list[tuple[str, str, str]],  # (id, status, text)
    mode_label: str,
    backend: LLMBackend,
    progress: ProgressCB = lambda stage, detail: None,
) -> FlowGraph:
    items_block = "\n".join(f"{i} [{s}]: {t}" for i, s, t in items)
    known_ids = {i for i, _, _ in items}
    # flow runs on at most one chunk's worth of hunks — huge PRs get a partial graph
    all_chunks = _chunk_hunks(review_hunks)
    hunks = all_chunks[0] if all_chunks else []
    is_partial = len(all_chunks) > 1
    feedback = ""
    flow = FlowGraph()
    for attempt in range(2):
        progress("flow", f"Building change-flow diagram (LLM call 3{', retry' if attempt else ''})")
        raw = await backend.structured(
            FLOW_PROMPT.format(mode_label=mode_label, items_block=items_block,
                               hunks_block=_hunks_block(hunks), feedback_block=feedback),
            FLOW_SCHEMA,
        )
        flow, errors = validate_flow(raw, review_hunks, known_ids)
        if not errors:
            break
        feedback = "\nYour previous answer had invalid elements — fix them:\n- " + "\n- ".join(errors[:12])
    flow.partial = is_partial
    return flow


def add_ghost_nodes(flow: FlowGraph, requirements: list[Requirement], links: list[Link]) -> FlowGraph:
    """Deterministic honesty: every not-found requirement renders as the hole
    where its implementation should be."""
    for l in links:
        if l.status != "notfound":
            continue
        req = next((r for r in requirements if r.id == l.requirement_id), None)
        if req is None:
            continue
        label = (req.text[:32] + "…") if len(req.text) > 33 else req.text
        flow.nodes.append(FlowNode(
            id=f"ghost-{req.id}", label=f"{label} — not found",
            kind="ghost", requirement_id=req.id,
        ))
    return flow


# ---------------------------------------------------------------- reviewer-added requirements

def _next_req_id(review: Review) -> str:
    n = max([int(r.id[1:]) for r in review.requirements if r.id[1:].isdigit()] or [0]) + 1
    return f"R{n}"


def _chunk_hunks(hunks: list[Hunk]) -> list[list[Hunk]]:
    chunks: list[list[Hunk]] = []
    cur: list[Hunk] = []
    cur_size = 0
    for h in hunks:
        # size as the prompt will actually see it, post-clamp
        size = min(len(h.patch), MAX_HUNK_CHARS)
        if cur and cur_size + size > MAP_CHUNK_CHARS:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(h)
        cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


async def map_new_requirement(
    review: Review,
    text: str,
    backend: LLMBackend,
    progress: ProgressCB = lambda stage, detail: None,
) -> Review:
    """Map a single reviewer-written requirement against the stored diff and
    append it (plus its evidence link) to the review."""
    req = Requirement(id=_next_req_id(review), text=text.strip(), source="reviewer")
    req_block = f"{req.id}: {req.text}  [source: reviewer]"
    base_note = "\nOnly map the single requirement above; leave `unexplained` and `net_effect` empty."

    outs: list[dict[str, Any]] = []
    chunks = _chunk_hunks(review.hunks)
    for i, chunk in enumerate(chunks, 1):
        label = f" (chunk {i}/{len(chunks)})" if len(chunks) > 1 else ""
        feedback = base_note
        raw: dict[str, Any] = {"links": [], "unexplained": [], "net_effect": []}
        for attempt in range(2):
            progress("map", f"Mapping new requirement{label}{', retry' if attempt else ''}")
            raw = await backend.structured(
                MAP_PROMPT.format(requirements_block=req_block, hunks_block=_hunks_block(chunk),
                                  feedback_block=feedback),
                MAP_SCHEMA,
            )
            _, _, errors = validate_mapping(raw, review.hunks, [req])
            if not errors:
                break
            feedback = base_note + "\nYour previous answer had invalid citations — fix them:\n- " + "\n- ".join(errors[:12])
        outs.append(raw)

    merged = _merge_chunk_maps(outs) if len(outs) > 1 else (outs[0] if outs else {"links": [], "unexplained": [], "net_effect": []})
    links, _, _ = validate_mapping(merged, review.hunks, [req])
    link = _finalize(links, [req])[0]

    review.requirements.append(req)
    review.links.append(link)
    save_review(review)
    return review


async def merge_rerun_state(
    new: Review,
    prev: Review,
    backend: LLMBackend,
    progress: ProgressCB = lambda stage, detail: None,
) -> Review:
    """Carry reviewer-owned state across a force re-run: verified checkmarks
    (matched by requirement/change text), reviewer-added requirements
    (re-mapped against the fresh diff), and the bugs section (re-anchored)."""
    # verified: map prev card ids → their text → matching new card ids
    prev_text: dict[str, str] = {}
    for r in prev.requirements:
        prev_text[r.id] = r.text.strip().lower()
    for u in prev.unexplained:
        prev_text[u.id] = u.label.strip().lower()
    new_by_text: dict[str, str] = {}
    for r in new.requirements:
        new_by_text.setdefault(r.text.strip().lower(), r.id)
    for u in new.unexplained:
        new_by_text.setdefault(u.label.strip().lower(), u.id)
    carried = {new_by_text[prev_text[v]] for v in prev.verified
               if v in prev_text and prev_text[v] in new_by_text}
    new.verified = sorted(set(new.verified) | carried)

    # reviewer-added requirements: re-map against the new diff (one LLM call each)
    new_texts = {r.text.strip().lower() for r in new.requirements}
    for r in prev.requirements:
        if r.source == "reviewer" and r.text.strip().lower() not in new_texts:
            was_verified = r.id in prev.verified
            await map_new_requirement(new, r.text, backend, progress)  # appends + saves
            if was_verified:
                new.verified = sorted(set(new.verified) | {new.requirements[-1].id})

    # bugs: carry findings, re-anchored against the new hunks
    if prev.bugs_ran:
        by_file: dict[str, list[Hunk]] = {}
        for h in new.hunks:
            by_file.setdefault(h.file, []).append(h)
        bugs = []
        for b in prev.bugs:
            kept = []
            for a in b.anchors:
                hit = next((h for h in by_file.get(a.file, []) if a.start <= h.end and a.end >= h.start), None)
                if hit:
                    kept.append(Anchor(file=a.file, start=max(a.start, hit.start), end=min(a.end, hit.end)))
            bugs.append(b.model_copy(update={"anchors": kept}))
        new.bugs = bugs
        new.bugs_ran = True
        new.bugs_stale = bool(bugs)  # carried, not re-derived — flag until re-run

    save_review(new)
    return new


# ---------------------------------------------------------------- pipeline

async def run_review(
    provider: PRProvider,
    repo: str,
    number: int,
    sources: dict[str, RequirementsSource],
    backend: LLMBackend,
    progress: ProgressCB = lambda stage, detail: None,
    instructions: str = "",
) -> Review:
    overflow: dict[str, int] = {}  # LLM output beyond hard caps — surfaced, not silent
    _instr = instructions_block(instructions)

    # FETCH
    progress("fetch", f"Fetching {provider.name}:{repo} #{number}")
    pr: PRContent = await provider.fetch(repo, number)

    # PARSE
    progress("parse", "Parsing diff into hunks")
    files, hunks = parse_diff(pr.diff)

    # ticket refs → ticket bodies
    progress("tickets", "Detecting ticket references")
    refs = detect_ticket_refs(pr.branch, pr.title, pr.description)
    tickets: list[TicketContent] = []
    for ref in refs:
        for src in sources.values():
            if not src.configured():
                continue
            t = await src.fetch(ref)
            if t:
                tickets.append(t)
                break
    pr.tickets = [TicketRef(key=t.key, source=t.source, url=t.url, title=t.title) for t in tickets]

    # PR discussion — reviewer conversations often carry the real requirements
    discussion_text = ""
    fetch_comments = getattr(provider, "fetch_comments", None)
    if fetch_comments is not None:
        try:
            comments = await fetch_comments(repo, number)
            discussion_text = "\n".join(f"@{c['author']}: {c['body']}" for c in comments).strip()
        except Exception:
            discussion_text = ""

    # EXTRACT (LLM #1)
    requirements: list[Requirement] = []
    if pr.description.strip() or tickets or discussion_text:
        progress("extract", "Extracting requirements (LLM call 1)")
        source_tags = ["pr-description"] + [f"{t.source}:{t.key}" for t in tickets]
        if discussion_text:
            source_tags.append("pr-discussion")
        tickets_block = "".join(
            f"\nTicket {t.source}:{t.key} — {t.title}\n{t.body or '(no body)'}\n" for t in tickets
        )
        if discussion_text:
            tickets_block += f"\nPR discussion (comments):\n{discussion_text[:8000]}\n"
        raw = await backend.structured(
            _instr + EXTRACT_PROMPT.format(
                sources_block="Allowed source tags: " + ", ".join(source_tags),
                title=pr.title,
                description=pr.description.strip() or "(empty)",
                tickets_block=tickets_block,
            ),
            EXTRACT_SCHEMA,
        )
        source_texts = {"pr-description": pr.description or ""}
        for t in tickets:
            source_texts[f"{t.source}:{t.key}"] = f"{t.title}\n{t.body or ''}"
        if discussion_text:
            source_texts["pr-discussion"] = discussion_text
        raw_reqs = raw.get("requirements", [])
        if len(raw_reqs) > 10:
            overflow["requirements"] = len(raw_reqs) - 10
        for i, r in enumerate(raw_reqs[:10], 1):
            src = r.get("source", "pr-description")
            if src not in source_tags:
                src = "pr-description"
            quote = (r.get("quote") or "").strip()
            if quote and quote.lower() not in source_texts.get(src, "").lower():
                quote = ""  # provenance check failed — don't underline a phantom phrase
            requirements.append(Requirement(id=f"R{i}", text=r.get("text", "").strip(), source=src, quote=quote))

    mode = "requirements" if requirements else "explain"

    # MAP (LLM #2) — generated files never reach the LLM; the rest chunks by file
    map_hunks, skipped_files = _analyzable(hunks)
    if skipped_files:
        overflow["skipped_generated"] = len(skipped_files)
    chunks = _chunk_hunks(map_hunks)

    req_block = "\n".join(f"{r.id}: {r.text}  [source: {r.source}]" for r in requirements)

    async def map_chunk(chunk: list[Hunk], label: str) -> dict[str, Any]:
        feedback = ""
        raw: dict[str, Any] = {"links": [], "unexplained": [], "net_effect": []}
        for attempt in range(2):
            progress("map", f"Mapping requirements to changes (LLM call 2{label}{', retry' if attempt else ''})")
            if mode == "requirements":
                prompt = _instr + MAP_PROMPT.format(
                    requirements_block=req_block,
                    hunks_block=_hunks_block(chunk),
                    feedback_block=feedback,
                )
            else:
                prompt = _instr + EXPLAIN_PROMPT.format(
                    title=pr.title,
                    description=pr.description.strip() or "(empty)",
                    hunks_block=_hunks_block(chunk),
                    feedback_block=feedback,
                )
            raw = await backend.structured(prompt, MAP_SCHEMA)
            _, _, errors = validate_mapping(raw, hunks, requirements)
            if not errors:
                break
            feedback = (
                "\nYour previous answer had invalid citations — fix them:\n- "
                + "\n- ".join(errors[:12])
            )
        return raw

    if len(chunks) <= 1:
        raw_map = await map_chunk(chunks[0] if chunks else [], "")
    else:
        outs = []
        for i, chunk in enumerate(chunks, 1):
            outs.append(await map_chunk(chunk, f", chunk {i}/{len(chunks)}"))
        raw_map = _merge_chunk_maps(outs)

    # VALIDATE
    progress("validate", "Validating anchors against parsed diff")
    links, unexplained, _errors = validate_mapping(raw_map, hunks, requirements)
    links = _finalize(links, requirements)
    unexplained = _coverage_fill(links, unexplained, hunks)

    # FLOW (LLM #3) — change-flow diagram topology (optional: never fails the review)
    flow = FlowGraph()
    if hunks:
        if mode == "requirements":
            items = [(r.id, next((l.status for l in links if l.requirement_id == r.id), "notfound"), r.text)
                     for r in requirements]
            mode_label = "requirements with mapping status"
        else:
            items = [(u.id, "explained", u.label) for u in unexplained]
            mode_label = "annotated changes"
        try:
            flow = await build_flow(hunks, items, mode_label, backend, progress)
        except Exception:
            flow = FlowGraph()
        if mode == "requirements":
            flow = add_ghost_nodes(flow, requirements, links)

    sources: list[SourceText] = []
    if pr.description.strip():
        sources.append(SourceText(tag="pr-description", title="PR description", text=pr.description.strip()))
    for t in tickets:
        sources.append(SourceText(tag=f"{t.source}:{t.key}", title=f"{t.key} — {t.title}", text=(t.body or "").strip()))
    if discussion_text:
        sources.append(SourceText(tag="pr-discussion", title="PR discussion", text=discussion_text[:8000]))

    raw_net = raw_map.get("net_effect", [])
    if len(raw_net) > 6:
        overflow["net_effect"] = len(raw_net) - 6
    if flow.dropped:
        overflow["flow_nodes"] = flow.dropped

    review = Review(
        id=review_id(provider.name, repo, number),
        pr=pr.model_copy(update={"diff": ""}),  # PRInfo view; diff lives in files/hunks
        mode=mode,
        requirements=requirements,
        sources=sources,
        overflow={k: v for k, v in overflow.items() if v},
        llm_usage=dict(getattr(backend, "usage", {}) or {}),
        hunks=hunks,
        links=links,
        unexplained=unexplained,
        net_effect=[l for l in raw_map.get("net_effect", [])][:6],
        files=files,
        flow=flow,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    progress("save", "Saving review")
    save_review(review)
    progress("done", "Review complete")
    return review
