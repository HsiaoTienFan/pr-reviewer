"""Complementary "code review" section (DECISIONS open-question #3).

Primary path: invoke Claude Code's /code-review skill on the PR URL — the skill
inspects the PR itself (via gh) and the --json-schema flag forces its findings
into our structure. Fallback: a plain schema-constrained bug hunt over the
parsed hunks, used when the skill path errors (skill missing, no gh, etc.).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable

from .llm.base import LLMBackend, LLMError
from .llm.claude_cli import SANDBOX_DIR
from .models import _LEGACY_SEVERITY, Anchor, BugFinding, Hunk, Review
from .models import ThreadMsg
from .pipeline import _clamp_patch, _hunks_block, _is_generated

_ORIGIN_REPO_RE = re.compile(r"[:/]([^/:\s]+/[^/\s]+?)(?:\.git)?\s*$")


def _clone_repo(child: Path) -> str:
    """owner/repo of a sandbox checkout's origin, or "" if unattributable."""
    cfg = child / ".git" / "config"
    try:
        for line in cfg.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("url = "):
                m = _ORIGIN_REPO_RE.search(line[6:])
                return m.group(1).lower() if m else ""
    except OSError:
        pass
    return ""


def cleanup_sandbox(active_repos: set[str], root: Path = SANDBOX_DIR) -> list[str]:
    """Remove sandbox checkouts (repo clones left by review skills) whose repo
    no longer has any stored review. Dirs we can't attribute to a repo are left
    alone — never delete what we can't identify."""
    import shutil

    removed: list[str] = []
    if not root.is_dir():
        return removed
    active = {r.lower() for r in active_repos}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        repo = _clone_repo(child)
        if repo and repo not in active:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(f"{child.name} ({repo})")
    return removed

BUGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["blocker", "major", "minor", "nit"]},
                    "category": {"type": "string",
                                 "enum": ["correctness", "security", "performance",
                                          "testing", "maintainability", "style",
                                          "docs", "other"]},
                    "file": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["severity", "category", "file", "start", "end",
                             "title", "detail"],
            },
        },
    },
    "required": ["findings"],
}

SKILL_PROMPT = "/code-review {url}"

STRUCTURE_PROMPT = """Below is a code-review report for a pull request. Convert it into structured findings.

Hard constraints:
- One finding per distinct issue in the report; drop praise, process notes, and non-issues.
- title: <= 10 words; detail: ONE terse sentence; suggestion: one terse sentence or empty.
- file: REPO-RELATIVE path (e.g. src/auth/index.js — strip any checkout/sandbox directory prefix); start/end: NEW-file line numbers.
- Assign lines aggressively: use the line the report cites; when it names a symbol or change without a number, pick the matching range from "Changed line ranges" below. Only use 0 when the issue genuinely concerns no changed line.
- severity: use the report's own rating when present, else judge. blocker = must fix before merge (broken/exploitable); major = real risk, should fix; minor = worth addressing; nit = optional polish.
- category: what KIND of issue it is, independent of severity. correctness (wrong behaviour/logic), security, performance, testing (missing/weak tests), maintainability (structure, duplication, dead code), style (naming, formatting), docs, other.
- Empty findings list if the report found nothing.

Changed line ranges (file — NEW-file ranges in this PR's diff):
{hunks_index}

Report:
{report}"""

FALLBACK_PROMPT = """You are code-reviewing a PR diff for real defects — bugs, security issues, correctness risks, performance problems. No style nits, no praise.

Hard constraints:
- title: <= 10 words; detail: ONE terse sentence; suggestion: one terse sentence or empty.
- severity: honest. blocker = must fix before merge (broken/exploitable); major = real risk, should fix; minor = worth addressing; nit = optional polish.
- category: what KIND of issue it is, independent of severity — correctness, security, performance, testing, maintainability, style, docs, other.
- Cite file + NEW-file line numbers that lie inside the hunks below.
- At most 10 findings; empty list if the diff is clean.

PR title: {title}

Diff hunks (id — file — new-file line range):
{hunks_block}"""

