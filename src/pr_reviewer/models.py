"""Core data model — the requirement↔hunk link graph (DESIGN.md §4)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Status = Literal["fulfilled", "partial", "notfound"]
Confidence = Literal["high", "medium", "low"]


class Requirement(BaseModel):
    id: str
    text: str
    source: str  # "linear:ENG-123" | "jira:PROJ-45" | "pr-description"
    quote: str = ""  # verbatim phrase in the source this was extracted from


class SourceText(BaseModel):
    """Original, unprocessed requirement source — for the provenance panel."""

    tag: str  # "pr-description" | "linear:ENG-123" | ...
    title: str = ""
    text: str = ""


class Anchor(BaseModel):
    file: str
    start: int  # new-file line numbers, inclusive
    end: int


class Hunk(BaseModel):
    id: str  # stable, assigned at parse time (H1, H2, ...)
    file: str
    start: int  # new-file line range covered by this hunk
    end: int
    patch: str


class Link(BaseModel):
    requirement_id: str
    hunk_ids: list[str] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)
    mechanism: str = ""  # ONE terse sentence: what the cited change does
    why: str = ""  # ONE terse sentence: why that satisfies (or fails) the requirement
    missing: str = ""  # what's absent, for partial/notfound
    status: Status
    confidence: Confidence


class UnexplainedChange(BaseModel):
    id: str  # U1, U2, ...
    label: str
    mechanism: str = ""
    hunk_ids: list[str] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)


Severity = Literal["blocker", "major", "minor", "nit"]
Category = Literal[
    "correctness", "security", "performance", "testing",
    "maintainability", "style", "docs", "other",
]

# Reviews saved before the severity scale gained blocker/nit used high/medium/low.
_LEGACY_SEVERITY = {"high": "blocker", "medium": "major", "low": "minor"}


class BugFinding(BaseModel):
    """One finding from the complementary /code-review bug-hunting pass."""

    id: str  # B1, B2, ...
    severity: Severity
    category: Category = "other"
    title: str
    detail: str = ""  # one terse sentence
    suggestion: str = ""
    anchors: list[Anchor] = Field(default_factory=list)
    # Where the report itself pointed, kept even when it can't be anchored to a
    # changed line (a real bug may sit outside the diff's hunks).
    cited_file: str = ""
    cited_line: int = 0

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, v: object) -> object:
        return _LEGACY_SEVERITY.get(v, v) if isinstance(v, str) else v


class DiffRow(BaseModel):
    """One row of the split (side-by-side) diff. Either a hunk-gap header or
    an old/new line pair; o/n are (line_number, text)."""

    gap: str | None = None
    o: tuple[int, str] | None = None
    n: tuple[int, str] | None = None


class FileDiff(BaseModel):
    path: str
    status: Literal["new", "mod", "del", "renamed"]
    rows: list[DiffRow] = Field(default_factory=list)
    hunk_ids: list[str] = Field(default_factory=list)


class FlowNode(BaseModel):
    """A changed symbol in the change-flow diagram (DESIGN.md §7.2)."""

    id: str
    label: str  # "check_lockout()", "_failures {}"
    file: str = ""  # anchor into the diff — validated like Link anchors
    line: int = 0
    kind: Literal["new", "modified", "store", "ghost"]
    requirement_id: str = ""  # ghosts: the not-found requirement they represent


class FlowEdge(BaseModel):
    source: str
    target: str  # FlowNode ids (validated: must exist)
    label: str = ""  # terse: "gate login", "append stamp"
    requirement_ids: list[str] = Field(default_factory=list)
    missing: bool = False  # true → expected interaction absent (partial req)


class FlowGraph(BaseModel):
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)  # terse lines: how the flow fulfills the requirements
    partial: bool = False  # True → built from only the first chunk of a big diff
    dropped: int = 0  # nodes returned by the LLM beyond the cap


class TicketRef(BaseModel):
    key: str  # e.g. ENG-142
    source: str  # linear | jira
    url: str = ""
    title: str = ""


class PRInfo(BaseModel):
    provider: str  # github | bitbucket
    repo: str  # owner/name
    number: int
    url: str
    title: str
    description: str = ""
    author: str = ""
    branch: str = ""
    base_branch: str = ""
    updated_at: str = ""
    state: str = "open"  # open | merged | closed
    additions: int = 0
    deletions: int = 0
    tickets: list[TicketRef] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)  # review requested from


class PRContent(PRInfo):
    diff: str = ""


class TicketContent(BaseModel):
    key: str
    source: str  # linear | jira
    title: str = ""
    body: str = ""
    url: str = ""


class Review(BaseModel):
    id: str  # provider:owner/repo:number
    pr: PRInfo
    mode: Literal["requirements", "explain"]
    requirements: list[Requirement] = Field(default_factory=list)
    hunks: list[Hunk] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    unexplained: list[UnexplainedChange] = Field(default_factory=list)
    net_effect: list[str] = Field(default_factory=list)
    files: list[FileDiff] = Field(default_factory=list)
    sources: list[SourceText] = Field(default_factory=list)
    bugs: list[BugFinding] = Field(default_factory=list)
    bugs_ran: bool = False  # distinguishes "ran, clean" from "not run yet"
    bugs_stale: bool = False  # findings carried across a re-run, not re-derived
    bugs_report: str = ""  # the raw /code-review report the findings were structured from
    overflow: dict[str, int] = Field(default_factory=dict)  # cap-truncated item counts, keyed by section
    llm_usage: dict[str, float] = Field(default_factory=dict)  # calls / cost_usd (API-equivalent) / ms
    flow: FlowGraph = Field(default_factory=FlowGraph)
    published_url: str = ""  # set once posted to the PR host
    published_at: str = ""
    verified: list[str] = Field(default_factory=list)  # card ids checked off
    created_at: str = ""


def review_id(provider: str, repo: str, number: int) -> str:
    return f"{provider}:{repo}:{number}"
