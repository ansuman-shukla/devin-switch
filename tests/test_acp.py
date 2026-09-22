import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from devin_switch import acp, acp_state, cli, sessions, usage
from devin_switch.native import Native
from devin_switch.store import SwitchError


@pytest.fixture
def acp_native(signed_in: Native, tmp_path: Path, monkeypatch):
    executable = tmp_path / "fake-acp"
    executable.write_text(
        f"#!{sys.executable}\n" + Path(__file__).with_name("fake_acp.py").read_text()
    )
    executable.chmod(0o700)
    native = Native(signed_in.store, executable)
    native.store.select(native.store.accounts()[0])
    monkeypatch.setenv("DS_BINARY", str(executable))
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "requests.jsonl"))
    monkeypatch.chdir(tmp_path)
    return native


class Client:
    def __init__(self, process, *, auto_respond=True):
        self.process = process
        self.sequence = 0
        self.pending = {}
        self.messages = []
        self.permissions = []
        self.requests = asyncio.Queue()
        self.auto_respond = auto_respond
        self.pump = asyncio.create_task(self.read())

    async def read(self):
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            self.messages.append(message)
            if "method" in message:
                if "id" in message:
                    self.permissions.append(message)
                    self.requests.put_nowait(message)
                    if self.auto_respond:
                        await self.send(
                            {
                                "id": message["id"],
                                "result": {
                                    "outcome": {"outcome": "selected", "optionId": "reject"}
                                },
                            }
                        )
            elif future := self.pending.pop(message.get("id"), None):
                future.set_result(message)
        for future in self.pending.values():
            future.set_exception(AssertionError("ACP bridge exited before responding"))

    async def send(self, message):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.sequence += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.sequence] = future
        await self.send({"id": self.sequence, "method": method, "params": params})
        return await asyncio.wait_for(future, 20)

    async def new(self, cwd):
        result = await self.request("session/new", {"cwd": str(cwd), "mcpServers": []})
        assert "result" in result, result
        return result["result"]["sessionId"]

    async def prompt(self, session_id, text):
        return await self.request(
            "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}
        )

    def texts(self):
        return [
            message["params"]["update"]["content"]["text"]
            for message in self.messages
            if message.get("method") == "session/update"
            and message["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
        ]

    def agent_replies(self):
        return [json.loads(text) for text in self.texts() if text.startswith('{"account":')]


@asynccontextmanager
async def connect(native, *arguments, auto_respond=True):
    environment = {
        **os.environ,
        "DS_HOME": str(native.store.root),
        "DS_BINARY": str(native.binary),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "devin_switch",
        "acp",
        *arguments,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = Client(process, auto_respond=auto_respond)
    try:
        result = await client.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "test-editor", "version": "1"},
            },
        )
        assert "result" in result, result
        assert result["result"]["authMethods"] == []
        assert result["result"]["agentCapabilities"]["loadSession"]
        yield client
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        await client.pump
        stderr = (await process.stderr.read()).decode()
        assert "sensitive-native-diagnostic" not in stderr


def logged(native):
    path = native.store.root.parent / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_gui_switch_a_b_a_keeps_exact_chat_settings_and_other_chat(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native, "--sandbox") as client:
            first = await client.new(tmp_path)
            other = await client.new(tmp_path)
            setting = await client.request(
                "session/set_config_option",
                {"sessionId": first, "configId": "mode", "value": "plan"},
            )
            assert "result" in setting
            assert (await client.prompt(first, "/switch ansuman-2"))["result"][
                "stopReason"
            ] == "end_turn"
            assert acp_native.store.selected().name == "ansuman-2"
            await client.prompt(first, "continue")
            await client.prompt(other, "continue")
            await client.prompt(first, "/switch ansuman-1")
            await client.prompt(first, "continue")
            assert [(item["account"], item["session"]) for item in client.agent_replies()] == [
                ("ansuman-2", first),
                ("ansuman-1", other),
                ("ansuman-1", first),
            ]
            assert client.agent_replies()[0]["settings"]["mode"] == "plan"
            assert client.agent_replies()[2]["settings"]["mode"] == "plan"
            assert "historical replay" not in client.texts()
            listing = await client.request("session/list", {"cwd": str(tmp_path)})
            assert {item["sessionId"] for item in listing["result"]["sessions"]} == {first, other}
            calls = logged(acp_native)
            assert len([call for call in calls if call["method"] == "session/prompt"]) == 3
            assert all("--sandbox" in call["args"] for call in calls)
            commands = [
                message["params"]["update"]["availableCommands"]
                for message in client.messages
                if message.get("method") == "session/update"
                and message["params"]["update"]["sessionUpdate"] == "available_commands_update"
            ]
            assert commands
            assert all("switch" in {item["name"] for item in batch} for batch in commands)
            assert all(
                not {"login", "logout"} & {item["name"] for item in batch} for batch in commands
            )
        assert not any(run["active"] for run in sessions.runs(acp_native.store))

    asyncio.run(scenario())


