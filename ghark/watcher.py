"""Poll subscriptions and pass new activity to the session state and channel."""

import asyncio
import dataclasses
import logging

from .channel import ClaudeChannel
from .github import GitHubClient
from .models import ActivityEvent
from .session import SessionState

__all__ = ["PullRequestPoller"]

logger = logging.getLogger("ghark")


@dataclasses.dataclass(slots=True)
class PullRequestPoller:
    """Coordinate API reads without owning subscription or queue state."""

    github: GitHubClient
    state: SessionState
    channel: ClaudeChannel
    interval: float
    batch_window: float

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            batch: list[ActivityEvent] = []
            for watch in self.state.subscriptions():
                try:
                    result = await self.github.observation(watch)
                    batch.extend(self.state.record_poll(watch, result))
                except Exception:
                    logger.exception("Poll failed for %s", watch.pr)
            if batch:
                if self.batch_window:
                    await asyncio.sleep(self.batch_window)
                await self.channel.send(tuple(batch))
