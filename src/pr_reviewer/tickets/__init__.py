"""Ticket source registry — PR description is always-on and needs no source here."""
from __future__ import annotations

from typing import Any

from .base import RequirementsSource, detect_ticket_refs
from .jira import JiraSource
from .linear import LinearSource

__all__ = ["build_sources", "detect_ticket_refs", "RequirementsSource"]


def build_sources(cfg: dict[str, Any]) -> dict[str, RequirementsSource]:
    lin = cfg.get("linear", {})
    jira = cfg.get("jira", {})
    return {
        "linear": LinearSource(api_key=lin.get("api_key", "")),
        "jira": JiraSource(
            site_url=jira.get("site_url", ""),
            email=jira.get("email", ""),
            api_token=jira.get("api_token", ""),
        ),
    }