def test_gui_reopen_restores_account_and_replays_only_on_explicit_load(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "/switch ansuman-2")
        acp_native.store.select(acp_native.store.account("ansuman-1"))
        async with connect(acp_native) as client:
            loaded = await client.request(
                "session/load", {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            )
            assert "result" in loaded, loaded
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["account"] == "ansuman-2"
            assert client.texts().count("historical replay") == 1

    asyncio.run(scenario())


def test_gui_failed_resume_rolls_back_without_default_change_or_prompt_replay(
    acp_native, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_FAIL_LOAD_ACCOUNT", "ansuman-2")

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "/switch ansuman-2")
            assert acp_native.store.selected().name == "ansuman-1"
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["account"] == "ansuman-1"
            assert any("restored" in text for text in client.texts())
            assert "sensitive-native-diagnostic" not in json.dumps(client.messages)
            assert (
                len([call for call in logged(acp_native) if call["method"] == "session/prompt"])
                == 1
            )

    asyncio.run(scenario())


def test_gui_auth_is_not_overridden_by_host_or_slash_login(acp_native, tmp_path, monkeypatch):
    monkeypatch.setenv("WINDSURF_API_KEY", "inherited-test-secret")

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            response = await client.request(
                "authenticate", {"methodId": "api-key", "token": "test-secret"}
            )
            assert "error" in response
            await client.prompt(session_id, "/login test-secret")
            await client.prompt(session_id, "/logout")
            await client.prompt(session_id, "/switch missing")
            assert not any(
                call["method"] in {"authenticate", "session/prompt"} for call in logged(acp_native)
            )
            assert "test-secret" not in json.dumps(client.messages)
            assert acp_native.store.selected().name == "ansuman-1"

    asyncio.run(scenario())


def test_gui_permission_requests_keep_separate_ids_and_user_decisions(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            first, second = await client.new(tmp_path), await client.new(tmp_path)
            responses = await asyncio.gather(
                client.prompt(first, "permission"), client.prompt(second, "permission")
            )
            assert all("result" in item for item in responses)
            assert len({item["id"] for item in client.permissions}) == 2
            assert {item["params"]["sessionId"] for item in client.permissions} == {first, second}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("text", "response"),
    [
        ("permission", {"result": {"outcome": {"outcome": "selected", "optionId": "allow"}}}),
        ("permission", {"result": {"outcome": {"outcome": "selected", "optionId": "reject"}}}),
        ("permission", {"result": {"outcome": {"outcome": "cancelled"}}}),
        (
            "permission",
            {
                "result": {
                    "outcome": {"outcome": "selected", "optionId": "allow"},
                    "_meta": {"cognition.ai/updatedInput": {"command": "printf safe"}},
                }
            },
        ),
        ("question", {"result": {"action": "accept", "content": {"approach": "two"}}}),
        (
            "question",
            {
                "result": {
                    "action": "decline",
                    "_meta": {"cognition.ai/partialContent": {"approach": "one"}},
                }
            },
        ),
        ("question", {"error": {"code": -32601, "message": "Unsupported by test client"}}),
    ],
)
def test_gui_delayed_decisions_reach_only_the_requesting_chat(acp_native, tmp_path, text, response):
    async def scenario():
        async with connect(acp_native, auto_respond=False) as client:
            first, second = await client.new(tmp_path), await client.new(tmp_path)
            pending = asyncio.create_task(client.prompt(first, text))
            request = await asyncio.wait_for(client.requests.get(), 5)
            assert request["params"]["sessionId"] == first
            assert not pending.done()
            await client.prompt(second, "continue")
            assert client.agent_replies()[-1]["session"] == second
            await client.send({"id": request["id"], **response})
            assert "result" in await pending
            assert client.agent_replies()[-1]["decision"] == {
                "jsonrpc": "2.0",
                "id": 100,
                **response,
            }
            assert client.agent_replies()[-1]["session"] == first

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session/set_config_option", {"configId": "mode", "value": "code"}),
        ("session/set_mode", {"modeId": "code"}),
        ("session/set_model", {"modelId": "other-model"}),
        ("_cognition.ai/command/revise", {"command": "printf test", "note": "Use safe flags"}),
    ],
)
def test_gui_controls_respond_while_tool_approval_is_pending(acp_native, tmp_path, method, params):
    async def scenario():
        async with connect(acp_native, auto_respond=False) as client:
            session_id = await client.new(tmp_path)
            pending = asyncio.create_task(client.prompt(session_id, "permission"))
            request = await asyncio.wait_for(client.requests.get(), 5)
            control = asyncio.create_task(
                client.request(method, {"sessionId": session_id, **params})
            )
            try:
                response = await asyncio.wait_for(asyncio.shield(control), 1)
                assert "result" in response
                assert not pending.done()
                if method == "_cognition.ai/command/revise":
                    assert response["result"] == {"command": "printf test --revised"}
            finally:
                await client.send(
                    {
                        "id": request["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": "reject"}},
                    }
                )
                await pending
                await control

    asyncio.run(scenario())


