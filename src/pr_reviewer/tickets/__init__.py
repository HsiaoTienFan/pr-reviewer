"""Ticket source registry — PR description is always-on and needs no source here."""
from __future__ import annotations

import os
from typing import Any

from .base import RequirementsSource, detect_ticket_refs
from .jira import JiraSource
from .linear import LinearSource

__all__ = ["build_sources", "detect_ticket_refs", "RequirementsSource"]


def build_sources(cfg: dict[str, Any]) -> dict[str, RequirementsSource]:
    lin = cfg.get("linear", {})
    jira = cfg.get("jira", {})
    api_key = lin.get("api_key", "")
    oauth_token = lin.get("oauth_access_token", "")
    # LINEAR_API_KEY matches project-flow-map's env convention, so a key can be
    # shared across tools without duplicating it into this app's config.json.
    if not api_key and not oauth_token:
        api_key = os.environ.get("LINEAR_API_KEY", "")
    return {
        "linear": LinearSource(api_key=api_key, oauth_token=oauth_token),
        "jira": JiraSource(
            site_url=jira.get("site_url", ""),
            email=jira.get("email", ""),
            api_token=jira.get("api_token", ""),
        ),
    }
