"""Linear ticket source — GraphQL with a personal API key."""
from __future__ import annotations

from typing import Any

import httpx

from ..models import TicketContent

API = "https://api.linear.app/graphql"

_ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) { identifier title description url }
}
"""

_VIEWER_QUERY = "query { viewer { name email } }"


class LinearSource:
    name = "linear"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                API,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            )
        r.raise_for_status()
        return r.json()

    async def fetch(self, key: str) -> TicketContent | None:
        if not self.configured():
            return None
        try:
            data = await self._gql(_ISSUE_QUERY, {"id": key})
        except httpx.HTTPError:
            return None
        issue = (data.get("data") or {}).get("issue")
        if not issue:
            return None
        return TicketContent(
            key=issue.get("identifier", key),
            source=self.name,
            title=issue.get("title", ""),
            body=issue.get("description") or "",
            url=issue.get("url", ""),
        )

    async def test_connection(self) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "message": "No API key configured"}
        try:
            data = await self._gql(_VIEWER_QUERY)
        except httpx.HTTPError as e:
            return {"ok": False, "message": f"Connection failed: {e}"}
        if data.get("errors"):
            return {"ok": False, "message": f"Linear error: {data['errors'][0].get('message', '?')}"}
        viewer = (data.get("data") or {}).get("viewer") or {}
        return {"ok": True, "message": f"Authenticated as {viewer.get('name') or viewer.get('email', '?')}"}
