"""Local config + review persistence. Tokens never leave this machine."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .models import Review

DATA_DIR = Path.home() / ".pr-reviewer"
CONFIG_PATH = DATA_DIR / "config.json"
REVIEWS_DIR = DATA_DIR / "reviews"

_lock = threading.Lock()

DEFAULT_CONFIG: dict[str, Any] = {
    "github": {"token": "", "repos": []},
    "bitbucket": {"username": "", "app_password": "", "repos": []},
    "linear": {"api_key": ""},
    "jira": {"site_url": "", "email": "", "api_token": ""},
    # skills_dir: where to discover user skills ("" = ~/.claude/skills).
    # review_skill: skill slash-command for the code-review pass ("" = built-in /code-review).
    "claude": {"model": "sonnet", "skills_dir": "", "review_skill": ""},
    "pins": [],  # individually added PRs: "provider:owner/repo:number"
}


def change_pin(rid: str, add: bool) -> list[str]:
    cfg = load_config()
    pins: list[str] = cfg.get("pins", [])
    if add and rid not in pins:
        pins.append(rid)
    if not add and rid in pins:
        pins.remove(rid)
    cfg["pins"] = pins
    save_config(cfg)
    return pins


def load_config() -> dict[str, Any]:
    with _lock:
        if not CONFIG_PATH.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG))
        cfg = json.loads(CONFIG_PATH.read_text())
    for key, val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, val)
        if isinstance(val, dict):
            for k2, v2 in val.items():
                cfg[key].setdefault(k2, v2)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def update_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    cfg.setdefault(section, {})
    cfg[section].update(values)
    save_config(cfg)
    return cfg


def _review_path(rid: str) -> Path:
    safe = rid.replace("/", "__").replace(":", "--")
    return REVIEWS_DIR / f"{safe}.json"


def save_review(review: Review) -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = _review_path(review.id)
    tmp = path.with_suffix(".tmp")  # atomic: never leave a half-written review
    tmp.write_text(review.model_dump_json(indent=1))
    tmp.replace(path)


def delete_review(rid: str) -> bool:
    path = _review_path(rid)
    if path.exists():
        path.unlink()
        return True
    return False


def load_review(rid: str) -> Review | None:
    path = _review_path(rid)
    if not path.exists():
        return None
    return Review.model_validate_json(path.read_text())


def all_reviews() -> list[Review]:
    if not REVIEWS_DIR.exists():
        return []
    out = []
    for p in sorted(REVIEWS_DIR.glob("*.json")):
        try:
            out.append(Review.model_validate_json(p.read_text()))
        except Exception:
            continue
    return out
