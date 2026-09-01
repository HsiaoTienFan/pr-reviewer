"""Bitbucket Cloud PRProvider — app password with pull request read scope."""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..models import PRContent, PRInfo

API = "https://api.bitbucket.org/2.0"
_URL_RE = re.compile(r"bitbucket\.org/([^/\s]+/[^/\s]+)/pull-requests/(\d+)")


class BitbucketProvider:
    name = "bitbucket"

    def __init__(self, username: str = "", app_password: str = "") -> None:
        self.username = username
        self.app_password = app_password

    def matches(self, url: str) -> bool:
        return "bitbucket.org/" in url

    def parse_url(self, url: str) -> tuple[str, int] | None:
        m = _URL_RE.search(url)
        return (m.group(1), int(m.group(2))) if m else None

    def configured(self) -> bool:
        return bool(self.username and self.app_password)

    @property
    def _auth(self) -> tuple[str, str] | None:
        return (self.username, self.app_password) if self.configured() else None

    async def fetch(self, repo: str, number: int) -> PRContent:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, auth=self._auth) as client:
            meta_r = await client.get(f"{API}/repositories/{repo}/pullrequests/{number}")
            meta_r.raise_for_status()
            meta = meta_r.json()
            diff_r = await client.get(f"{API}/repositories/{repo}/pullrequests/{number}/diff")
            diff_r.raise_for_status()
        diff = diff_r.text
        additions = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        deletions = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
        return PRContent(
            provider=self.name,
            repo=repo,
            number=number,
            url=((meta.get("links") or {}).get("html") or {}).get("href", ""),
            title=meta.get("title", ""),
            description=meta.get("description") or "",
            author=(meta.get("author") or {}).get("display_name", ""),
            branch=((meta.get("source") or {}).get("branch") or {}).get("name", ""),
            base_branch=((meta.get("destination") or {}).get("branch") or {}).get("name", ""),
            updated_at=meta.get("updated_on", ""),
            additions=additions,
            deletions=deletions,
            diff=diff,
        )

    async def list_open_prs(self, repo: str) -> list[PRInfo]:
        async with httpx.AsyncClient(timeout=30, auth=self._auth) as client:
            r = await client.get(
                f"{API}/repositories/{repo}/pullrequests",
                params={"state": "OPEN", "pagelen": 50},
            )
            r.raise_for_status()
        out = []
        for pr in r.json().get("values", []):
            out.append(PRInfo(
                provider=self.name,
                repo=repo,
                number=pr["id"],
                url=((pr.get("links") or {}).get("html") or {}).get("href", ""),
                title=pr.get("title", ""),
                description=pr.get("description") or "",
                author=(pr.get("author") or {}).get("display_name", ""),
                branch=((pr.get("source") or {}).get("branch") or {}).get("name", ""),
                base_branch=((pr.get("destination") or {}).get("branch") or {}).get("name", ""),
                updated_at=pr.get("updated_on", ""),
                reviewers=[(p.get("user") or p or {}).get("display_name", "")
                           for p in (pr.get("reviewers") or pr.get("participants") or [])],
            ))
        return out

    async def fetch_info(self, repo: str, number: int) -> PRInfo:
        """Metadata only — used for pinned PRs without pulling the diff."""
        async with httpx.AsyncClient(timeout=30, auth=self._auth) as client:
            r = await client.get(f"{API}/repositories/{repo}/pullrequests/{number}")
            r.raise_for_status()
        pr = r.json()
        return PRInfo(
            provider=self.name,
            repo=repo,
            number=pr["id"],
            url=((pr.get("links") or {}).get("html") or {}).get("href", ""),
            title=pr.get("title", ""),
            description=pr.get("description") or "",
            author=(pr.get("author") or {}).get("display_name", ""),
            branch=((pr.get("source") or {}).get("branch") or {}).get("name", ""),
            base_branch=((pr.get("destination") or {}).get("branch") or {}).get("name", ""),
            updated_at=pr.get("updated_on", ""),
            state={"OPEN": "open", "MERGED": "merged"}.get(pr.get("state", "OPEN"), "closed"),
            reviewers=[(p.get("user") or p or {}).get("display_name", "")
                       for p in (pr.get("reviewers") or [])],
        )

    async def current_user(self) -> str:
        if not self.configured():
            return ""
        try:
            async with httpx.AsyncClient(timeout=15, auth=self._auth) as client:
                r = await client.get(f"{API}/user")
            if r.status_code == 200:
                return r.json().get("display_name", "")
        except httpx.HTTPError:
            pass
        return ""

    async def search_prs(self, repos: list[str], query: str) -> list[PRInfo]:
        """Title-substring search over open PRs (Bitbucket has no cross-repo search)."""
        out: list[PRInfo] = []
        needle = query.strip().lower()
        if not needle:
            return out
        for repo in repos:
            try:
                for pr in await self.list_open_prs(repo):
                    if needle in pr.title.lower() or needle == str(pr.number):
                        out.append(pr)
            except httpx.HTTPError:
                continue
        return out

    async def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "Username and app password required"}
        try:
            async with httpx.AsyncClient(timeout=15, auth=self._auth) as client:
                r = await client.get(f"{API}/user")
            if r.status_code == 200:
                return {"ok": True, "message": f"Authenticated as {r.json().get('display_name', '?')}"}
            return {"ok": False, "message": f"Bitbucket returned {r.status_code} — check credentials"}
        except httpx.HTTPError as e:
            return {"ok": False, "message": f"Connection failed: {e}"}
