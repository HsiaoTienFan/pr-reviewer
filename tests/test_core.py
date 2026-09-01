"""Core-logic tests: diff parsing, ticket-ref detection, anchor validation."""
from __future__ import annotations

from pr_reviewer.diff_parser import parse_diff
from pr_reviewer.models import Hunk, Link, Requirement
from pr_reviewer.pipeline import _coverage_fill, _finalize, _merge_chunk_maps, validate_mapping
from pr_reviewer.tickets import detect_ticket_refs

SAMPLE_DIFF = """\
diff --git a/auth/lockout.py b/auth/lockout.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/auth/lockout.py
@@ -0,0 +1,5 @@
+MAX_ATTEMPTS = 5
+
+def check_lockout(user_id):
+    return False
+
diff --git a/auth/login.py b/auth/login.py
index 2222222..3333333 100644
--- a/auth/login.py
+++ b/auth/login.py
@@ -38,7 +38,10 @@ def login(request):
 def login(request):
     user = get_user(request.username)
     if user is None:
         return err(404)
+    if check_lockout(user.id):
+        return err(423)
     if not verify(user, request.password):
-        return err(401)
+        record_failure(user.id)
+        return err(401)
     return session_for(user)
"""


def test_parse_diff_files_and_hunks():
    files, hunks = parse_diff(SAMPLE_DIFF)
    assert [f.path for f in files] == ["auth/lockout.py", "auth/login.py"]
    assert files[0].status == "new"
    assert files[1].status == "mod"
    assert [h.id for h in hunks] == ["H1", "H2"]
    assert hunks[0].file == "auth/lockout.py"
    assert (hunks[0].start, hunks[0].end) == (1, 5)
    assert (hunks[1].start, hunks[1].end) == (38, 47)


def test_parse_diff_split_rows_line_numbers():
    files, _ = parse_diff(SAMPLE_DIFF)
    login = files[1]
    # first row is the hunk gap header
    assert login.rows[0].gap and login.rows[0].gap.startswith("@@")
    # pure additions have no old side
    adds = [r for r in login.rows if r.o is None and r.n is not None]
    assert (42, "    if check_lockout(user.id):") in [r.n for r in adds]
    # del/add pairing: "-        return err(401)" pairs with the first replacement line
    pair = next(r for r in login.rows if r.o and r.o[1] == "        return err(401)")
    assert pair.n is not None and pair.n[1] == "        record_failure(user.id)"
    # context lines advance both counters
    last = login.rows[-1]
    assert last.o == (44, "    return session_for(user)")
    assert last.n == (47, "    return session_for(user)")


def test_detect_ticket_refs_dedup_and_order():
    refs = detect_ticket_refs("feature/eng-142-lockout".upper(), "Fix ENG-142 and BILL-203", "See ENG-142.")
    assert refs == ["ENG-142", "BILL-203"]
    assert detect_ticket_refs("no refs here") == []


def _hunks() -> list[Hunk]:
    return [
        Hunk(id="H1", file="auth/lockout.py", start=1, end=5, patch="..."),
        Hunk(id="H2", file="auth/login.py", start=38, end=47, patch="..."),
    ]


def _reqs() -> list[Requirement]:
    return [Requirement(id="R1", text="Lock account", source="pr-description")]


def test_validate_mapping_accepts_good_anchor():
    raw = {"links": [{"requirement_id": "R1", "status": "fulfilled", "confidence": "high",
                      "mechanism": "m", "missing": "", "hunk_ids": ["H1"],
                      "anchors": [{"file": "auth/lockout.py", "start": 1, "end": 3}]}],
           "unexplained": [], "net_effect": []}
    links, unx, errors = validate_mapping(raw, _hunks(), _reqs())
    assert errors == []
    assert links[0].anchors[0].end == 3