def test_gui_concurrent_approvals_keep_opposite_decisions_separate(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native, auto_respond=False) as client:
            first, second = await client.new(tmp_path), await client.new(tmp_path)
            pending = [
                asyncio.create_task(client.prompt(session_id, "permission"))
                for session_id in (first, second)
            ]
            requests = [await asyncio.wait_for(client.requests.get(), 5) for _ in pending]
            assert len({request["id"] for request in requests}) == 2
            decisions = {first: "allow", second: "reject"}
            for request in reversed(requests):
                await client.send(
                    {
                        "id": request["id"],
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": decisions[request["params"]["sessionId"]],
                            }
                        },
                    }
                )
            assert all("result" in response for response in await asyncio.gather(*pending))
            assert {
                reply["session"]: reply["decision"]["result"]["outcome"]["optionId"]
                for reply in client.agent_replies()
            } == decisions

    asyncio.run(scenario())


def test_terminal_gui_switch_is_queued_until_turn_finishes(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            pending = asyncio.create_task(client.prompt(session_id, "slow"))
            await asyncio.sleep(0.1)
            args = cli.parser().parse_args(["switch", "ansuman-2", "--session", session_id])
            assert await asyncio.to_thread(cli.execute, args, acp_native.store) == 0
            assert acp_native.store.selected().name == "ansuman-1"
            await pending
            async with asyncio.timeout(10):
                while acp_native.store.selected().name != "ansuman-2":
                    await asyncio.sleep(0.05)
            await client.prompt(session_id, "continue")
            assert [item["account"] for item in client.agent_replies()] == [
                "ansuman-1",
                "ansuman-2",
            ]

    asyncio.run(scenario())


def test_gui_duplicate_resume_and_removal_are_blocked(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as first, connect(acp_native) as second:
            session_id = await first.new(tmp_path)
            blocked = await second.request(
                "session/load", {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            )
            assert "error" in blocked
            with pytest.raises(SwitchError, match="in use"):
                acp_native.store.remove(acp_native.store.account("ansuman-1"))

    asyncio.run(scenario())


def test_gui_native_crash_is_reported_without_leaking_diagnostics(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            result = await client.prompt(session_id, "crash")
            assert "error" in result
            assert "sensitive-native-diagnostic" not in json.dumps(client.messages)
            result = await client.request(
                "session/load", {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            )
            assert "result" in result
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["session"] == session_id

    asyncio.run(scenario())


@asynccontextmanager
async def in_process(native):
    messages = []

    async def emit(message):
        messages.append(message)

    bridge = acp.Bridge(native, emit)
    try:
        await bridge.dispatch("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        yield bridge, messages
    finally:
        await bridge.close()


def test_gui_handoff_waits_for_inflight_controls_and_restores_their_settings(
    acp_native, tmp_path, monkeypatch
):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            chat = bridge.chats[session_id]
            original = chat.backend
            request = original.request
            started, finish = asyncio.Event(), asyncio.Event()

            async def delayed_control(method, params, timeout=None):
                if method == "session/set_config_option":
                    started.set()
                    await finish.wait()
                return await request(method, params, timeout)

            monkeypatch.setattr(original, "request", delayed_control)
            prompt = bridge.spawn(
                bridge.dispatch(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": "permission"}]},
                )
            )
            async with asyncio.timeout(5):
                while not any(
                    message.get("method") == "session/request_permission" for message in messages
                ):
                    await asyncio.sleep(0.01)
            permission = next(
                message
                for message in messages
                if message.get("method") == "session/request_permission"
            )
            control = bridge.spawn(
                bridge.dispatch(
                    "session/set_config_option",
                    {"sessionId": session_id, "configId": "mode", "value": "code"},
                )
            )
            await asyncio.wait_for(started.wait(), 5)
            acp_state.queue(acp_native, session_id, "ansuman-2")
            path = acp_state.request_path(acp_native.store, original.lease.run.id)
            switching = bridge.spawn(bridge.queued_switch(chat, path))
            await bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": permission["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "reject"}},
                }
            )
            async with asyncio.timeout(5):
                while chat.controls is not None:
                    await asyncio.sleep(0.01)
            assert not prompt.done() and not control.done() and not switching.done()
            assert chat.backend is original and original.process.poll() is None
            assert acp_native.store.selected().name == "ansuman-1"
            finish.set()
            await asyncio.wait_for(asyncio.gather(prompt, control, switching), 5)
            assert chat.backend.account.name == "ansuman-2"
            assert chat.backend.configs["mode"]["currentValue"] == "code"
            assert not bridge.client_requests
            assert (
                len([call for call in logged(acp_native) if call["method"] == "session/prompt"])
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("available", [True, False])
def test_gui_auto_switch_uses_fresh_quotas_and_bound_account(
    acp_native, tmp_path, monkeypatch, available
):
    import time

    checked = []

    def refresh(store, account, *, force):
        assert force and not store.locked("lock")
        checked.append(account.name)
        now = time.time()
        return usage.Usage(
            status="ok",
            fetched_at=now,
            checked_at=now,
            daily=usage.Window(20 if available else 100, now + 3600, "available"),
            weekly=usage.Window(40, now + 86400, "available"),
        )

    monkeypatch.setattr(usage, "refresh_account", refresh)

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = result["sessionId"]
            acp_native.store.select(acp_native.store.account("ansuman-2"))
            await bridge.dispatch(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "/switch"}]},
            )
            assert checked == ["ansuman-2"]
            assert bridge.chats[session_id].backend.account.name == (
                "ansuman-2" if available else "ansuman-1"
            )
            assert not any(call["method"] == "session/prompt" for call in logged(acp_native))
            if not available:
                assert "No other saved login" in json.dumps(messages)

    asyncio.run(scenario())


