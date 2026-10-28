"""Poll subscriptions and pass new activity to the session state and channel."""

import asyncio
import dataclasses
import logging
import pathlib

from .channel import ClaudeChannel
from .github import GitHubClient
from .models import ActivityEvent, ChannelMessage
from .session import SessionState

__all__ = ["PullRequestPoller"]

logger = logging.getLogger("ghark")


@dataclasses.dataclass(slots=True)
class PullRequestPoller:
    """Coordinate API reads without owning subscription or queue state."""

    github: GitHubClient
    state: SessionState
    channel: ClaudeChannel
    wake_file: pathlib.Path
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
        if batch:
            if self.batch_window:
                await asyncio.sleep(self.batch_window)
            self._wake(tuple(event for event in batch if event.actionable))
            await self.channel.send(tuple(batch))

    def _wake(self, events: tuple[ActivityEvent, ...]) -> None:
        """Append one line per PR with reviewer requests to the wake file."""
        lines = "".join(
            f"{message.pr}: {message.event_count} review request(s); "
            "call pending_events\n"
            for message in ChannelMessage.from_events(events)
        )
        if not lines:
            return
        try:
            with self.wake_file.open("a") as wake:
                wake.write(lines)
        except OSError:
            logger.exception("Could not write wake file %s", self.wake_file)
