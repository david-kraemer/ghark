"""Claude Code channel messages and transport."""

import dataclasses
import logging

from mcp.server.session import ServerSession
from mcp.types import JSONRPCNotification

from .models import ActivityEvent, ChannelMessage

__all__ = ["ClaudeChannel"]

logger = logging.getLogger("ghark")


@dataclasses.dataclass(slots=True)
class ClaudeChannel:
    """Write best-effort channel notifications to the active MCP session."""

    _session: ServerSession | None = dataclasses.field(default=None, repr=False)

    def attach(self, session: ServerSession) -> None:
        self._session = session

    async def send(self, events: tuple[ActivityEvent, ...]) -> None:
        if self._session is None:
            return
        for message in ChannelMessage.from_events(events):
            notification = JSONRPCNotification(
                method="notifications/claude/channel",
                params=message.as_dict(),
            )
            try:
                await self._session.send_notification(notification)  # type: ignore[arg-type]
            except Exception:
                logger.exception(
                    "Channel delivery failed for %s#%s", message.repo, message.number
                )
