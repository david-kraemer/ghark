"""Domain records and their pure behavior."""

import dataclasses
import datetime
import enum
import itertools
import re
from collections.abc import Iterator
from typing import Any, Self

__all__ = [
    "ActivityEvent",
    "ActivityKind",
    "Baseline",
    "ChannelMessage",
    "CheckState",
    "MergeState",
    "Mergeability",
    "PendingBatch",
    "PollResult",
    "PullRequestRef",
    "PullRequestSnapshot",
    "PullRequestStatus",
    "ReviewComment",
    "SnapshotChange",
    "TimelineEntry",
    "WatchState",
]


class ActivityKind(enum.StrEnum):
    COMMIT = "commit"
    REVIEW = "review"
    COMMENT = "comment"
    THREAD_COMMENT = "thread_comment"
    CHECKS = "checks"
    MERGEABILITY = "mergeability"
    CLOSED = "closed"
    MERGED = "merged"
    REOPENED = "reopened"


class PullRequestStatus(enum.StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"
    UNKNOWN = "UNKNOWN"


class Mergeability(enum.StrEnum):
    MERGEABLE = "MERGEABLE"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class MergeState(enum.StrEnum):
    BEHIND = "BEHIND"
    BLOCKED = "BLOCKED"
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    DRAFT = "DRAFT"
    HAS_HOOKS = "HAS_HOOKS"
    UNKNOWN = "UNKNOWN"
    UNSTABLE = "UNSTABLE"


class CheckState(enum.StrEnum):
    ERROR = "ERROR"
    EXPECTED = "EXPECTED"
    FAILURE = "FAILURE"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    UNKNOWN = "UNKNOWN"


@dataclasses.dataclass(frozen=True, slots=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    def __post_init__(self) -> None:
        if not _SLUG.fullmatch(self.owner) or not _SLUG.fullmatch(self.repo):
            raise ValueError("PR owner and repository must be valid GitHub names.")
        if type(self.number) is not int or self.number < 1:
            raise ValueError("PR number must be positive.")

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @classmethod
    def from_input(cls, value: str) -> Self:
        value = value.strip()
        if match := _PR_URL.fullmatch(value):
            owner, repo, number = match.groups()
        elif match := _PR_REF.fullmatch(value):
            owner, repo, number = match.groups()
        else:
            raise ValueError("Use a GitHub PR URL or OWNER/REPO#NUMBER.")
        return cls(owner, repo, int(number))


@dataclasses.dataclass(frozen=True, slots=True)
class TimelineEntry:
    kind: ActivityKind
    summary: str
    actor: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActivityKind):
            raise TypeError("TimelineEntry.kind must be an ActivityKind.")


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotChange:
    kind: ActivityKind
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActivityKind):
            raise TypeError("SnapshotChange.kind must be an ActivityKind.")