def test_gui_cancel_queued_switch_leaves_process_and_default_unchanged(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            pending = asyncio.create_task(client.prompt(session_id, "slow"))
            await asyncio.sleep(0.05)
            args = cli.parser().parse_args(["switch", "ansuman-2", "--session", session_id])
            await asyncio.to_thread(cli.execute, args, acp_native.store)
            cancel = cli.parser().parse_args(["switch", "--session", session_id, "--cancel"])
            await asyncio.to_thread(cli.execute, cancel, acp_native.store)
            await pending
            await asyncio.sleep(0.2)
            assert acp_native.store.selected().name == "ansuman-1"
            assert not any(call["method"] == "session/load" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_reloading_current_tab_reuses_process_and_replays_history(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            for method in ("session/load", "session/resume"):
                result = await client.request(
                    method, {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
                )
                assert "result" in result
            assert client.texts().count("historical replay") == 1
            loads = [call for call in logged(acp_native) if call["method"] == "session/load"]
            assert len({call["pid"] for call in loads}) == 1

    asyncio.run(scenario())


def test_gui_switch_preserves_workspace_and_mcp_context_without_persisting_it(
    acp_native, tmp_path, monkeypatch
):
    context = {
        "additionalDirectories": [str(tmp_path)],
        "mcpServers": [
            {
                "name": "test",
                "command": "not-executed",
                "args": [],
                "env": [{"name": "TEST_VALUE", "value": "synthetic-mcp-marker"}],
            }
        ],
    }
    monkeypatch.setenv("FAKE_EXPECT_CONTEXT", json.dumps(context))

    async def scenario():
        async with connect(acp_native) as client:
            result = await client.request("session/new", {"cwd": str(tmp_path), **context})
            session_id = result["result"]["sessionId"]
            await client.prompt(session_id, "/switch ansuman-2")
            assert acp_native.store.selected().name == "ansuman-2"
            for directory in ("acp", "runs", "runtime"):
                for path in (acp_native.store.root / directory).rglob("*.json"):
                    assert "synthetic-mcp-marker" not in path.read_text()

    asyncio.run(scenario())


def test_gui_refuses_destination_that_ignores_permission_configuration(
    acp_native, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_IGNORE_CONFIG_ACCOUNT", "ansuman-2")

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": "plan"},
            )
            await client.prompt(session_id, "/switch ansuman-2")
            assert acp_native.store.selected().name == "ansuman-1"
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["settings"]["mode"] == "plan"
            assert any("restored" in text for text in client.texts())

    asyncio.run(scenario())


def test_gui_slow_shutdown_does_not_force_kill_or_start_replacement(
    acp_native, tmp_path, monkeypatch
):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            monkeypatch.setenv("FAKE_SHUTDOWN_DELAY", "0.5")
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = result["sessionId"]
            backend = bridge.chats[session_id].backend
            monkeypatch.setattr(acp, "SHUTDOWN_TIMEOUT", 0.05)
            await bridge.dispatch(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "/switch ansuman-2"}],
                },
            )
            assert backend.process.poll() is None
            assert acp_native.store.selected().name == "ansuman-1"
            assert "still shutting down" in json.dumps(messages)
            assert not any(call["method"] == "session/load" for call in logged(acp_native))
            await asyncio.wait_for(backend.watcher, 3)

    asyncio.run(scenario())


def test_gui_background_lease_does_not_keep_old_chat_live(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "background")
            original = acp_state.live(acp_native.store)[0]
            await client.prompt(session_id, "/switch ansuman-2")
            assert acp_native.store.selected().name == "ansuman-2"
            assert acp_native.store.locked(f"run-{original['id']}.lock")
            runs = sessions.runs(acp_native.store)
            assert not next(run for run in runs if run["id"] == original["id"])["active"]
            assert len(acp_state.live(acp_native.store)) == 1
            await asyncio.sleep(2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        "/switch missing",
        "/switch ../invalid",
        "/switch ansuman-1",
        "/switch-status extra",
        "!ds switch ansuman-1",
    ],
)
def test_gui_invalid_local_commands_do_not_reach_model(acp_native, tmp_path, text):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, text)
            assert acp_native.store.selected().name == "ansuman-1"
            assert not any(call["method"] == "session/prompt" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_explicit_account_override_does_not_change_shared_default(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native, "--account", "ansuman-2") as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["account"] == "ansuman-2"
            assert acp_native.store.selected().name == "ansuman-1"

    asyncio.run(scenario())


def test_gui_cli_target_does_not_guess_latest_history(acp_native):
    with pytest.raises(SwitchError, match="No unique live GUI"):
        cli.execute(cli.parser().parse_args(["switch", "--session", "unknown"]), acp_native.store)
    assert acp_native.store.selected().name == "ansuman-1"


def test_gui_registry_is_secret_free_and_frozen_aware(acp_native, monkeypatch):
    for frozen in (False, True):
        monkeypatch.setattr(sys, "frozen", frozen, raising=False)
        config = acp.registry(acp_native, "ansuman-2", True)
        binary = next(iter(config["agents"][0]["distribution"]["binary"].values()))
        assert ("-m" in binary["args"]) is not frozen
        assert binary["args"][-4:] == ["acp", "--account", "ansuman-2", "--sandbox"]
        assert set(binary["env"]) == {"DS_HOME", "DS_BINARY"}
        assert not (acp_native.store.root / "acp").exists()


def test_gui_malformed_protocol_fails_closed_without_echoing_payload(acp_native):
    async def scenario():
        messages = []

        async def emit(message):
            messages.append(message)

        bridge = acp.Bridge(acp_native, emit)
        for message in (
            None,
            [],
            {"jsonrpc": "2.0", "id": {"secret": "marker"}, "method": "initialize"},
        ):
            await bridge.handle(message)
        assert len(messages) == 3
        assert all("error" in message and message["id"] is None for message in messages)
        assert "marker" not in json.dumps(messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["session/cancel", "session/close"])
def test_gui_cancel_and_close_reach_a_running_turn(acp_native, tmp_path, method):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            pending = asyncio.create_task(client.prompt(session_id, "wait"))
            async with asyncio.timeout(5):
                while not any(call["method"] == "session/prompt" for call in logged(acp_native)):
                    await asyncio.sleep(0.01)
            if method == "session/cancel":
                await client.send({"method": method, "params": {"sessionId": session_id}})
            else:
                args = cli.parser().parse_args(["switch", "ansuman-2", "--session", session_id])
                await asyncio.to_thread(cli.execute, args, acp_native.store)
                response = await client.request(method, {"sessionId": session_id})
                assert "result" in response
            assert (await pending)["result"]["stopReason"] == "cancelled"
            assert len(acp_state.live(acp_native.store)) == (1 if method == "session/cancel" else 0)
            assert acp_native.store.selected().name == "ansuman-1"

    asyncio.run(scenario())


def test_gui_idless_prompt_cannot_start_untracked_model_work(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            await bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "method": "session/prompt",
                    "params": {
                        "sessionId": result["sessionId"],
                        "prompt": [{"type": "text", "text": "wait"}],
                    },
                }
            )
            assert not any(call["method"] == "session/prompt" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_abnormal_shutdown_does_not_start_a_replacement(acp_native, tmp_path, monkeypatch):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            monkeypatch.setenv("FAKE_EXIT_ON_EOF", "7")
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            await bridge.dispatch(
                "session/prompt",
                {
                    "sessionId": result["sessionId"],
                    "prompt": [{"type": "text", "text": "/switch ansuman-2"}],
                },
            )
            assert acp_native.store.selected().name == "ansuman-1"
            assert "exited abnormally" in json.dumps(messages)
            assert not any(call["method"] == "session/load" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_missing_destination_login_leaves_original_agent_open(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            acp_native.store.credentials(acp_native.store.account("ansuman-2")).unlink()
            await client.prompt(session_id, "/switch ansuman-2")
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["account"] == "ansuman-1"
            assert acp_native.store.selected().name == "ansuman-1"
            assert not any(call["method"] == "session/load" for call in logged(acp_native))

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/switch", "/switch ansuman-2"])
def test_gui_unsaved_chat_stays_open_then_switches_after_saved(
    acp_native, tmp_path, monkeypatch, command
):
    monkeypatch.setenv("FAKE_SAVE_ON_PROMPT", "1")

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            original = acp_state.live(acp_native.store)[0]
            assert not sessions.history(acp_native.store)
            await client.prompt(session_id, "/switch-status")
            assert "not saved" in client.texts()[-1]
            await client.prompt(session_id, command)
            assert acp_state.live(acp_native.store)[0]["id"] == original["id"]
            assert acp_native.store.selected().name == "ansuman-1"
            assert any("not saved" in text and "unchanged" in text for text in client.texts())
            assert not any(
                call["method"] in {"session/prompt", "session/load"} for call in logged(acp_native)
            )
            await client.prompt(session_id, "first normal message")
            await client.prompt(session_id, "/switch ansuman-2")
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["account"] == "ansuman-2"
            assert client.agent_replies()[-1]["session"] == session_id
            await client.prompt(session_id, "/switch ansuman-1")
            await client.prompt(session_id, "continue")
            assert client.agent_replies()[-1]["account"] == "ansuman-1"
            assert client.agent_replies()[-1]["session"] == session_id

    asyncio.run(scenario())


def test_gui_unsaved_queued_handoff_does_not_close_source(acp_native, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SAVE_ON_PROMPT", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = result["sessionId"]
            chat = bridge.chats[session_id]
            original = chat.backend
            path = acp_state.request_path(acp_native.store, original.lease.run.id)
            acp_state.queue(acp_native, session_id, "ansuman-2")
            await bridge.queued_switch(chat, path)
            assert chat.backend is original and original.process.poll() is None
            assert acp_native.store.selected().name == "ansuman-1"
            assert "not saved" in json.dumps(messages)
            assert not path.exists()

    asyncio.run(scenario())


def test_gui_handoff_failure_reports_destination_and_recovery_stages(
    acp_native, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_FAIL_LOAD_ACCOUNT", "ansuman-1,ansuman-2")

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "/switch ansuman-2")
            output = " ".join(client.texts())
            assert "Switch to ansuman-2 failed" in output
            assert "Recovery to ansuman-1 failed" in output
            assert output.count("loading the saved conversation") == 2
            assert "RPC code -32603" in output
            assert "sensitive-native-diagnostic" not in output
            assert "The conversation is saved" not in output
            assert acp_native.store.selected().name == "ansuman-1"
            assert not acp_state.live(acp_native.store)

    asyncio.run(scenario())
