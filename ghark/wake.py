"""Wake an idle Claude Code session when the reviewer asks for changes.

Three files per Claude Code session, in the state directory, carry the
protocol between the server and the waiter:

- ``<session>.alive`` is locked by the session's server for its whole life.
  The kernel releases the lock however the server dies.
- ``<session>.pending`` exists while unread review requests are queued, and
  names their PRs. The server raises it after a poll and lowers it on drain.
  It is level-triggered: a request that arrives while nothing waits is still
  flagged when a waiter starts.
- ``<session>.waiter`` is locked by the one waiter allowed per session.

The waiter runs as a Claude Code ``asyncRewake`` hook, armed at session start
and again after every ``pending_events`` call. It exits with status 2 when the
flag is raised under a live server, which wakes Claude even when the session is
idle. It trusts the flag only while the lease is held: a flag left by a dead
server names events that died with it. Hooks are short-lived processes, so this
module uses the standard library only.
"""

import contextlib
import fcntl
import json
import logging
import math
import os
import pathlib
import re
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, TextIO

from .models import PullRequestRef

__all__ = [
    "claude_session_id",
    "clear_flag",
    "ghark_state_dir",
    "hook_session_id",
    "hook_timeout",
    "pending_flag",
    "raise_flag",
    "server_lease",
    "sweep_state",
    "wait",
    "wake_after",
    "wake_hook",
    "wake_message",
]

logger = logging.getLogger("ghark")

#: The hook ``timeout``, in seconds, that the README's settings use.
DEFAULT_TIMEOUT = 86_400
#: Most of a hook's timeout that the waiter leaves for ``uv`` to start it.
MAX_MARGIN = 300
#: How often the waiter looks at the flag and the lease, in seconds.
POLL_SECONDS = 2
#: How long the waiter outlives a server that is gone, in seconds. It covers a
#: server restart; past it the session is presumed over.
SERVER_GRACE = 300
#: How long the server waits for an earlier server's lease, in seconds.
LEASE_WAIT = 10
#: Age, in seconds, after which an unlocked state file is presumed abandoned.
ABANDONED_AGE = 30 * 86_400

#: Exit status that makes an ``asyncRewake`` hook wake Claude.
WAKE = 2
#: Exit status of a waiter that has nothing to say; Claude Code stays quiet.
QUIET = 0

#: Session IDs name files, so only these characters are accepted.
_SESSION_ID = re.compile(r"[\w-]{1,128}")


# --- The waiter ---------------------------------------------------------------


def wake_hook() -> None:
    """Run the waiter as an ``asyncRewake`` hook and exit with its status.

    The optional argument is the hook's ``timeout`` in seconds. The waiter
    wakes Claude shortly before it, because a hook killed at its timeout is
    discarded silently and nothing would arm the next one.
    """
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        event = None
    session = hook_session_id(event, os.environ)
    if session is None:
        sys.exit(QUIET)
    message = wait(session, time.monotonic() + wake_after(hook_timeout(sys.argv)))
    if message is None:
        sys.exit(QUIET)
    print(message, file=sys.stderr)
    sys.exit(WAKE)


def wait(session: str, deadline: float) -> str | None:
    """Block until this session should wake; return what to tell Claude.

    Return ``None`` at once when another waiter holds this session, and
    ``None`` once no server has held the lease for ``SERVER_GRACE`` seconds.

    :param session: The Claude Code session ID.
    :param deadline: ``time.monotonic()`` value at which to wake regardless.
    """
    with _exclusive(_waiter_file(session)) as held:
        if not held:
            return None
        last_served = time.monotonic()
        while True:
            now = time.monotonic()
            requests = None
            if _leased(session):
                last_served = now
                requests = _flag_text(session)
            if now - last_served >= SERVER_GRACE:
                return None
            message = wake_message(requests, overdue=now >= deadline)
            if message is not None:
                return message
            time.sleep(POLL_SECONDS)


def wake_message(requests: str | None, overdue: bool) -> str | None:
    """What to tell Claude, or ``None`` to keep waiting.

    :param requests: The flag's PR list, or ``None`` when nothing is flagged.
    :param overdue: Whether the hook is about to reach its timeout.
    """
    if requests:
        return (
            f"ghark: the reviewer left review requests on {requests}. "
            "Call pending_events and address them."
        )
    if overdue:
        return (
            "ghark: the review watch is about to time out. Call pending_events "
            "to re-arm it, and address any review requests."
        )
    return None


def wake_after(timeout: float) -> float:
    """Seconds the waiter may wait under a hook ``timeout``.

    The margin is a tenth of the timeout, at most ``MAX_MARGIN``, so a short
    timeout still waits most of its length instead of waking at once.
    """
    return timeout - min(MAX_MARGIN, timeout / 10)