def test_validate_mapping_rejects_hallucinated_anchor():
    raw = {"links": [{"requirement_id": "R1", "status": "fulfilled", "confidence": "high",
                      "mechanism": "m", "missing": "", "hunk_ids": ["H1"],
                      "anchors": [{"file": "auth/lockout.py", "start": 200, "end": 210}]}],
           "unexplained": [], "net_effect": []}
    links, _, errors = validate_mapping(raw, _hunks(), _reqs())
    assert errors  # hallucinated line range flagged
    # falls back to the cited hunk's real range
    assert links[0].anchors[0].start == 1 and links[0].anchors[0].end == 5


def test_validate_mapping_clamps_overlapping_anchor():
    raw = {"links": [{"requirement_id": "R1", "status": "fulfilled", "confidence": "high",
                      "mechanism": "m", "missing": "", "hunk_ids": ["H2"],
                      "anchors": [{"file": "auth/login.py", "start": 30, "end": 43}]}],
           "unexplained": [], "net_effect": []}
    links, _, errors = validate_mapping(raw, _hunks(), _reqs())
    assert links[0].anchors[0].start == 38  # clamped into hunk range


def test_finalize_demotes_unproven_and_fills_missing():
    links, _, _ = validate_mapping(
        {"links": [{"requirement_id": "R1", "status": "fulfilled", "confidence": "high",
                    "mechanism": "m", "missing": "", "hunk_ids": [], "anchors": []}],
         "unexplained": [], "net_effect": []},
        _hunks(), _reqs())
    final = _finalize(links, _reqs())
    assert final[0].status == "notfound"  # claimed fulfilled with zero evidence

    final2 = _finalize([], _reqs())
    assert final2[0].status == "notfound" and final2[0].requirement_id == "R1"


def test_coverage_fill_adds_uncited_hunks():
    out = _coverage_fill([], [], _hunks())
    assert len(out) == 2
    assert out[0].anchors[0].file == "auth/lockout.py"


def test_map_new_requirement_appends_reviewer_req(tmp_path, monkeypatch):
    import asyncio

    from pr_reviewer import config as cfg_mod
    from pr_reviewer.models import PRInfo, Review
    from pr_reviewer.pipeline import map_new_requirement

    monkeypatch.setattr(cfg_mod, "REVIEWS_DIR", tmp_path)

    review = Review(
        id="github:x/y:1",
        pr=PRInfo(provider="github", repo="x/y", number=1, url="", title="t"),
        mode="requirements",
        requirements=[Requirement(id="R1", text="a", source="pr-description"),
                      Requirement(id="R3", text="b", source="pr-description")],
        hunks=_hunks(),
    )

    class FakeBackend:
        name = "fake"

        async def structured(self, prompt, schema, allowed_tools=None):
            assert "R4: No sql injection" in prompt  # next id after R3
            return {"links": [{"requirement_id": "R4", "status": "fulfilled", "confidence": "high",
                               "mechanism": "m", "why": "w", "missing": "", "hunk_ids": ["H1"],
                               "anchors": [{"file": "auth/lockout.py", "start": 2, "end": 3}]}],
                    "unexplained": [], "net_effect": []}

    out = asyncio.run(map_new_requirement(review, "No sql injection", FakeBackend()))
    assert out.requirements[-1].id == "R4"
    assert out.requirements[-1].source == "reviewer"
    link = out.links[-1]
    assert link.requirement_id == "R4" and link.status == "fulfilled" and link.why == "w"


