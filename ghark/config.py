"""Immutable process settings."""

import dataclasses
import math
import os
import pathlib

from .github import github_token
from .wake import ghark_state_dir

__all__ = [
    "Settings",
    "ghark_batch_seconds",
    "ghark_discover",
    "ghark_log_file",
    "ghark_poll_seconds",
    "ghark_project_dir",
    "ghark_reviewers",
    "settings_from_env",
]


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    github_token: str = dataclasses.field(repr=False)
    poll_interval: float = 30
    batch_window: float = 5
    max_pending_events: int = 1_000
    reviewers: frozenset[str] = frozenset()
    discover: bool = True
    project_dir: pathlib.Path = pathlib.Path()


def settings_from_env() -> Settings:
    return Settings(
        github_token=github_token(),
        poll_interval=ghark_poll_seconds(),
        batch_window=ghark_batch_seconds(),
        reviewers=ghark_reviewers(),
        discover=ghark_discover(),
        project_dir=ghark_project_dir(),
    )


def ghark_poll_seconds(default: float = 30, minimum: float = 5) -> float:
    seconds = _environ_get_float("GHARK_POLL_SECONDS", default)
    return _seconds(seconds, default, minimum)


def ghark_batch_seconds(default: float = 5, minimum: float = 0) -> float:
    seconds = _environ_get_float("GHARK_BATCH_SECONDS", default)
    return _seconds(seconds, default, minimum)


def ghark_reviewers() -> frozenset[str]:
    """Logins in ``GHARK_REVIEWERS``; empty means the token's owner."""
    logins = os.environ.get("GHARK_REVIEWERS", "").split(",")
    return frozenset(login.strip().lower() for login in logins if login.strip())


def ghark_discover() -> bool:
    """Whether to subscribe at startup to the PRs this session owns.

    ``GHARK_DISCOVER`` set to ``0``, ``false``, ``no`` or ``off`` disables it.
    """
    value = os.environ.get("GHARK_DISCOVER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def ghark_project_dir() -> pathlib.Path:
    """The client's project directory, where discovery looks for a checkout.

    The working directory is a poor default: ``uv run --directory`` moves it to
    the ghark checkout. Claude Code sets ``CLAUDE_PROJECT_DIR`` for stdio
    servers; ``PWD`` is inherited from the client and survives uv's chdir.
    """
    path = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD")
    return pathlib.Path(path) if path else pathlib.Path.cwd()


def ghark_log_file() -> pathlib.Path:
    """Log path; the MCP client may not keep the server's stderr."""
    if path := os.environ.get("GHARK_LOG_FILE"):
        return pathlib.Path(path).expanduser()
    return ghark_state_dir() / "ghark.log"


def _environ_get_float(key: str, default: float | None = None) -> float:
    value = os.environ[key] if default is None else os.environ.get(key, default)
    return float(value)


def _seconds(value: float, default: float, minimum: float) -> float:
    return max(minimum, value) if math.isfinite(value) else default
