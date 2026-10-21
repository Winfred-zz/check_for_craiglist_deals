"""deliver.py: stub transport accounting + mention-safe Discord sends."""

import asyncio

import pytest

from deliver import DeliverFailed, StubTransport, make_discord_deliverer


class CustomDeliveryError(Exception):
    """Stands in for a discord HTTPException from a failing Messageable."""


def test_stub_rejects_first_n_attempts_then_accepts() -> None:
    stub = StubTransport(fail_after=2)
    for _ in range(2):
        with pytest.raises(DeliverFailed) as raised:
            asyncio.run(stub.deliver("nope"))
        assert "stub transport rejection" in str(raised.value)
    asyncio.run(stub.deliver("yes"))
    assert stub.delivered == ["yes"]
    assert stub.remaining_failures == 0


def test_stub_without_fail_after_always_accepts() -> None:
    stub = StubTransport()
    asyncio.run(stub.deliver("one"))
    asyncio.run(stub.deliver("two"))
    assert stub.delivered == ["one", "two"]


def test_stub_zero_failures_accepts_immediately() -> None:
    stub = StubTransport(fail_after=0)
    asyncio.run(stub.deliver("first"))
    assert stub.delivered == ["first"]


def test_discord_deliverer_suppresses_every_mention() -> None:
    calls: list[dict] = []

    class FakeChannel:
        async def send(self, content: str, **kwargs: object) -> None:
            calls.append(kwargs)

    deliverer = make_discord_deliverer(FakeChannel())
    asyncio.run(deliverer("hello @owner"))
    assert len(calls) == 1
    allowed = calls[0]["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def test_discord_send_errors_are_not_swallowed() -> None:
    class FailingChannel:
        async def send(self, content: str, **kwargs: object) -> None:
            raise CustomDeliveryError("http 500")

    deliverer = make_discord_deliverer(FailingChannel())
    with pytest.raises(CustomDeliveryError):
        asyncio.run(deliverer("hello"))


def test_stub_and_discord_share_one_call_contract() -> None:
    seen: list[str] = []

    class RecordingChannel:
        async def send(self, content: str, **kwargs: object) -> None:
            seen.append(content)

    asyncio.run(make_discord_deliverer(RecordingChannel())("recorded"))
    assert seen == ["recorded"]
