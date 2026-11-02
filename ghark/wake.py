"""Wake an idle Claude Code session when the reviewer asks for changes.

The server raises a per-session flag file while unread review requests are
queued, and ``pending_events`` lowers it. The flag is level-triggered: a
request that arrives while nothing waits is still flagged when a waiter starts.

The waiter runs as a Claude Code ``asyncRewake`` hook, armed at session start
and again after every ``pending_events`` call. It blocks until the flag is
raised, then exits with status 2, which wakes Claude even when the session is
idle. Hooks are short-lived processes, so this module uses the standard library
only.
"""

import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time

__all__ = [
    "clear_flag",
    "pending_flag",
    "raise_flag",
    "session_id",
    "state_dir",
    "wait",
    "wake_hook",
]

#: The hook's ``timeout`` in the README's settings, in seconds.
DEFAULT_TIMEOUT = 86_400
#: How long before the hook's timeout the waiter wakes Claude to be re-armed.
TIMEOUT_MARGIN = 60
POLL_SECONDS = 2
#: Session IDs name files, so only these characters are accepted.
_SESSION_ID = re.compile(r"[\w-]{1,128}")

#: Exit status that makes an ``asyncRewake`` hook wake Claude.
WAKE = 2
#: Exit status of a waiter that has nothing to say; Claude Code stays quiet.
QUIET = 0


def wake_hook() -> None:
    """Run the waiter as an ``asyncRewake`` hook and exit with its status.

    The optional argument is the hook's ``timeout`` in seconds. The waiter
    wakes Claude shortly before it, because a hook killed at its timeout is
    discarded silently and nothing would arm the next one.
    """
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        event = {}
    sid = event.get("session_id") if isinstance(event, dict) else None
    sid = sid if isinstance(sid, str) and sid else session_id()
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TIMEOUT
    if sid is None or not _SESSION_ID.fullmatch(sid):
        sys.exit(QUIET)
    message = wait(sid, deadline=time.monotonic() + timeout - TIMEOUT_MARGIN)
    if message is None:
        sys.exit(QUIET)
    print(message, file=sys.stderr)
    sys.exit(WAKE)


def wait(sid: str, deadline: float) -> str | None:
    """Block until this session should wake; return what to tell Claude.

    Return ``None`` without waiting when another waiter already holds this
    session, and ``None`` when the Claude Code process that started the waiter
    exits.

    :param sid: The Claude Code session ID.
    :param deadline: ``time.monotonic()`` value at which to wake regardless.
    """
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{sid}.waiter").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        ancestors = _ancestors()
        flag = pending_flag(sid)
        while True:
            try:
                requests = flag.read_text().strip()
            except FileNotFoundError:
                pass
            else:
                return (
                    f"ghark: the reviewer left review requests on {requests}. "
                    "Call pending_events and address them."
                )
            if not all(map(_alive, ancestors)):
                return None
            if time.monotonic() >= deadline:
                return (
                    "ghark: the review watch is about to time out. Call "
                    "pending_events to re-arm it, and address any review requests."
                )
            time.sleep(POLL_SECONDS)


def raise_flag(sid: str, prs: list[str]) -> None:
    """Flag unread review requests on ``prs`` for this session's waiter.

    The write is atomic, so a waiter never reads a partial list.
    """
    flag = pending_flag(sid)
    flag.parent.mkdir(parents=True, exist_ok=True)
    staging = flag.with_suffix(".tmp")
    staging.write_text(", ".join(prs) + "\n")
    staging.replace(flag)


def clear_flag(sid: str) -> None:
    pending_flag(sid).unlink(missing_ok=True)


def pending_flag(sid: str) -> pathlib.Path:
    """File that exists exactly while this session has unread review requests."""
    return state_dir() / f"{sid}.pending"


def session_id() -> str | None:
    """The Claude Code session ID that Claude Code sets in its children's env."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def state_dir() -> pathlib.Path:
    state = os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local/state"
    return pathlib.Path(state) / "ghark"


def _ancestors() -> list[int]:
    """Process IDs from the parent up to, but not including, init.

    Claude Code starts the hook through a shell and ``uv``. When Claude Code
    exits, some ancestor dies, even if the waiter's own parent survives.
    """
    pids: list[int] = []
    pid = os.getppid()
    while pid > 1 and len(pids) < 32:
        pids.append(pid)
        pid = _parent(pid)
    return pids


def _parent(pid: int) -> int:
    result = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 1


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True