# /code-review dispatches parallel reviewer subagents → Task must be allowed.
# PR content is attacker-controlled, so the sandbox is tight:
# - Bash: READ-ONLY gh/git subcommands only; `gh api` excluded entirely
#   (its patterns can't distinguish GETs from mutations).
# - No WebFetch/Grep/Glob and Read scoped to the empty sandbox cwd — with no
#   network-write tool, a prompt-injected PR has no exfiltration channel.
SKILL_TOOLS = [
    "Task", "TodoWrite", "Read(./**)",
    "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)",
    "Bash(git log *)", "Bash(git show *)", "Bash(git diff *)",
]

DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _skills_root(skills_dir: str = "") -> Path:
    return Path(skills_dir).expanduser() if skills_dir.strip() else DEFAULT_SKILLS_DIR


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML-frontmatter reader: scalar `key: value` lines plus
    string-list blocks (`key:` followed by `- item` lines). No yaml dep."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, Any] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        item = re.match(r"\s+-\s+(.+)", line)
        if item and key is not None:
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(item.group(1).strip().strip("\"'"))
            continue
        kv = re.match(r"([A-Za-z][\w-]*):\s*(.*)", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip().strip("\"'")
            if val:
                out[key] = val
                key = None  # scalar — don't attach stray list items
            else:
                out[key] = []  # opening a block list
    return out


def list_skills(skills_dir: str = "") -> list[dict[str, Any]]:
    """Discover user skills at runtime: every <root>/<name>/SKILL.md."""
    root = _skills_root(skills_dir)
    skills: list[dict[str, Any]] = []
    if not root.is_dir():
        return skills
    for md in sorted(root.glob("*/SKILL.md")):
        try:
            fm = _parse_frontmatter(md.read_text(errors="replace"))
        except OSError:
            continue
        name = str(fm.get("name") or md.parent.name)
        tools = fm.get("allowed-tools")
        skills.append({
            "name": name,
            "description": str(fm.get("description") or ""),
            "tools": tools if isinstance(tools, list) else [],
        })
    return skills


def resolve_review_skill(skill: str, skills_dir: str = "") -> tuple[str, list[str]] | None:
    """→ (prompt, allowed_tools) for a discovered skill, or None if unknown.

    Only names found on disk are accepted — the value arrives over HTTP and is
    interpolated into the CLI prompt, so it must never be free text. Tools come
    from the skill's own frontmatter, falling back to the tight built-in set.
    """
    for s in list_skills(skills_dir):
        if s["name"] == skill:
            return f"/{s['name']} {{url}}", (s["tools"] or SKILL_TOOLS)
    return None


ASK_PROMPT = """You are answering a follow-up question about ONE finding from a code review you have context on below. Be direct and technical; cite file:line when you make claims. If the provided context cannot answer the question, say exactly what is missing rather than guessing.

## The finding
{finding_block}

## Changed code the finding anchors to
{hunks_block}

## Full review report (context)
{report_block}
{thread_block}
## Question
{question}"""

# Every component of the ask prompt is HARD-BOUNDED — a finding can anchor to
# an arbitrarily large hunk and threads grow without limit, so unbounded
# assembly would recreate the "Prompt is too long" failure MAP chunking fixed.
ASK_HUNKS_BUDGET = 30_000   # total chars of anchored hunks (each also _clamp_patch'd)
ASK_REPORT_BUDGET = 20_000
ASK_THREAD_BUDGET = 20_000  # most recent turns first


def _ask_context(review: Review, finding: BugFinding, question: str) -> str:
    fb = [f"{finding.id} [{finding.severity} / {finding.category}] {finding.title}"]
    if finding.detail:
        fb.append(f"Detail: {finding.detail}")
    if finding.suggestion:
        fb.append(f"Suggested fix: {finding.suggestion}")
    where = finding.anchors[0] if finding.anchors else None
    if where:
        fb.append(f"Anchored at {where.file}:{where.start}\u2013{where.end}")
    elif finding.cited_file:
        fb.append(f"Report cites {finding.cited_file}:{finding.cited_line} (outside the diff)")

    targets = {a.file for a in finding.anchors} or ({finding.cited_file} if finding.cited_file else set())
    parts, used = [], 0
    for h in review.hunks:
        if h.file not in targets:
            continue
        if _is_generated(h.file):
            parts.append(f"### {h.file} — generated file, patch omitted")
            continue
        patch = _clamp_patch(h.patch)
        if used + len(patch) > ASK_HUNKS_BUDGET:
            parts.append(f"### {h.file} — omitted for size (lines {h.start}\u2013{h.end})")
            continue
        used += len(patch)
        parts.append(f"### {h.id} — {h.file} (new-file lines {h.start}\u2013{h.end})\n{patch}")
    hunks_block = "\n\n".join(parts) or "(the finding anchors to no changed code)"

    report = review.bugs_report or "(no report stored)"
    if len(report) > ASK_REPORT_BUDGET:
        report = report[:ASK_REPORT_BUDGET] + "\n\u2026 [report truncated for size]"

    thread = review.threads.get(finding.id, [])
    kept: list[ThreadMsg] = []
    tused = 0
    for m in reversed(thread):
        if tused + len(m.text) > ASK_THREAD_BUDGET:
            break
        kept.append(m)
        tused += len(m.text)
    kept.reverse()
    tb = ""
    if kept:
        dropped = len(thread) - len(kept)
        lines = [f"{'Q' if m.role == 'user' else 'A'}: {m.text}" for m in kept]
        note = f"[{dropped} earlier turns omitted for size]\n" if dropped else ""
        tb = f"\n## Discussion so far\n{note}" + "\n\n".join(lines) + "\n"

    return ASK_PROMPT.format(finding_block="\n".join(fb), hunks_block=hunks_block,
                             report_block=report, thread_block=tb, question=question.strip())


async def ask_finding(
    review: Review,
    finding_id: str,
    question: str,
    backend: LLMBackend,
    inspect: bool = False,
) -> str:
    """Answer a follow-up question about one finding. inspect=True grants the
    read-only sandbox tools so the model can look at the repo, else text-only."""
    finding = next((b for b in review.bugs if b.id == finding_id), None)
    if finding is None:
        raise LLMError(f"no finding {finding_id} in this review")
    prompt = _ask_context(review, finding, question)
    return await backend.text(prompt, allowed_tools=SKILL_TOOLS if inspect else None)


# Skill reports cite files from the sandbox checkout, i.e. absolute paths like
# /…/.pr-reviewer/sandbox/repo/src/x.js — strip that prefix before matching.
_SANDBOX_PREFIX_RE = re.compile(r"^.*?/\.pr-reviewer/sandbox/(?:repo/)?")


def _resolve_cited_file(cited: str, files: list[str]) -> str:
    """Map a report-cited path onto a diff file path, or "" if it isn't one.

    Exact match after sandbox-prefix stripping; otherwise a suffix match at a
    path boundary, accepted only when unambiguous."""
    cited = _SANDBOX_PREFIX_RE.sub("", cited.strip().lstrip("/"))
    if not cited:
        return ""
    if cited in files:
        return cited
    ends = [f for f in files if cited.endswith("/" + f) or f.endswith("/" + cited)]
    return ends[0] if len(ends) == 1 else ""


def attach_findings(findings_raw: list[dict[str, Any]], hunks: list[Hunk]) -> list[BugFinding]:
    """Anchor findings to diff lines where possible; keep un-anchorable ones
    (a real bug may sit outside the changed ranges) with their citation intact."""
    by_file: dict[str, list[Hunk]] = {}
    for h in hunks:
        by_file.setdefault(h.file, []).append(h)
    out: list[BugFinding] = []
    for f in findings_raw[:15]:
        sev = _LEGACY_SEVERITY.get(f.get("severity"), f.get("severity"))
        if sev not in ("blocker", "major", "minor", "nit"):
            sev = "minor"
        cat = f.get("category", "other")
        if cat not in ("correctness", "security", "performance", "testing",
                       "maintainability", "style", "docs", "other"):
            cat = "other"
        file = _resolve_cited_file(str(f.get("file", "")), list(by_file))
        try:
            start, end = int(f.get("start", 0)), int(f.get("end", 0))
        except (TypeError, ValueError):
            start = end = 0
        if start > end:
            start, end = end, start
        hit = next((h for h in by_file.get(file, []) if start <= h.end and end >= h.start), None)
        anchors = [Anchor(file=file, start=max(start, hit.start), end=min(end, hit.end))] if hit else []
        out.append(BugFinding(
            id=f"B{len(out) + 1}",
            severity=sev,
            category=cat,
            title=(f.get("title") or "Finding").strip(),
            detail=(f.get("detail") or "").strip(),
            suggestion=(f.get("suggestion") or "").strip(),
            anchors=anchors,
            cited_file=file or _SANDBOX_PREFIX_RE.sub("", str(f.get("file", "")).strip().lstrip("/")),
            cited_line=start,
        ))
    return out


def _hunks_index(hunks: list[Hunk]) -> str:
    by_file: dict[str, list[str]] = {}
    for h in hunks:
        by_file.setdefault(h.file, []).append(f"{h.start}\u2013{h.end}")
    return "\n".join(f"- {f}: {', '.join(r)}" for f, r in sorted(by_file.items())) or "- (none)"


async def collect_findings_raw(
    pr_url: str,
    backend: LLMBackend,
    progress: Callable[[str, str], None] = lambda s, d: None,
    skill: str = "",
    skills_dir: str = "",
) -> str:
    """URL-only phase: run the review skill on the PR and return its report.

    Structuring happens later in run_code_review, where the parsed hunks exist
    and line assignment can target the diff's changed ranges.

    Needs no parsed hunks, so the main pipeline can start this concurrently
    before the diff is even fetched. Raises LLMError when the skill path fails;
    the caller decides whether to fall back to a direct diff review."""
    prompt, tools = SKILL_PROMPT, SKILL_TOOLS
    label = "/code-review"
    if skill:
        resolved = resolve_review_skill(skill, skills_dir)
        if resolved:
            prompt, tools = resolved
            label = f"/{skill}"
    progress("findings", f"Running Claude Code {label} on the PR")
    return await backend.text(prompt.format(url=pr_url), allowed_tools=tools)


async def run_code_review(
    review: Review,
    backend: LLMBackend,
    progress: Callable[[str, str], None] = lambda s, d: None,
    skill: str = "",
    skills_dir: str = "",
    precollected: "asyncio.Task[str] | None" = None,
) -> tuple[list[BugFinding], str, int]:
    """→ (findings, raw report text, findings dropped beyond the cap).
    The raw report is preserved verbatim — the table is a compression of it.

    skill: name of a user skill (discovered from skills_dir) to run instead of
    the built-in /code-review flow; "" or an unknown name uses the built-in.
    precollected: an already-running collect_findings_raw task (started by the
    main review pipeline so both run concurrently); its failure falls back to
    the direct diff review exactly like an inline skill failure."""
    raw: dict[str, Any] | None = None
    report = ""
    if precollected is not None:
        try:
            report = await precollected
        except asyncio.CancelledError:
            raise
        except Exception:
            report = ""
    elif review.pr.url:
        try:
            report = await collect_findings_raw(
                review.pr.url, backend, progress, skill=skill, skills_dir=skills_dir)
        except LLMError:
            report = ""
    if report.strip():
        progress("findings", "Structuring the review report")
        try:
            raw = await backend.structured(
                STRUCTURE_PROMPT.format(hunks_index=_hunks_index(review.hunks), report=report),
                BUGS_SCHEMA,
            )
        except LLMError:
            raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("findings"), list):
        progress("code-review", "Skill path unavailable — reviewing the diff directly")
        raw = await backend.structured(
            FALLBACK_PROMPT.format(title=review.pr.title, hunks_block=_hunks_block(review.hunks)),
            BUGS_SCHEMA,
        )
    findings_raw = raw.get("findings", [])
    dropped = max(0, len(findings_raw) - 15)
    return attach_findings(findings_raw, review.hunks), report, dropped
