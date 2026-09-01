"""RequirementsSource protocol + ticket-ref detection (DESIGN.md §8)."""
from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from ..models import TicketContent

TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def detect_ticket_refs(*texts: str) -> list[str]:
    """Unique ticket keys (e.g. ENG-142) in order of first appearance."""
    seen: list[str] = []
    for text in texts:
        for m in TICKET_RE.finditer(text or ""):
            key = m.group(1)
            if key not in seen:
                seen.append(key)
    return seen


@runtime_checkable
class RequirementsSource(Protocol):
    name: str

    def configured(self) -> bool: ...

    async def fetch(self, key: str) -> TicketContent | None:
        """Fetch a ticket by key; None if not found in this source."""
        ...

    async def test_connection(self) -> dict[str, Any]: ...
