"""Jira Cloud ticket source — REST v2, basic auth (email + API token)."""
from __future__ import annotations

from typing import Any

import httpx

from ..models import TicketContent


def _flatten_adf(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text (defensive — v2 usually
    returns plain strings, but some instances hand back ADF dicts)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten_adf(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        text = _flatten_adf(node.get("content", []))
        if node.get("type") in {"paragraph", "heading", "listItem", "codeBlock"}:
            text += "\n"
        return text
    return ""


class JiraSource:
    name = "jira"

    def __init__(self, site_url: str = "", email: str = "", api_token: str = "") -> None:
        self.site_url = site_url.rstrip("/")
        self.email = email
        self.api_token = api_token

    def configured(self) -> bool:
        return bool(self.site_url and self.email and self.api_token)

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.email, self.api_token)

    async def fetch(self, key: str) -> TicketContent | None:
        if not self.configured():
            return None
        try:
            async with httpx.AsyncClient(timeout=20, auth=self._auth) as client:
                r = await client.get(
                    f"{self.site_url}/rest/api/2/issue/{key}",
                    params={"fields": "summary,description"},
                )
            if r.status_code != 200:
                return None
            data = r.json()
        except httpx.HTTPError:
            return None
        fields = data.get("fields") or {}
        return TicketContent(
            key=data.get("key", key),
            source=self.name,
            title=fields.get("summary") or "",
            body=_flatten_adf(fields.get("description") or ""),
            url=f"{self.site_url}/browse/{data.get('key', key)}",
        )

    async def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "Site URL, email, and API token required"}
        try:
            async with httpx.AsyncClient(timeout=15, auth=self._auth) as client:
                r = await client.get(f"{self.site_url}/rest/api/2/myself")
            if r.status_code == 200:
                return {"ok": True, "message": f"Authenticated as {r.json().get('displayName', '?')}"}
            if r.status_code == 401:
                return {"ok": False, "message": "401 from Jira — token may be expired"}
            return {"ok": False, "message": f"Jira returned {r.status_code}"}
        except httpx.HTTPError as e:
            return {"ok": False, "message": f"Connection failed: {e}"}
