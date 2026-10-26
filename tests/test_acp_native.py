import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from devin_switch import acp, acp_state, sessions
from devin_switch.acp import Bridge
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError

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

    asyncio.run(scenario())


def test_installed_cli_shares_user_mcp_servers_across_logins(store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    first, second = store.accounts()
    native.capture(first, ("mcp", "add", "-s", "user", "switch-shared", "--", "true"))
    assert "switch-shared" in native.capture(second, ("mcp", "list"))
    native.capture(second, ("mcp", "remove", "-s", "user", "switch-shared"))
    assert "switch-shared" not in native.capture(first, ("mcp", "list"))
    for account in (first, second):
        config = store.directory(account.name) / "config/devin/mcp_config.json"
        assert config.is_symlink()
        assert config.resolve() == (store.root / "shared/config/mcp_config.json").resolve()
        assert not store.credentials(account).exists()


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


def test_installed_bridge_keeps_blank_chat_on_switch_and_releases_it_idle(
    store, tmp_path, monkeypatch
):
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
            initialized = await bridge.dispatch(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "windsurf", "version": "0.3.0"},
                },
            )
            capabilities = initialized["agentCapabilities"]["sessionCapabilities"]
            assert "additionalDirectories" in capabilities
            # Desktop offers transcript export only to bundled agents; the bridge asks for it.
            meta = initialized["agentCapabilities"]["_meta"]
            assert meta.get("cognition.ai/diagnosticsSessionExport") is True
            # Editor context replayed to the new native and broadcast to the open one.
            document = {"uri": (tmp_path / "a.py").as_uri()}
            await bridge.dispatch("_cognition.ai/document/didOpen", document, notification=True)
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            await bridge.dispatch("_cognition.ai/document/didFocus", document, notification=True)
            session_id = created["sessionId"]
            chat = bridge.chats[session_id]
            original = chat.backend
            with pytest.raises(SwitchError, match="not available in shared history"):
                acp_state.queue_close(store, original.lease.run.id)
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
            original.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            assert chat.suspended and original.process.poll() == 0
            with pytest.raises(SwitchError, match="start a new chat"):
                await bridge.dispatch(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": "hello"}]},
                )
            assert bridge.chats[session_id].backend is original
            assert session_id not in {item["id"] for item in sessions.history(store)}
        finally:
            await bridge.close()
        assert not any(store.credentials(account).exists() for account in store.accounts())

    asyncio.run(scenario())


def test_installed_bridge_manages_shared_mcp_servers_for_desktop_customize(
    store, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Native, "require_login", lambda self, account: None)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    first, second = store.accounts()
    store.select(first)

    async def scenario():
        bridge = Bridge(native, lambda message: asyncio.sleep(0))
        try:
            await bridge.dispatch(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {"_meta": {"cognition.ai/mcp": True}},
                    "clientInfo": {"name": "windsurf", "version": "0.3.0"},
                },
            )
            config = {"command": "true", "args": []}
            await bridge.dispatch(
                "_cognition.ai/mcp/installServer", {"serverId": "switch-gui", "config": config}
            )
            assert "switch-gui" in native.capture(second, ("mcp", "list"))
            listed = await bridge.dispatch("_cognition.ai/mcp/listServers", {})
            (server,) = [item for item in listed["servers"] if item["serverId"] == "switch-gui"]
            await bridge.dispatch(
                "_cognition.ai/mcp/removeServer",
                {"serverId": "switch-gui", "sourcePath": server["sourcePath"]},
            )
            assert "switch-gui" not in native.capture(second, ("mcp", "list"))
        finally:
            await bridge.close()
        for account in (first, second):
            path = store.directory(account.name) / "config/devin/mcp_config.json"
            assert path.resolve() == (store.root / "shared/config/mcp_config.json").resolve()
            assert not store.credentials(account).exists()

    asyncio.run(scenario())


def relay_test_native(store, tmp_path, monkeypatch):
    from fake_upstream import Upstream

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Native, "require_login", lambda self, account: None)
    native = Native(store, Path(os.environ["DS_TEST_NATIVE"]))
    first = store.accounts()[0]
    store.select(first)
    upstream = Upstream([account.name for account in store.accounts()])
    upstream.empty_status = True
    store.credentials(first).write_text(upstream.credentials(first.name))
    return native, first, upstream


async def open_new_chat(native, cwd):
    """Open a chat through the bridge; returns whether it relays, or the native's RPC code."""
    bridge = Bridge(native, lambda message: asyncio.sleep(0))
    try:
        await bridge.dispatch("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        created = await bridge.dispatch("session/new", {"cwd": str(cwd), "mcpServers": []})
        return bridge.chats[created["sessionId"]].backend.relay is not None
    except acp.RpcError as exc:
        return exc.code
    finally:
        await bridge.close()


def test_installed_cli_routes_api_traffic_through_the_relay(store, tmp_path, monkeypatch):
    from fake_upstream import api_key

    from devin_switch import relay

    relayed = []
    forward = relay.Handler.forward

    def recording(handler, login, body):
        relayed.append((handler.path, login.account.name))
        return forward(handler, login, body)

    monkeypatch.setattr(relay.Handler, "forward", recording)
    native, first, upstream = relay_test_native(store, tmp_path, monkeypatch)
    saved = store.credentials(first).read_text()
    try:
        assert asyncio.run(open_new_chat(native, tmp_path)) is True
    finally:
        upstream.close()
    # The native keeps the host login in memory and never rewrites saved credentials.
    assert store.credentials(first).read_text() == saved
    paths = {path.rsplit("/", 1)[-1] for path, _ in relayed}
    assert {"GetUserStatus", "GetCliTeamSettings"} <= paths
    assert "GetChatMessage" not in paths
    assert {account for _, account in relayed} == {first.name}
    # The key travels in the body too: the relay's same-length swap depends on it.
    assert all(api_key(first.name).encode() in request["raw"] for request in upstream.requests)


def test_installed_cli_failed_host_login_behaves_like_no_relay(store, tmp_path, monkeypatch):
    from devin_switch import relay

    native, first, upstream = relay_test_native(store, tmp_path, monkeypatch)
    upstream.close()
    with_relay = asyncio.run(open_new_chat(native, tmp_path))
    monkeypatch.setattr(relay, "login", lambda store, account: None)
    without_relay = asyncio.run(open_new_chat(native, tmp_path))
    assert with_relay is not True
    assert with_relay == without_relay
