"""FORK: the outbound dispatcher honors a cancel that an awaited step swallowed.

On Python 3.11 ``asyncio.wait_for`` can return a ready queue item and drop a cancel that
raced it, leaving the task ``cancelling()`` but still looping, so ``stop_all()`` waits
forever. The fork's extra tools-summary message made that race common at shutdown
(``test_scheduled_delivery.py`` websocket-origin cron case hung). The loop now checks
``current_task().cancelling()`` before every iteration.
"""

import asyncio

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


@pytest.mark.asyncio
async def test_dispatcher_exits_when_a_swallowed_cancel_is_pending(tmp_path):
    config = Config.model_validate({"channels": {"websocket": {"enabled": False}}})
    manager = ChannelManager(config, MessageBus(), config_path=tmp_path / "config.json")
    swallowed = asyncio.Event()

    async def recover_and_swallow_cancel() -> None:
        # Reproduce the 3.11 wait_for race: a cancel arrives and an inner await eats it.
        task = asyncio.current_task()
        assert task is not None
        if not swallowed.is_set():
            task.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                swallowed.set()

    manager._recover_scheduled = recover_and_swallow_cancel  # type: ignore[method-assign]
    dispatcher = asyncio.create_task(manager._dispatch_outbound())
    await asyncio.wait_for(swallowed.wait(), 5)
    # Without the fork guard the loop keeps polling the empty queue forever. asyncio.wait
    # never cancels (wait_for's own timeout-cancel would end the loop and hide the bug).
    done, _ = await asyncio.wait({dispatcher}, timeout=3)
    try:
        assert dispatcher in done, "dispatcher ignored a pending cancel"
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
