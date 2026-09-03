"""FastAPI app — API for the three screens + static frontend serving."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .llm import build_backend
from .models import Review
from .pipeline import run_review
from .providers import build_providers, provider_for_url
from .tickets import build_sources, detect_ticket_refs

app = FastAPI(title="PR Reviewer")

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

SECRET_FIELDS = {
    "github": ["token"],
    "bitbucket": ["app_password"],
    "linear": ["api_key"],
    "jira": ["api_token"],
}

# ---------------------------------------------------------------- jobs

JOBS: dict[str, dict[str, Any]] = {}
RUNNING: dict[str, str] = {}  # review_id → job_id
TASKS: dict[str, asyncio.Task] = {}  # job_id → task, for cancellation
REVIEW_LOCKS: dict[str, asyncio.Lock] = {}  # review_id → save lock


def _review_lock(rid: str) -> asyncio.Lock:
    return REVIEW_LOCKS.setdefault(rid, asyncio.Lock())


def _job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _is_stale(pr_updated_at: str, review_created_at: str) -> bool | None:
    """True if the PR changed after the review was generated; None if unknown."""
    pr_dt, rev_dt = _parse_iso(pr_updated_at), _parse_iso(review_created_at)
    if pr_dt is None or rev_dt is None:
        return None
    if pr_dt.tzinfo is None or rev_dt.tzinfo is None:
        return None
    return pr_dt > rev_dt


# ---------------------------------------------------------------- settings

def _masked(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section, values in cfg.items():
        if not isinstance(values, dict):
            continue
        sec_out = {}
        for key, val in values.items():
            if key in SECRET_FIELDS.get(section, []):
                if val:
                    hint = f"{val[:4]}{'•' * 12}{val[-4:]}" if len(val) > 12 else "•" * 16
                    sec_out[key] = {"set": True, "hint": hint}
                else:
                    sec_out[key] = {"set": False, "hint": ""}
            else:
                sec_out[key] = val
        out[section] = sec_out
    # derived: browser-login fallback via the GitHub CLI when no PAT is stored
    from .providers.github import gh_cli_token

    out["github"]["gh_cli"] = {"active": not cfg["github"].get("token") and bool(gh_cli_token())}
    return out


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _masked(config.load_config())


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


@app.put("/api/settings/{section}")
async def put_settings(section: str, body: SettingsUpdate) -> dict[str, Any]:
    if section not in config.DEFAULT_CONFIG:
        raise HTTPException(404, f"unknown section '{section}'")
    allowed = set(config.DEFAULT_CONFIG[section])
    values = {k: v for k, v in body.values.items() if k in allowed}
    config.update_section(section, values)
    return _masked(config.load_config())


@app.get("/api/skills")
async def get_skills() -> dict[str, Any]:
    """User review skills, discovered from disk at request time — never cached,
    never hardcoded. `selected` = "" means the built-in /code-review flow."""
    from .bugs import _skills_root, list_skills

    ccfg = config.load_config().get("claude", {})
    skills_dir = ccfg.get("skills_dir", "")
    return {
        "dir": str(_skills_root(skills_dir)),
        "selected": ccfg.get("review_skill", ""),
        "skills": list_skills(skills_dir),
    }


@app.post("/api/settings/{section}/test")
async def test_connection(section: str) -> dict[str, Any]:
    cfg = config.load_config()
    if section in ("github", "bitbucket"):
        return await build_providers(cfg)[section].test_connection()
    if section in ("linear", "jira"):
        return await build_sources(cfg)[section].test_connection()
    raise HTTPException(404, f"no connection test for '{section}'")


class RepoChange(BaseModel):
    add: str | None = None
    remove: str | None = None


@app.post("/api/settings/{section}/repos")
async def change_repos(section: str, body: RepoChange) -> dict[str, Any]:
    if section not in ("github", "bitbucket"):
        raise HTTPException(404, "repos only apply to PR hosts")
    cfg = config.load_config()
    repos: list[str] = cfg[section].get("repos", [])
    if body.add:
        repo = body.add.strip().strip("/")
        if "/" not in repo:
            raise HTTPException(400, "repo must be owner/name")
        if repo not in repos:
            repos.append(repo)
    if body.remove and body.remove in repos:
        repos.remove(body.remove)
    config.update_section(section, {"repos": repos})
    return _masked(config.load_config())


# ---------------------------------------------------------------- claude status

@app.get("/api/claude/status")
async def claude_status(full: bool = False) -> dict[str, Any]:
    backend = build_backend(config.load_config())
    status = await backend.status(full=full)
    return status.model_dump()


# ---------------------------------------------------------------- auto-review watcher

# Scope is a design rule, not a setting: ONLY PRs where the user's review is
# requested or they are assigned. Never PRs they authored, never whole repos.
AUTO: dict[str, Any] = {"was_enabled": False, "starts": []}
_BOT_AUTHORS = {"dependabot", "renovate", "github-actions"}


def _auto_candidates(prs: list, me: str, since: datetime | None) -> list:
    """Which PRs qualify for an automatic review. Pure — unit-tested."""
    out = []
    if not me:
        return out
    for pr in prs:
        if pr.state != "open" or pr.draft:
            continue
        if pr.author == me:  # tagged/requested only — own PRs are out of scope
            continue
        author = (pr.author or "").lower()
        if author.endswith("[bot]") or author in _BOT_AUTHORS:
            continue
        if me not in pr.assignees and me not in pr.reviewers:
            continue
        if since is not None:
            upd = _parse_iso(pr.updated_at)
            if upd is None or upd <= since:  # no backfill of pre-enable PRs
                continue
        out.append(pr)
    return out


async def _auto_review_tick() -> list[str]:
    cfg = config.load_config()
    ar = cfg.get("auto_review", {})
    enabled = bool(ar.get("enabled"))
    if enabled and not AUTO["was_enabled"]:
        # rising edge: stamp `since` so pre-existing PRs are never swept in
        config.update_section(
            "auto_review", {"since": datetime.now(timezone.utc).isoformat()})
        cfg = config.load_config()
        ar = cfg.get("auto_review", {})
    AUTO["was_enabled"] = enabled
    if not enabled:
        return []
    # one pipeline at a time — never pile onto a manual run either
    if any(not j.get("done") for j in JOBS.values()):
        return []
    import time as _time
    now = _time.time()
    AUTO["starts"] = [t for t in AUTO["starts"] if now - t < 3600]
    if len(AUTO["starts"]) >= int(ar.get("max_per_hour", 3) or 3):
        return []
    since = _parse_iso(ar.get("since", ""))

    providers = build_providers(cfg)
    for pname, provider in providers.items():
        if not provider.configured():
            continue
        try:
            me = await provider.current_user()
        except Exception:
            continue
        for repo in cfg.get(pname, {}).get("repos", []):
            try:
                prs = await provider.list_open_prs(repo)
            except Exception:
                continue
            for pr in _auto_candidates(prs, me, since):
                rid = f"{pname}:{repo}:{pr.number}"
                if config.load_review(rid) is not None:
                    continue
                try:
                    res = await start_review(ReviewRequest(
                        provider=pname, repo=repo, number=pr.number))
                except HTTPException as e:
                    print(f"[auto-review] cannot start {rid}: {e.detail}")
                    return []
                if res.get("job_id"):
                    AUTO["starts"].append(now)
                    print(f"[auto-review] started {rid}: {pr.title[:70]}")
                    return [rid]  # at most one launch per tick
    return []


async def _auto_review_loop() -> None:
    await asyncio.sleep(5)  # let the server finish booting
    while True:
        try:
            await _auto_review_tick()
        except Exception as e:  # a bad tick must never kill the watcher
            print(f"[auto-review] tick failed: {type(e).__name__}: {e}")
        poll = int(config.load_config().get("auto_review", {}).get("poll_seconds", 120) or 120)
        await asyncio.sleep(max(60, poll))


@app.on_event("startup")
async def _start_watcher() -> None:
    app.state.auto_task = asyncio.create_task(_auto_review_loop())


@app.on_event("shutdown")
async def _stop_watcher() -> None:
    task = getattr(app.state, "auto_task", None)
    if task is not None:
        task.cancel()


# ---------------------------------------------------------------- PR listing

def _summarize(review: Review) -> dict[str, Any]:
    fulfilled = sum(1 for l in review.links if l.status == "fulfilled")
    partial = sum(1 for l in review.links if l.status == "partial")
    gaps = sum(1 for l in review.links if l.status == "notfound")
    total = len(review.requirements)
    claim_cards = total if review.mode == "requirements" else len(review.unexplained)
    return {
        "mode": review.mode,
        "fulfilled": fulfilled,
        "partial": partial,
        "gaps": gaps,
        "total": total,
        "unexplained": len(review.unexplained),
        "verified": len(review.verified),
        "claims": claim_cards,
    }


def _ticket_src(cfg: dict[str, Any]) -> str:
    lin_ok = bool(cfg["linear"].get("api_key"))
    jira_ok = bool(cfg["jira"].get("site_url"))
    if lin_ok and not jira_ok:
        return "linear"
    if jira_ok and not lin_ok:
        return "jira"
    return "ticket"


def _pin_map(cfg: dict[str, Any]) -> dict[tuple[str, str], set[int]]:
    out: dict[tuple[str, str], set[int]] = {}
    for rid in cfg.get("pins", []):
        try:
            pname, rest = rid.split(":", 1)
            repo, num = rest.rsplit(":", 1)
            out.setdefault((pname, repo), set()).add(int(num))
        except ValueError:
            continue
    return out


@app.get("/api/prs")
async def list_prs() -> dict[str, Any]:
    cfg = config.load_config()
    providers = build_providers(cfg)
    src = _ticket_src(cfg)
    pin_map = _pin_map(cfg)

    users: dict[str, str] = {}
    for pname, provider in providers.items():
        try:
            users[pname] = await provider.current_user()
        except Exception:
            users[pname] = ""

    def role_of(pr, me: str) -> str:
        """mine = authored; assigned = review requested / assignee; other = pin-only."""
        if me and pr.author == me:
            return "mine"
        if me and (me in pr.assignees or me in pr.reviewers):
            return "assigned"
        return "other"

    def row(pname: str, repo: str, pr, pinned: bool, me: str) -> dict[str, Any]:
        refs = detect_ticket_refs(pr.branch, pr.title, pr.description)
        stored = config.load_review(f"{pname}:{repo}:{pr.number}")
        return {
            **pr.model_dump(),
            "tickets": [{"key": r, "source": src, "url": "", "title": ""} for r in refs],
            "review": _summarize(stored) if stored else None,
            "stale": _is_stale(pr.updated_at, stored.created_at) if stored else None,
            "pinned": pinned,
            "role": role_of(pr, me),
        }

    async def fetch_group(pname: str, repo: str) -> dict[str, Any]:
        provider = providers[pname]
        me = users.get(pname, "")
        pinned_nums = pin_map.get((pname, repo), set())
        try:
            prs = await provider.list_open_prs(repo)
        except Exception as e:
            return {"provider": pname, "repo": repo, "prs": [], "error": str(e)[:200]}
        rows, seen = [], set()
        for pr in prs:
            is_pin = pr.number in pinned_nums
            if role_of(pr, me) == "other" and not is_pin:
                continue
            seen.add(pr.number)
            rows.append(row(pname, repo, pr, is_pin, me))
        for num in sorted(pinned_nums - seen):  # pinned but not in the open list
            try:
                info = await provider.fetch_info(repo, num)
            except Exception:
                continue
            if info.state != "open":
                # merged/closed → the pin has served its purpose; drop it
                config.change_pin(f"{pname}:{repo}:{num}", add=False)
                continue
            rows.append(row(pname, repo, info, True, me))
        return {"provider": pname, "repo": repo, "prs": rows, "error": None,
                "truncated": len(prs) >= 50}

    repo_keys: list[tuple[str, str]] = []
    for pname in ("github", "bitbucket"):
        for repo in cfg[pname].get("repos", []):
            repo_keys.append((pname, repo))
    for key in pin_map:  # pin-only repos still get a group
        if key not in repo_keys:
            repo_keys.append(key)

    groups = list(await asyncio.gather(*[fetch_group(p, r) for p, r in repo_keys])) if repo_keys else []
    return {
        "groups": groups,
        "configured": {p: providers[p].configured() for p in providers},
        "me": users,
    }


@app.get("/api/prs/search")
async def search_prs(q: str) -> dict[str, Any]:
    cfg = config.load_config()
    providers = build_providers(cfg)
    pins = set(cfg.get("pins", []))
    results: list[dict[str, Any]] = []
    for pname in ("github", "bitbucket"):
        repos = cfg[pname].get("repos", [])
        if not repos:
            continue
        try:
            found = await providers[pname].search_prs(repos, q)
        except Exception:
            found = []
        for pr in found:
            results.append({
                **pr.model_dump(),
                "pinned": f"{pname}:{pr.repo}:{pr.number}" in pins,
            })
    return {"results": results}


class PinChange(BaseModel):
    provider: str
    repo: str
    number: int
    pinned: bool


@app.post("/api/pins")
async def change_pin(body: PinChange) -> dict[str, Any]:
    rid = f"{body.provider}:{body.repo}:{body.number}"
    return {"pins": config.change_pin(rid, body.pinned)}


# ---------------------------------------------------------------- reviews

class ReviewRequest(BaseModel):
    url: str | None = None
    provider: str | None = None
    repo: str | None = None
    number: int | None = None
    force: bool = False  # False → reuse a stored review instead of re-analyzing


@app.post("/api/reviews")
async def start_review(body: ReviewRequest) -> dict[str, Any]:
    cfg = config.load_config()
    providers = build_providers(cfg)

    if body.url:
        hit = provider_for_url(providers, body.url.strip())
        if hit is None:
            raise HTTPException(400, "URL not recognized — expected a GitHub or Bitbucket PR URL")
        provider, repo, number = hit
    elif body.provider and body.repo and body.number:
        provider = providers.get(body.provider)
        if provider is None:
            raise HTTPException(400, f"unknown provider '{body.provider}'")
        repo, number = body.repo, body.number
    else:
        raise HTTPException(400, "provide a url or provider+repo+number")

    rid = f"{provider.name}:{repo}:{number}"
    if rid in RUNNING and not JOBS[RUNNING[rid]]["done"]:
        return {"job_id": RUNNING[rid], "review_id": rid}

    if not body.force and config.load_review(rid) is not None:
        return {"job_id": None, "review_id": rid, "cached": True}

    backend = build_backend(cfg)
    status = await backend.status()
    if not status.ready:
        raise HTTPException(409, f"Claude backend not ready: {status.summary}. {status.fix}")

    job_id = uuid.uuid4().hex[:12]
    job = {"stage": "queued", "detail": "Queued", "error": None, "done": False, "review_id": rid}
    JOBS[job_id] = job
    RUNNING[rid] = job_id

    def progress(stage: str, detail: str) -> None:
        job["stage"], job["detail"] = stage, detail

    prev = config.load_review(rid) if body.force else None

    async def run() -> None:
        from .bugs import collect_findings_raw, run_code_review
        from .llm.base import LLMError
        from .pipeline import merge_rerun_state

        claude_cfg = cfg.get("claude", {})
        instr = cfg.get("custom_review", {}).get("instructions", "")
        # Findings are part of every review. The skill run is the slowest stage,
        # so it starts now — concurrent with extract/map/flow — and joins below.
        findings_task = asyncio.create_task(collect_findings_raw(
            provider.pr_url(repo, number), backend,
            skill=claude_cfg.get("review_skill", ""),
            skills_dir=claude_cfg.get("skills_dir", ""),
            instructions=instr,
        ))
        try:
            review = await run_review(
                provider, repo, number,
                sources=build_sources(cfg),
                backend=backend,
                progress=progress,
                instructions=instr,
            )
            if prev is not None:
                progress("save", "Carrying over verified/reviewer state")
                await merge_rerun_state(review, prev, backend, progress)
            progress("findings", "Collecting code-review findings")
            try:
                findings, report, dropped = await run_code_review(
                    review, backend, progress, precollected=findings_task,
                    instructions=instr)
            except LLMError as e:
                # the review itself succeeded — surface the miss, don't fail the job
                progress("done", f"Review complete (findings failed: {e} — use ↻ Findings to retry)")
            else:
                async with _review_lock(rid):
                    from .bugs import carry_finding_edits
                    latest = config.load_review(rid) or review
                    latest.bugs = carry_finding_edits(latest.bugs, findings)
                    latest.bugs_ran = True
                    latest.bugs_stale = False
                    latest.bugs_report = report
                    if dropped:
                        latest.overflow = {**latest.overflow, "findings": dropped}
                    config.save_review(latest)
                progress("done", "Review complete")
        except asyncio.CancelledError:
            job["error"] = "Cancelled"
            raise
        except Exception as e:  # surfaced to the UI, not swallowed
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            if not findings_task.done():
                findings_task.cancel()
            job["done"] = True
            RUNNING.pop(rid, None)
            TASKS.pop(job_id, None)

    TASKS[job_id] = asyncio.create_task(run())
    return {"job_id": job_id, "review_id": rid}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    return _job(job_id)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    task = TASKS.get(job_id)
    if task is not None and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False, "done": job.get("done", False)}


@app.get("/api/reviews/{rid:path}/jobs")
async def review_jobs(rid: str) -> dict[str, Any]:
    """In-flight jobs for a review — lets the UI resume tracking after a reload."""
    def live(key: str) -> str | None:
        job_id = RUNNING.get(key)
        return job_id if job_id and not JOBS.get(job_id, {}).get("done") else None

    return {
        "requirements": live(rid),
        "code_review": live(f"bugs:{rid}"),
        "add_requirement": live(f"addreq:{rid}"),
    }


class AddRequirementRequest(BaseModel):
    text: str


@app.post("/api/reviews/{rid:path}/requirements")
async def add_requirement(rid: str, body: AddRequirementRequest) -> dict[str, Any]:
    from .pipeline import map_new_requirement

    review = config.load_review(rid)
    if review is None:
        raise HTTPException(404, "no stored review")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "requirement text is empty")
    key = f"addreq:{rid}"
    if key in RUNNING and not JOBS[RUNNING[key]]["done"]:
        return {"job_id": RUNNING[key], "review_id": rid}

    backend = build_backend(config.load_config())
    status = await backend.status()
    if not status.ready:
        raise HTTPException(409, f"Claude backend not ready: {status.summary}. {status.fix}")

    job_id = uuid.uuid4().hex[:12]
    job = {"stage": "map", "detail": "Mapping new requirement", "error": None, "done": False, "review_id": rid}
    JOBS[job_id] = job
    RUNNING[key] = job_id

    def progress(stage: str, detail: str) -> None:
        job["stage"], job["detail"] = stage, detail

    async def run() -> None:
        try:
            async with _review_lock(rid):
                latest = config.load_review(rid) or review
                await map_new_requirement(latest, text, backend, progress)
                usage = getattr(backend, "usage", {}) or {}
                latest.llm_usage = {k: latest.llm_usage.get(k, 0) + v for k, v in usage.items()}
                config.save_review(latest)
        except asyncio.CancelledError:
            job["error"] = "Cancelled"
            raise
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            job["done"] = True
            RUNNING.pop(key, None)
            TASKS.pop(job_id, None)

    TASKS[job_id] = asyncio.create_task(run())
    return {"job_id": job_id, "review_id": rid}


@app.get("/api/reviews/{rid:path}/data")
async def get_review(rid: str) -> Any:
    review = config.load_review(rid)
    if review is None:
        raise HTTPException(404, "no stored review")
    stale: bool | None = None
    try:  # best-effort freshness check against the live PR
        providers = build_providers(config.load_config())
        provider = providers.get(review.pr.provider)
        if provider is not None:
            info = await provider.fetch_info(review.pr.repo, review.pr.number)
            stale = _is_stale(info.updated_at, review.created_at)
    except Exception:
        stale = None
    return JSONResponse({**review.model_dump(), "stale": stale})


@app.delete("/api/reviews/{rid:path}")
async def remove_review(rid: str) -> dict[str, Any]:
    from .bugs import cleanup_sandbox

    if not config.delete_review(rid):
        raise HTTPException(404, "no stored review")
    # review skills clone the PR's repo into the sandbox; once the last review
    # for a repo is gone, its checkout has no reason to stay on disk
    active = {r.pr.repo for r in config.all_reviews()}
    removed = cleanup_sandbox(active)
    return {"deleted": rid, "sandbox_removed": removed}


class FindingEdit(BaseModel):
    severity: str | None = None
    category: str | None = None
    note: str | None = None


@app.patch("/api/reviews/{rid:path}/findings/{fid}")
async def edit_finding(rid: str, fid: str, body: FindingEdit) -> dict[str, Any]:
    """Reviewer adjustments to a finding: severity, category, attached note.
    These are the reviewer's decisions — they survive re-runs (title-matched)."""
    from .bugs import VALID_CATEGORIES, VALID_SEVERITIES

    if body.severity is not None and body.severity not in VALID_SEVERITIES:
        raise HTTPException(400, f"severity must be one of {VALID_SEVERITIES}")
    if body.category is not None and body.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {VALID_CATEGORIES}")
    async with _review_lock(rid):
        review = config.load_review(rid)
        if review is None:
            raise HTTPException(404, "no stored review")
        finding = next((b for b in review.bugs if b.id == fid), None)
        if finding is None:
            raise HTTPException(404, f"no finding {fid}")
        if body.severity is not None and body.severity != finding.severity:
            finding.severity = body.severity
            finding.edited = True
        if body.category is not None and body.category != finding.category:
            finding.category = body.category
            finding.edited = True
        if body.note is not None:
            finding.note = body.note.strip()[:4000]
        config.save_review(review)
        return {"finding": finding.model_dump()}