def test_validate_flow_drops_bad_anchors_and_edges():
    from pr_reviewer.pipeline import validate_flow

    raw = {
        "nodes": [
            {"id": "n1", "label": "check_lockout()", "file": "auth/lockout.py", "line": 3, "kind": "new"},
            {"id": "n2", "label": "login()", "file": "auth/login.py", "line": 40, "kind": "modified"},
            {"id": "bad", "label": "ghost()", "file": "auth/lockout.py", "line": 999, "kind": "new"},
        ],
        "edges": [
            {"source": "n2", "target": "n1", "label": "gate login", "requirement_ids": ["R1", "RX"], "missing": False},
            {"source": "n2", "target": "bad", "label": "x", "requirement_ids": [], "missing": False},
            {"source": "n1", "target": "n1", "label": "self", "requirement_ids": [], "missing": False},
        ],
    }
    flow, errors = validate_flow(raw, _hunks(), {"R1"})
    assert [n.id for n in flow.nodes] == ["n1", "n2"]
    assert len(flow.edges) == 1
    assert flow.edges[0].requirement_ids == ["R1"]  # unknown RX filtered
    assert len(errors) == 3  # bad anchor, bad edge target, self-edge


def test_validate_flow_canonicalizes_duplicate_nodes():
    from pr_reviewer.pipeline import validate_flow

    raw = {
        "nodes": [
            {"id": "a", "label": "check_lockout()", "file": "auth/lockout.py", "line": 2, "kind": "new"},
            {"id": "a_dup", "label": "check_lockout()", "file": "auth/lockout.py", "line": 4, "kind": "new"},
            {"id": "b", "label": "login()", "file": "auth/login.py", "line": 40, "kind": "modified"},
        ],
        "edges": [
            {"source": "b", "target": "a", "label": "gate", "requirement_ids": [], "missing": False},
            {"source": "b", "target": "a_dup", "label": "gate", "requirement_ids": [], "missing": False},
            {"source": "b", "target": "a_dup", "label": "other", "requirement_ids": [], "missing": False},
        ],
        "summary": ["R1: login gates on lockout.", "  ", "extra"],
    }
    flow, errors = validate_flow(raw, _hunks(), set())
    assert [n.id for n in flow.nodes] == ["a", "b"]  # duplicate merged, not an error
    assert errors == []
    assert flow.summary == ["R1: login gates on lockout.", "extra"]  # stripped, blanks dropped
    # edges re-pointed to the survivor and deduped
    assert [(e.source, e.target, e.label) for e in flow.edges] == [("b", "a", "gate"), ("b", "a", "other")]


def test_add_ghost_nodes_for_notfound():
    from pr_reviewer.models import FlowGraph
    from pr_reviewer.pipeline import add_ghost_nodes

    reqs = [Requirement(id="R1", text="Lock account", source="pr-description"),
            Requirement(id="R2", text="Admins can manually unlock an account somehow", source="pr-description")]
    links = [Link(requirement_id="R1", status="fulfilled", confidence="high"),
             Link(requirement_id="R2", status="notfound", confidence="low")]
    flow = add_ghost_nodes(FlowGraph(), reqs, links)
    assert len(flow.nodes) == 1
    ghost = flow.nodes[0]
    assert ghost.kind == "ghost" and ghost.requirement_id == "R2"
    assert ghost.label.endswith("— not found")


def test_is_stale_datetime_logic():
    from pr_reviewer.app import _is_stale

    assert _is_stale("2026-08-09T12:00:00Z", "2026-08-09T10:00:00+00:00") is True
    assert _is_stale("2026-08-09T09:00:00Z", "2026-08-09T10:00:00+00:00") is False
    assert _is_stale("", "2026-08-09T10:00:00+00:00") is None
    assert _is_stale("garbage", "2026-08-09T10:00:00+00:00") is None


