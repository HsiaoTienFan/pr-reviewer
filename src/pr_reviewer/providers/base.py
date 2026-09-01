"""PRProvider protocol (DESIGN.md §8) — everything downstream sees PRContent only."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models import PRContent, PRInfo


@runtime_checkable
class PRProvider(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    def parse_url(self, url: str) -> tuple[str, int] | None:
        """URL → (owner/repo, pr_number) or None."""
        ...

    def pr_url(self, repo: str, number: int) -> str:
        """Canonical web URL for a PR — constructible without any API call."""
        ...

    def configured(self) -> bool: ...

    async def fetch(self, repo: str, number: int) -> PRContent: ...

    async def list_open_prs(self, repo: str) -> list[PRInfo]: ...

    async def test_connection(self) -> dict[str, Any]:
        """→ {"ok": bool, "message": str}"""
        ...