@dataclasses.dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    status: PullRequestStatus
    mergeable: Mergeability
    merge_state: MergeState
    head_oid: str
    checks: CheckState
    title: str
    url: str

    def __post_init__(self) -> None:
        fields = (
            (self.status, PullRequestStatus),
            (self.mergeable, Mergeability),
            (self.merge_state, MergeState),
            (self.checks, CheckState),
        )
        if any(not isinstance(value, kind) for value, kind in fields):
            raise TypeError("Snapshot status fields must use their enum types.")

    @property
    def is_terminal(self) -> bool:
        return self.status in {PullRequestStatus.CLOSED, PullRequestStatus.MERGED}

    def changes_from(
        self,
        previous: Self,
        timeline: tuple[TimelineEntry, ...],
    ) -> tuple[SnapshotChange, ...]:
        changes: list[SnapshotChange] = []
        has_commit = any(entry.kind is ActivityKind.COMMIT for entry in timeline)
        if self.head_oid and previous.head_oid != self.head_oid and not has_commit:
            changes.append(
                SnapshotChange(
                    ActivityKind.COMMIT,
                    "Pull request head changed; commit details unavailable",
                )
            )
        if previous.checks != self.checks and self.checks is not CheckState.UNKNOWN:
            changes.append(
                SnapshotChange(
                    ActivityKind.CHECKS,
                    f"Checks: {previous.checks} → {self.checks}",
                )
            )
        if (previous.mergeable, previous.merge_state) != (
            self.mergeable,
            self.merge_state,
        ):
            changes.append(
                SnapshotChange(
                    ActivityKind.MERGEABILITY,
                    f"Merge state: {self.mergeable}, {self.merge_state}",
                )
            )

        transition = {
            PullRequestStatus.CLOSED: (ActivityKind.CLOSED, "closed"),
            PullRequestStatus.MERGED: (ActivityKind.MERGED, "merged"),
        }.get(self.status)
        if (
            self.status is PullRequestStatus.OPEN
            and previous.status is PullRequestStatus.CLOSED
        ):
            transition = (ActivityKind.REOPENED, "reopened")
        if (
            transition is not None
            and previous.status is not self.status
            and not any(entry.kind is transition[0] for entry in timeline)
        ):
            kind, verb = transition
            changes.append(SnapshotChange(kind, f"Pull request {verb}"))
        return tuple(changes)


@dataclasses.dataclass(frozen=True, slots=True)
class ReviewComment:
    comment_id: int
    created_at: datetime.datetime
    body: str
    actor: str

    def __post_init__(self) -> None:
        if self.comment_id < 1:
            raise ValueError("Review comment ID must be positive.")
        if self.created_at.utcoffset() is None:
            raise ValueError("Review comment time must include a timezone.")