def test_merge_rerun_state_carries_reviewer_owned_state(tmp_path, monkeypatch):
    import asyncio

    from pr_reviewer import config as cfg_mod
    from pr_reviewer.models import Anchor, BugFinding, PRInfo, Review
    from pr_reviewer.pipeline import merge_rerun_state

    monkeypatch.setattr(cfg_mod, "REVIEWS_DIR", tmp_path)
    pr = PRInfo(provider="github", repo="x/y", number=1, url="", title="t")

    prev = Review(
        id="github:x/y:1", pr=pr, mode="requirements",
        requirements=[Requirement(id="R1", text="Lock account", source="pr-description"),
                      Requirement(id="R2", text="No SQL injection", source="reviewer")],
        verified=["R1", "R2"],
        bugs=[BugFinding(id="B1", severity="high", title="bug", detail="d",
                         anchors=[Anchor(file="auth/lockout.py", start=2, end=3),
                                  Anchor(file="gone.py", start=1, end=2)])],
        bugs_ran=True,
    )
    new = Review(
        id="github:x/y:1", pr=pr, mode="requirements",
        requirements=[Requirement(id="R1", text="Lock account", source="pr-description")],
        hunks=_hunks(),
    )

    class FakeBackend:
        name = "fake"

        async def structured(self, prompt, schema, allowed_tools=None):
            return {"links": [{"requirement_id": "R2", "status": "fulfilled", "confidence": "high",
                               "mechanism": "m", "why": "w", "missing": "", "hunk_ids": ["H1"],
                               "anchors": [{"file": "auth/lockout.py", "start": 1, "end": 2}]}],
                    "unexplained": [], "net_effect": []}

    out = asyncio.run(merge_rerun_state(new, prev, FakeBackend()))
    assert "R1" in out.verified  # carried by matching text
    reviewer_req = next(r for r in out.requirements if r.source == "reviewer")
    assert reviewer_req.text == "No SQL injection"
    assert reviewer_req.id in out.verified  # was verified before the re-run
    assert out.bugs_ran and len(out.bugs) == 1
    assert out.bugs_stale is True  # carried findings are flagged until code review re-runs
    assert [(a.file, a.start, a.end) for a in out.bugs[0].anchors] == [("auth/lockout.py", 2, 3)]  # dead anchor dropped


def test_linear_source_parses_issue(monkeypatch):
    import asyncio

    from pr_reviewer.tickets.linear import LinearSource

    src = LinearSource(api_key="lin_api_SYNTHETIC")

    async def fake_gql(query, variables=None):
        assert variables == {"id": "ENG-142"}
        return {"data": {"issue": {"identifier": "ENG-142", "title": "Harden login",
                                   "description": "After 5 failures, lock.", "url": "https://linear.app/x/ENG-142"}}}

    monkeypatch.setattr(src, "_gql", fake_gql)
    t = asyncio.run(src.fetch("ENG-142"))
    assert t.key == "ENG-142" and t.source == "linear"
    assert t.title == "Harden login" and "lock" in t.body


