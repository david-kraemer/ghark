"""Mutable state owned by one stdio MCP session."""

import collections
import dataclasses

from .models import (
    ActivityEvent,
    Baseline,
    PendingBatch,
    PollResult,
    PullRequestRef,
    WatchState,
)

__all__ = ["SessionState"]


@dataclasses.dataclass(slots=True)
class SessionState:
    """Own subscriptions and the bounded unread-event queue for one session.

    This is an aggregate, not a mapping. Its methods control subscription
    transitions and keep the event queue consistent with those transitions.
    """

    max_pending: int = 1_000
    _watches: dict[PullRequestRef, WatchState] = dataclasses.field(default_factory=dict)
    _pending: collections.deque[ActivityEvent] = dataclasses.field(
        init=False, repr=False
    )
    _dropped: int = 0

    def __post_init__(self) -> None:
        if self.max_pending < 1:
            raise ValueError("max_pending must be at least 1.")
        self._pending = collections.deque(maxlen=self.max_pending)

    def subscribe(self, pr: PullRequestRef, baseline: Baseline) -> bool:
        if pr in self._watches:
            return False
        self._watches[pr] = baseline.watch(pr)
        return True

    def unsubscribe(self, pr: PullRequestRef) -> bool:
        return self._watches.pop(pr, None) is not None

    def watch(self, pr: PullRequestRef) -> WatchState | None:
        return self._watches.get(pr)

    def subscriptions(self) -> tuple[WatchState, ...]:
        return tuple(sorted(self._watches.values(), key=lambda watch: str(watch.pr)))

    def record_poll(
        self, previous: WatchState, result: PollResult
    ) -> tuple[ActivityEvent, ...]:
        pr = previous.pr
        # A poll may finish after the session has unsubscribed or replaced it.
        if self._watches.get(pr) is not previous:
            return ()

        events = tuple(previous.events_from(result))
        if result.snapshot.is_terminal:
            del self._watches[pr]
        else:
            self._watches[pr] = previous.after_poll(result)
        if events:
            self._enqueue(events)
        return events

    def flush_pending(self) -> PendingBatch:
        events = tuple(self._pending)
        self._pending.clear()
        dropped, self._dropped = self._dropped, 0
        return PendingBatch(events, dropped)

    def _enqueue(self, events: tuple[ActivityEvent, ...]) -> None:
        overflow = max(
            0,
            len(self._pending) + len(events) - self.max_pending,
        )
        self._dropped += overflow
        self._pending.extend(events)