@dataclasses.dataclass(frozen=True, slots=True)
class ActivityEvent:
    pr: PullRequestRef
    snapshot: PullRequestSnapshot
    kind: ActivityKind
    raw_summary: str
    actor: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActivityKind):
            raise TypeError("ActivityEvent.kind must be an ActivityKind.")

    @property
    def summary(self) -> str:
        summary = " ".join(self.raw_summary.split())
        return summary if len(summary) <= 180 else summary[:179] + "…"

    @classmethod
    def from_timeline(
        cls,
        pr: PullRequestRef,
        snapshot: PullRequestSnapshot,
        entry: TimelineEntry,
    ) -> Self:
        return cls(pr, snapshot, entry.kind, entry.summary, entry.actor)

    @classmethod
    def from_change(
        cls,
        pr: PullRequestRef,
        snapshot: PullRequestSnapshot,
        change: SnapshotChange,
    ) -> Self:
        return cls(pr, snapshot, change.kind, change.summary)

    @classmethod
    def from_review_comment(
        cls,
        pr: PullRequestRef,
        snapshot: PullRequestSnapshot,
        comment: ReviewComment,
    ) -> Self:
        return cls(
            pr,
            snapshot,
            ActivityKind.THREAD_COMMENT,
            comment.body,
            comment.actor,
        )

    @property
    def header(self) -> str:
        return f"{self.pr}: {self.snapshot.title}"

    @property
    def title(self) -> str:
        return self.snapshot.title

    @property
    def url(self) -> str:
        return self.snapshot.url

    @property
    def detail(self) -> str:
        detail = f"{self.kind}: {self.summary}"
        return f"{detail} (by {self.actor})" if self.actor else detail

    def as_dict(self) -> dict[str, str]:
        return {
            "pr": str(self.pr),
            "url": self.snapshot.url,
            "title": self.snapshot.title,
            "kind": self.kind,
            "summary": self.summary,
            "actor": self.actor,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class PollResult:
    snapshot: PullRequestSnapshot
    timeline_cursor: str | None
    review_cursor: datetime.datetime
    timeline_entries: tuple[TimelineEntry, ...]
    review_comments: tuple[ReviewComment, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class WatchState:
    pr: PullRequestRef
    snapshot: PullRequestSnapshot
    timeline_cursor: str | None
    review_cursor: datetime.datetime
    subscribed_at: datetime.datetime
    seen_comment_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        times = (self.review_cursor, self.subscribed_at)
        if any(value.utcoffset() is None for value in times):
            raise ValueError("Watch times must include a timezone.")
        if self.review_cursor < self.subscribed_at:
            raise ValueError("Review cursor cannot predate the subscription.")

    def events_from(self, result: PollResult) -> Iterator[ActivityEvent]:
        timeline = result.timeline_entries
        if result.snapshot.status is PullRequestStatus.MERGED:
            timeline = tuple(
                entry for entry in timeline if entry.kind is not ActivityKind.CLOSED
            )

        entries = itertools.chain(
            (
                ActivityEvent.from_timeline(self.pr, result.snapshot, entry)
                for entry in timeline
            ),
            (
                ActivityEvent.from_change(self.pr, result.snapshot, change)
                for change in result.snapshot.changes_from(self.snapshot, timeline)
            ),
            (
                ActivityEvent.from_review_comment(self.pr, result.snapshot, comment)
                for comment in result.review_comments
                if comment.comment_id not in self.seen_comment_ids
                and comment.created_at >= self.subscribed_at
            ),
        )
        return entries

    def after_poll(self, result: PollResult) -> Self:
        return dataclasses.replace(
            self,
            snapshot=result.snapshot,
            timeline_cursor=result.timeline_cursor,
            review_cursor=result.review_cursor,
            seen_comment_ids=self.seen_comment_ids.union(
                comment.comment_id for comment in result.review_comments
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "pr": str(self.pr),
            "title": self.snapshot.title,
            "url": self.snapshot.url,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class Baseline:
    snapshot: PullRequestSnapshot
    timeline_cursor: str | None
    review_cursor: datetime.datetime

    def watch(self, pr: PullRequestRef) -> WatchState:
        return WatchState(
            pr=pr,
            snapshot=self.snapshot,
            timeline_cursor=self.timeline_cursor,
            review_cursor=self.review_cursor,
            subscribed_at=self.review_cursor,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class PendingBatch:
    events: tuple[ActivityEvent, ...]
    dropped_events: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": [event.as_dict() for event in self.events],
            "dropped_events": self.dropped_events,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ChannelMessage:
    pr: PullRequestRef
    events: tuple[ActivityEvent, ...]

    def __post_init__(self) -> None:
        if not self.events or any(event.pr != self.pr for event in self.events):
            raise ValueError("A channel message needs events from one PR.")

    @classmethod
    def from_events(cls, events: tuple[ActivityEvent, ...]) -> tuple[Self, ...]:
        grouped: dict[PullRequestRef, list[ActivityEvent]] = {}
        for event in events:
            grouped.setdefault(event.pr, []).append(event)
        return tuple(cls(pr, tuple(group)) for pr, group in grouped.items())

    @property
    def content(self) -> str:
        first = self.events[0]
        return (
            f"{first.header}\n"
            f"{'; '.join(event.detail for event in self.events)}\n"
            f"{first.url}\n"
            "Notify the user and wait for direction before acting."
        )

    @property
    def repo(self) -> str:
        return f"{self.pr.owner}/{self.pr.repo}"

    @property
    def number(self) -> str:
        return str(self.pr.number)

    @property
    def event_count(self) -> int:
        return len(self.events)

    def as_dict(self) -> dict[str, str | dict[str, str]]:
        return {
            "content": self.content,
            "meta": {
                "repo": self.repo,
                "number": self.number,
                "event_count": str(self.event_count),
            },
        }


_SLUG = re.compile(r"[A-Za-z0-9_.-]+")
_PR_URL = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9]\d*)/?(?:\?.*)?$"
)
_PR_REF = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9]\d*)$")
