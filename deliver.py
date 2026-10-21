"""Delivery backends: real Discord sends, and a stub for offline runs.

A send is one attempt against an injected Messageable — test transports
can verify exact payload bytes on a loopback server without Discord.
Delivery failures raise DeliverFailed so callers can keep the pending
event instead of assuming at-most-once success.
"""

import discord


class DeliverFailed(Exception):
    """The injected transport rejected one send attempt."""


def make_discord_deliverer(channel):
    """Return an async callable delivering one message line."""

    async def deliver(message: str) -> None:
        await channel.send(
            message, allowed_mentions=discord.AllowedMentions.none()
        )

    return deliver


class StubTransport:
    """Test transport: validates without network; optional failures.

    fail_after=N fails the FIRST N attempts (DeliverFailed), then
    accepts later ones — exercises partial-failure recovery paths.
    """

    def __init__(self, fail_after: int | None = None) -> None:
        self.remaining_failures = fail_after
        self.delivered: list[str] = []

    async def deliver(self, message: str) -> None:
        if self.remaining_failures is not None and self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise DeliverFailed("stub transport rejection")
        self.delivered.append(message)