def test_publish_dry_run_payload(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pr_reviewer import config as cfg_mod
    from pr_reviewer.app import app
    from pr_reviewer.models import Anchor, PRInfo, Review

    monkeypatch.setattr(cfg_mod, "REVIEWS_DIR", tmp_path)
    review = Review(
        id="github:x/y:1",
        pr=PRInfo(provider="github", repo="x/y", number=1, url="https://github.com/x/y/pull/1", title="t"),
        mode="requirements",
        requirements=[Requirement(id="R1", text="Lock account", source="pr-description")],
        links=[Link(requirement_id="R1", status="fulfilled", confidence="high",
                    mechanism="adds lockout", why="gates login",
                    anchors=[Anchor(file="auth/lockout.py", start=2, end=3)])],
        net_effect=["Accounts lock after 5 failures"],
    )
    cfg_mod.save_review(review)

    client = TestClient(app)
    res = client.post("/api/reviews/github:x/y:1/publish", json={"dry_run": True})
    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is True
    assert "R1" in data["body"] and "Lock account" in data["body"]
    assert data["comments"][0]["path"] == "auth/lockout.py"
    assert data["comments"][0]["line"] == 2


def test_merge_chunk_maps_reconciles_statuses():
    merged = _merge_chunk_maps([
        {"links": [{"requirement_id": "R1", "status": "notfound", "confidence": "high",
                    "mechanism": "", "missing": "not here", "hunk_ids": [], "anchors": []}],
         "unexplained": [], "net_effect": ["a"]},
        {"links": [{"requirement_id": "R1", "status": "fulfilled", "confidence": "medium",
                    "mechanism": "found it", "missing": "", "hunk_ids": ["H2"],
                    "anchors": [{"file": "auth/login.py", "start": 43, "end": 44}]}],
         "unexplained": [], "net_effect": ["a", "b"]},
    ])
    l = merged["links"][0]
    assert l["status"] == "fulfilled" and l["mechanism"] == "found it"
    assert merged["net_effect"] == ["a", "b"]


def test_merge_chunk_maps_joins_all_mechanisms():
    from pr_reviewer.pipeline import _merge_chunk_maps

    merged = _merge_chunk_maps([
        {"links": [{"requirement_id": "R1", "status": "partial", "confidence": "high",
                    "mechanism": "adds counter", "why": "counts fails", "missing": "no reset", "hunk_ids": ["H1"], "anchors": []}],
         "unexplained": [], "net_effect": []},
        {"links": [{"requirement_id": "R1", "status": "fulfilled", "confidence": "high",
                    "mechanism": "wires reset", "why": "clears on success", "missing": "", "hunk_ids": ["H2"], "anchors": []}],
         "unexplained": [], "net_effect": []},
    ])
    l = merged["links"][0]
    assert l["status"] == "fulfilled"
    assert l["mechanism"] == "adds counter; wires reset"  # both chunks' explanations kept
    assert l["why"] == "counts fails; clears on success"


def test_finding_severity_migrates_from_legacy_scale():
    """Reviews saved on the old high/medium/low scale must still load."""
    from pr_reviewer.models import BugFinding

    assert BugFinding(id="B1", severity="high", title="x").severity == "blocker"
    assert BugFinding(id="B2", severity="medium", title="x").severity == "major"
    assert BugFinding(id="B3", severity="low", title="x").severity == "minor"
    # new vocabulary passes through untouched; category defaults when absent
    nit = BugFinding(id="B4", severity="nit", title="x")
    assert (nit.severity, nit.category) == ("nit", "other")
    assert BugFinding(id="B5", severity="blocker", category="security",
                      title="x").category == "security"


def test_attach_findings_validates_severity_and_category():
    from pr_reviewer.bugs import attach_findings

    out = attach_findings([
        {"severity": "blocker", "category": "correctness", "file": "a.py",
         "start": 1, "end": 1, "title": "t", "detail": "d"},
        {"severity": "bogus", "category": "bogus", "file": "a.py",
         "start": 1, "end": 1, "title": "t2", "detail": "d"},
    ], [])
    assert (out[0].severity, out[0].category) == ("blocker", "correctness")
    assert (out[1].severity, out[1].category) == ("minor", "other")


def test_generated_files_never_reach_the_llm():
    """A wholesale-rewritten fixture is one huge hunk — it must be filtered,
    and any remaining oversized hunk clamped, so the prompt stays bounded."""
    from pr_reviewer.models import Hunk
    from pr_reviewer.pipeline import (MAP_CHUNK_CHARS, _analyzable, _chunk_hunks,
                                      _hunks_block)

    hunks = [
        Hunk(id="H1", file="cypress/fixtures/results-export.js", start=1, end=9,
             patch="x" * 1_600_000),
        Hunk(id="H2", file="package-lock.json", start=1, end=9, patch="y" * 5000),
        Hunk(id="H3", file="src/app.js", start=1, end=9, patch="z" * 900_000),
    ]
    keep, skipped = _analyzable(hunks)
    assert [h.id for h in keep] == ["H3"]
    assert set(skipped) == {"cypress/fixtures/results-export.js", "package-lock.json"}

    # the surviving 900k hunk is not generated, so the clamp is what saves us
    for chunk in _chunk_hunks(keep):
        assert len(_hunks_block(chunk)) < MAP_CHUNK_CHARS


def test_pr_url_constructible_without_api():
    """The integrated findings task starts before fetch, so the canonical PR
    URL must be constructible from repo+number alone."""
    from pr_reviewer.providers.bitbucket import BitbucketProvider
    from pr_reviewer.providers.github import GitHubProvider

    assert GitHubProvider().pr_url("o/r", 5) == "https://github.com/o/r/pull/5"
    assert BitbucketProvider().pr_url("w/r", 7) == "https://bitbucket.org/w/r/pull-requests/7"


async def test_run_code_review_precollected_failure_falls_back():
    """When the concurrently-started skill task dies, the integrated flow must
    fall back to the direct diff review — and must NOT re-run the skill."""
    import asyncio

    from pr_reviewer.bugs import run_code_review
    from pr_reviewer.models import PRInfo, Review

    class FakeBackend:
        async def text(self, prompt, allowed_tools=None):
            raise AssertionError("skill path must not be re-run after precollected failure")

        async def structured(self, prompt, schema, allowed_tools=None):
            return {"findings": [{"severity": "nit", "category": "style", "file": "a.py",
                                  "start": 1, "end": 1, "title": "t", "detail": "d"}]}

    async def dead_skill():
        raise RuntimeError("skill blew up")

    review = Review(
        id="github:x/y:1",
        pr=PRInfo(provider="github", repo="x/y", number=1,
                  url="https://github.com/x/y/pull/1", title="t"),
        mode="requirements",
        hunks=[],
    )
    findings, report, dropped = await run_code_review(
        review, FakeBackend(), precollected=asyncio.create_task(dead_skill()))
    assert [f.severity for f in findings] == ["nit"]
    assert report == "" and dropped == 0


def test_skill_frontmatter_discovery(tmp_path):
    """Skills are discovered from disk at call time — never hardcoded."""
    from pr_reviewer.bugs import list_skills, resolve_review_skill

    d = tmp_path / "my-review"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: my-review\ndescription: House review rules\n"
        "allowed-tools:\n  - Read\n  - Bash(gh pr diff *)\n---\nbody\n")
    skills = list_skills(str(tmp_path))
    assert [s["name"] for s in skills] == ["my-review"]
    assert skills[0]["tools"] == ["Read", "Bash(gh pr diff *)"]

    resolved = resolve_review_skill("my-review", str(tmp_path))
    assert resolved == ("/my-review {url}", ["Read", "Bash(gh pr diff *)"])
    # unknown names are rejected — the value reaches the CLI prompt
    assert resolve_review_skill("../evil", str(tmp_path)) is None


