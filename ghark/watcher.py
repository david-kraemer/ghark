"""Poll subscriptions and pass new activity to the session state and channel."""

import asyncio
import dataclasses
import logging

from .channel import ClaudeChannel
from .github import GitHubClient
from .models import ActivityEvent
from .session import SessionState
from .wake import raise_flag

__all__ = ["PullRequestPoller"]

logger = logging.getLogger("ghark")


@dataclasses.dataclass(slots=True)
class PullRequestPoller:
    """Coordinate API reads without owning subscription or queue state."""

    github: GitHubClient
    state: SessionState
    channel: ClaudeChannel
    #: Claude Code session whose waiter to wake; ``None`` outside Claude Code.
    claude_session: str | None
    interval: float
    batch_window: float

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.poll()

    async def poll(self) -> None:
        """Poll every watch once, then deliver what it found.

        Nothing here may raise: an exception ends the server process.
        """
        batch: list[ActivityEvent] = []
        for watch in self.state.subscriptions():
            try:
                result = await self.github.observation(watch)
                batch.extend(self.state.record_poll(watch, result))
            except Exception:
                logger.exception("Poll failed for %s", watch.pr)
        if batch and self.batch_window:
            await asyncio.sleep(self.batch_window)
        self._flag_review_requests()
        if batch:
            await self.channel.send(tuple(batch))

    def _flag_review_requests(self) -> None:
        """Flag the PRs with unread review requests for the session's waiter.

        Runs after every poll, not only one that found events, so a failed
        write is retried. It follows the batch window: a drain during it leaves
        nothing to flag.
        """
        prs = self.state.review_requests()
        if self.claude_session is None or not prs:
            return
        try:
            raise_flag(self.claude_session, prs)
        except OSError:
            logger.exception(
                "Could not raise the review flag for %s", self.claude_session
            )