class AskRequest(BaseModel):
    question: str
    inspect: bool = False


@app.post("/api/reviews/{rid:path}/findings/{fid}/ask")
async def ask_about_finding(rid: str, fid: str, body: AskRequest) -> dict[str, Any]:
    from .bugs import ask_finding
    from .models import ThreadMsg

    review = config.load_review(rid)
    if review is None:
        raise HTTPException(404, "no stored review")
    if not any(b.id == fid for b in review.bugs):
        raise HTTPException(404, f"no finding {fid}")
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")

    cfg = config.load_config()
    backend = build_backend(cfg)
    job_id = uuid.uuid4().hex[:12]
    job = {"stage": "ask", "detail": f"Answering question about {fid}", "error": None,
           "done": False, "review_id": rid, "answer": None}
    JOBS[job_id] = job

    async def run() -> None:
        from datetime import datetime, timezone
        try:
            answer = await ask_finding(review, fid, q, backend, inspect=body.inspect)
            now = datetime.now(timezone.utc).isoformat()
            async with _review_lock(rid):
                latest = config.load_review(rid) or review
                thread = latest.threads.setdefault(fid, [])
                thread.append(ThreadMsg(role="user", text=q, ts=now))
                thread.append(ThreadMsg(role="assistant", text=answer, ts=now))
                config.save_review(latest)
            job["answer"] = answer
        except asyncio.CancelledError:
            job["error"] = "Cancelled"
            raise
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            job["done"] = True
            TASKS.pop(job_id, None)

    TASKS[job_id] = asyncio.create_task(run())
    return {"job_id": job_id, "review_id": rid}


class PublishRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/reviews/{rid:path}/publish")
async def publish_review(rid: str, body: PublishRequest) -> dict[str, Any]:
    """Post the link graph to the PR as a review: summary body + inline
    comments on each claim's first anchor. Only ever called from an explicit,
    user-confirmed action in the UI."""
    review = config.load_review(rid)
    if review is None:
        raise HTTPException(404, "no stored review")
    provider = build_providers(config.load_config()).get(review.pr.provider)
    publish = getattr(provider, "publish_review", None)
    if publish is None:
        raise HTTPException(400, f"publishing not supported for {review.pr.provider}")

    glyph = {"fulfilled": "✅", "partial": "🟡", "notfound": "❌"}
    lines = ["## Requirements review", ""]
    if review.net_effect:
        lines += ["**Net effect:**"] + [f"- {l}" for l in review.net_effect] + [""]
    for req in review.requirements:
        link = next((l for l in review.links if l.requirement_id == req.id), None)
        if link is None:
            continue
        lines.append(f"{glyph.get(link.status, '•')} **{req.id}** {req.text}")
        if link.mechanism:
            lines.append(f"  - {link.mechanism}")
        if link.why:
            lines.append(f"  - _why:_ {link.why}")
        if link.missing:
            lines.append(f"  - _missing:_ {link.missing}")
    if review.unexplained:
        lines.append("")
        more_u = f" …and {len(review.unexplained) - 8} more" if len(review.unexplained) > 8 else ""
        lines.append("**Unexplained changes:** " + ", ".join(u.label for u in review.unexplained[:8]) + more_u)
    if review.bugs_ran and review.bugs:
        lines.append("")
        more_b = f" …and {len(review.bugs) - 8} more" if len(review.bugs) > 8 else ""
        lines.append("**Code-review findings:** " + "; ".join(f"[{b.severity}] {b.title}" for b in review.bugs[:8]) + more_b)
    lines.append("")
    lines.append("_Generated by PR Reviewer (requirements-grounded review)._")
    md_body = "\n".join(lines)

    comments: list[dict[str, Any]] = []
    for req in review.requirements:
        link = next((l for l in review.links if l.requirement_id == req.id), None)
        if link is None or not link.anchors:
            continue
        a = link.anchors[0]
        text = f"**{req.id} · {link.status}** — {req.text}\n\n{link.mechanism}"
        if link.why:
            text += f"\n\n_Why:_ {link.why}"
        comments.append({"path": a.file, "line": a.start, "body": text})
    for u in review.unexplained[:5]:
        if u.anchors:
            comments.append({"path": u.anchors[0].file, "line": u.anchors[0].start,
                             "body": f"❓ **Unexplained change** — {u.label}\n\n{u.mechanism}"})
    if len(comments) > 20:
        md_body += f"\n\n_{len(comments) - 20} additional inline comments omitted._"
    comments = comments[:20]

    if body.dry_run:
        return {"ok": True, "dry_run": True, "body": md_body, "comments": comments}
    result = await publish(review.pr.repo, review.pr.number, md_body, comments)
    if result.get("ok"):
        async with _review_lock(rid):
            latest = config.load_review(rid) or review
            latest.published_url = result.get("url", "")
            latest.published_at = datetime.now().astimezone().isoformat()
            config.save_review(latest)
        result["published_at"] = latest.published_at
    return result


