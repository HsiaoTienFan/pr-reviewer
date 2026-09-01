"""Path A backend: subprocess to `claude -p` (headless) with --json-schema.

Uses the existing claude.ai subscription login — credentials are never touched
here; auth defers entirely to the Claude Code CLI (DESIGN.md §10).
"""
from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from ..config import DATA_DIR
from .base import BackendCheck, BackendStatus, LLMError

CALL_TIMEOUT_S = 900
SKILL_TIMEOUT_S = 1800  # tool-using skill runs (/code-review) legitimately take longer

# All subprocess runs execute from an EMPTY sandbox dir — never from
# ~/.pr-reviewer itself, whose config.json holds tokens a tool-enabled,
# prompt-injected run could otherwise read.
SANDBOX_DIR = DATA_DIR / "sandbox"


def _exit_error(code: int, out: str, err: str) -> str:
    """Build a readable message for a non-zero `claude` exit.

    The CLI writes its JSON envelope to stdout even when it fails, so the
    useful text lives in envelope["result"] — well past any short truncation
    of the raw JSON (e.g. "Prompt is too long · the request is ~N tokens").
    """
    try:
        envelope = json.loads(out)
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if isinstance(result, str) and result.strip():
            return f"claude exited {code}: {result.strip()[:600]}"
    except json.JSONDecodeError:
        pass
    return f"claude exited {code}: {err.strip()[:600] or out.strip()[:600]}"


class ClaudeCLIBackend:
    name = "claude-cli"

    def __init__(self, model: str = "sonnet") -> None:
        self.model = model
        # per-instance accumulation; each job builds a fresh backend, so this
        # is that job's usage (subscription quota — cost is API-equivalent info)
        self.usage: dict[str, float] = {"calls": 0, "cost_usd": 0.0, "ms": 0.0}

    def _track(self, envelope: dict[str, Any]) -> None:
        self.usage["calls"] += 1
        self.usage["cost_usd"] += float(envelope.get("total_cost_usd") or 0)
        self.usage["ms"] += float(envelope.get("duration_ms") or 0)

    # ---------- status ----------

    async def _run(self, *args: str, stdin: str | None = None, timeout: float = 30) -> tuple[int, str, str]:
        path = shutil.which("claude")
        if not path:
            raise LLMError("claude CLI not found on PATH")
        proc = await asyncio.create_subprocess_exec(
            path, *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SANDBOX_DIR) if SANDBOX_DIR.exists() else None,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise LLMError(f"claude call timed out after {timeout:.0f}s")
        except asyncio.CancelledError:
            proc.kill()  # job cancelled — don't leave the subprocess burning quota
            raise
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def status(self, full: bool = False) -> BackendStatus:
        checks: list[BackendCheck] = []
        path = shutil.which("claude")
        if not path:
            return BackendStatus(
                ready=False,
                checks=[BackendCheck(ok=False, label="claude CLI not found on PATH")],
                summary="Not installed",
                fix="Install Claude Code: https://code.claude.com — then hit Re-check.",
            )
        try:
            _, ver_out, _ = await self._run("--version", timeout=20)
            version = ver_out.strip().split()[0] if ver_out.strip() else "?"
        except LLMError:
            version = "?"
        checks.append(BackendCheck(ok=True, label=f"CLI installed — v{version} at {path}"))

        try:
            _, auth_out, _ = await self._run("auth", "status", timeout=20)
            auth = json.loads(auth_out)
        except (LLMError, json.JSONDecodeError):
            auth = {}
        logged_in = bool(auth.get("loggedIn"))
        if logged_in:
            who = auth.get("email", "?")
            plan = auth.get("subscriptionType", "")
            plan_label = f" ({plan.capitalize()} plan)" if plan else ""
            checks.append(BackendCheck(ok=True, label=f"Logged in as {who}{plan_label}"))
        else:
            checks.append(BackendCheck(ok=False, label="Not logged in"))
            return BackendStatus(
                ready=False, checks=checks, summary="Not logged in",
                fix="Run `claude login` in a terminal, then hit Re-check.",
            )

        if full:
            try:
                obj = await self.structured(
                    'Reply with the JSON object {"ok": true} and nothing else.',
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                )
                checks.append(BackendCheck(ok=bool(obj.get("ok")), label="Schema-constrained output check passed"))
            except LLMError as e:
                checks.append(BackendCheck(ok=False, label=f"Schema check failed: {e}"))
                return BackendStatus(
                    ready=False, checks=checks, summary="Schema check failed",
                    fix="Headless call failed — try `claude -p 'hi'` in a terminal to debug.",
                )

        return BackendStatus(ready=True, checks=checks, summary="Ready")

    # ---------- structured calls ----------

    async def text(self, prompt: str, allowed_tools: list[str] | None = None) -> str:
        """Plain headless call returning the final message text. Needed for skill
        slash-commands (e.g. /code-review), which end with zero turns when
        combined with --json-schema — structure the text in a second call."""
        SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
        args = ["-p", "--output-format", "json", "--model", self.model, "--no-session-persistence"]
        if allowed_tools:
            args += ["--allowedTools", ",".join(allowed_tools)]
        code, out, err = await self._run(*args, stdin=prompt, timeout=SKILL_TIMEOUT_S)
        if code != 0:
            raise LLMError(_exit_error(code, out, err))
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            raise LLMError(f"claude returned non-JSON output: {out.strip()[:400]}")
        if isinstance(envelope, dict) and envelope.get("is_error"):
            raise LLMError(f"claude reported an error: {str(envelope.get('result'))[:400]}")
        if isinstance(envelope, dict):
            self._track(envelope)
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(result, str) or not result.strip():
            raise LLMError("claude returned an empty result")
        return result

    async def structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        allowed_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
        args = [
            "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--model", self.model,
            "--no-session-persistence",
        ]
        if allowed_tools:
            # e.g. /code-review needs gh/git + file reads to inspect the PR
            args += ["--allowedTools", ",".join(allowed_tools)]
        code, out, err = await self._run(*args, stdin=prompt, timeout=CALL_TIMEOUT_S)
        if code != 0:
            raise LLMError(_exit_error(code, out, err))
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            raise LLMError(f"claude returned non-JSON output: {out.strip()[:400]}")
        if isinstance(envelope, dict) and envelope.get("is_error"):
            raise LLMError(f"claude reported an error: {str(envelope.get('result'))[:400]}")
        if isinstance(envelope, dict):
            self._track(envelope)

        if isinstance(envelope, dict):
            structured = envelope.get("structured_output")
            if isinstance(structured, dict):
                return structured
            result = envelope.get("result")
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                text = result.strip()
                if text.startswith("```"):
                    text = text.strip("`")
                    text = text[text.find("{"):text.rfind("}") + 1]
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
        raise LLMError(f"could not extract structured output: {out.strip()[:400]}")
