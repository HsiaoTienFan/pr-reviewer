"""LLM backend factory — Path A (claude CLI subprocess); SDK backend is a drop-in swap."""
from __future__ import annotations

from typing import Any

from .base import BackendStatus, LLMBackend, LLMError
from .claude_cli import ClaudeCLIBackend

__all__ = ["build_backend", "BackendStatus", "LLMBackend", "LLMError"]


def build_backend(cfg: dict[str, Any]) -> LLMBackend:
    model = (cfg.get("claude") or {}).get("model", "sonnet")
    return ClaudeCLIBackend(model=model)
