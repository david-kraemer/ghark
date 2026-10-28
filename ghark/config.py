"""Immutable process settings."""

import dataclasses
import math
import os

from .github import github_token

__all__ = ["Settings", "ghark_poll_seconds", "ghark_batch_seconds", "settings_from_env"]


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    github_token: str = dataclasses.field(repr=False)
    poll_interval: float = 30
    batch_window: float = 5
    max_pending_events: int = 1_000


def settings_from_env() -> Settings:
    return Settings(
        github_token=github_token(),
        poll_interval=ghark_poll_seconds(),
        batch_window=ghark_batch_seconds(),
    )


def ghark_poll_seconds(default: float = 30, minimum: float = 5) -> float:
    seconds = _environ_get_float("GHARK_POLL_SECONDS", default)
    return _seconds(seconds, default, minimum)


def ghark_batch_seconds(default: float = 5, minimum: float = 0) -> float:
    seconds = _environ_get_float("GHARK_BATCH_SECONDS", default)
    return _seconds(seconds, default, minimum)


def _environ_get_float(key: str, default: float | None = None) -> float:
    value = os.environ[key] if default is None else os.environ.get(key, default)
    return float(value)


def _seconds(value: float, default: float, minimum: float) -> float:
    return max(minimum, value) if math.isfinite(value) else default