def hook_session_id(event: Any, environ: Mapping[str, str]) -> str | None:
    """The session ID from the hook input, else from the environment.

    :param event: The decoded hook input, which may be malformed.
    """
    session = event.get("session_id") if isinstance(event, dict) else None
    if isinstance(session, str) and _SESSION_ID.fullmatch(session):
        return session
    return claude_session_id(environ)


def hook_timeout(argv: Sequence[str]) -> float:
    """The hook ``timeout`` named by the first argument, else the default."""
    try:
        timeout = float(argv[1])
    except (IndexError, ValueError):
        return DEFAULT_TIMEOUT
    return timeout if math.isfinite(timeout) and timeout > 0 else DEFAULT_TIMEOUT


# --- The server's side --------------------------------------------------------


@contextlib.contextmanager
def server_lease(session: str) -> Iterator[None]:
    """Hold this session's lease while serving, after lowering a stale flag.

    The flag is lowered before the lease is taken, so a waiter never trusts a
    flag left by an earlier server. An earlier server that is still exiting
    gets ``LEASE_WAIT`` seconds to let go; past that the server runs without
    the lease, and waiters treat it as absent.
    """
    clear_flag(session)
    with _exclusive(_lease_file(session), wait=LEASE_WAIT) as held:
        if not held:
            logger.warning("Another ghark server holds session %s.", session)
        try:
            yield
        finally:
            clear_flag(session)


def raise_flag(session: str, prs: Iterable[PullRequestRef]) -> None:
    """Flag unread review requests on ``prs`` for this session's waiter.

    The write is atomic, so a waiter never reads a partial list.
    """
    flag = pending_flag(session)
    flag.parent.mkdir(parents=True, exist_ok=True)
    staging = flag.with_suffix(".tmp")
    try:
        staging.write_text(", ".join(map(str, prs)) + "\n")
        staging.replace(flag)
    finally:
        staging.unlink(missing_ok=True)


def clear_flag(session: str) -> None:
    """Lower this session's flag; ``pending_events`` has drained the queue."""
    pending_flag(session).unlink(missing_ok=True)


def sweep_state(now: float) -> None:
    """Delete state files abandoned by sessions that ended long ago.

    A file is abandoned when it is older than ``ABANDONED_AGE`` and, if it is a
    lock, unlocked. Files from the Monitor design, ``<pid>.wake``, go the same
    way.

    :param now: ``time.time()`` value to measure age from.
    """
    for path in ghark_state_dir().glob("*.*"):
        if path.suffix not in {".alive", ".waiter", ".pending", ".tmp", ".wake"}:
            continue
        with contextlib.suppress(OSError):
            if now - path.stat().st_mtime < ABANDONED_AGE:
                continue
            if path.suffix in {".alive", ".waiter"}:
                with _exclusive(path) as held:
                    if held:
                        path.unlink()
            else:
                path.unlink()


# --- Files and environment ----------------------------------------------------


def pending_flag(session: str) -> pathlib.Path:
    """File that exists while this session has unread review requests."""
    return ghark_state_dir() / f"{session}.pending"


def claude_session_id(environ: Mapping[str, str] = os.environ) -> str | None:
    """The Claude Code session ID that Claude Code sets in its children's env.

    An ID that could not safely name a file counts as absent.
    """
    session = environ.get("CLAUDE_CODE_SESSION_ID", "")
    return session if _SESSION_ID.fullmatch(session) else None


def ghark_state_dir() -> pathlib.Path:
    """Directory for the log and the per-session wake files."""
    state = os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local/state"
    return pathlib.Path(state) / "ghark"


def _lease_file(session: str) -> pathlib.Path:
    return ghark_state_dir() / f"{session}.alive"


def _waiter_file(session: str) -> pathlib.Path:
    return ghark_state_dir() / f"{session}.waiter"


def _flag_text(session: str) -> str | None:
    try:
        return pending_flag(session).read_text().strip()
    except FileNotFoundError:
        return None


def _leased(session: str) -> bool:
    """Whether a server holds this session's lease."""
    try:
        lease = _lease_file(session).open()
    except FileNotFoundError:
        return False
    with lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


@contextlib.contextmanager
def _exclusive(path: pathlib.Path, wait: float = 0) -> Iterator[bool]:
    """Hold an exclusive lock on ``path`` for the block; yield whether it was won.

    :param wait: Seconds to keep trying before giving up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        yield _lock(lock, time.monotonic() + wait)


def _lock(lock: TextIO, deadline: float) -> bool:
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        else:
            return True
