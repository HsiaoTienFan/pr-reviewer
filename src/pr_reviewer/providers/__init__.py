"""Provider registry — selected from config + pasted URLs."""
from __future__ import annotations

from typing import Any

from .base import PRProvider
from .bitbucket import BitbucketProvider
from .github import GitHubProvider, gh_cli_token


def build_providers(cfg: dict[str, Any]) -> dict[str, PRProvider]:
    gh = cfg.get("github", {})
    bb = cfg.get("bitbucket", {})
    gh_token = gh.get("token", "")
    via_gh_cli = False
    if not gh_token:
        gh_token = gh_cli_token()
        via_gh_cli = bool(gh_token)
    return {
        "github": GitHubProvider(token=gh_token, via_gh_cli=via_gh_cli),
        "bitbucket": BitbucketProvider(
            username=bb.get("username", ""),
            app_password=bb.get("app_password", ""),
        ),
    }


def provider_for_url(providers: dict[str, PRProvider], url: str) -> tuple[PRProvider, str, int] | None:
    for p in providers.values():
        if p.matches(url):
            parsed = p.parse_url(url)
            if parsed:
                return p, parsed[0], parsed[1]
    return None