@app.post("/api/reviews/{rid:path}/code-review")
async def start_code_review(rid: str) -> dict[str, Any]:
    from .bugs import run_code_review

    review = config.load_review(rid)
    if review is None:
        raise HTTPException(404, "no stored review — run the requirements review first")
    key = f"bugs:{rid}"
    if key in RUNNING and not JOBS[RUNNING[key]]["done"]:
        return {"job_id": RUNNING[key], "review_id": rid}

    cfg = config.load_config()
    backend = build_backend(cfg)
    status = await backend.status()
    if not status.ready:
        raise HTTPException(409, f"Claude backend not ready: {status.summary}. {status.fix}")

    job_id = uuid.uuid4().hex[:12]
    job = {"stage": "code-review", "detail": "Starting code review", "error": None, "done": False, "review_id": rid}
    JOBS[job_id] = job
    RUNNING[key] = job_id

    def progress(stage: str, detail: str) -> None:
        job["stage"], job["detail"] = stage, detail

    async def run() -> None:
        try:
            claude_cfg = cfg.get("claude", {})
            findings, report, dropped = await run_code_review(
                review, backend, progress,
                skill=claude_cfg.get("review_skill", ""),
                skills_dir=claude_cfg.get("skills_dir", ""),
                instructions=cfg.get("custom_review", {}).get("instructions", ""),
            )
            async with _review_lock(rid):
                # reload: a re-run may have replaced the review while we worked
                from .bugs import carry_finding_edits
                latest = config.load_review(rid) or review
                latest.bugs = carry_finding_edits(latest.bugs, findings)
                latest.bugs_ran = True
                latest.bugs_stale = False
                latest.bugs_report = report
                if dropped:
                    latest.overflow["findings"] = dropped
                else:
                    latest.overflow.pop("findings", None)
                usage = getattr(backend, "usage", {}) or {}
                latest.llm_usage = {k: latest.llm_usage.get(k, 0) + v for k, v in usage.items()}
                config.save_review(latest)
        except asyncio.CancelledError:
            job["error"] = "Cancelled"
            raise
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            job["done"] = True
            RUNNING.pop(key, None)
            TASKS.pop(job_id, None)

    TASKS[job_id] = asyncio.create_task(run())
    return {"job_id": job_id, "review_id": rid}


class VerifyRequest(BaseModel):
    card_id: str
    verified: bool


@app.post("/api/reviews/{rid:path}/verify")
async def verify_card(rid: str, body: VerifyRequest) -> dict[str, Any]:
    async with _review_lock(rid):
        review = config.load_review(rid)
        if review is None:
            raise HTTPException(404, "no stored review")
        verified = set(review.verified)
        (verified.add if body.verified else verified.discard)(body.card_id)
        review.verified = sorted(verified)
        config.save_review(review)
    return {"verified": review.verified}


# ---------------------------------------------------------------- static

@app.middleware("http")
async def no_static_cache(request, call_next):
    # local single-user app: always serve fresh UI after upgrades
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8712)


if __name__ == "__main__":
    main()
