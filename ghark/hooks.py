"""Claude Code hook commands.

Hooks run as short-lived processes on every matching tool call, so this module
uses the standard library only.
"""

import json
import re
import sys
from typing import Any

__all__ = ["subscribe_context", "subscribe_hook_main"]

SUBSCRIBE_TOOL = "mcp__ghark__subscribe"

_PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def subscribe_hook_main() -> None:
    """Run the PostToolUse hook that reminds Claude to watch a new PR.

    Read the hook event from stdin. Print a ``hookSpecificOutput`` object when a
    Bash call created a pull request; print nothing otherwise. Malformed input
    is ignored so a hook failure never blocks the session.
    """
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    context = subscribe_context(event) if isinstance(event, dict) else None
    if context is None:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }
    json.dump(output, sys.stdout)


def subscribe_context(event: dict[str, Any]) -> str | None:
    """Context for Claude after a successful ``gh pr create``, or ``None``.

    :param event: The PostToolUse hook input.
    """
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not _PR_CREATE.search(command):
        return None
    response = event.get("tool_response")
    if not isinstance(response, dict) or _failed(response):
        return None

    url = _created_pr_url(response)
    if url is None:
        return (
            "A pull request was just created with `gh pr create`. Call the "
            f"`{SUBSCRIBE_TOOL}` tool with its URL so ghark watches it for "
            "review activity, then start the Monitor the result names, if any."
        )
    return (
        f"Pull request {url} was just created. Call the `{SUBSCRIBE_TOOL}` tool "
        f'with `pr` set to "{url}" so ghark watches it for review activity, '
        "then start the Monitor the result names, if any."
    )


def _failed(response: dict[str, Any]) -> bool:
    """Whether a Bash tool response reports an unsuccessful run.

    Claude Code sends a non-zero exit to ``PostToolUseFailure`` instead, and
    the Bash response carries no exit code. An interrupted run is the one
    failure left to detect here.
    """
    return response.get("interrupted") is True


def _created_pr_url(response: dict[str, Any]) -> str | None:
    """The last PR URL ``gh pr create`` printed on stdout, if any."""
    stdout = response.get("stdout")
    if not isinstance(stdout, str):
        return None
    urls = _PR_URL.findall(stdout)
    return urls[-1] if urls else None
