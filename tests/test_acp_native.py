import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest

from devin_switch import sessions
from devin_switch.acp import Bridge
from devin_switch.native import Native
from devin_switch.store import Store

pytestmark = pytest.mark.skipif(
    not os.environ.get("DS_TEST_NATIVE"), reason="Set DS_TEST_NATIVE for installed CLI"
)


@asynccontextmanager
async def native_acp(native, account, project, client_name="windsurf"):
    environment = native.environment(account)
    environment["HOME"] = str(project)
    process = await asyncio.create_subprocess_exec(
        str(native.binary),
        "acp",
        env=environment,
        cwd=project,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    sequence = 0

    async def send(message):
        process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        await process.stdin.drain()

    async def request(method, params):
        nonlocal sequence
        sequence += 1
        await send({"id": sequence, "method": method, "params": params})
        async with asyncio.timeout(20):
            while True:
                line = await process.stdout.readline()
                assert line, "Native ACP exited before responding"
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        await send(
                            {
                                "id": message["id"],
                                "error": {"code": -32601, "message": "Unavailable in offline test"},
                            }
                        )
                elif message.get("id") == sequence:
                    return message

    try:
        initialized = await request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": client_name, "version": "0.3.0"},
            },
        )
        assert "result" in initialized, initialized
        assert initialized["result"]["protocolVersion"] == 1
        assert initialized["result"]["agentCapabilities"]["loadSession"] is True
        yield request
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
    assert not native.store.credentials(account).exists()


@pytest.mark.parametrize("client_name", ["devin-switch-test", "windsurf"])
def test_installed_acp_shared_history_without_login(
    store: Store, tmp_path: Path, monkeypatch, client_name
):
    monkeypatch.chdir(tmp_path)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    first, second = store.accounts()
    assert native.sessions(first) == []
    with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
        connection.execute(
            "INSERT INTO sessions "
            "(id, working_directory, backend_type, model, agent_mode, "
            "created_at, last_activity_at, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "acp-continuity-test",
                str(tmp_path),
                "local",
                "test-model",
                "normal",
                1,
                1,
                "ACP continuity marker",
            ),
        )

    async def scenario():
        for account in (first, second, first):
            async with native_acp(native, account, tmp_path, client_name) as request:
                listed = await request("session/list", {"cwd": str(tmp_path)})
                assert "result" in listed, listed
                assert any(
                    item["sessionId"] == "acp-continuity-test"
                    for item in listed["result"]["sessions"]
                ), listed
                expected = listed["result"]["sessions"][0]
                adapted = (await Bridge(native, None).list_sessions({"cwd": str(tmp_path)}))[
                    "sessions"
                ][0]
                for key in ("sessionId", "cwd", "title"):
                    assert adapted[key] == expected[key]
                assert datetime.fromisoformat(adapted["updatedAt"]) == datetime.fromisoformat(
                    expected["updatedAt"]
                )

    asyncio.run(scenario())


def test_installed_acp_new_chat_is_not_saved_before_a_prompt(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    account = store.accounts()[0]

    async def scenario():
        async with native_acp(native, account, tmp_path) as request:
            created = await request("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            assert "result" in created, created
            session_id = created["result"]["sessionId"]
            assert isinstance(session_id, str) and session_id
            assert session_id not in {item["id"] for item in sessions.history(store)}
        assert session_id not in {item["id"] for item in sessions.history(store)}

    asyncio.run(scenario())


def test_installed_bridge_keeps_unsaved_chat_open_on_switch(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Native, "require_login", lambda self, account: None)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    first, second = store.accounts()
    store.select(first)

    async def scenario():
        messages = []

        async def emit(message):
            messages.append(message)

        bridge = Bridge(native, emit)
        try:
            await bridge.dispatch(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "windsurf", "version": "0.3.0"},
                },
            )
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            original = bridge.chats[session_id].backend
            await bridge.dispatch(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": f"/switch {second.name}"}],
                },
            )
            assert original.process.poll() is None
            assert bridge.chats[session_id].backend is original
            assert store.selected() == first
            assert "not saved" in json.dumps(messages)
        finally:
            await bridge.close()
        assert not any(store.credentials(account).exists() for account in store.accounts())

    asyncio.run(scenario())
