"""LLMBackend interface (DESIGN.md §10) — uniform ready/not-ready + structured calls."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class BackendCheck(BaseModel):
    ok: bool
    label: str


class BackendStatus(BaseModel):
    ready: bool
    checks: list[BackendCheck] = Field(default_factory=list)
    summary: str = ""
    fix: str = ""  # how to fix, when not ready


class LLMError(Exception):
    pass


@runtime_checkable
class LLMBackend(Protocol):
    name: str

    async def status(self, full: bool = False) -> BackendStatus: ...

    async def text(self, prompt: str, allowed_tools: list[str] | None = None) -> str:
        """Run one call returning the final message text."""
        ...

    async def structured(
        self, prompt: str, schema: dict[str, Any], allowed_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one schema-constrained call; returns the validated object."""
        ...
