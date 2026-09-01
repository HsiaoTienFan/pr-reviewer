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
from .models import _LEGACY_SEVERITY, Anchor, BugFinding, Hunk, Review
from .pipeline import _hunks_block

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
- file: path as given in the report; start/end: the NEW-file line numbers it cites (start == end for a single line; 0 if none given).
- severity: use the report's own rating when present, else judge. blocker = must fix before merge (broken/exploitable); major = real risk, should fix; minor = worth addressing; nit = optional polish.
- category: what KIND of issue it is, independent of severity. correctness (wrong behaviour/logic), security, performance, testing (missing/weak tests), maintainability (structure, duplication, dead code), style (naming, formatting), docs, other.
- Empty findings list if the report found nothing.

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


def attach_findings(findings_raw: list[dict[str, Any]], hunks: list[Hunk]) -> list[BugFinding]:
    """Anchor findings to diff lines where possible; keep un-anchorable ones
    (a real bug may sit outside the changed ranges) without line anchors."""
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
        file = f.get("file", "")
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
        ))
    return out


async def collect_findings_raw(
    pr_url: str,
    backend: LLMBackend,
    progress: Callable[[str, str], None] = lambda s, d: None,
    skill: str = "",
    skills_dir: str = "",
) -> tuple[dict[str, Any], str]:
    """URL-only phase: run the review skill on the PR and structure its report.

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
    report = await backend.text(prompt.format(url=pr_url), allowed_tools=tools)
    progress("findings", "Structuring the review report")
    raw = await backend.structured(STRUCTURE_PROMPT.format(report=report), BUGS_SCHEMA)
    return raw, report


async def run_code_review(
    review: Review,
    backend: LLMBackend,
    progress: Callable[[str, str], None] = lambda s, d: None,
    skill: str = "",
    skills_dir: str = "",
    precollected: "asyncio.Task[tuple[dict[str, Any], str]] | None" = None,
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
            raw, report = await precollected
        except asyncio.CancelledError:
            raise
        except Exception:
            raw, report = None, ""
    elif review.pr.url:
        try:
            raw, report = await collect_findings_raw(
                review.pr.url, backend, progress, skill=skill, skills_dir=skills_dir)
        except LLMError:
            raw = None
            report = ""
    if not isinstance(raw, dict) or not isinstance(raw.get("findings"), list):
        progress("code-review", "Skill path unavailable — reviewing the diff directly")
        raw = await backend.structured(
            FALLBACK_PROMPT.format(title=review.pr.title, hunks_block=_hunks_block(review.hunks)),
            BUGS_SCHEMA,
        )
    findings_raw = raw.get("findings", [])
    dropped = max(0, len(findings_raw) - 15)
    return attach_findings(findings_raw, review.hunks), report, dropped