def test_findings_anchor_despite_sandbox_paths():
    """Skill reports cite sandbox-absolute paths; findings must still anchor
    to the repo-relative hunks, and off-diff citations must be preserved."""
    from pr_reviewer.bugs import attach_findings
    from pr_reviewer.models import Hunk

    hunks = [Hunk(id="H1", file="graphql/authorization/index.js",
                  start=180, end=230, patch="x")]
    out = attach_findings([
        {"severity": "blocker", "category": "security", "title": "t", "detail": "d",
         "file": "/Users/u/.pr-reviewer/sandbox/repo/graphql/authorization/index.js",
         "start": 197, "end": 197},
        {"severity": "major", "category": "security", "title": "t2", "detail": "d",
         "file": "server/passport.js", "start": 125, "end": 125},  # not in diff
        {"severity": "nit", "category": "style", "title": "t3", "detail": "d",
         "file": "", "start": 0, "end": 0},
    ], hunks)

    a = out[0]
    assert a.anchors and a.anchors[0].file == "graphql/authorization/index.js"
    assert a.anchors[0].start == 197 and a.cited_line == 197

    b = out[1]  # keeps its citation for display even though it can't anchor
    assert not b.anchors and (b.cited_file, b.cited_line) == ("server/passport.js", 125)

    assert not out[2].anchors and out[2].cited_file == ""
