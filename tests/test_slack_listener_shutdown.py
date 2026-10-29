from __future__ import annotations

import asyncio

from src.brain.plugins.listeners import slack as module


def test_listener_closes_socket_mode_client_on_cancellation(monkeypatch) -> None:
    import slack_sdk.socket_mode.aiohttp as socket_module
    import slack_sdk.web.async_client as web_module

    connected = asyncio.Event()
    instances = []

    class FakeWebClient:
        def __init__(self, *, token: str) -> None:
            self.token = token

    class FakeSocketModeClient:
        def __init__(self, *, app_token: str, web_client) -> None:
            self.app_token = app_token
            self.web_client = web_client
            self.socket_mode_request_listeners = []
            self.closed = False
            instances.append(self)

        async def connect(self) -> None:
            connected.set()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(web_module, "AsyncWebClient", FakeWebClient)
    monkeypatch.setattr(socket_module, "SocketModeClient", FakeSocketModeClient)
    monkeypatch.setattr(module, "_stamp_heartbeat", lambda: None)

    async def scenario() -> None:
        task = asyncio.create_task(
            module.SlackListener(bot_token="xoxb-test", app_token="xapp-test").run(
                lambda _envelope: None
            )
        )
        await asyncio.wait_for(connected.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(instances) == 1
    assert instances[0].closed is True
