"""GitHub PRProvider — REST v3. Auth: pasted PAT, or fallback to the GitHub
CLI's browser-based login (`gh auth login`) via `gh auth token` — credentials
stay managed by gh, mirroring the Claude backend approach (DECISIONS #14)."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Any

import httpx

from ..models import PRContent, PRInfo

API = "https://api.github.com"
_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

_gh_cache: tuple[float, str] = (0.0, "")
_GH_TTL_S = 300
_user_cache: tuple[float, str, str] = (0.0, "", "")  # (ts, token, login)


def _set_user_cache(ts: float, token: str, login: str) -> None:
    global _user_cache
    _user_cache = (ts, token, login)


def gh_cli_token() -> str:
    """Token from the GitHub CLI's own login, '' if gh is absent/logged out."""
    global _gh_cache
    ts, tok = _gh_cache
    if time.time() - ts < _GH_TTL_S:
        return tok
    tok = ""
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=8)
            if out.returncode == 0:
                tok = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            tok = ""
    _gh_cache = (time.time(), tok)
    return tok


class GitHubProvider:
    name = "github"

    def __init__(self, token: str = "", via_gh_cli: bool = False) -> None:
        self.token = token
        self.via_gh_cli = via_gh_cli

    def matches(self, url: str) -> bool:
        return "github.com/" in url

    def pr_url(self, repo: str, number: int) -> str:
        return f"https://github.com/{repo}/pull/{number}"

    def parse_url(self, url: str) -> tuple[str, int] | None:
        m = _URL_RE.search(url)
        return (m.group(1), int(m.group(2))) if m else None

    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def fetch(self, repo: str, number: int) -> PRContent:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            meta_r = await client.get(f"{API}/repos/{repo}/pulls/{number}", headers=self._headers())
            meta_r.raise_for_status()
            meta = meta_r.json()
            diff_r = await client.get(
                f"{API}/repos/{repo}/pulls/{number}",
                headers=self._headers("application/vnd.github.diff"),
            )
            diff_r.raise_for_status()
        return PRContent(
            provider=self.name,
            repo=repo,
            number=number,
            url=meta.get("html_url", ""),
            title=meta.get("title", ""),
            description=meta.get("body") or "",
            author=(meta.get("user") or {}).get("login", ""),
            branch=(meta.get("head") or {}).get("ref", ""),
            base_branch=(meta.get("base") or {}).get("ref", ""),
            updated_at=meta.get("updated_at", ""),
            additions=meta.get("additions", 0),
            deletions=meta.get("deletions", 0),
            diff=diff_r.text,
        )

    def _pr_info(self, repo: str, pr: dict) -> PRInfo:
        return PRInfo(
            provider=self.name,
            repo=repo,
            number=pr["number"],
            url=pr.get("html_url", ""),
            title=pr.get("title", ""),
            description=pr.get("body") or "",
            author=(pr.get("user") or {}).get("login", ""),
            branch=(pr.get("head") or {}).get("ref", ""),
            base_branch=(pr.get("base") or {}).get("ref", ""),
            updated_at=pr.get("updated_at", ""),
            state="merged" if pr.get("merged_at") else pr.get("state", "open"),
            additions=pr.get("additions", 0),
            deletions=pr.get("deletions", 0),
            assignees=[(a or {}).get("login", "") for a in pr.get("assignees") or []],
            reviewers=[(a or {}).get("login", "") for a in pr.get("requested_reviewers") or []],
        )

    async def list_open_prs(self, repo: str) -> list[PRInfo]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{API}/repos/{repo}/pulls",
                params={"state": "open", "per_page": 50},
                headers=self._headers(),
            )
            r.raise_for_status()
        return [self._pr_info(repo, pr) for pr in r.json()]

    async def fetch_info(self, repo: str, number: int) -> PRInfo:
        """Metadata only — used for pinned PRs without pulling the diff."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/repos/{repo}/pulls/{number}", headers=self._headers())
            r.raise_for_status()
        return self._pr_info(repo, r.json())

    async def current_user(self) -> str:
        """Login of the authenticated user; '' when unauthenticated."""
        if not self.token:
            return ""
        ts, tok, login = _user_cache
        if tok == self.token and time.time() - ts < _GH_TTL_S:
            return login
        login = ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{API}/user", headers=self._headers())
            if r.status_code == 200:
                login = r.json().get("login", "")
        except httpx.HTTPError:
            login = ""
        _set_user_cache(time.time(), self.token, login)
        return login

    async def search_prs(self, repos: list[str], query: str) -> list[PRInfo]:
        """Open-PR search across the given repos via the GitHub search API."""
        if not repos or not query.strip():
            return []
        q = f"is:pr is:open {query.strip()} " + " ".join(f"repo:{r}" for r in repos)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{API}/search/issues",
                params={"q": q, "per_page": 20},
                headers=self._headers(),
            )
            r.raise_for_status()
        out = []
        for item in r.json().get("items", []):
            repo_url = item.get("repository_url", "")
            repo = "/".join(repo_url.rsplit("/", 2)[-2:]) if repo_url else ""
            out.append(PRInfo(
                provider=self.name,
                repo=repo,
                number=item.get("number", 0),
                url=item.get("html_url", ""),
                title=item.get("title", ""),
                author=(item.get("user") or {}).get("login", ""),
                updated_at=item.get("updated_at", ""),
            ))
        return out

    async def fetch_comments(self, repo: str, number: int, limit: int = 30) -> list[dict[str, str]]:
        """PR discussion (issue comments + review comments) — extra requirement
        context for EXTRACT. Read-only; failures degrade to no discussion."""
        out: list[dict[str, str]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            for path in (f"issues/{number}/comments", f"pulls/{number}/comments"):
                try:
                    r = await client.get(
                        f"{API}/repos/{repo}/{path}",
                        params={"per_page": limit}, headers=self._headers(),
                    )
                    if r.status_code != 200:
                        continue
                    for c in r.json():
                        body = (c.get("body") or "").strip()
                        if body:
                            out.append({"author": (c.get("user") or {}).get("login", ""), "body": body[:2000]})
                except httpx.HTTPError:
                    continue
        return out[:limit]

    async def publish_review(
        self, repo: str, number: int, body: str, comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Post the link graph back to GitHub as a PR review with inline comments."""
        payload: dict[str, Any] = {
            "body": body,
            "event": "COMMENT",
            "comments": [
                {"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": c["body"]}
                for c in comments
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{API}/repos/{repo}/pulls/{number}/reviews",
                    json=payload, headers=self._headers(),
                )
            if r.status_code in (200, 201):
                return {"ok": True, "url": r.json().get("html_url", ""), "message": "Review posted"}
            # inline positions can be rejected (e.g. outdated diff) — retry summary-only
            if payload["comments"]:
                async with httpx.AsyncClient(timeout=30) as client:
                    r2 = await client.post(
                        f"{API}/repos/{repo}/pulls/{number}/reviews",
                        json={"body": body, "event": "COMMENT"}, headers=self._headers(),
                    )
                if r2.status_code in (200, 201):
                    return {"ok": True, "url": r2.json().get("html_url", ""),
                            "message": "Inline anchors rejected by GitHub — posted summary comment only"}
            return {"ok": False, "message": f"GitHub returned {r.status_code}: {r.text[:200]}"}
        except httpx.HTTPError as e:
            return {"ok": False, "message": f"Connection failed: {e}"}

    async def test_connection(self) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "message": "No token configured"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{API}/user", headers=self._headers())
            if r.status_code == 200:
                via = " (via GitHub CLI login)" if self.via_gh_cli else ""
                return {"ok": True, "message": f"Authenticated as {r.json().get('login', '?')}{via}"}
            return {"ok": False, "message": f"GitHub returned {r.status_code} — check the token"}
        except httpx.HTTPError as e:
            return {"ok": False, "message": f"Connection failed: {e}"}
