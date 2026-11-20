import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from fake_upstream import Upstream

from devin_switch import acp, acp_state, cli, desktop, handoff, sessions, subagents, usage
from devin_switch.native import Native
from devin_switch.store import Store, SwitchError


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


CHOOSERS: dict[int, int] = {}
LOCK_HOLDERS: dict[int, int] = {}


def choose_best_holds_store_lock() -> bool:
    """Whether a thread running choose_best holds the global store lock right now."""
    return any(LOCK_HOLDERS.get(thread) for thread in CHOOSERS)


@pytest.fixture(autouse=True)
def track_store_lock_holders(monkeypatch):
    """Record which threads hold the store lock and which run choose_best.

    Usage refreshes must not run under the store lock taken by choose_best. Other launches
    (a warm spare, another window) legitimately take it meanwhile, so tests check the
    choosing thread, not whether anyone holds the lock.
    """
    original_lock, original_choose = Store.lock, handoff.choose_best

    def count(table, thread, step):
        table[thread] = table.get(thread, 0) + step

    @contextmanager
    def lock(self, filename="lock", **options):
        with original_lock(self, filename, **options) as handle:
            thread = threading.get_ident()
            if filename == "lock":
                count(LOCK_HOLDERS, thread, 1)
            try:
                yield handle
            finally:
                if filename == "lock":
                    count(LOCK_HOLDERS, thread, -1)

    def choose_best(*arguments, **options):
        thread = threading.get_ident()
        count(CHOOSERS, thread, 1)
        try:
            return original_choose(*arguments, **options)
        finally:
            count(CHOOSERS, thread, -1)
            if not CHOOSERS[thread]:
                del CHOOSERS[thread]

    monkeypatch.setattr(Store, "lock", lock)
    monkeypatch.setattr(handoff, "choose_best", choose_best)
    yield
    CHOOSERS.clear()
    LOCK_HOLDERS.clear()


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
        # Like Desktop, the client reads the bridge's lines however long they are.
        limit=2**30,
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


def test_gui_agents_stay_out_of_the_clis_shared_daemon(acp_native, tmp_path, monkeypatch):
    """A daemon runs every chat on the login that started it, whatever login Switch chose."""
    monkeypatch.setenv("FAKE_PROCESS_MODEL", "1")

    async def scenario():
        async with connect(acp_native, "--sandbox") as client:
            chat = await client.new(tmp_path)
            await client.prompt(chat, "/switch ansuman-2")
            await client.prompt(chat, "continue")
            calls = logged(acp_native)
            assert {call["account"] for call in calls} == {"ansuman-1", "ansuman-2"}
            assert all(
                call["args"] == ["--process-model=in-process", "--sandbox", "acp"] for call in calls
            )

    asyncio.run(scenario())


def test_gui_agents_get_no_process_flag_from_a_cli_without_one(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            await client.prompt(await client.new(tmp_path), "continue")
            assert all(call["args"] == ["acp"] for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_chats_and_handoffs_share_user_github_config(acp_native, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(acp_native.store.directory("ansuman-1") / "config"))

    async def scenario():
        async with connect(acp_native) as client:
            first = await client.new(tmp_path)
            await client.new(tmp_path)
            await client.prompt(first, "/switch ansuman-2")
            assert acp_native.store.selected().name == "ansuman-2"
        async with connect(acp_native) as client:
            await client.new(tmp_path)
        calls = [
            call for call in logged(acp_native) if call["method"] in {"session/new", "session/load"}
        ]
        assert len(calls) == 4
        assert {call["account"] for call in calls} == {"ansuman-1", "ansuman-2"}
        assert {call["gh_config_dir"] for call in calls} == {str(tmp_path / "home/.config/gh")}

    asyncio.run(scenario())


def test_gui_chats_tell_the_whiteboard_their_conversation_across_handoffs(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            first = await client.new(tmp_path)
            second = await client.new(tmp_path)
            await client.prompt(first, "whiteboard-chat")
            await client.prompt(second, "whiteboard-chat")
            await client.prompt(first, "/switch ansuman-2")
            await client.prompt(first, "whiteboard-chat")
        asked = [call for call in logged(acp_native) if call["chat_id"] is not None]
        assert [call["chat_id"] for call in asked] == [first, second, first]
        assert asked[0]["pid"] != asked[2]["pid"], "the handoff started a new native process"

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


def test_gui_question_card_shows_multi_select_and_option_descriptions(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "question")
            (card,) = [
                message["params"]["update"]
                for message in client.messages
                if message.get("method") == "session/update"
                and message["params"]["update"].get("sessionUpdate") == "tool_call"
            ]
            # Desktop's card reads only allow_multiple and plain-string labels from this list.
            assert card["_meta"]["cognition.ai/questions"] == [
                {
                    "question": "Pick any\n\n- **One**: First\n- **Two**: Second",
                    "header": "T",
                    "options": ["One", "Two"],
                    "multiSelect": True,
                    "allow_multiple": True,
                },
                {
                    "question": "Pick one\n\n- **Fast**: Quick but rough",
                    "header": "T",
                    "options": ["Fast", "Slow"],
                },
                {"question": "Plain", "header": "T", "options": ["Yes", "No"]},
            ]
            assert [item["question"] for item in card["rawInput"]["questions"]] == [
                "Pick any",
                "Pick one",
                "Plain",
            ]
            (form,) = client.permissions
            assert form["params"]["message"] == "Choose an approach"
            assert form["params"]["requestedSchema"]["properties"] == {
                "approach": {"type": "string", "enum": ["one", "two"]}
            }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session/set_config_option", {"configId": "mode", "value": "code"}),
        ("session/set_mode", {"modeId": "code"}),
        ("session/set_model", {"modelId": "other-model"}),
        ("_cognition.ai/command/revise", {"command": "printf test", "note": "Use safe flags"}),
        ("_cognition.ai/subagent/cancel", {"agentId": "s1"}),
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


def test_gui_other_window_takes_over_an_idle_chat_and_hands_it_back(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as first, connect(acp_native) as second:
            session_id = await first.new(tmp_path)
            await first.prompt(session_id, "hello")
            owner = next(
                run for run in acp_state.live(acp_native.store) if run["session_id"] == session_id
            )
            setup = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            loaded = await second.request("session/load", setup)
            assert "result" in loaded, loaded
            live = [
                run for run in acp_state.live(acp_native.store) if run["session_id"] == session_id
            ]
            assert len(live) == 1 and live[0]["id"] != owner["id"]
            taken = live[0]["id"]
            with pytest.raises(SwitchError, match="in use"):
                acp_native.store.remove(acp_native.store.account("ansuman-1"))
            await second.prompt(session_id, "here")
            assert second.agent_replies()[-1]["session"] == session_id
            reply = await first.prompt(session_id, "back")
            assert "result" in reply, reply
            assert first.agent_replies()[-1]["session"] == session_id
            live = [
                run for run in acp_state.live(acp_native.store) if run["session_id"] == session_id
            ]
            assert len(live) == 1 and live[0]["id"] not in (owner["id"], taken)
            prompts = [call for call in logged(acp_native) if call["method"] == "session/prompt"]
            assert len(prompts) == 3

    asyncio.run(scenario())


def test_gui_sidebar_action_does_not_take_a_chat_back(acp_native, tmp_path):
    """A rename in window 1 must not pull the chat out of window 2, which now has it open."""

    async def scenario():
        async with connect(acp_native) as first, connect(acp_native) as second:
            session_id = await first.new(tmp_path)
            await first.prompt(session_id, "hello")
            setup = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            assert "result" in await second.request("session/load", setup)
            taken = [
                run["id"]
                for run in acp_state.live(acp_native.store)
                if run["session_id"] == session_id
            ]
            renamed = await first.request(
                "_cognition.ai/session/rename", {"sessionId": session_id, "title": "New"}
            )
            # Like a native without the chat loaded; Desktop handles "Session not found" quietly.
            assert renamed["error"]["code"] == -32602
            assert renamed["error"]["message"].startswith("Session not found")
            live = [
                run["id"]
                for run in acp_state.live(acp_native.store)
                if run["session_id"] == session_id
            ]
            assert live == taken
            closed = await first.request("session/close", {"sessionId": "never-opened-here"})
            assert closed == {"jsonrpc": "2.0", "id": closed["id"], "result": {}}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method",
    [
        "_cognition.ai/session/rename",
        "_cognition.ai/queuedMessages/update",
        # Desktop 3.14 pages a bounded load's older rows while the agent works.
        "_cognition.ai/session/loadPage",
        "_cognition.ai/session/feedback",
        "_cognition.ai/session/reaction",
        "_cognition.ai/session/stop",
    ],
)
def test_gui_sidebar_requests_answer_during_a_turn(acp_native, tmp_path, method):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            answer = await asyncio.wait_for(client.request(method, {"sessionId": session_id}), 5)
            assert "result" in answer and not turn.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            await turn

    asyncio.run(scenario())


MEBIBYTE = 1024 * 1024
# What Desktop 3.14's pager sends after a bounded load: every row before the replayed tail, up
# to its default of 10,000 (`.probes/history-page` in the main checkout).
PAGE = {"startRowId": 0, "endRowId": 23475, "limit": 10000}
UNREADABLE = "The GUI agent sent a message that could not be read"
CLOSED = "The GUI agent connection closed. Reopen the saved chat to reconnect."


def image_block(size, mark=0):
    """An attached image as ACP carries it: `size` bytes of base64 text, told apart by `mark`."""
    return {"type": "image", "mimeType": "image/png", "data": f"{mark:04d}".ljust(size, "A")}


def history_page(session_id, mebibytes):
    """An answer to `loadPage` like CLI 3000.14's: older rows in ONE line, images and all."""
    return {
        "updates": [
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": image_block(MEBIBYTE, row),
                },
            }
            for row in range(mebibytes)
        ],
        "coveredRange": {"startRowId": 0, "endRowId": PAGE["endRowId"]},
        "nextEndRowId": None,
        "exhausted": True,
        "replayFilterVersion": "1",
    }


def agent_text(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def only_native(native, session_id):
    """The one native process that served a chat's requests, checked to be running still."""
    (pid,) = {call["pid"] for call in logged(native) if call["sessionId"] == session_id}
    os.kill(pid, 0)
    return pid


@pytest.mark.parametrize("mebibytes", [9, 40])
def test_gui_history_page_of_any_length_reaches_desktop_and_keeps_the_agent(
    acp_native, tmp_path, monkeypatch, mebibytes
):
    """Desktop 3.14 asks for a chat's older rows at once, and the native answers in one line.

    A chat with 11.5 MB of images answered 15.4 MB. The bridge read lines of up to 8 MiB, and
    a longer one ended the chat's healthy native: every later message got "has stopped".
    """
    answer = tmp_path / "page.json"
    monkeypatch.setenv("FAKE_PAGE", str(answer))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            page = history_page(session_id, mebibytes)
            answer.write_text(json.dumps(page))
            assert answer.stat().st_size > mebibytes * MEBIBYTE
            got = await client.request(
                "_cognition.ai/session/loadPage", {"sessionId": session_id, **PAGE}
            )
            assert got.get("result") == page
            assert (await client.prompt(session_id, "hello"))["result"]["stopReason"] == "end_turn"
            assert client.agent_replies()[-1]["session"] == session_id
            pages = [call for call in logged(acp_native) if call["method"].endswith("/loadPage")]
            assert [call["pid"] for call in pages] == [only_native(acp_native, session_id)]

    asyncio.run(scenario())


def test_gui_update_longer_than_the_read_buffer_reaches_desktop(acp_native, tmp_path, monkeypatch):
    """A turn's update of 9 MiB, such as an image, is an update like any other."""
    said = tmp_path / "said.json"
    monkeypatch.setenv("FAKE_SAY_FILE", str(said))
    long = {"sessionUpdate": "user_message_chunk", "content": image_block(9 * MEBIBYTE)}
    after = agent_text("after the long one")
    said.write_text(json.dumps([long, after]))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            assert (await client.prompt(session_id, "say"))["result"]["stopReason"] == "end_turn"
            updates = [
                message["params"]["update"]
                for message in client.messages
                if message.get("method") == "session/update"
            ]
            assert updates.index(long) < updates.index(after)
            assert (await client.prompt(session_id, "hello"))["result"]["stopReason"] == "end_turn"
            assert client.agent_replies()[-1]["session"] == session_id
            only_native(acp_native, session_id)

    asyncio.run(scenario())


def test_gui_prompt_longer_than_the_read_buffer_reaches_the_agent_and_other_chats_go_on(
    acp_native, tmp_path
):
    """A prompt with pasted images is one line too; reading it once ended the whole bridge."""

    async def scenario():
        async with connect(acp_native) as client:
            first = await client.new(tmp_path)
            other = await client.new(tmp_path)
            image = image_block(9 * MEBIBYTE)
            params = {"sessionId": first, "prompt": [{"type": "text", "text": "hello"}, image]}
            answered = await client.request("session/prompt", params)
            assert answered["result"]["stopReason"] == "end_turn"
            (sent,) = [call for call in logged(acp_native) if call["method"] == "session/prompt"]
            assert sent["images"] == [hashlib.sha256(image["data"].encode()).hexdigest()]
            # A line that is no message is answered as invalid, and the window's chats go on.
            client.process.stdin.write(b"not a message\n")
            await client.process.stdin.drain()
            invalid = {"code": -32600, "message": "Invalid ACP message."}
            await until(lambda: invalid in [message.get("error") for message in client.messages])
            assert (await client.prompt(other, "hello"))["result"]["stopReason"] == "end_turn"
            assert client.agent_replies()[-1]["session"] == other
            assert client.process.returncode is None

    asyncio.run(scenario())


def test_gui_desktop_line_over_the_ceiling_is_answered_and_the_bridge_keeps_serving(
    acp_native, monkeypatch
):
    """Desktop's lines follow the same rule: a dropped one must not end every chat's bridge."""
    monkeypatch.setattr(acp, "MAX_MESSAGE", 64 * 1024)
    (bridge_in, desktop_out), (desktop_in, bridge_out) = os.pipe(), os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(bridge_in, "rb"))
    monkeypatch.setattr(sys, "stdout", os.fdopen(bridge_out, "wb"))

    async def scenario():
        loop = asyncio.get_running_loop()
        answers = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(answers), os.fdopen(desktop_in, "rb")
        )
        desktop, _ = await loop.connect_write_pipe(
            asyncio.BaseProtocol, os.fdopen(desktop_out, "wb")
        )
        serving = asyncio.create_task(acp.serve(acp_native))
        desktop.write(b"x" * (65 * 1024) + b"\n")
        dropped = json.loads(await asyncio.wait_for(answers.readline(), 10))
        assert dropped["error"] == {"code": -32600, "message": "Invalid ACP message."}
        listing = {"jsonrpc": "2.0", "id": 1, "method": "session/list"}
        desktop.write(json.dumps(listing).encode() + b"\n")
        answer = json.loads(await asyncio.wait_for(answers.readline(), 10))
        assert (answer["id"], answer["error"]["message"]) == (1, "Initialize the GUI bridge first.")
        desktop.close()
        await asyncio.wait_for(serving, 10)

    asyncio.run(scenario())


def test_gui_answer_over_the_ceiling_fails_its_request_and_keeps_the_agent(
    acp_native, tmp_path, monkeypatch
):
    """A line over the ceiling is dropped unread, so the request it may have answered is told."""
    monkeypatch.setattr(acp, "MAX_MESSAGE", 9 * MEBIBYTE)
    answer = tmp_path / "page.json"
    monkeypatch.setenv("FAKE_PAGE", str(answer))

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            answer.write_text(json.dumps(history_page(session_id, 12)))
            with pytest.raises(SwitchError, match=UNREADABLE):
                # Bounded: a request must never wait for an answer that was dropped.
                await asyncio.wait_for(
                    bridge.dispatch("_cognition.ai/session/loadPage", {"sessionId": session_id}), 10
                )
            reply = await bridge.dispatch("session/prompt", prompt_params(session_id, "hello"))
            assert reply["stopReason"] == "end_turn"
            assert bridge.chats[session_id].backend is backend
            assert backend.process.poll() is None and not backend.writer.is_closing()

    asyncio.run(scenario())


def test_gui_update_over_the_ceiling_is_dropped_and_the_next_one_arrives(
    acp_native, tmp_path, monkeypatch
):
    """Nothing says what a dropped line was, so the turn's request is not left waiting either."""
    monkeypatch.setattr(acp, "MAX_MESSAGE", 9 * MEBIBYTE)
    said = tmp_path / "said.json"
    monkeypatch.setenv("FAKE_SAY_FILE", str(said))
    long = {"sessionUpdate": "user_message_chunk", "content": image_block(12 * MEBIBYTE)}
    said.write_text(json.dumps([long, agent_text("after the long one")]))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            with pytest.raises(SwitchError, match=UNREADABLE):
                await asyncio.wait_for(
                    bridge.dispatch("session/prompt", prompt_params(session_id, "say")), 10
                )
            await until(
                lambda: "after the long one" in replayed_texts(messages, "agent_message_chunk")
            )
            assert not replayed(messages, "user_message_chunk")
            reply = await bridge.dispatch("session/prompt", prompt_params(session_id, "hello"))
            assert reply["stopReason"] == "end_turn"
            assert bridge.chats[session_id].backend is backend
            assert backend.process.poll() is None and not backend.writer.is_closing()

    asyncio.run(scenario())


@pytest.mark.parametrize("turn", ["noise", "odd"])
def test_gui_line_that_is_no_usable_message_is_dropped_and_keeps_the_agent(
    acp_native, tmp_path, turn
):
    """`noise` prints a line that is not JSON; `odd` sends an update without its update."""

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            with pytest.raises(SwitchError, match=UNREADABLE):
                await asyncio.wait_for(
                    bridge.dispatch("session/prompt", prompt_params(session_id, turn)), 10
                )
            # The turn's reply came after that line, and still arrives.
            await until(lambda: len(replayed(messages, "agent_message_chunk")) == 1)
            reply = await bridge.dispatch("session/prompt", prompt_params(session_id, "hello"))
            assert reply["stopReason"] == "end_turn"
            assert len(replayed(messages, "agent_message_chunk")) == 2
            assert bridge.chats[session_id].backend is backend
            assert backend.process.poll() is None and not backend.writer.is_closing()

    asyncio.run(scenario())


def test_gui_native_exit_still_ends_the_connection_for_a_waiting_request(acp_native, tmp_path):
    """Only a stream that really ended closes a native's connection, with the message it had."""

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            with pytest.raises(SwitchError) as failure:
                await asyncio.wait_for(
                    bridge.dispatch("session/prompt", prompt_params(session_id, "crash")), 10
                )
            assert str(failure.value) == CLOSED
            await until(lambda: backend.process.poll() is not None)
            assert backend.process.poll() == 7 and backend.writer.is_closing()

    asyncio.run(scenario())


def test_read_line_takes_lines_of_any_length_and_skips_only_one_over_the_ceiling(monkeypatch):
    monkeypatch.setattr(acp, "MAX_MESSAGE", 100)

    async def scenario():
        # A buffer far shorter than the lines, so each is read in parts.
        reader = asyncio.StreamReader(limit=16)
        at_ceiling, over = b"a" * 99 + b"\n", b"b" * 100 + b"\n"
        reader.feed_data(b"short\n" + at_ceiling + over + b"after\n" + b"cut off by the end")
        reader.feed_eof()
        assert await acp.read_line(reader) == b"short\n"
        assert await acp.read_line(reader) == at_ceiling
        with pytest.raises(acp.LineTooLong):
            await acp.read_line(reader)
        assert await acp.read_line(reader) == b"after\n"
        assert await acp.read_line(reader) == b"cut off by the end"
        assert await acp.read_line(reader) == b""

        async def trickle(line):
            for start in range(0, len(line), 7):
                arriving.feed_data(line[start : start + 7])
                await asyncio.sleep(0)

        arriving = asyncio.StreamReader(limit=16)
        monkeypatch.setattr(acp, "MAX_MESSAGE", 5000)
        long = bytes(range(32, 127)) * 40 + b"\n"
        feeding = asyncio.create_task(trickle(long + b"next\n"))
        assert await acp.read_line(arriving) == long
        assert await acp.read_line(arriving) == b"next\n"
        await feeding

    asyncio.run(scenario())


async def wire_error(bridge, messages, method, params):
    """The error object Desktop receives for a request, as the bridge writes it."""
    identifier = f"probe-{len(messages)}"
    await bridge.handle({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
    (response,) = [message for message in messages if message.get("id") == identifier]
    return response["error"]


def holder_pid(store, run_id):
    return json.loads((store.root / "runtime" / f"{run_id}.json").read_text())["pid"]


def test_gui_other_window_leaves_a_busy_chat_alone(acp_native, tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "HANDOVER_TIMEOUT", 1)

    async def scenario():
        async with connect(acp_native, auto_respond=False) as first:
            session_id = await first.new(tmp_path)
            owner = next(
                run for run in acp_state.live(acp_native.store) if run["session_id"] == session_id
            )
            pending = asyncio.create_task(first.prompt(session_id, "permission"))
            request = await asyncio.wait_for(first.requests.get(), 5)
            async with in_process(acp_native) as (second, messages):
                load = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
                error = await wire_error(second, messages, "session/load", load)
                # Desktop shows its "open in another process (PID …)" state for session_locked.
                assert error["code"] == -32015
                assert "busy in another Devin window" in error["message"]
                assert error["data"] == {
                    "cognition.ai/errorKind": "session_locked",
                    "cognition.ai/retryable": True,
                    "cognition.ai/lockHolderPid": holder_pid(acp_native.store, owner["id"]),
                }
                assert session_id not in second.chats
            assert acp_state.lifecycle(acp_native.store, owner["id"]) == "open"
            response = {"outcome": {"outcome": "selected", "optionId": "reject"}}
            await first.send({"id": request["id"], "result": response})
            assert "result" in await pending
            assert first.agent_replies()[-1]["decision"]["result"] == response
            await asyncio.sleep(0.3)
            assert [run["id"] for run in acp_state.live(acp_native.store)] == [owner["id"]]

    asyncio.run(scenario())


def orphan_native(store, session_id, project):
    """A GUI run whose bridge has exited while its native still holds the run lock."""
    import subprocess as sp
    import uuid
    from dataclasses import asdict

    run = sessions.Run(uuid.uuid4().hex, "ansuman-1", str(project), "chat", 0.0, session_id)
    (store.root / "runs").mkdir(exist_ok=True)
    (store.root / "runs" / f"{run.id}.json").write_text(json.dumps(asdict(run)))
    (store.root / f"acp-controller-{run.id}.lock").touch()
    lock = store.root / f"run-{run.id}.lock"
    native = sp.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time; handle = open(sys.argv[1], 'a'); "
            "fcntl.flock(handle, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)",
            str(lock),
        ],
        stdout=sp.PIPE,
    )
    assert native.stdout.readline() == b"locked\n"
    sessions.record_process(store, run.id, native.pid, role="native")
    return run, native


@pytest.mark.parametrize("exits", [True, False])
def test_gui_reopen_waits_for_an_agent_orphaned_by_an_acp_reload(
    acp_native, tmp_path, monkeypatch, exits
):
    store = acp_native.store
    monkeypatch.setattr(acp, "ORPHAN_TIMEOUT", 3 if exits else 0.5)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            await bridge.dispatch("session/close", {"sessionId": session_id})
        run, native = orphan_native(store, session_id, tmp_path)
        try:
            assert acp_state.orphaned(store, run.id)
            async with in_process(acp_native) as (bridge, messages):
                load = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
                if exits:
                    asyncio.get_running_loop().call_later(0.5, native.kill)
                    await bridge.dispatch("session/load", load)
                    assert session_id in bridge.chats
                else:
                    error = await wire_error(bridge, messages, "session/load", load)
                    assert error["code"] == -32015 and "still shutting down" in error["message"]
                    assert error["data"]["cognition.ai/lockHolderPid"] == native.pid
                    assert native.poll() is None  # never force-killed
        finally:
            native.kill()
            native.wait()

    asyncio.run(scenario())


def test_gui_terminal_launch_of_the_same_chat_still_blocks_gui_load(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as first:
            session_id = await first.new(tmp_path)
            owner = next(
                run for run in acp_state.live(acp_native.store) if run["session_id"] == session_id
            )
            acp_state.lifecycle_path(acp_native.store, owner["id"]).unlink()
            async with in_process(acp_native) as (second, messages):
                load = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
                error = await wire_error(second, messages, "session/load", load)
                assert error["code"] == -32015
                assert "already has an open CLI launch" in error["message"]
                assert error["data"]["cognition.ai/errorKind"] == "session_locked"
                assert error["data"]["cognition.ai/lockHolderPid"] == holder_pid(
                    acp_native.store, owner["id"]
                )
            assert [run["id"] for run in acp_state.live(acp_native.store)] == [owner["id"]]

    asyncio.run(scenario())


def test_gui_concurrent_loads_of_one_chat_share_an_agent(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as first:
            session_id = await first.new(tmp_path)
        async with connect(acp_native) as client:
            setup = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            results = await asyncio.gather(
                client.request("session/load", setup), client.request("session/load", setup)
            )
            assert all("result" in result for result in results), results
            live = [
                run for run in acp_state.live(acp_native.store) if run["session_id"] == session_id
            ]
            assert len(live) == 1

    asyncio.run(scenario())


def test_gui_session_list_is_the_natives_own_listing_from_one_reused_agent(
    acp_native, tmp_path, monkeypatch
):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            params = {
                "cwd": str(tmp_path),
                "limit": 1,
                "_meta": {"cognition.ai/archivedStatus": "ALL"},
            }
            first = await bridge.dispatch("session/list", params)
            second = await bridge.dispatch("session/list", {})
            assert first["nextCursor"] == "fake-next" and "nextCursor" not in second
            assert [item["sessionId"] for item in first["sessions"]] == [created["sessionId"]]
            assert first["sessions"][0]["_meta"] == {"fake/row": True}
            lists = [call for call in logged(acp_native) if call["method"] == "session/list"]
            assert len({call["pid"] for call in lists}) == 1
            # Desktop polls session/list every 60 s; a late poll must still find the same native.
            lister = bridge.lister
            lister.last_activity -= 3 * 60
            await bridge.close_lister()
            assert bridge.lister is lister
            bridge.lister.last_activity -= acp.LISTER_IDLE
            await bridge.close_lister()
            assert bridge.lister is None
            await bridge.dispatch("session/list", {})
            lists = [call for call in logged(acp_native) if call["method"] == "session/list"]
            assert len({call["pid"] for call in lists}) == 2

    asyncio.run(scenario())


def test_gui_customize_requests_without_a_chat_manage_the_shared_mcp_servers(
    acp_native, tmp_path, monkeypatch
):
    shared = acp_native.store.root / "shared/config/mcp_config.json"
    gate = tmp_path / "connect-gate"
    monkeypatch.setenv("FAKE_MCP_CONNECT_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            await client.send({"method": "_cognition.ai/mcp/removeServer", "params": {}})
            stray = await client.request("_cognition.ai/mcp/listServers", {"sessionId": "other"})
            assert stray["error"]["code"] == -32602
            connecting = asyncio.create_task(
                client.request("_cognition.ai/mcp/connectServer", {"serverId": "docs"})
            )
            async with asyncio.timeout(10):
                while not any(
                    call["method"].endswith("connectServer") for call in logged(acp_native)
                ):
                    await asyncio.sleep(0.01)
            listing = await client.request("session/list", {})
            assert "result" in listing and not connecting.done()
            gate.touch()
            assert (await connecting)["result"] == {}
            server = {"command": "docs-mcp", "args": []}
            installed = await client.request(
                "_cognition.ai/mcp/installServer", {"serverId": "docs", "config": server}
            )
            assert installed["result"] == {}, installed
            assert json.loads(shared.read_text())["mcpServers"] == {"docs": server}
            assert {
                "jsonrpc": "2.0",
                "method": "_cognition.ai/mcp/serversChanged",
                "params": {},
            } in (client.messages)
            listed = await client.request(
                "_cognition.ai/mcp/listServers", {"workspaceDirs": [str(tmp_path)]}
            )
            assert [item["serverId"] for item in listed["result"]["servers"]] == ["docs"]
            mentioned = await client.request(
                "_cognition.ai/scope_search",
                {"scope_type": "mcpServer", "query": "do", "cwd": str(tmp_path)},
            )
            assert [item["name"] for item in mentioned["result"]["items"]] == ["docs"]
            removed = await client.request(
                "_cognition.ai/mcp/removeServer",
                {"serverId": "docs", "sourcePath": listed["result"]["servers"][0]["sourcePath"]},
            )
            assert removed["result"] == {}, removed
            assert json.loads(shared.read_text())["mcpServers"] == {}
            missing = await client.request("session/set_mode", {"modeId": "plan"})
            assert missing["error"]["code"] == -32602
        calls = [call for call in logged(acp_native) if call["method"].startswith("_cognition.ai/")]
        assert [call["method"].rsplit("/", 1)[1] for call in calls] == [
            "connectServer",
            "installServer",
            "listServers",
            "scope_search",
            "removeServer",
        ]
        assert {(call["pid"], call["account"], call["sessionId"]) for call in calls} == {
            (calls[0]["pid"], "ansuman-1", None)
        }

    asyncio.run(scenario())


async def warm(bridge):
    async with asyncio.timeout(10):
        while bridge.filling or bridge.spare is None:
            await asyncio.sleep(0.01)
    return bridge.spare


def test_gui_chats_open_on_a_warm_logged_in_agent(acp_native, tmp_path):
    store = acp_native.store

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            spare = await warm(bridge)
            assert spare.account.name == "ansuman-1"
            assert not any(run["kind"] == "chat" for run in sessions.runs(store) if run["active"])
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            assert bridge.chats[session_id].backend is spare
            # Without project MCP config any folder can use it, so it stays in the home folder.
            assert native_folder(acp_native, spare) == Path.home().resolve()
            run = next(run for run in sessions.runs(store) if run["id"] == spare.lease.run.id)
            assert (run["kind"], run["project"], run["session_id"]) == (
                "chat",
                str(tmp_path),
                session_id,
            )
            replacement = await warm(bridge)
            assert replacement is not spare
            await bridge.dispatch("session/close", {"sessionId": session_id})
            loaded = await bridge.dispatch(
                "session/load", {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            )
            assert "configOptions" in loaded
            assert bridge.chats[session_id].backend is replacement
            calls = logged(acp_native)
            loads = [call for call in calls if call["method"] == "session/load"]
            assert [call["pid"] for call in loads] == [replacement.process.pid]
            # Another login's chat starts its own agent and leaves the spare for the default.
            other = await warm(bridge)
            acp_state.bind(store, session_id, store.account("ansuman-2"))
            await bridge.dispatch("session/close", {"sessionId": session_id})
            await bridge.dispatch(
                "session/load", {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            )
            assert bridge.chats[session_id].backend.account.name == "ansuman-2"
            assert bridge.spare is other and other.process.poll() is None
            other.last_activity -= acp.SPARE_IDLE
            await bridge.drop_spare()
            assert bridge.spare is None and other.process.poll() is not None

    asyncio.run(scenario())


def native_folder(native, backend):
    """The folder a native process runs in (it reads project MCP servers from there)."""
    calls = [call for call in logged(native) if call["pid"] == backend.process.pid]
    return Path(calls[-1]["process_cwd"]).resolve()


@pytest.mark.parametrize("config", [".mcp.json", ".devin/mcp_config.json", ".cursor/mcp.json"])
def test_gui_warm_agent_serves_folders_without_project_mcp(acp_native, tmp_path, config):
    """Chats in ordinary folders keep the warm spare; it never runs in a project with MCP config."""

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            spare = await warm(bridge)
            plain = (tmp_path / "plain").resolve()
            plain.mkdir()
            created = await bridge.dispatch("session/new", {"cwd": str(plain), "mcpServers": []})
            assert bridge.chats[created["sessionId"]].backend is spare
            project = (tmp_path / "project").resolve()
            (project / config).parent.mkdir(parents=True)
            (project / config).write_text('{"mcpServers": {}}')
            replacement = await warm(bridge)
            created = await bridge.dispatch("session/new", {"cwd": str(project), "mcpServers": []})
            chat = bridge.chats[created["sessionId"]].backend
            assert chat is not replacement and native_folder(acp_native, chat) == project
            # Back in an ordinary folder: a spare from the MCP project would add its servers.
            moved = await warm(bridge)
            assert native_folder(acp_native, moved) == project
            created = await bridge.dispatch("session/new", {"cwd": str(plain), "mcpServers": []})
            assert bridge.chats[created["sessionId"]].backend is not moved
            assert native_folder(acp_native, await warm(bridge)) == Path.home().resolve()

    asyncio.run(scenario())


def test_gui_warm_agent_follows_the_chat_folder(acp_native, tmp_path):
    """The native reads a project's own MCP servers from its process folder, not the chat's."""

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            await bridge.dispatch("session/close", {"sessionId": session_id})
            spare = await warm(bridge)
            other = (tmp_path / "other").resolve()
            other.mkdir()
            (other / ".mcp.json").write_text('{"mcpServers": {}}')
            await bridge.dispatch(
                "session/load", {"sessionId": session_id, "cwd": str(other), "mcpServers": []}
            )
            chat = bridge.chats[session_id]
            # A spare in another folder would miss this project's MCP servers: start fresh here.
            assert chat.backend is not spare and spare.process.poll() is not None
            assert native_folder(acp_native, chat.backend) == other
            assert chat.backend.lease.run.project == str(other)
            moved = await warm(bridge)
            assert native_folder(acp_native, moved) == other
            nearby = await bridge.dispatch("session/new", {"cwd": str(other), "mcpServers": []})
            assert bridge.chats[nearby["sessionId"]].backend is moved

    asyncio.run(scenario())


def test_gui_warm_agent_still_refuses_a_chat_missing_from_history(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            spare = await warm(bridge)
            params = {"sessionId": "never-saved", "cwd": str(tmp_path), "mcpServers": []}
            error = await wire_error(bridge, messages, "session/load", params)
            # Desktop drops a deleted chat quietly only when it sees session_not_found.
            assert error["code"] == -32016 and "no longer in shared history" in error["message"]
            assert error["data"] == {
                "cognition.ai/errorKind": "session_not_found",
                "cognition.ai/retryable": False,
            }
            assert spare.process.poll() is not None
            assert not any(call["method"] == "session/load" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_opens_chats_whose_project_folder_is_gone(acp_native, tmp_path):
    async def scenario():
        gone = tmp_path / "deleted-worktree"
        gone.mkdir()
        async with in_process(acp_native) as (bridge, _):
            store = acp_native.store
            params = {"cwd": str(gone), "mcpServers": []}
            session_id = (await bridge.dispatch("session/new", params))["sessionId"]
            cold = (await bridge.dispatch("session/new", params))["sessionId"]
            for chat in (session_id, cold):
                await bridge.dispatch("session/close", {"sessionId": chat})
            gone.rmdir()
            # Another login's chat starts its own agent instead of the warm spare.
            acp_state.bind(store, cold, store.account("ansuman-2"))
            for chat in (session_id, cold):
                await bridge.dispatch("session/load", {**params, "sessionId": chat})
                assert bridge.chats[chat].backend.process.poll() is None
            assert bridge.chats[cold].backend.account.name == "ansuman-2"
            assert acp_state.check_saved(store, session_id)["id"] == session_id
            acp_state.check_handoff(
                store, session_id, bridge.chats[session_id].backend.lease.run.id
            )
            created = await bridge.dispatch(
                "session/new",
                {**params, "_meta": {"cognition.ai/worktreeFor": str(tmp_path)}},
            )
            assert bridge.chats[created["sessionId"]].setup["cwd"] == str(gone)

    asyncio.run(scenario())


def test_gui_reopens_an_open_chat_from_another_folder(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = (
                await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            )["sessionId"]
            backend = bridge.chats[session_id].backend
            other = tmp_path / "other"
            other.mkdir()
            await bridge.dispatch(
                "session/load", {"sessionId": session_id, "cwd": str(other), "mcpServers": []}
            )
            assert bridge.chats[session_id].backend is backend
            assert bridge.chats[session_id].setup["cwd"] == str(other.resolve())

    asyncio.run(scenario())


@pytest.mark.parametrize("cwd", ["relative/project", None])
def test_gui_still_requires_an_absolute_project_path(acp_native, cwd):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            with pytest.raises(SwitchError, match="absolute project"):
                await bridge.dispatch("session/new", {"cwd": cwd, "mcpServers": []})

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
async def in_process(native, capabilities=None):
    messages = []

    async def emit(message):
        messages.append(message)

    bridge = acp.Bridge(native, emit)
    try:
        await bridge.dispatch(
            "initialize", {"protocolVersion": 1, "clientCapabilities": capabilities or {}}
        )
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
        assert force and not choose_best_holds_store_lock()
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


async def wait_for_text(client, text):
    async with asyncio.timeout(10):
        while text not in client.texts():
            await asyncio.sleep(0.02)


def replayed(messages, kind):
    return [
        message["params"]["update"]
        for message in messages
        if message.get("method") == "session/update"
        and message["params"]["update"].get("sessionUpdate") == kind
    ]


def replayed_texts(messages, kind):
    return [update["content"]["text"] for update in replayed(messages, kind)]


DOCUMENT = "_cognition.ai/document/"


def context_calls(native, pid):
    return [
        (call["method"].removeprefix("_cognition.ai/"), call["uri"])
        for call in logged(native)
        if call["pid"] == pid
        and call["method"].startswith(("_cognition.ai/doc", "_cognition.ai/w"))
    ]


def test_gui_editor_and_terminal_context_reach_every_open_chat(acp_native, tmp_path):
    """Desktop sends editor and terminal context without a sessionId, like the built-in agent."""

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chats = [
                (await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[
                    "sessionId"
                ]
                for _ in range(2)
            ]
            backends = [bridge.chats[chat].backend for chat in chats]
            idle = [backend.last_activity for backend in backends]
            for method, params in [
                (DOCUMENT + "didFocus", {"uri": "file:///a.py", "cursorLine": 3}),
                ("_cognition.ai/terminal/commandStarted", {"commandLine": "ls", "terminalId": "t"}),
            ]:
                assert await bridge.dispatch(method, params, notification=True) == {}
            await asyncio.sleep(0.3)
            for backend in backends:
                seen = [
                    call["method"]
                    for call in logged(acp_native)
                    if call["pid"] == backend.process.pid
                ]
                assert DOCUMENT + "didFocus" in seen
                assert "_cognition.ai/terminal/commandStarted" in seen
            # Editor context is not chat activity: it must not keep an idle chat open.
            assert [backend.last_activity for backend in backends] == idle

    asyncio.run(scenario())


@pytest.mark.parametrize("native_supports", [True, False])
def test_gui_advertises_extra_folders_only_when_the_native_does(
    acp_native, monkeypatch, native_supports
):
    """Desktop drops additionalDirectories from session/new unless the agent advertises them."""
    if not native_supports:
        monkeypatch.setenv("FAKE_NO_EXTRA_DIRS", "1")

    async def scenario():
        async def emit(message):
            pass

        bridge = acp.Bridge(acp_native, emit)
        try:
            result = await bridge.dispatch(
                "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
            )
        finally:
            await bridge.close()
        expected = {"list": {}, "resume": {}, "close": {}}
        if native_supports:
            expected["additionalDirectories"] = {}
        assert result["agentCapabilities"]["sessionCapabilities"] == expected

    asyncio.run(scenario())


def test_gui_new_chat_native_gets_the_current_editor_state(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            for method, uri in [
                ("didFocus", "file:///a.py"),
                ("didOpen", "file:///a.py"),
                ("didOpen", "file:///b.py"),
                ("didFocus", "file:///a.py"),
                ("didClose", "file:///a.py"),
                ("didChangeDirty", "file:///b.py"),
                ("didFocus", "file:///b.py"),
            ]:
                await bridge.dispatch(DOCUMENT + method, {"uri": uri}, notification=True)
            await bridge.dispatch(
                "_cognition.ai/workspace/didChangeRevision",
                {"uri": "file:///repo", "branch": "main"},
                notification=True,
            )
            await bridge.dispatch(
                "_cognition.ai/workspace/didChangeFiles", {"created": ["x"]}, notification=True
            )
            await bridge.drop_spare()
            session_id = (
                await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            )["sessionId"]
            pid = bridge.chats[session_id].backend.process.pid
            assert context_calls(acp_native, pid) == [
                ("document/didOpen", "file:///b.py"),
                ("document/didFocus", "file:///b.py"),
                ("workspace/didChangeRevision", "file:///repo"),
            ]
            first = [call["method"] for call in logged(acp_native) if call["pid"] == pid]
            assert first.index("initialize") < first.index(DOCUMENT + "didOpen")
            assert first.index(DOCUMENT + "didOpen") < first.index("session/new")

    asyncio.run(scenario())


def test_gui_mid_turn_prompt_reaches_the_native_during_the_turn(acp_native, tmp_path):
    """The native advertises midTurnPrompts, so Desktop sends steering while a turn runs."""

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            steer = await asyncio.wait_for(client.prompt(session_id, "steer now"), 5)
            assert steer["result"]["stopReason"] == "end_turn"
            assert not turn.done()
            prompts = [call["text"] for call in logged(acp_native) if call["text"]]
            assert prompts == ["busy", "steer now"]

            start = len(client.messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            await asyncio.wait_for(client.request("session/load", params), 5)
            replay = client.messages[start:]
            assert replayed_texts(replay, "user_message_chunk") == ["busy", "steer now"]

            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"

    asyncio.run(scenario())


def test_gui_stop_with_steering_pending_ends_the_turn(acp_native, tmp_path):
    """The native survives a Stop with steering pending, and Desktop greys out Stop meanwhile."""

    def cancels():
        return [call for call in logged(acp_native) if call["method"] == "session/cancel"]

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            cancel = {"method": "session/cancel", "params": {"sessionId": session_id}}
            plain = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            await client.send(cancel)
            assert (await asyncio.wait_for(plain, 5))["result"]["stopReason"] == "cancelled"
            await asyncio.sleep(acp.CANCEL_RETRY_DELAY * 2)
            assert len(cancels()) == 1

            turn = asyncio.create_task(client.prompt(session_id, "foreground"))
            await asyncio.sleep(0.5)
            steer = asyncio.create_task(client.prompt(session_id, "steer: whats the update"))
            while "steer: whats the update" not in [call["text"] for call in logged(acp_native)]:
                await asyncio.sleep(0.05)
            await client.send(cancel)
            assert (await asyncio.wait_for(turn, 10))["result"]["stopReason"] == "cancelled"
            assert (await asyncio.wait_for(steer, 5))["result"]["stopReason"] == "cancelled"
            assert "checking on subagent" in client.texts()
            sent = len(cancels())
            assert sent > 2
            await asyncio.sleep(acp.CANCEL_RETRY_DELAY * 2)
            assert len(cancels()) == sent

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/switch-status", "!ds switch", "/login"])
def test_gui_mid_turn_local_command_waits_for_the_turn(acp_native, tmp_path, text):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            local = asyncio.create_task(client.prompt(session_id, text))
            await asyncio.sleep(0.5)
            assert not local.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            await turn
            await asyncio.wait_for(local, 10)
            assert text not in [call["text"] for call in logged(acp_native)]

    asyncio.run(scenario())


def test_gui_prompt_after_cancel_starts_a_new_turn(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native, auto_respond=False) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "permission"))
            request = await asyncio.wait_for(client.requests.get(), 5)
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            follow_up = asyncio.create_task(client.prompt(session_id, "after cancel"))
            await asyncio.sleep(0.5)
            assert "after cancel" not in [call["text"] for call in logged(acp_native)]
            await client.send(
                {
                    "id": request["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": "reject"}},
                }
            )
            await turn
            assert "result" in await asyncio.wait_for(follow_up, 10)
            assert "after cancel" in [call["text"] for call in logged(acp_native)]

    asyncio.run(scenario())


async def started(request):
    """A request's task, run up to its first wait (the chat's gate, say): it has arrived."""
    task = asyncio.create_task(request)
    await asyncio.sleep(0)
    return task


async def stop_before_the_turn_opens(native, bridge, chat, held, proceed):
    """Stop a prompt held before its turn opens: the native gets the prompt, then the Stop.

    Desktop greys out Stop until the prompt answers, so this Stop cannot be repeated. Like the
    native, the fake "foreground" turn ignores a Stop sent before its prompt.
    """
    session_id = chat.session_id
    cancel = {"method": "session/cancel", "params": {"sessionId": session_id}}
    turn = await started(bridge.dispatch("session/prompt", prompt_params(session_id, "foreground")))
    await asyncio.wait_for(held.wait(), 10)
    await bridge.dispatch(cancel["method"], cancel["params"], notification=True)
    proceed.set()
    try:
        answer = await asyncio.wait_for(turn, 5)
    except TimeoutError:
        # The fake agent exits only once its open turn is stopped.
        await chat.backend.send(cancel)
        answer = "still running"
    assert answer == {"stopReason": "cancelled"}
    assert chat.controls is None
    following = bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
    assert await asyncio.wait_for(following, 10) == {"stopReason": "end_turn"}
    received = [
        (call["method"], call["sessionId"], call["text"])
        for call in logged(native)
        if call["pid"] == chat.backend.process.pid
        and call["method"] in {"session/prompt", "session/cancel"}
    ]
    typed = received.index(("session/prompt", session_id, "foreground"))
    *stops, last = received[typed + 1 :]
    assert stops and set(stops) == {("session/cancel", session_id, None)}
    assert last == ("session/prompt", session_id, "next")


async def released_chat_with_a_held_load(bridge, tmp_path, monkeypatch):
    """An idle-released chat whose reconnect waits in its native load until told to go on."""
    created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    chat = bridge.chats[created["sessionId"]]
    chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
    assert await bridge.release_idle(chat)
    assert chat.suspended
    request = acp.Backend.request
    loading, loaded = asyncio.Event(), asyncio.Event()

    async def slow_load(backend, method, params, timeout=None):
        if method == "session/load":
            loading.set()
            await loaded.wait()
        return await request(backend, method, params, timeout)

    monkeypatch.setattr(acp.Backend, "request", slow_load)
    return chat, loading, loaded


def test_gui_stop_while_reconnecting_a_released_chat_cancels_its_prompt(
    acp_native, tmp_path, monkeypatch
):
    """A released chat takes seconds to reconnect for its next message: time to press Stop."""
    monkeypatch.setattr(acp, "CANCEL_RETRY_DELAY", 0.05)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat, loading, loaded = await released_chat_with_a_held_load(
                bridge, tmp_path, monkeypatch
            )
            await stop_before_the_turn_opens(acp_native, bridge, chat, loading, loaded)

    asyncio.run(scenario())


def test_gui_stop_while_a_prompt_waits_for_a_reconnecting_chat_cancels_it(
    acp_native, tmp_path, monkeypatch
):
    """A sidebar rename reconnects a released chat too, and the next message waits behind it."""
    monkeypatch.setattr(acp, "CANCEL_RETRY_DELAY", 0.05)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat, loading, loaded = await released_chat_with_a_held_load(
                bridge, tmp_path, monkeypatch
            )
            rename = asyncio.create_task(
                bridge.dispatch(
                    "_cognition.ai/session/rename", {"sessionId": chat.session_id, "title": "New"}
                )
            )
            await asyncio.wait_for(loading.wait(), 10)
            await stop_before_the_turn_opens(acp_native, bridge, chat, loading, loaded)
            await asyncio.wait_for(rename, 5)

    asyncio.run(scenario())


def test_gui_stop_while_a_prompt_waits_to_open_its_turn_cancels_it(
    acp_native, tmp_path, monkeypatch
):
    """A connected chat reads the closed subagents note before its prompt opens the turn."""
    monkeypatch.setattr(acp, "CANCEL_RETRY_DELAY", 0.05)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[created["sessionId"]]
            closed_note = bridge.closed_note_params
            reading, read = asyncio.Event(), asyncio.Event()

            async def slow_note(chat, params):
                reading.set()
                await read.wait()
                return await closed_note(chat, params)

            monkeypatch.setattr(bridge, "closed_note_params", slow_note)
            await stop_before_the_turn_opens(acp_native, bridge, chat, reading, read)

    asyncio.run(scenario())


def test_gui_stop_for_an_open_turn_spares_a_command_waiting_behind_it(acp_native, tmp_path):
    """Typed during a turn, /switch-continue waits for it to end: that Stop is for the turn."""

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "foreground"))
            )
            await until(lambda: "live progress" in texts(messages))
            command = await started(
                bridge.dispatch(
                    "session/prompt", prompt_params(session_id, "/switch-continue ansuman-2")
                )
            )
            await bridge.dispatch("session/cancel", {"sessionId": session_id}, notification=True)
            assert await asyncio.wait_for(turn, 5) == {"stopReason": "cancelled"}
            assert await asyncio.wait_for(command, 10) == {"stopReason": "end_turn"}
            assert prompts(acp_native)[-1] == ("ansuman-2", "continue")

    asyncio.run(scenario())


def test_gui_startup_and_listing_skip_the_login_check(acp_native, tmp_path, monkeypatch):
    """A window reload waits for initialize; the login check can take 10 s on a slow network."""
    checked = []

    def require_login(self, account, **options):
        checked.append(account.name)
        raise SwitchError(f"No saved login for {account.name!r}. Run: ds login {account.name}")

    monkeypatch.setattr(Native, "require_login", require_login)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            listing = await bridge.dispatch("session/list", {"cwd": str(tmp_path)})
            assert "sessions" in listing
            async with asyncio.timeout(10):
                while bridge.filling:
                    await asyncio.sleep(0.02)
            assert bridge.spare is None
            with pytest.raises(SwitchError, match="No saved login"):
                await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert checked and set(checked) == {"ansuman-1"}

    asyncio.run(scenario())


def test_gui_bridge_start_clears_old_launch_records_in_the_background(acp_native, tmp_path):
    import uuid
    from dataclasses import asdict

    store = acp_native.store
    old = sessions.Run(uuid.uuid4().hex, "ansuman-1", str(tmp_path), "chat", 1.0, None, 2.0)
    (store.root / "runs").mkdir(exist_ok=True)
    record = store.root / "runs" / f"{old.id}.json"
    record.write_text(json.dumps(asdict(old)))

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            async with asyncio.timeout(5):
                while record.exists():
                    await asyncio.sleep(0.02)
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            assert created["sessionId"] in bridge.chats

    asyncio.run(scenario())


def test_gui_customize_mcp_changes_reach_open_chats_once(acp_native, tmp_path, monkeypatch):
    """Each chat native runs its own MCP servers; the built-in agent has one per window."""
    gate = tmp_path / "connect-gate"
    gate.touch()
    monkeypatch.setenv("FAKE_MCP_CONNECT_GATE", str(gate))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[created["sessionId"]].backend
            server = {"command": "docs-mcp", "args": []}
            await bridge.dispatch(
                "_cognition.ai/mcp/installServer", {"serverId": "docs", "config": server}
            )
            listed = await bridge.dispatch("_cognition.ai/mcp/listServers", {})
            source = listed["servers"][0]["sourcePath"]
            changed = len([m for m in messages if m.get("method", "").endswith("serversChanged")])
            for _ in range(2):
                await bridge.dispatch(
                    "_cognition.ai/mcp/toggleServer", {"serverId": "docs", "sourcePath": source}
                )
            await bridge.dispatch(
                "_cognition.ai/mcp/removeServer", {"serverId": "docs", "sourcePath": source}
            )

            def followed():
                return [
                    call["method"].rsplit("/", 1)[1]
                    for call in logged(acp_native)
                    if call["pid"] == chat.process.pid and "/mcp/" in call["method"]
                ]

            async with asyncio.timeout(5):
                while len(followed()) < 4:
                    await asyncio.sleep(0.02)
            # Install connects, disable disconnects, enable connects, remove disconnects.
            assert followed() == [
                "connectServer",
                "disconnectServer",
                "connectServer",
                "disconnectServer",
            ]
            await asyncio.sleep(0.2)
            notices = [m for m in messages if m.get("method", "").endswith("serversChanged")]
            # Only the listing native's notices reach Desktop: one per Customize change.
            assert len(notices) - changed == 3

    asyncio.run(scenario())


def cancelled_cards(messages):
    return [
        message["params"]["requestId"]
        for message in messages
        if message.get("method") == "$/cancel_request"
    ]


def test_gui_native_withdrawn_request_clears_the_desktop_card(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            await bridge.dispatch(
                "session/prompt", prompt_params(created["sessionId"], "withdrawn")
            )
            (card,) = [m for m in messages if m.get("method") == "elicitation/create"]
            assert card["id"].startswith("ds-client-")
            # Desktop only knows its own id for the card; the native's 101 would be ignored.
            assert cancelled_cards(messages) == [card["id"]]
            assert bridge.client_requests == {}

    asyncio.run(scenario())


def test_gui_native_abandoned_question_clears_only_its_desktop_card(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            abandoned, asked = [
                (await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[
                    "sessionId"
                ]
                for _ in range(2)
            ]

            def cards(method):
                return {
                    message["params"]["sessionId"]: message["id"]
                    for message in messages
                    if message.get("method") == method
                }

            # Another chat's question, for a tool call with the same id, stays open throughout.
            turns = [
                asyncio.create_task(
                    bridge.dispatch("session/prompt", prompt_params(asked, "question"))
                )
            ]
            async with asyncio.timeout(5):
                while asked not in cards("elicitation/create"):
                    await asyncio.sleep(0.02)
            turns.append(
                asyncio.create_task(
                    bridge.dispatch("session/prompt", prompt_params(abandoned, "abandoned"))
                )
            )
            async with asyncio.timeout(5):
                while not cancelled_cards(messages):
                    await asyncio.sleep(0.02)
            questions, approvals = cards("elicitation/create"), cards("session/request_permission")
            # Desktop shows "awaiting instructions" for as long as a question card is unanswered.
            assert cancelled_cards(messages) == [questions[abandoned]]
            (failed,) = [
                message
                for message in messages
                if message.get("params", {}).get("update", {}).get("status") == "failed"
            ]
            assert messages.index(failed) < messages.index(acp.cancel_request(questions[abandoned]))
            # That chat's approval and the other chat's question are still the user's to answer.
            assert set(bridge.client_requests) == {approvals[abandoned], questions[asked]}
            for card, result in (
                (approvals[abandoned], {"outcome": {"outcome": "selected", "optionId": "allow"}}),
                (questions[asked], {"action": "accept", "content": {"approach": "one"}}),
            ):
                await bridge.handle({"jsonrpc": "2.0", "id": card, "result": result})
            await asyncio.wait_for(asyncio.gather(*turns), 5)
            assert cancelled_cards(messages) == [questions[abandoned]]
            assert bridge.client_requests == {}

    asyncio.run(scenario())


def test_gui_native_exit_clears_its_open_desktop_cards(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            backend = bridge.chats[created["sessionId"]].backend
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(created["sessionId"], "permission"))
            )
            async with asyncio.timeout(5):
                while not bridge.client_requests:
                    await asyncio.sleep(0.02)
            (card,) = bridge.client_requests
            backend.process.kill()
            with pytest.raises(SwitchError):
                await turn
            async with asyncio.timeout(5):
                while card not in cancelled_cards(messages):
                    await asyncio.sleep(0.02)
            assert bridge.client_requests == {}

    asyncio.run(scenario())


def test_gui_chats_check_the_saved_login_without_the_network(acp_native, tmp_path, monkeypatch):
    """`devin auth status` took 1-10 s per chat (30 s offline); the built-in agent never runs it."""
    capture = Native.capture
    checked = []

    def offline(self, account, arguments, **options):
        if arguments == ("auth", "status"):
            checked.append(account.name)
            raise SwitchError("network unreachable")
        return capture(self, account, arguments, **options)

    monkeypatch.setattr(Native, "capture", offline)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            assert created["sessionId"] in bridge.chats
            acp_native.store.credentials(acp_native.store.account("ansuman-2")).unlink()
            acp_state.bind(
                acp_native.store, created["sessionId"], acp_native.store.account("ansuman-2")
            )
            await bridge.dispatch("session/close", {"sessionId": created["sessionId"]})
            with pytest.raises(SwitchError, match="No saved login for 'ansuman-2'"):
                await bridge.dispatch(
                    "session/load",
                    {"sessionId": created["sessionId"], "cwd": str(tmp_path), "mcpServers": []},
                )
        assert checked == []

    asyncio.run(scenario())


def test_gui_reload_mid_turn_replays_without_interrupting_the_turn(acp_native, tmp_path):
    """Switching Desktop surfaces reloads the open chat; a native load would end the turn."""

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": "plan"},
            )
            await client.prompt(session_id, "hello")
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}

            start = len(client.messages)
            loaded = await asyncio.wait_for(client.request("session/load", params), 5)
            replay = client.messages[start:]
            assert "sessionId" not in loaded["result"]
            options = {
                item["id"]: item["currentValue"] for item in loaded["result"]["configOptions"]
            }
            assert options == {"mode": "plan", "model": "test-model"}
            assert replayed_texts(replay, "user_message_chunk") == ["hello", "busy"]
            agent = replayed_texts(replay, "agent_message_chunk")
            assert len(agent) == 2
            assert json.loads(agent[0])["settings"]["mode"] == "plan"
            assert agent[1] == "live progress"
            (commands,) = replayed(replay, "available_commands_update")
            assert "switch" in {item["name"] for item in commands["availableCommands"]}

            start = len(client.messages)
            resumed = await asyncio.wait_for(client.request("session/resume", params), 5)
            assert "result" in resumed
            assert not any(
                message.get("method") == "session/update" for message in client.messages[start:]
            )

            assert not turn.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"
            assert not any(call["method"] == "session/load" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_reload_mid_turn_is_the_natives_own_when_it_loads_under_a_turn(
    acp_native, tmp_path, monkeypatch
):
    """Desktop 3.14 reads row ids and message ids off a load: only the native has them."""
    monkeypatch.setenv("FAKE_PROCESS_MODEL", "1")

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "hello")
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            tail = {"cognition.ai/replayFrom": {"type": "_cognition.ai/tail"}}
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}

            start = len(client.messages)
            loaded = await asyncio.wait_for(
                client.request("session/load", {**params, "_meta": tail}), 5
            )
            assert "result" in loaded and not turn.done()
            assert replayed_texts(client.messages[start:], "agent_message_chunk") == [
                "historical replay"
            ]
            (load,) = [call for call in logged(acp_native) if call["method"] == "session/load"]
            (busy,) = [call for call in logged(acp_native) if call["text"] == "busy"]
            assert load["pid"] == busy["pid"]

            # A resume shows nothing again, so it still never reaches the native.
            resumed = await asyncio.wait_for(client.request("session/resume", params), 5)
            assert "result" in resumed and not turn.done()
            assert len([c for c in logged(acp_native) if c["method"] == "session/load"]) == 1
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"

    asyncio.run(scenario())


def test_gui_load_under_a_turn_tells_a_live_subagent_end_from_replayed_ones(
    acp_native, tmp_path, monkeypatch
):
    for switch in ("PROCESS_MODEL", "FOREGROUND_SUBAGENT", "REPLAY_SUBAGENT", "FINISH_IN_LOAD"):
        monkeypatch.setenv(f"FAKE_{switch}", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "foreground"))
            )
            await until(lambda: backend.running)
            (worker,) = backend.running
            await bridge.dispatch(
                "session/load", {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            )
            rows = bridge.roster.rows[session_id]
            # The saved chat's old subagent is history; the working one really finished.
            assert {agent: row["status"] for agent, row in rows.items()} == {worker: "done"}
            assert not backend.running and not turn.done()
            await bridge.dispatch("session/cancel", {"sessionId": session_id}, notification=True)
            assert (await turn)["stopReason"] == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("reopen", ["reload", "switch"])
def test_gui_reload_mid_turn_replays_from_the_latest_native_load(acp_native, tmp_path, reopen):
    """The native's own replay replaces what came before, including one Desktop never saw."""

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "hello")
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            if reopen == "reload":
                assert "result" in await client.request("session/load", params)
            else:
                await client.prompt(session_id, "/switch ansuman-2")
                assert acp_native.store.selected().name == "ansuman-2"
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")

            start = len(client.messages)
            await asyncio.wait_for(client.request("session/load", params), 5)
            replay = client.messages[start:]
            assert replayed_texts(replay, "user_message_chunk") == ["busy"]
            assert replayed_texts(replay, "agent_message_chunk") == [
                "historical replay",
                "live progress",
            ]

            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"

    asyncio.run(scenario())


EVENT = "cognition.ai/eventId"
STREAMING = "cognition.ai/streamingMessageId"
LOAD_PAGE = "_cognition.ai/session/loadPage"
SAVED_AT = "2026-11-19T09:30:00.000Z"
README = '<ref_file file="/work/app/README.md" />'
LINKS = f'See {README} and <ref_snippet file="/work/app/main.py" lines="3-9" />.'


def saved(kind, text, event=None):
    """One message as CLI 3000.14 replays it: whole, and named by its saved event if it has one."""
    meta = {EVENT: event, "timestamp": SAVED_AT} if event else {"timestamp": SAVED_AT}
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}, "_meta": meta}


def saved_chat(shown=False):
    """A saved chat's rows as the native replays them, or as Desktop is `shown` them.

    Desktop 3.14 takes an agent message that names an event for a cloud message: a click on its
    file link waits for a snippet that nothing sends in a local chat. So the agent's messages
    reach Desktop without that name, and everything else as the native sent it.
    """
    read = {"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "Read README.md"}
    return [
        saved("user_message_chunk", f"What is in {README}?", "23934"),
        saved("agent_message_chunk", "Let me read it.", None if shown else "23935"),
        saved("agent_thought_chunk", f"{README} is short.", "23936"),
        {**read, "status": "completed", "_meta": {EVENT: "23936", "timestamp": SAVED_AT}},
        saved("agent_message_chunk", LINKS, None if shown else "23937"),
    ]


def saved_page(session_id, shown=False):
    """An answer to `loadPage` that holds those rows, with a page's own fields around them."""
    return {
        "updates": [{"sessionId": session_id, "update": row} for row in saved_chat(shown)],
        "coveredRange": {"startRowId": 120, "endRowId": 125},
        "nextEndRowId": 120,
        "exhausted": False,
        "replayFilterVersion": "1",
    }


def live_reply(text):
    """One chunk of a reply as CLI 3000.14 streams it: named by its message, never by an event."""
    return {**agent_text(text), "_meta": {STREAMING: "0b6f1c1e", "timestamp": SAVED_AT}}


def rows(messages):
    """The chat's rows among what Desktop was sent, in order (its settings come beside them)."""
    return [
        message["params"]["update"]
        for message in messages
        if message.get("method") == "session/update"
        and message["params"]["update"].get("sessionUpdate") not in acp.BACKGROUND_UPDATES
    ]


def test_gui_replayed_reply_reaches_desktop_without_its_event_id(acp_native, tmp_path, monkeypatch):
    """A file link in a reply opened its file only until the chat was loaded again."""
    monkeypatch.setenv("FAKE_REPLAY_SAY", json.dumps(saved_chat()))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            start = len(client.messages)
            assert "result" in await client.request("session/load", params)
            _, question, first, thought, tool, reply = rows(client.messages[start:])
            # The reply's text and time are the native's; only the event's name is gone.
            assert reply == saved("agent_message_chunk", LINKS)
            assert first == saved("agent_message_chunk", "Let me read it.")
            # Desktop shows a user message's tags as text and knows it by this id elsewhere,
            # and thoughts and tool calls were not checked: all three stay as sent.
            assert [question, thought, tool] == [saved_chat()[0], *saved_chat()[2:4]]
            assert all(EVENT in row["_meta"] for row in (question, thought, tool))

    asyncio.run(scenario())


def test_gui_live_reply_reaches_desktop_exactly_as_sent(acp_native, tmp_path, monkeypatch):
    """A live reply names no event, so nothing in it changes: not a key, not their order."""
    live = live_reply(LINKS)
    monkeypatch.setenv("FAKE_SAY", json.dumps([live]))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            assert (await client.prompt(session_id, "say"))["result"]["stopReason"] == "end_turn"
            (sent,) = rows(client.messages)
            assert json.dumps(sent) == json.dumps(live)

    asyncio.run(scenario())


@pytest.mark.parametrize("reopen", ["reload", "switch"])
def test_gui_reload_mid_turn_replays_replies_without_event_ids(
    acp_native, tmp_path, monkeypatch, reopen
):
    """The reload record holds what Desktop is shown, also of a replay that was kept from it."""
    live, gate = live_reply("Working on it."), tmp_path / "say-gate"
    monkeypatch.setenv("FAKE_REPLAY_SAY", json.dumps(saved_chat()))
    monkeypatch.setenv("FAKE_SAY", json.dumps([live]))
    monkeypatch.setenv("FAKE_SAY_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            if reopen == "reload":
                assert "result" in await client.request("session/load", params)
            else:
                # A handoff loads the chat in the next login's native and keeps that replay
                # from Desktop, which already shows the chat.
                await client.prompt(session_id, "/switch ansuman-2")
                assert acp_native.store.selected().name == "ansuman-2"
                assert README not in json.dumps(client.messages)
            turn = asyncio.create_task(client.prompt(session_id, "say"))
            try:
                await until(lambda: live in rows(client.messages))
                start = len(client.messages)
                assert "result" in await asyncio.wait_for(client.request("session/load", params), 5)
                assert rows(client.messages[start:]) == [
                    agent_text("historical replay"),
                    *saved_chat(shown=True),
                    {**agent_text("say"), "sessionUpdate": "user_message_chunk"},
                    live,
                ]
            finally:
                # Also when this test fails: the stand-in agent must not wait on for good.
                gate.touch()
            assert (await turn)["result"]["stopReason"] == "end_turn"
            # The reload during the turn was answered from the record, not by the native.
            loads = [call for call in logged(acp_native) if call["method"] == "session/load"]
            assert len(loads) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("working", [False, True])
def test_gui_history_page_replies_reach_desktop_without_event_ids(
    acp_native, tmp_path, monkeypatch, working
):
    """Desktop 3.14 fetches older rows as an answer, on open and on scroll, also mid-turn.

    An answer never passes the path a chat's updates take, and it is a replay like a load's.
    """
    answer, gate = tmp_path / "page.json", tmp_path / "say-gate"
    monkeypatch.setenv("FAKE_PAGE", str(answer))
    monkeypatch.setenv("FAKE_SAY", json.dumps([agent_text("live progress")]))
    monkeypatch.setenv("FAKE_SAY_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)

            async def page(sent):
                answer.write_text(json.dumps(sent))
                request = client.request(LOAD_PAGE, {"sessionId": session_id, **PAGE})
                return (await asyncio.wait_for(request, 5)).get("result")

            turn = asyncio.create_task(client.prompt(session_id, "say")) if working else None
            try:
                if working:
                    await wait_for_text(client, "live progress")
                # The rows in the native's order and the page's own fields are as sent.
                assert await page(saved_page(session_id)) == saved_page(session_id, shown=True)
                # A row that is no row is passed on as it is, and the rows beside it are read.
                odd, reply = [None, {"update": "text"}], saved("agent_message_chunk", "Hi", "7")
                assert await page({"updates": [*odd, {"update": reply}], "exhausted": True}) == {
                    "updates": [*odd, {"update": saved("agent_message_chunk", "Hi")}],
                    "exhausted": True,
                }
                assert await page({"updates": "none"}) == {"updates": "none"}
                assert not turn or not turn.done()
            finally:
                gate.touch()
            if turn:
                assert (await turn)["result"]["stopReason"] == "end_turn"
            only_native(acp_native, session_id)

    asyncio.run(scenario())


WORKER, HELPER = "d29b75b1", "b7e2c9d4"


def run_event(agent, what, run=None, **details):
    """A subagent's `started`, `completed` or `paused` as the native reports it: no run named.

    `run`: as Desktop is sent it, naming the run it belongs to.
    """
    event = {"agentId": agent, **details, **({"runId": run} if run else {})}
    change = {"sessionUpdate": "tool_call_update", "toolCallId": agent}
    return {**change, "_meta": {f"cognition.ai/subagent_{what}": event}}


def run_step(agent, run=None):
    """A thought of the subagent's own, which says whose it is and nothing of the run."""
    context = {"parentAgentId": agent, **({"runId": run} if run else {})}
    thought = saved("agent_thought_chunk", "Reading on.", "23940")
    return {**thought, "_meta": {**thought["_meta"], "cognition.ai/subagent_context": context}}


def run_of(agent, run=None):
    return [
        run_event(agent, "started", run),
        run_step(agent, run),
        run_event(agent, "completed", run),
    ]


def page_of(session_id, updates, start=120, end=125):
    """An answer to `loadPage` that holds these updates, oldest first."""
    return {
        "updates": [{"sessionId": session_id, "update": update} for update in updates],
        "coveredRange": {"startRowId": start, "endRowId": end},
        "nextEndRowId": start,
        "exhausted": False,
        "replayFilterVersion": "1",
    }


@pytest.mark.parametrize("working", [False, True])
def test_gui_history_page_names_the_runs_of_its_subagents(
    acp_native, tmp_path, monkeypatch, working
):
    """Desktop 3.14 folds a page of older rows on its own and counts a resumed run as running.

    The native names no run, so the second start in a page gets a row that its end never
    finds: "3 subagents running" in a chat where none was. A page is an answer, which never
    passes the path a chat's updates take.
    """
    answer, gate = tmp_path / "page.json", tmp_path / "say-gate"
    monkeypatch.setenv("FAKE_PAGE", str(answer))
    monkeypatch.setenv("FAKE_SAY", json.dumps([agent_text("live progress")]))
    monkeypatch.setenv("FAKE_SAY_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)

            async def page(updates, **rows):
                answer.write_text(json.dumps(page_of(session_id, updates, **rows)))
                request = client.request(LOAD_PAGE, {"sessionId": session_id, **PAGE})
                return (await asyncio.wait_for(request, 5)).get("result")

            turn = asyncio.create_task(client.prompt(session_id, "say")) if working else None
            try:
                if working:
                    await wait_for_text(client, "live progress")
                # Every run that starts in the page says which run it is, on its start, its
                # own work and its end: the first one too, and each subagent on its own.
                sent = [*run_of(WORKER), *run_of(HELPER), *run_of(WORKER)]
                assert await page(sent) == page_of(
                    session_id,
                    [
                        *run_of(WORKER, f"{WORKER}-h125-1"),
                        *run_of(HELPER, f"{HELPER}-h125-1"),
                        *run_of(WORKER, f"{WORKER}-h125-2"),
                    ],
                )
                # The next older page names its runs after its own rows.
                assert await page(run_of(WORKER), start=90, end=120) == page_of(
                    session_id, run_of(WORKER, f"{WORKER}-h120-1"), start=90, end=120
                )
                assert not turn or not turn.done()
            finally:
                gate.touch()
            if turn:
                assert (await turn)["result"]["stopReason"] == "end_turn"
            only_native(acp_native, session_id)

    asyncio.run(scenario())


def about_runs(messages):
    """The updates Desktop was sent that are a subagent's start, end, pause or own work."""
    return [row for row in rows(messages) if "cognition.ai/subagent_" in json.dumps(row)]


def test_gui_run_that_ends_in_the_tail_is_ended_in_the_page_that_holds_its_start(
    acp_native, tmp_path, monkeypatch
):
    """Desktop 3.14 applies an end as its segment arrives, to a row of that segment.

    The tail comes first, so an end whose start sits in an older page finds no row and is
    dropped for good: the run counted as running although its start now had a name.
    """
    answer = tmp_path / "page.json"
    end = run_event(WORKER, "completed", success=True, summary="Read on.")
    monkeypatch.setenv("FAKE_PAGE", str(answer))
    monkeypatch.setenv("FAKE_REPLAY_SAY", json.dumps([run_step(WORKER), end]))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            start = len(client.messages)
            assert "result" in await client.request("session/load", params)
            # Nothing in the tail started this run, so the tail is as the native sent it.
            assert about_runs(client.messages[start:]) == [run_step(WORKER), end]

            older = [*run_of(WORKER), run_event(WORKER, "started")]
            answer.write_text(json.dumps(page_of(session_id, older)))
            second = f"{WORKER}-h125-2"
            # The page that holds the start ends with a copy of the end, under the name it
            # gave that run: one more row, shaped like the page's own.
            named = [
                *run_of(WORKER, f"{WORKER}-h125-1"),
                run_event(WORKER, "started", second),
                run_event(WORKER, "completed", second, success=True, summary="Read on."),
            ]
            for _ in range(2):
                # Desktop may ask again: the same answer, not one without the copy.
                request = client.request(LOAD_PAGE, {"sessionId": session_id, **PAGE})
                got = (await asyncio.wait_for(request, 5)).get("result")
                assert got == page_of(session_id, named)

    asyncio.run(scenario())


def test_gui_live_end_names_the_run_that_a_history_page_left_open(
    acp_native, tmp_path, monkeypatch
):
    """A subagent that works on while Desktop pages in the row of its start."""
    answer, gate = tmp_path / "page.json", tmp_path / "say-gate"
    end = run_event(HELPER, "completed", success=True, summary="Done.")
    monkeypatch.setenv("FAKE_PAGE", str(answer))
    monkeypatch.setenv("FAKE_SAY", json.dumps([run_step(HELPER), end, run_step(HELPER)]))
    monkeypatch.setenv("FAKE_SAY_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            name = f"{HELPER}-h125-1"
            answer.write_text(
                json.dumps(page_of(session_id, [run_event(HELPER, "started"), run_step(HELPER)]))
            )
            request = client.request(LOAD_PAGE, {"sessionId": session_id, **PAGE})
            assert (await asyncio.wait_for(request, 5)).get("result") == page_of(
                session_id, [run_event(HELPER, "started", name), run_step(HELPER, name)]
            )
            turn = asyncio.create_task(client.prompt(session_id, "say"))
            try:
                await until(lambda: len(about_runs(client.messages)) == 3)
                # Its work and its end find the row of the page; what follows the end is
                # that run's no more.
                assert about_runs(client.messages) == [
                    run_step(HELPER, name),
                    run_event(HELPER, "completed", name, success=True, summary="Done."),
                    run_step(HELPER),
                ]
            finally:
                gate.touch()
            assert (await turn)["result"]["stopReason"] == "end_turn"

    asyncio.run(scenario())


def test_gui_second_load_starts_the_history_pages_over(acp_native, tmp_path, monkeypatch):
    """A load shows Desktop a new tail, and Desktop fetches every page again from it."""
    answer, gate = tmp_path / "page.json", tmp_path / "say-gate"
    end = run_event(WORKER, "completed", success=True, summary="Read on.")
    live = run_event(HELPER, "completed", success=True, summary="Done.")
    monkeypatch.setenv("FAKE_PAGE", str(answer))
    monkeypatch.setenv("FAKE_REPLAY_SAY", json.dumps([end]))
    monkeypatch.setenv("FAKE_SAY", json.dumps([live]))
    monkeypatch.setenv("FAKE_SAY_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}

            async def page(updates):
                answer.write_text(json.dumps(page_of(session_id, updates)))
                request = client.request(LOAD_PAGE, {"sessionId": session_id, **PAGE})
                return (await asyncio.wait_for(request, 5)).get("result")

            # The first round: an end leads the tail, and a page leaves another run open.
            assert "result" in await client.request("session/load", params)
            open_run = run_event(HELPER, "started", f"{HELPER}-h125-1")
            assert await page([run_event(HELPER, "started")]) == page_of(session_id, [open_run])

            # The second round owes nothing to the first. The tail's end is copied once, to
            # the page that now covers those rows, which is not answered from the first
            # round's memory of them.
            assert "result" in await client.request("session/load", params)
            name = f"{WORKER}-h125-1"
            assert await page([run_event(WORKER, "started")]) == page_of(
                session_id,
                [
                    run_event(WORKER, "started", name),
                    run_event(WORKER, "completed", name, success=True, summary="Read on."),
                ],
            )
            # And the run that the first round's page left open is no longer named.
            start = len(client.messages)
            turn = asyncio.create_task(client.prompt(session_id, "say"))
            try:
                await until(lambda: about_runs(client.messages[start:]))
                assert about_runs(client.messages[start:]) == [live]
            finally:
                gate.touch()
            assert (await turn)["result"]["stopReason"] == "end_turn"

    asyncio.run(scenario())


def test_gui_history_page_the_native_refuses_is_answered_with_its_error(
    acp_native, tmp_path, monkeypatch
):
    """An error is not a page: whatever its details hold reaches Desktop as before."""
    details = {"cognition.ai/errorKind": "internal", "updates": saved_page("any")["updates"]}
    refused = {"code": -32603, "message": "Internal error", "data": details}
    monkeypatch.setenv("FAKE_PAGE_ERROR", json.dumps(refused))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            error = await wire_error(bridge, messages, LOAD_PAGE, {**created, **PAGE})
            assert error["code"] == -32603
            assert error["message"].startswith("Native ACP rejected the request")
            assert error["data"] == details

    asyncio.run(scenario())


def test_gui_answer_to_another_request_keeps_its_event_ids(acp_native, tmp_path, monkeypatch):
    """Only a history page is a replay: no other answer is read, whatever it holds."""

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]

            async def answer(method, params, timeout=None):
                return saved_page(session_id)

            monkeypatch.setattr(bridge.chats[session_id].backend, "request", answer)
            steps = await bridge.dispatch("_cognition.ai/revert/listSteps", created)
            assert steps == saved_page(session_id)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "kept"),
    [
        ("agent_message_chunk", False),
        ("user_message_chunk", True),
        ("agent_thought_chunk", True),
        ("tool_call", True),
        ("tool_call_update", True),
        ("usage_update", True),
    ],
)
def test_event_id_is_dropped_from_an_agent_message_alone(kind, kept):
    update = saved(kind, LINKS, "23937")
    acp.drop_event_id(update)
    assert update == saved(kind, LINKS, "23937" if kept else None)


ODD_UPDATES = [
    None,
    7,
    "agent_message_chunk",
    ["agent_message_chunk"],
    {},
    {"_meta": {EVENT: "1"}},
    {"sessionUpdate": ["agent_message_chunk"], "_meta": {EVENT: "1"}},
    {"sessionUpdate": "agent_message_chunk"},
    {"sessionUpdate": "agent_message_chunk", "_meta": None},
    {"sessionUpdate": "agent_message_chunk", "_meta": EVENT},
    {"sessionUpdate": "agent_message_chunk", "_meta": [EVENT]},
    {"sessionUpdate": "agent_message_chunk", "_meta": {}},
]


@pytest.mark.parametrize("update", ODD_UPDATES)
def test_event_id_drop_leaves_an_odd_update_as_it_is(update):
    """It runs in a chat's read loop, where an error fails the chat's requests or its reading."""
    given = json.dumps(update)
    acp.drop_event_id(update)
    assert json.dumps(update) == given


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"exhausted": True, "nextEndRowId": None},
        {"updates": None},
        {"updates": "none"},
        {"updates": {"0": {"update": saved("agent_message_chunk", "Hi", "7")}}},
        {"updates": [None, 7, "row", [], {}, {"sessionId": "any"}]},
        {"updates": [{"update": odd} for odd in ODD_UPDATES]},
    ],
)
def test_event_id_drop_leaves_an_odd_history_page_as_it_is(page):
    given = json.dumps(page)
    acp.drop_page_event_ids(page)
    assert json.dumps(page) == given


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
        "/switch-continue missing",
        "/switch-continue ansuman-1",
        "!ds switch --continue ansuman-1",
        "/switch-continue ansuman-2 extra",
        "/auto-switch maybe",
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


def test_gui_idle_release_reconnects_once_with_same_account_and_settings(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            bridge.flags = ("--sandbox",)
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = result["sessionId"]
            chat = bridge.chats[session_id]
            original = chat.backend
            await bridge.dispatch(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": "plan"},
            )
            assert not await bridge.release_idle(chat)
            original.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            assert chat.suspended and original.process.poll() == 0
            assert not acp_state.live(acp_native.store)
            acp_native.store.select(acp_native.store.account("ansuman-2"))
            await bridge.dispatch(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "/switch-status"}]},
            )
            assert "suspended" in json.dumps(messages)
            assert chat.backend is original
            await asyncio.gather(
                *(
                    bridge.dispatch(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": "next"}]},
                    )
                    for _ in range(2)
                )
            )
            assert not chat.suspended and chat.backend is not original
            assert chat.backend.account.name == "ansuman-1"
            assert chat.backend.configs["mode"]["currentValue"] == "plan"
            assert acp_native.store.selected().name == "ansuman-2"
            assert "historical replay" not in json.dumps(messages)
            calls = logged(acp_native)
            assert len([call for call in calls if call["method"] == "session/load"]) == 1
            assert len([call for call in calls if call["method"] == "session/prompt"]) == 2
            assert all("--sandbox" in call["args"] for call in calls[1:])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "params", "key", "value"),
    [
        ("session/set_mode", {"modeId": "plan"}, "mode", "plan"),
        ("session/set_model", {"modelId": "other-model"}, "model", "other-model"),
    ],
)
def test_gui_idle_preserves_mode_and_model_controls(
    acp_native, tmp_path, method, params, key, value
):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            await bridge.dispatch(method, {"sessionId": chat.session_id, **params})
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            await bridge.dispatch(
                "session/prompt",
                {"sessionId": chat.session_id, "prompt": [{"type": "text", "text": "next"}]},
            )
            reply = json.loads(messages[-1]["params"]["update"]["content"]["text"])
            assert reply["settings"][key] == value

    asyncio.run(scenario())


def test_gui_sidebar_action_reconnects_an_idle_released_chat(acp_native, tmp_path):
    """Only a chat another window holds refuses; one released for idleness reconnects."""

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            await bridge.dispatch(
                "_cognition.ai/session/rename", {"sessionId": chat.session_id, "title": "New"}
            )
            assert not chat.suspended
            renames = [c for c in logged(acp_native) if c["method"].endswith("session/rename")]
            assert [call["pid"] for call in renames] == [chat.backend.process.pid]

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["unsaved", "history_error", "permission", "control", "switch"])
def test_gui_idle_release_leaves_unsafe_chats_open(acp_native, tmp_path, monkeypatch, reason):
    if reason == "unsaved":
        monkeypatch.setenv("FAKE_SAVE_ON_PROMPT", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            backend = chat.backend
            if reason == "unsaved":
                await bridge.dispatch(
                    "session/prompt",
                    {"sessionId": chat.session_id, "prompt": [{"type": "text", "text": "hi"}]},
                )
                monkeypatch.setattr(sessions, "history", lambda store: [])
            if reason == "history_error":

                def unavailable(store):
                    raise SwitchError("History unavailable")

                monkeypatch.setattr(sessions, "history", unavailable)
            if reason == "permission":
                bridge.client_requests["pending"] = acp.ClientRequest(backend, 123)
            if reason == "control":
                chat.controls = set()
            if reason == "switch":
                acp_state.queue(acp_native, chat.session_id, "ansuman-2")
            backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert not await bridge.release_idle(chat)
            assert not chat.suspended and backend.process.poll() is None
            assert chat.backend is backend

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_code", [0, 7])
def test_gui_idle_slow_shutdown_never_overlaps_replacement(
    acp_native, tmp_path, monkeypatch, exit_code
):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            monkeypatch.setenv("FAKE_SHUTDOWN_DELAY", "0.5")
            monkeypatch.setenv("FAKE_EXIT_ON_EOF", str(exit_code))
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            backend = chat.backend
            backend.last_activity -= acp.IDLE_TIMEOUT + 1
            monkeypatch.setattr(acp, "SHUTDOWN_TIMEOUT", 0.01)
            assert not await bridge.release_idle(chat)
            assert chat.suspended and backend.process.poll() is None
            params = {"sessionId": chat.session_id, "prompt": [{"type": "text", "text": "next"}]}
            with pytest.raises(SwitchError, match="still shutting down"):
                await bridge.dispatch("session/prompt", params)
            assert not any(call["method"] == "session/load" for call in logged(acp_native))
            await asyncio.wait_for(backend.watcher, 3)
            if exit_code:
                with pytest.raises(SwitchError, match="abnormally"):
                    await bridge.dispatch("session/prompt", params)
            else:
                await bridge.dispatch("session/prompt", params)
                assert chat.backend is not backend

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["desktop", "cli"])
def test_gui_close_exact_launch_waits_for_decision_and_reconnects(
    acp_native, tmp_path, source, monkeypatch
):
    monkeypatch.setattr(desktop.browser, "profiles", lambda: ())

    async def scenario():
        async with connect(acp_native, auto_respond=False) as client:
            first, other = await client.new(tmp_path), await client.new(tmp_path)
            original = next(
                run for run in acp_state.live(acp_native.store) if run["session_id"] == first
            )
            pending = asyncio.create_task(client.prompt(first, "permission"))
            request = await asyncio.wait_for(client.requests.get(), 5)
            if source == "desktop":
                result = await asyncio.to_thread(
                    desktop.action,
                    acp_native.store,
                    {"action": "close_session", "run": original["id"]},
                )
                assert "when idle" in result["message"]
            else:
                args = cli.parser().parse_args(["close", "--run", original["id"]])
                assert await asyncio.to_thread(cli.execute, args, acp_native.store) == 0
            await asyncio.sleep(0.2)
            assert not pending.done()
            assert len(acp_state.live(acp_native.store)) == 2
            assert acp_state.lifecycle(acp_native.store, original["id"]) == "close_requested"
            with pytest.raises(SwitchError, match="closing"):
                acp_state.queue(acp_native, first, "ansuman-2")
            response = {"outcome": {"outcome": "selected", "optionId": "reject"}}
            await client.send({"id": request["id"], "result": response})
            assert "result" in await pending
            assert client.agent_replies()[-1]["decision"]["result"] == response
            async with asyncio.timeout(5):
                while any(run["id"] == original["id"] for run in acp_state.live(acp_native.store)):
                    await asyncio.sleep(0.05)
            assert [run["session_id"] for run in acp_state.live(acp_native.store)] == [other]
            await client.prompt(first, "next")
            assert client.agent_replies()[-1]["session"] == first
            assert client.agent_replies()[-1]["account"] == original["account"]
            with pytest.raises(SwitchError, match="No unique live GUI"):
                acp_state.queue_close(acp_native.store, original["id"])
            assert "historical replay" not in client.texts()
            assert acp_native.store.selected().name == original["account"]

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["unsaved", "legacy", "switch", "invalid"])
def test_gui_close_rejects_unsafe_or_unsupported_launches(
    acp_native, tmp_path, monkeypatch, reason
):
    if reason == "unsaved":
        monkeypatch.setenv("FAKE_SAVE_ON_PROMPT", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            run_id = chat.backend.lease.run.id
            if reason == "legacy":
                acp_state.lifecycle_path(acp_native.store, run_id).unlink()
            if reason == "switch":
                acp_state.queue(acp_native, chat.session_id, "ansuman-2")
            with pytest.raises(SwitchError):
                acp_state.queue_close(
                    acp_native.store, "../invalid" if reason == "invalid" else run_id
                )
            assert chat.backend.process.poll() is None
            assert acp_state.lifecycle(acp_native.store, run_id) in (None, "open")

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["missing", "duplicate", "restore_failure", "ignored_settings"])
def test_gui_idle_reconnect_failure_never_sends_prompt(acp_native, tmp_path, monkeypatch, reason):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            await bridge.dispatch(
                "session/set_config_option",
                {"sessionId": chat.session_id, "configId": "mode", "value": "plan"},
            )
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            if reason == "missing":
                monkeypatch.setattr(sessions, "history", lambda store: [])
            if reason == "restore_failure":
                monkeypatch.setenv("FAKE_FAIL_LOAD_ACCOUNT", "ansuman-1")
            if reason == "ignored_settings":
                monkeypatch.setenv("FAKE_IGNORE_CONFIG_ACCOUNT", "ansuman-1")
            # The warm spare started before the fake CLI's failure switches were set.
            while bridge.filling:
                await asyncio.sleep(0.01)
            await bridge.drop_spare()
            monkeypatch.setattr(acp, "HANDOVER_TIMEOUT", 0.5)
            async with in_process(acp_native) as (other, _):
                if reason == "duplicate":
                    await other.dispatch("session/load", chat.setup)
                with pytest.raises(SwitchError) as error:
                    await bridge.dispatch(
                        "session/prompt",
                        {
                            "sessionId": chat.session_id,
                            "prompt": [{"type": "text", "text": "next"}],
                        },
                    )
                assert "sensitive-native-diagnostic" not in str(error.value)
                assert chat.suspended
                assert "historical replay" not in json.dumps(messages)
                assert not any(call["method"] == "session/prompt" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_idle_poll_releases_only_old_idle_connections(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            first = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            second = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            old, recent = bridge.chats[first["sessionId"]], bridge.chats[second["sessionId"]]
            old.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            bridge.spawn(bridge.poll())
            async with asyncio.timeout(5):
                while not old.suspended or old.backend.process.poll() is None:
                    await asyncio.sleep(0.05)
            assert not recent.suspended and recent.backend.process.poll() is None

    asyncio.run(scenario())


@pytest.mark.parametrize(("periodic", "released"), [("telemetry", True), ("work", False)])
def test_gui_idle_poll_ignores_native_telemetry_but_not_chat_output(
    acp_native, tmp_path, monkeypatch, periodic, released
):
    monkeypatch.setenv("FAKE_PERIODIC", periodic)
    monkeypatch.setattr(acp, "IDLE_TIMEOUT", 0.5)
    monkeypatch.setattr(acp, "IDLE_RECHECK", 0.05)

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            backend = chat.backend
            bridge.spawn(bridge.poll())
            if released:
                async with asyncio.timeout(5):
                    while not chat.suspended or backend.process.poll() is None:
                        await asyncio.sleep(0.05)
                assert any(m.get("method") == "_cognition.ai/processMemory" for m in messages)
            else:
                await asyncio.sleep(1.5)
                assert not chat.suspended and backend.process.poll() is None

    asyncio.run(scenario())


def test_gui_idle_releases_blank_chat_without_restarting_it(acp_native, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SAVE_ON_PROMPT", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            backend = chat.backend
            status = {
                "sessionId": chat.session_id,
                "prompt": [{"type": "text", "text": "/switch-status"}],
            }
            await bridge.dispatch("session/prompt", status)
            backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            assert chat.suspended and backend.process.poll() == 0
            assert not acp_state.live(acp_native.store)
            for method, params in (
                ("session/prompt", {"prompt": [{"type": "text", "text": "hello"}]}),
                ("session/set_mode", {"modeId": "plan"}),
                ("session/load", chat.setup),
            ):
                with pytest.raises(SwitchError, match="start a new chat"):
                    await bridge.dispatch(method, {"sessionId": chat.session_id, **params})
            await bridge.dispatch("session/prompt", status)
            assert "start a new chat" in messages[-1]["params"]["update"]["content"]["text"]
            assert chat.backend is backend
            await bridge.dispatch("session/close", {"sessionId": chat.session_id})
            assert chat.session_id not in bridge.chats
            calls = logged(acp_native)
            assert not any(call["method"] in {"session/prompt", "session/load"} for call in calls)
            sessions_served = {
                call["pid"] for call in calls if call["method"].startswith("session/")
            }
            assert sessions_served == {backend.process.pid}

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["session/load", "session/resume"])
def test_gui_idle_explicit_reopen_restores_settings_and_replay_policy(acp_native, tmp_path, method):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            await bridge.dispatch(
                "session/set_config_option",
                {"sessionId": chat.session_id, "configId": "mode", "value": "plan"},
            )
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            result = await bridge.dispatch(method, chat.setup)
            assert (
                next(option for option in result["configOptions"] if option["id"] == "mode")[
                    "currentValue"
                ]
                == "plan"
            )
            assert ("historical replay" in json.dumps(messages)) == (method == "session/load")
            assert not any(call["method"] == "session/prompt" for call in logged(acp_native))

    asyncio.run(scenario())


def test_gui_idle_release_ignores_background_lease_and_keeps_server_alive(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            await bridge.dispatch(
                "session/prompt",
                {"sessionId": chat.session_id, "prompt": [{"type": "text", "text": "background"}]},
            )
            old = chat.backend
            old.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            assert old.process.poll() == 0
            assert acp_native.store.locked(f"run-{old.lease.run.id}.lock")
            assert not acp_state.live(acp_native.store)
            await bridge.dispatch(
                "session/set_mode", {"sessionId": chat.session_id, "modeId": "plan"}
            )
            assert chat.backend is not old and not chat.suspended
            assert acp_native.store.locked(f"run-{old.lease.run.id}.lock")
            await asyncio.sleep(2)

    asyncio.run(scenario())


def test_gui_close_rechecks_history_and_keeps_legacy_run_schema(acp_native, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.browser, "profiles", lambda: ())

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            backend = chat.backend
            run_id = backend.lease.run.id
            record = backend.lease.path.read_text()
            state = desktop.snapshot(acp_native.store)
            assert (
                next(run for run in state["runs"] if run["id"] == run_id)["gui_lifecycle"] == "open"
            )
            acp_state.queue_close(acp_native.store, run_id)
            state = desktop.snapshot(acp_native.store)
            assert (
                next(run for run in state["runs"] if run["id"] == run_id)["gui_lifecycle"]
                == "close_requested"
            )
            monkeypatch.setattr(sessions, "history", lambda store: [])
            assert not await bridge.release_idle(chat)
            assert not chat.suspended and backend.process.poll() is None
            assert acp_state.lifecycle(acp_native.store, run_id) == "open"
            assert "Close canceled" in json.dumps(messages)
            assert backend.lease.path.read_text() == record
            acp_state.lifecycle_path(acp_native.store, run_id).unlink()
            state = desktop.snapshot(acp_native.store)
            assert (
                next(run for run in state["runs"] if run["id"] == run_id)["gui_lifecycle"] is None
            )

    asyncio.run(scenario())


def test_gui_idle_close_tab_does_not_restart_a_suspended_chat(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            await bridge.dispatch(
                "session/cancel", {"sessionId": chat.session_id}, notification=True
            )
            await bridge.dispatch("session/close", {"sessionId": chat.session_id})
            assert chat.session_id not in bridge.chats
            assert not any(call["method"] == "session/load" for call in logged(acp_native))

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


def fresh_usage(exhausted=()):
    import time

    def refresh(store, account, *, force):
        assert force and not choose_best_holds_store_lock()
        now = time.time()
        used = 100 if account.name in exhausted else 20
        return usage.Usage(
            status="ok",
            fetched_at=now,
            checked_at=now,
            daily=usage.Window(used, now + 3600, "available"),
            weekly=usage.Window(40, now + 86400, "available"),
        )

    return refresh


def prompts(native):
    return [(call["account"], call["text"]) for call in logged(native) if call["text"] is not None]


def prompt_params(session_id, text):
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}


@pytest.mark.parametrize("style", ["error", "stopped"])
def test_gui_quota_exhaustion_switches_and_continues_in_the_same_turn(
    acp_native, tmp_path, monkeypatch, style
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    monkeypatch.setenv("FAKE_QUOTA_STYLE", style)

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            await bridge.dispatch(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": "plan"},
            )
            result = await bridge.dispatch("session/prompt", prompt_params(session_id, "work"))
            assert result == {"stopReason": "end_turn"}
            chat = bridge.chats[session_id]
            assert chat.backend.account.name == "ansuman-2"
            assert chat.backend.configs["mode"]["currentValue"] == "plan"
            assert acp_native.store.selected().name == "ansuman-2"
            assert prompts(acp_native) == [("ansuman-1", "work"), ("ansuman-2", "continue")]
            text = json.dumps(messages)
            assert "Out of usage on ansuman-1" in text and "Sending" in text
            assert chat.controls is None and not chat.gate.locked()

    asyncio.run(scenario())


def test_gui_reload_while_a_prompt_changes_logins_is_answered_at_once(
    acp_native, tmp_path, monkeypatch
):
    """No turn is open between the failed prompt and its follow-up, yet the prompt holds the chat.

    Desktop 3.14 shows only a loader, and queues what is typed, until its reload is answered.
    """
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            switch = bridge.switch
            switching, proceed = asyncio.Event(), asyncio.Event()

            async def slow_switch(chat, *arguments, **options):
                switching.set()
                await proceed.wait()
                await switch(chat, *arguments, **options)

            monkeypatch.setattr(bridge, "switch", slow_switch)
            turn = bridge.spawn(
                bridge.dispatch("session/prompt", prompt_params(session_id, "work"))
            )
            await asyncio.wait_for(switching.wait(), 5)
            start = len(messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            loaded = await asyncio.wait_for(bridge.dispatch("session/load", params), 5)
            assert "configOptions" in loaded
            assert replayed_texts(messages[start:], "user_message_chunk") == ["work"]
            assert not turn.done()
            proceed.set()
            assert await turn == {"stopReason": "end_turn"}
            assert prompts(acp_native) == [("ansuman-1", "work"), ("ansuman-2", "continue")]
            loads = [
                call["account"] for call in logged(acp_native) if call["method"] == "session/load"
            ]
            assert loads == ["ansuman-2"], "only the switch loaded the chat, in the new native"

    asyncio.run(scenario())


REWIND = "_cognition.ai/revert/historyRewound"
# What the native sends from the rewind on, until it answers the prompt with the error.
TAKEN_BACK = [REWIND, "_cognition.ai/turn_stats", "_cognition.ai/agent_stopped"]
# Like Desktop, a client that shows revert steps: the native tells only those of a rewind.
REVERTING = {"_meta": {"cognition.ai/revert": True}}


def asked(session_id, text, identifier="prompt-1"):
    """A prompt as Desktop sends it: the native names the message it takes back by this id."""
    return {
        **prompt_params(session_id, text),
        "_meta": {"cognition.ai/clientMessageId": identifier},
    }


def methods(messages):
    return [message.get("method") for message in messages]


@pytest.mark.parametrize("client", [REVERTING, {}], ids=["desktop", "plain-client"])
def test_gui_prompt_the_native_took_back_is_sent_again_after_the_switch(
    acp_native, tmp_path, monkeypatch, client
):
    """CLI 3000.14 removes a prompt whose first model call failed: `continue` has no context.

    Desktop 3.14 drops the message and reloads the chat when it hears of the rewind, so the
    failed attempt stays between the bridge and the native. The native announces a rewind
    only to a client that shows revert steps, so the bridge asks for that itself.
    """
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    monkeypatch.setenv("FAKE_QUOTA_STYLE", "rewound")

    async def scenario():
        async with in_process(acp_native, client) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            result = await bridge.dispatch("session/prompt", asked(session_id, "work"))
            assert result == {"stopReason": "end_turn"}
            assert prompts(acp_native) == [("ansuman-1", "work"), ("ansuman-2", "work")]
            assert not set(TAKEN_BACK) & set(methods(messages))
            # Only a client that asked for them gets the revert steps of the turn.
            steps = [m for m in methods(messages) if str(m).startswith("_cognition.ai/revert/")]
            assert steps == (["_cognition.ai/revert/stepsUpdated"] if client else [])
            notes = "".join(texts(messages))
            assert "Sending your message again…" in notes and "continue" not in notes
            chat = bridge.chats[session_id]
            assert chat.backend.account.name == "ansuman-2"
            assert recorded_prompts(chat.backend) == ["work"]
            assert chat.rewound is None

    asyncio.run(scenario())


def test_gui_prompt_the_native_took_back_is_sent_again_in_the_same_process(
    acp_native, relayed, tmp_path, monkeypatch
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage(exhausted={"ansuman-1"}))
    # The relay cannot replay this request, so the agent sees the quota error itself.
    monkeypatch.setattr(acp.relay, "ROTATION_LIMIT", 0)
    monkeypatch.setenv("FAKE_QUOTA_STYLE", "rewound")
    relayed.exhausted = {"ansuman-1"}

    async def scenario():
        async with in_process(acp_native, REVERTING) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            result = await bridge.dispatch("session/prompt", asked(session_id, "infer"))
            assert result == {"stopReason": "end_turn"}
            calls = [call for call in logged(acp_native) if call["text"] is not None]
            assert [call["text"] for call in calls] == ["infer", "infer"]
            assert len({call["pid"] for call in calls}) == 1
            assert relayed.inference()[-1] == ("ansuman-2", "ansuman-2")
            assert not set(TAKEN_BACK) & set(methods(messages))
            assert "in the same agent, so subagents keep running. Sending your message again…" in (
                "".join(texts(messages))
            )
            # A reload replays the message once: the first copy went with the rewind.
            assert bridge.chats[session_id].backend is backend
            assert recorded_prompts(backend) == ["infer"]

    asyncio.run(scenario())


@pytest.mark.parametrize("client", [REVERTING, {}], ids=["desktop", "plain-client"])
def test_gui_rewind_reaches_desktop_when_the_prompt_is_not_sent_again(
    acp_native, tmp_path, monkeypatch, client
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    monkeypatch.setenv("FAKE_QUOTA_STYLE", "rewound")
    acp.handoff.set_auto_switch(acp_native.store, False)

    async def scenario():
        async with in_process(acp_native, client) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            start = len(messages)
            error = await wire_error(bridge, messages, "session/prompt", asked(session_id, "work"))
            assert error["code"] == -32011
            # Desktop hears what the native said, in its order, and then the error. A client
            # that never asked for revert notices gets the rest without the rewind.
            told = [method for method in methods(messages[start:]) if method in TAKEN_BACK]
            assert told == (TAKEN_BACK if client else TAKEN_BACK[1:])
            assert "error" in messages[-1]
            rewinds = [message for message in messages if message.get("method") == REWIND]
            assert [rewind["params"]["firstRemovedUserMessageId"] for rewind in rewinds] == (
                ["prompt-1"] if client else []
            )
            assert prompts(acp_native) == [("ansuman-1", "work")]
            chat = bridge.chats[session_id]
            assert recorded_prompts(chat.backend) == [] and chat.rewound is None

    asyncio.run(scenario())


def test_gui_prompt_the_native_took_back_carries_the_subagents_to_resume(
    acp_native, tmp_path, monkeypatch
):
    """The message is sent again as it was, with the resume request in a block of its own."""
    store = acp_native.store
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    monkeypatch.setenv("FAKE_QUOTA_STYLE", "rewound")

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            subagents.record(
                store, session_id, subagents.STARTED, {"agentId": "a", "task": "Fix it"}
            )
            await bridge.dispatch("session/prompt", asked(session_id, "work"))
            failed, again = prompt_texts(acp_native)
            assert failed == ["work"]
            assert again[0] == "work" and len(again) == 2
            assert "stopped before finishing" in again[1] and "Fix it" in again[1]
            assert subagents.interrupted(store, session_id) == {}

    asyncio.run(scenario())


def cold_catalog(native, monkeypatch, starts):
    counter = native.store.root.parent / "cold-starts"
    counter.write_text(str(starts))
    monkeypatch.setenv("FAKE_COLD_CATALOG_ACCOUNT", "ansuman-2")
    monkeypatch.setenv("FAKE_COLD_CATALOG_STARTS", str(counter))
    monkeypatch.setattr(acp, "CATALOG_RETRY_DELAY", 0)


def test_gui_auto_switch_retries_destination_whose_model_list_did_not_load(
    acp_native, tmp_path, monkeypatch
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    cold_catalog(acp_native, monkeypatch, 1)

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            result = await bridge.dispatch("session/prompt", prompt_params(session_id, "work"))
            assert result == {"stopReason": "end_turn"}
            chat = bridge.chats[session_id]
            assert chat.backend.account.name == "ansuman-2"
            assert chat.backend.configs["model"]["currentValue"] == "test-model"
            assert acp_native.store.selected().name == "ansuman-2"
            assert prompts(acp_native) == [("ansuman-1", "work"), ("ansuman-2", "continue")]
            loads = [call for call in logged(acp_native) if call["method"] == "session/load"]
            assert [call["account"] for call in loads] == ["ansuman-2", "ansuman-2"]
            assert len({call["pid"] for call in loads}) == 2
            text = json.dumps(messages)
            assert "retrying with a fresh agent" in text
            assert "Automatic switch stopped" not in text

    asyncio.run(scenario())


def test_gui_switch_gives_up_when_destination_model_list_never_loads(
    acp_native, tmp_path, monkeypatch
):
    cold_catalog(acp_native, monkeypatch, -1)

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/switch ansuman-2"))
            output = json.dumps(messages, ensure_ascii=False)
            assert "Switch to ansuman-2 failed" in output
            assert "could not load its model list" in output
            assert "the previous account was restored" in output
            assert output.count("retrying with a fresh agent") == acp.CATALOG_ATTEMPTS - 1
            loads = [call for call in logged(acp_native) if call["method"] == "session/load"]
            assert [call["account"] for call in loads] == ["ansuman-2"] * acp.CATALOG_ATTEMPTS + [
                "ansuman-1"
            ]
            assert acp_native.store.selected().name == "ansuman-1"
            assert bridge.chats[session_id].backend.account.name == "ansuman-1"
            await bridge.dispatch("session/prompt", prompt_params(session_id, "continue"))
            assert prompts(acp_native) == [("ansuman-1", "continue")]

    asyncio.run(scenario())


def test_gui_switch_does_not_retry_a_model_the_destination_lacks(acp_native, tmp_path, monkeypatch):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[created["sessionId"]]
            chat.backend.configs["model"]["currentValue"] = "unknown-model"
            request = acp.Backend.request

            async def missing_model(backend, method, params, timeout=None):
                if method == "session/set_config_option" and params["value"] == "unknown-model":
                    raise acp.RpcError("Native ACP rejected the request (RPC code -32002).", -32002)
                return await request(backend, method, params, timeout)

            monkeypatch.setattr(acp.Backend, "request", missing_model)
            with pytest.raises(SwitchError, match="RPC code -32002"):
                await bridge.switch(chat, "ansuman-2")
            assert "retrying" not in json.dumps(messages)
            assert [
                call["account"] for call in logged(acp_native) if call["method"] == "session/load"
            ] == ["ansuman-2", "ansuman-1"]

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["disabled", "rate-limited", "canceled"])
def test_gui_quota_error_without_auto_switch_is_returned_unchanged(
    acp_native, tmp_path, monkeypatch, case
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    monkeypatch.setenv("FAKE_QUOTA_STYLE", "rate" if case == "rate-limited" else "error")
    if case == "disabled":
        acp.handoff.set_auto_switch(acp_native.store, False)

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            chat = bridge.chats[session_id]
            if case == "canceled":
                forward = bridge.forward_request

                async def canceled_during_turn(backend, method, params):
                    try:
                        return await forward(backend, method, params)
                    finally:
                        await bridge.dispatch(
                            "session/cancel", {"sessionId": session_id}, notification=True
                        )

                monkeypatch.setattr(bridge, "forward_request", canceled_during_turn)
            with pytest.raises(acp.RpcError) as raised:
                await bridge.dispatch("session/prompt", prompt_params(session_id, "work"))
            assert raised.value.code == -32011
            assert chat.backend.account.name == "ansuman-1"
            assert acp_native.store.selected().name == "ansuman-1"
            assert prompts(acp_native) == [("ansuman-1", "work")]
            assert "out of usage" not in json.dumps(messages)

    asyncio.run(scenario())


def test_gui_native_error_details_reach_desktop(acp_native, tmp_path, monkeypatch):
    """Desktop's error classifier reads errorKind, retryable and retryAfterSeconds from data."""
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")
    monkeypatch.setenv("FAKE_QUOTA_STYLE", "rate")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            params = prompt_params(created["sessionId"], "work")
            error = await wire_error(bridge, messages, "session/prompt", params)
            assert error["code"] == -32011
            assert error["message"].startswith("Native ACP rejected the request")
            assert error["data"] == {
                "cognition.ai/errorKind": "resource_exhausted",
                "cognition.ai/retryable": True,
                "cognition.ai/retryAfterSeconds": 30,
            }

    asyncio.run(scenario())


def test_gui_auto_switch_never_revisits_exhausted_logins(acp_native, tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1,ansuman-2")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            with pytest.raises(acp.RpcError):
                await bridge.dispatch("session/prompt", prompt_params(session_id, "work"))
            assert prompts(acp_native) == [("ansuman-1", "work"), ("ansuman-2", "continue")]
            assert bridge.chats[session_id].backend.account.name == "ansuman-2"
            assert "Automatic switch stopped" in json.dumps(messages)

    asyncio.run(scenario())


def test_gui_auto_switch_canceled_during_handoff_sends_no_continue(
    acp_native, tmp_path, monkeypatch
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-1")

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            switch = bridge.switch

            async def canceled_switch(chat, *args, **kwargs):
                await switch(chat, *args, **kwargs)
                chat.canceled = True

            monkeypatch.setattr(bridge, "switch", canceled_switch)
            result = await bridge.dispatch("session/prompt", prompt_params(session_id, "work"))
            assert result == {"stopReason": "cancelled"}
            assert bridge.chats[session_id].backend.account.name == "ansuman-2"
            assert prompts(acp_native) == [("ansuman-1", "work")]

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/switch-continue ansuman-2", "!ds switch --continue ansuman-2"])
def test_gui_switch_continue_resumes_work_on_destination(acp_native, tmp_path, text):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            result = await client.prompt(session_id, text)
            assert result["result"]["stopReason"] == "end_turn"
            assert client.agent_replies() == [
                {
                    "account": "ansuman-2",
                    "session": session_id,
                    "settings": {"mode": "ask", "model": "test-model"},
                }
            ]
            assert prompts(acp_native) == [("ansuman-2", "continue")]
            assert acp_native.store.selected().name == "ansuman-2"
            commands = [
                message["params"]["update"]["availableCommands"]
                for message in client.messages
                if message.get("method") == "session/update"
                and message["params"]["update"]["sessionUpdate"] == "available_commands_update"
            ]
            assert all(
                {"switch", "switch-continue", "auto-switch"} <= {item["name"] for item in batch}
                for batch in commands
            )

    asyncio.run(scenario())


def test_gui_auto_switch_toggle_is_global_and_local(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "/switch-status")
            assert "Auto-switch: on" in client.texts()[-1]
            await client.prompt(session_id, "/auto-switch off")
            assert "off" in client.texts()[-1]
            assert not acp.handoff.auto_switch_enabled(acp_native.store)
            await client.prompt(session_id, "/switch-status")
            assert "Auto-switch: off" in client.texts()[-1]
            await client.prompt(session_id, "/auto-switch on")
            assert acp.handoff.auto_switch_enabled(acp_native.store)
            assert not prompts(acp_native)

    asyncio.run(scenario())


@pytest.fixture
def relayed(acp_native, tmp_path, monkeypatch):
    """Saved API logins for every fake account, served by an offline API upstream."""
    store = acp_native.store
    with store.lock():
        store.add("ansuman-3", None)
    names = [account.name for account in store.accounts()]
    upstream = Upstream(names)
    for name in names:
        store.credentials(store.account(name)).write_text(upstream.credentials(name))
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(tmp_path / "subagents-may-infer"))
    yield upstream
    upstream.close()


def texts(messages):
    return [
        message["params"]["update"]["content"]["text"]
        for message in messages
        if message.get("method") == "session/update"
        and message["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
    ]


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def subagent_events(messages, key):
    return [
        message["params"]["update"]["_meta"][key]
        for message in messages
        if message.get("method") == "session/update"
        and key in (message["params"]["update"].get("_meta") or {})
    ]


def secrets_absent(native, upstream, messages):
    exposed = json.dumps(messages) + "".join(json.dumps(call) for call in logged(native))
    return not any(key in exposed for key in upstream.keys)


def test_gui_relay_keeps_subagents_running_when_usage_runs_out_mid_task(
    acp_native, relayed, tmp_path, monkeypatch
):
    store = acp_native.store
    monkeypatch.setattr(usage, "refresh_account", fresh_usage(exhausted={"ansuman-3"}))
    saved = {
        name: store.credentials(store.account(name)).read_text() for name in relayed.keys.values()
    }

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            chat = bridge.chats[session_id]
            pid = chat.backend.process.pid
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "subagents"))
            )
            await until(
                lambda: len(subagent_events(messages, "cognition.ai/subagent_started")) == 2
            )
            relayed.exhausted = {"ansuman-1"}
            Path(os.environ["FAKE_SUBAGENT_GATE"]).touch()
            assert await turn == {"stopReason": "end_turn"}

            reply = [json.loads(text) for text in texts(messages) if '"subagents"' in text]
            assert reply == [
                {
                    "account": "ansuman-1",
                    "session": session_id,
                    "upstream": "reply from ansuman-2",
                    "subagents": {"s1": "reply from ansuman-2", "s2": "reply from ansuman-2"},
                }
            ]
            completed = subagent_events(messages, "cognition.ai/subagent_completed")
            assert sorted((event["agentId"], event["success"]) for event in completed) == [
                ("s1", True),
                ("s2", True),
            ]
            # One native process and one turn: no restart, no replayed or "continue" prompt.
            assert chat.backend.process.pid == pid
            assert prompts(acp_native) == [("ansuman-1", "subagents")]
            assert [c["method"] for c in logged(acp_native) if c["pid"] == pid].count(
                "authenticate"
            ) == 1
            calls = relayed.inference()
            assert all(header == body for header, body in calls)
            assert calls[0] == ("ansuman-1", "ansuman-1")
            assert [header for header, _ in calls].count("ansuman-2") == 3
            assert [header for header, _ in calls[1:]].count("ansuman-1") in (1, 2)

            assert chat.backend.account.name == "ansuman-2"
            assert acp_state.bound_account(store, session_id).name == "ansuman-2"
            assert store.selected().name == "ansuman-2"
            run = json.loads(
                (store.root / "runs" / f"{chat.backend.lease.run.id}.json").read_text()
            )
            assert run["account"] == "ansuman-2"
            assert "out of usage" not in json.dumps(messages)
            assert secrets_absent(acp_native, relayed, messages)
            await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            assert relayed.inference()[-1] == ("ansuman-2", "ansuman-2")

    asyncio.run(scenario())
    assert saved == {
        name: store.credentials(store.account(name)).read_text() for name in relayed.keys.values()
    }


def save_subagent_transcript(store, session_id, agent):
    """Write a subagent chain and head the way the native saves them in shared history."""
    import sqlite3

    with sqlite3.connect(store.root / "shared/cli/sessions.db") as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS subagent_heads (session_id TEXT, agent_id TEXT, "
            "chain_node_id INTEGER, updated_at INTEGER, PRIMARY KEY (session_id, agent_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS message_nodes (row_id INTEGER PRIMARY KEY, "
            "session_id TEXT, node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, "
            "created_at INTEGER, metadata TEXT)"
        )
        steps = [
            {"role": "system", "content": "You are a subagent"},
            {"role": "user", "content": f"Task for {agent}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"name": "exec", "arguments": {"command": "pytest -q"}}],
            },
            {"role": "tool", "content": f"{agent} ran the tests: 3 passed"},
        ]
        for node, step in enumerate(steps, 1):
            connection.execute(
                "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message) "
                "VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    100 * len(agent) + node,
                    (100 * len(agent) + node - 1) if node > 1 else None,
                    json.dumps(step),
                ),
            )
        connection.execute(
            "INSERT OR REPLACE INTO subagent_heads VALUES (?, ?, ?, 0)",
            (session_id, agent, 100 * len(agent) + len(steps)),
        )


def test_gui_subagents_stopped_by_a_crash_resume_from_their_saved_progress(
    acp_native, relayed, tmp_path, monkeypatch
):
    store = acp_native.store
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(tmp_path / "never-opened"))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            chat = bridge.chats[session_id]
            backend = chat.backend
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "subagents"))
            )
            await until(lambda: backend.running == {"s1", "s2"})
            # The saved record is written right after, off the event loop.
            await until(lambda: set(subagents.load(store, session_id)["running"]) == {"s1", "s2"})
            # Idle release never stops a chat whose subagents are still working.
            turn_gate, chat.gate = chat.gate, asyncio.Lock()
            backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert not await bridge.release_idle(chat)
            chat.gate = turn_gate
            save_subagent_transcript(store, session_id, "s1")
            backend.process.kill()
            with pytest.raises(SwitchError):
                await turn
            await bridge.dispatch("session/load", chat.setup)
            await until(lambda: "/resume-subagents" in json.dumps(messages))
            assert "2 subagent(s) stopped before finishing (Title s1, Title s2)" in json.dumps(
                messages
            )
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/resume-subagents"))
            sent = [text for _, text in prompts(acp_native)][-1]
            assert "2 of your subagents stopped before finishing" in sent
            assert "Task for s1" in sent and "Task for s2" in sent
            assert "s1 ran the tests: 3 passed" in sent and "pytest -q" in sent
            assert "(none saved)" in sent  # s2 had no saved transcript
            assert subagents.interrupted(store, session_id) == {}
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/resume-subagents"))
            assert "No subagents of this chat are waiting to resume" in json.dumps(messages)

    asyncio.run(scenario())


def test_subagents_that_fail_on_quota_are_offered_for_resume(store):
    session_id = "chat-quota"
    subagents.record(store, session_id, subagents.STARTED, {"agentId": "a", "task": "Fix it"})
    subagents.record(store, session_id, subagents.STARTED, {"agentId": "b", "task": "Test it"})
    subagents.record(
        store,
        session_id,
        subagents.COMPLETED,
        {
            "agentId": "a",
            "success": False,
            "summary": "[Error] daily usage quota has been exhausted",
        },
    )
    subagents.record(
        store, session_id, subagents.COMPLETED, {"agentId": "b", "success": True, "summary": "done"}
    )
    state = subagents.load(store, session_id)
    assert state["running"] == {} and list(state["interrupted"]) == ["a"]
    assert state["interrupted"]["a"]["reason"] == "usage ran out"
    text = subagents.resume_text(store, session_id, state["interrupted"])
    assert "Fix it" in text and "Test it" not in text


def finish(store, session_id, agent, *, success=True, summary="done", **started):
    subagents.record(store, session_id, subagents.STARTED, {"agentId": agent, **started})
    subagents.record(
        store,
        session_id,
        subagents.COMPLETED,
        {"agentId": agent, "success": success, "summary": summary},
    )


def test_subagents_that_finish_are_remembered_while_their_process_lives(store):
    session_id = "chat-finished"
    before = time.time()
    finish(store, session_id, "ok", title="Review", profile="reviewer", isBackground=True, task="x")
    finish(store, session_id, "failed", success=False, summary="[Error] Canceled by user")
    quota = "[Error] weekly usage quota has been exhausted"
    finish(store, session_id, "quota", success=False, summary=quota)
    ended = subagents.load_ended(store, session_id)
    assert list(ended["finished"]) == ["ok", "failed"] and ended["closed"] == {}
    assert before <= ended["finished"]["ok"].pop("ended_at") <= time.time()
    assert ended["finished"]["ok"] == {"title": "Review", "profile": "reviewer", "background": True}
    state = subagents.load(store, session_id)
    assert state["running"] == {} and list(state["interrupted"]) == ["quota"]
    assert subagents.closed(store, session_id) == {}
    # Resumed in the same process, it runs again.
    subagents.record(store, session_id, subagents.STARTED, {"agentId": "ok", "title": "Review"})
    assert list(subagents.load_ended(store, session_id)["finished"]) == ["failed"]
    assert list(subagents.load(store, session_id)["running"]) == ["ok"]


def test_subagent_completion_whose_start_was_missed_still_counts_as_finished(store):
    """Events are not counted while a load replays, so a subagent's start can be missed."""
    session_id = "chat-missed"
    before = time.time()
    quota = "[Error] daily usage quota has been exhausted"
    for agent, success, summary in (
        ("late", True, "done"),
        ("spent", False, quota),
        ("stopped", False, "[Error] Canceled by user"),
    ):
        event = {"agentId": agent, "success": success, "summary": summary}
        subagents.record(store, session_id, subagents.COMPLETED, event)
    finished = subagents.load_ended(store, session_id)["finished"]
    assert list(finished) == ["late", "stopped"]
    assert before <= finished["late"].pop("ended_at") <= time.time()
    assert finished["late"] == {"title": "", "profile": "", "background": False}
    # A quota failure nobody saw start has no task to resume.
    assert subagents.interrupted(store, session_id) == {}
    assert not subagents.path(store, session_id).exists()
    subagents.interrupt_running(store, session_id)
    note = subagents.closed_note(subagents.closed(store, session_id))
    assert note.splitlines()[1:-1] == ['- "subagent" (late)', '- "subagent" (stopped)']
    # A completion repeated without a start keeps what is known about the subagent.
    finish(store, session_id, "named", title="Named", profile="General", isBackground=True)
    known = subagents.load_ended(store, session_id)["finished"]["named"]
    subagents.record(store, session_id, subagents.COMPLETED, {"agentId": "named", "success": True})
    again = subagents.load_ended(store, session_id)["finished"]["named"]
    assert again["ended_at"] >= known.pop("ended_at")
    assert again == {**known, "ended_at": again["ended_at"]} and again["title"] == "Named"


def test_replacement_process_closes_finished_subagents_and_interrupts_running_ones(store):
    session_id = "chat-replaced"
    finish(store, session_id, "done", title="Done")
    subagents.record(store, session_id, subagents.STARTED, {"agentId": "busy", "title": "Busy"})
    finished = subagents.load_ended(store, session_id)["finished"]
    assert subagents.interrupt_running(store, session_id) == ["busy"]
    state = subagents.load(store, session_id)
    assert state["running"] == {}
    assert state["interrupted"]["busy"]["reason"] == "the agent restarted"
    ended = subagents.load_ended(store, session_id)
    assert ended["finished"] == {} and ended["closed"] == finished and list(finished) == ["done"]
    # Later replacements keep earlier closed subagents and add the newly finished ones.
    finish(store, session_id, "later", title="Later")
    assert subagents.interrupt_running(store, session_id) == []
    assert list(subagents.closed(store, session_id)) == ["done", "later"]


def test_only_the_latest_finished_and_closed_subagents_are_kept(store):
    session_id = "chat-many"
    limit = subagents.FINISHED_LIMIT
    # Saved in the reverse of the order they ended in, so age decides and not position.
    old = {f"old-{n}": {"title": f"Old {n}", "ended_at": float(limit - n)} for n in range(limit)}
    subagents.save_ended(store, {"session_id": session_id, "finished": old, "closed": {}})
    finish(store, session_id, "new")
    finished = subagents.load_ended(store, session_id)["finished"]
    assert set(finished) == {"new", *(f"old-{n}" for n in range(limit - 1))}
    closed = {
        "oldest": {"title": "Oldest", "ended_at": -1.0},
        "recent": {"title": "Recent", "ended_at": limit + 1.0},
        "older": {"title": "Older", "ended_at": 0.0},
    }
    subagents.save_ended(store, {"session_id": session_id, "finished": finished, "closed": closed})
    subagents.interrupt_running(store, session_id)
    kept = subagents.closed(store, session_id)
    assert set(kept) == {"recent", "new", *(f"old-{n}" for n in range(limit - 2))}
    assert list(kept)[-3:] == ["old-0", "recent", "new"]


def old_bridge_saves(store, session_id, interrupted):
    """Save the subagent record the way a bridge that knows only its two lists does."""
    target = subagents.path(store, session_id)
    if interrupted:
        target.parent.mkdir(parents=True, exist_ok=True)
        state = {"session_id": session_id, "running": {}, "interrupted": interrupted}
        target.write_text(json.dumps(state))
    else:
        target.unlink(missing_ok=True)


def test_finished_and_closed_subagents_stay_out_of_the_record_older_bridges_save(store):
    """Bridges started before this change still run in other windows and rewrite that file."""
    session_id = "chat-shared"
    legacy, ended = subagents.path(store, session_id), subagents.ended_path(store, session_id)
    assert ended == store.root / "acp" / "closed-subagents" / legacy.name
    finish(store, session_id, "done", title="Done")
    subagents.record(store, session_id, subagents.STARTED, {"agentId": "busy", "title": "Busy"})
    assert set(json.loads(legacy.read_text())) == {"session_id", "running", "interrupted"}
    assert set(json.loads(ended.read_text())) == {"session_id", "finished", "closed"}
    assert ended.parent.stat().st_mode & 0o777 == 0o700
    # The shared record is still removed as soon as nothing is running or interrupted.
    subagents.record(store, session_id, subagents.COMPLETED, {"agentId": "busy", "success": True})
    assert not legacy.exists()
    left = {"x": {"title": "X", "task": "Do X", "reason": "usage ran out"}}
    for interrupted in (left, {}):
        old_bridge_saves(store, session_id, interrupted)
        assert list(subagents.load_ended(store, session_id)["finished"]) == ["done", "busy"]
    subagents.interrupt_running(store, session_id)
    for interrupted in (left, {}):
        old_bridge_saves(store, session_id, interrupted)
        assert list(subagents.closed(store, session_id)) == ["done", "busy"]


def test_unreadable_closed_subagents_file_counts_as_empty(store):
    session_id = "chat-unreadable"
    target = subagents.ended_path(store, session_id)
    target.parent.mkdir(parents=True)
    other = {"session_id": "another-chat", "finished": {}, "closed": {"a": {"title": "A"}}}
    wrong = {"session_id": session_id, "finished": {}, "closed": ["a"]}
    for content in ("not json", "[]", json.dumps(other), json.dumps(wrong)):
        target.write_text(content)
        assert subagents.closed(store, session_id) == {}
    finish(store, session_id, "a", title="A")
    assert list(subagents.load_ended(store, session_id)["finished"]) == ["a"]


def test_closed_subagents_are_kept_until_they_are_forgotten(store):
    session_id = "chat-closed"
    for agent in ("a", "b", "c"):
        finish(store, session_id, agent, title=agent.upper())
    subagents.interrupt_running(store, session_id)
    quota = "[Error] daily usage quota has been exhausted"
    finish(store, session_id, "q", success=False, summary=quota)
    assert list(subagents.closed(store, session_id)) == ["a", "b", "c"]
    subagents.forget_closed(store, session_id, ["a", "c", "q", "unseen"])
    assert list(subagents.closed(store, session_id)) == ["b"]
    assert list(subagents.interrupted(store, session_id)) == ["q"]
    subagents.forget(store, session_id, ["q", "b"])
    # Closed subagents outlive the shared record, which goes once nothing is interrupted.
    assert not subagents.path(store, session_id).exists()
    assert list(subagents.closed(store, session_id)) == ["b"]
    subagents.forget_closed(store, session_id, {"b": {}})
    assert not subagents.ended_path(store, session_id).exists()


def test_closed_subagents_note_names_them_and_says_what_no_longer_works():
    note = subagents.closed_note(
        {
            "a1": {"title": "Fix tests", "profile": "coder"},
            "b2": {"title": "", "profile": "reviewer"},
            "c3": {"title": "", "profile": ""},
        }
    )
    assert note.startswith("[Note from Devin Switch, not typed by the user] ")
    lines = note.splitlines()
    assert lines[1:-1] == ['- "Fix tests" (a1)', '- "reviewer" (b2)', '- "subagent" (c3)']
    assert "agent process that ran these subagents has ended" in lines[0]
    for fact in (
        "results were already delivered earlier in this conversation",
        "read_subagent and run_subagent with resume no longer work for these IDs",
        '"No subagent found"',
        "start a new subagent with the context it needs",
    ):
        assert fact in lines[-1]
    assert "#" not in note and len(f"{lines[0]} {lines[-1]}".split()) < 90
    many = {f"id-{n}": {"title": f"Task {n}"} for n in range(23)}
    assert subagents.closed_note(many).splitlines()[1:-1] == [
        *(f'- "Task {n}" (id-{n})' for n in range(3, 23)),
        "- and 3 more",
    ]
    exact = dict(list(many.items())[:20])
    assert len(subagents.closed_note(exact).splitlines()) == 22


def prompt_texts(native):
    """Every text block of each prompt the native agents received."""
    return [call["texts"] for call in logged(native) if call["method"] == "session/prompt"]


def recorded_prompts(backend):
    """The prompts a reload during a turn would replay."""
    updates = [json.loads(text)["params"]["update"] for text in backend.transcript]
    return [
        update["content"]["text"]
        for update in updates
        if update["sessionUpdate"] == "user_message_chunk"
    ]


async def chat_with_finished_subagents(bridge, tmp_path, replaced=None):
    """Open a chat, finish subagents f1 and f2 in it, then replace its agent process."""
    created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
    session_id = created["sessionId"]
    chat = bridge.chats[session_id]
    await bridge.dispatch("session/prompt", prompt_params(session_id, "finished-subagents"))
    if replaced == "idle":
        # The next prompt reconnects the chat on a new process.
        chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
        assert await bridge.release_idle(chat)
    elif replaced == "switch":
        await bridge.dispatch("session/prompt", prompt_params(session_id, "/switch ansuman-2"))
    elif replaced == "killed":
        chat.backend.process.kill()
        await until(lambda: chat.backend.process.poll() is not None)
        await bridge.dispatch("session/load", chat.setup)
    return session_id


@pytest.mark.parametrize("replaced", ["idle", "switch", "killed"])
def test_gui_first_prompt_to_a_replacement_process_names_the_closed_subagents(
    acp_native, tmp_path, replaced
):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, replaced)
            ended = subagents.load_ended(acp_native.store, session_id)
            params = prompt_params(session_id, "next")
            await bridge.dispatch("session/prompt", params)
            assert params == prompt_params(session_id, "next")
            text, note = prompt_texts(acp_native)[-1]
            # A reload shows a prompt's blocks joined as they are: the note needs its own lines.
            assert text == "next" and note.startswith("\n\n[Note from Devin Switch, not typed")
            assert note[2:] == subagents.closed_note({**ended["closed"], **ended["finished"]})
            assert '"Title f1" (f1)' in note and '"Title f2" (f2)' in note
            assert subagents.closed(acp_native.store, session_id) == {}
            await bridge.dispatch("session/prompt", prompt_params(session_id, "again"))
            assert prompt_texts(acp_native)[-1] == ["again"]
            assert recorded_prompts(bridge.chats[session_id].backend) == ["next", "again"]

    asyncio.run(scenario())


def test_gui_finished_subagents_stay_usable_in_their_own_process(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path)
            await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            # Desktop reloading the open chat keeps its process, and with it the subagents.
            await bridge.dispatch("session/load", bridge.chats[session_id].setup)
            await bridge.dispatch("session/prompt", prompt_params(session_id, "again"))
            assert prompt_texts(acp_native) == [["finished-subagents"], ["next"], ["again"]]
            calls = [call for call in logged(acp_native) if call["sessionId"] == session_id]
            assert len({call["pid"] for call in calls}) == 1
            ended = subagents.load_ended(acp_native.store, session_id)
            assert list(ended["finished"]) == ["f1", "f2"] and ended["closed"] == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "left",
    [{}, {"x": {"title": "X", "task": "Do X", "reason": "usage ran out"}}],
    ids=["deletes", "rewrites"],
)
def test_gui_closed_subagents_note_survives_an_older_bridge_saving_the_record(
    acp_native, tmp_path, left
):
    store = acp_native.store

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path)
            old_bridge_saves(store, session_id, left)
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/switch ansuman-2"))
            old_bridge_saves(store, session_id, left)
            await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            text, note = prompt_texts(acp_native)[-1]
            assert text == "next" and '"Title f1" (f1)' in note and '"Title f2" (f2)' in note
            assert subagents.interrupted(store, session_id) == left

    asyncio.run(scenario())


def test_gui_slash_command_is_forwarded_as_typed_and_leaves_the_subagents_note_for_later(
    acp_native, tmp_path
):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, "idle")
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/help topics"))
            assert prompt_texts(acp_native)[-1] == ["/help topics"]
            assert list(subagents.closed(acp_native.store, session_id)) == ["f1", "f2"]
            await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            text, note = prompt_texts(acp_native)[-1]
            assert text == "next" and '"Title f1" (f1)' in note and '"Title f2" (f2)' in note

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
def test_gui_closed_subagents_note_is_repeated_until_a_prompt_gets_through(
    acp_native, tmp_path, ending
):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, "idle")
            if ending == "failed":
                with pytest.raises(acp.RpcError):
                    await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            else:
                turn = asyncio.create_task(
                    bridge.dispatch("session/prompt", prompt_params(session_id, "wait"))
                )
                await until(lambda: prompt_texts(acp_native)[-1][0] == "wait")
                await bridge.dispatch(
                    "session/cancel", {"sessionId": session_id}, notification=True
                )
                assert await turn == {"stopReason": "cancelled"}
            _, note = prompt_texts(acp_native)[-1]
            assert '"Title f1" (f1)' in note and '"Title f2" (f2)' in note
            await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            assert prompt_texts(acp_native)[-1] == ["next", note]
            await bridge.dispatch("session/prompt", prompt_params(session_id, "again"))
            assert prompt_texts(acp_native)[-1] == ["again"]

    asyncio.run(scenario())


def test_gui_auto_switch_continue_repeats_the_subagents_note_its_failed_prompt_carried(
    acp_native, tmp_path, monkeypatch
):
    monkeypatch.setattr(usage, "refresh_account", fresh_usage())
    monkeypatch.setenv("FAKE_QUOTA_ACCOUNTS", "ansuman-2")

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, "switch")
            result = await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            assert result == {"stopReason": "end_turn"}
            accounts = [account for account, _ in prompts(acp_native)[-2:]]
            assert accounts == ["ansuman-2", "ansuman-1"]
            failed, continued = prompt_texts(acp_native)[-2:]
            assert failed[0] == "next" and continued == ["continue", failed[1]]
            assert '"Title f1" (f1)' in failed[1] and '"Title f2" (f2)' in failed[1]
            await bridge.dispatch("session/prompt", prompt_params(session_id, "again"))
            assert prompt_texts(acp_native)[-1] == ["again"]

    asyncio.run(scenario())


def test_gui_steering_prompt_neither_carries_nor_forgets_the_closed_subagents_note(
    acp_native, tmp_path
):
    """A prompt sent during a turn goes straight to the native, not through a turn of its own."""
    store = acp_native.store

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, "switch")
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "busy"))
            )
            await until(lambda: "live progress" in texts(messages))
            steer = await bridge.dispatch("session/prompt", prompt_params(session_id, "steer now"))
            assert steer == {"stopReason": "end_turn"} and not turn.done()
            (busy, note), steered = prompt_texts(acp_native)[-2:]
            assert busy == "busy" and '"Title f1" (f1)' in note and steered == ["steer now"]
            assert list(subagents.closed(store, session_id)) == ["f1", "f2"]
            await bridge.dispatch("session/cancel", {"sessionId": session_id}, notification=True)
            assert await turn == {"stopReason": "cancelled"}
            await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            assert prompt_texts(acp_native)[-1] == ["next", note]
            assert subagents.closed(store, session_id) == {}

    asyncio.run(scenario())


def test_gui_resume_subagents_prompt_also_carries_the_closed_subagents_note(
    acp_native, relayed, tmp_path, monkeypatch
):
    store = acp_native.store
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(tmp_path / "never-opened"))

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path)
            chat = bridge.chats[session_id]
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "subagents"))
            )
            await until(lambda: set(subagents.load(store, session_id)["running"]) == {"s1", "s2"})
            chat.backend.process.kill()
            with pytest.raises(SwitchError):
                await turn
            await bridge.dispatch("session/load", chat.setup)
            assert set(subagents.interrupted(store, session_id)) == {"s1", "s2"}
            sent = len(prompt_texts(acp_native))
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/resume-subagents"))
            ((resume, note),) = prompt_texts(acp_native)[sent:]
            assert "2 of your subagents stopped before finishing" in resume
            assert "Task for s1" in resume and "Task for s2" in resume
            assert note.startswith("\n\n[Note from Devin Switch, not typed by the user] ")
            assert note.splitlines()[3:-1] == ['- "Title f1" (f1)', '- "Title f2" (f2)']
            assert subagents.interrupted(store, session_id) == {}
            assert subagents.closed(store, session_id) == {}

    asyncio.run(scenario())


def test_gui_prompt_sent_while_the_closed_subagents_are_read_waits_for_its_own_turn(
    acp_native, tmp_path, monkeypatch
):
    """Reading the record must not open the turn: a second prompt would overtake as steering."""
    reading, finish_reading = threading.Event(), threading.Event()
    closed = subagents.closed

    def slow(store, session_id):
        reading.set()
        finish_reading.wait(10)
        return closed(store, session_id)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, "idle")
            monkeypatch.setattr(subagents, "closed", slow)
            first = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "first"))
            )
            await asyncio.to_thread(reading.wait, 10)
            second = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "second"))
            )
            try:
                await asyncio.sleep(0.3)
                assert prompt_texts(acp_native)[-1] == ["finished-subagents"]
            finally:
                finish_reading.set()
            await asyncio.gather(first, second)
            sent = prompt_texts(acp_native)[-2:]
            assert [len(texts) for texts in sent] == [2, 1]
            assert [texts[0] for texts in sent] == ["first", "second"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failing", "error", "blocks"),
    [("closed", OSError("unreadable"), 1), ("forget_closed", SwitchError("locked"), 2)],
    ids=["reading", "forgetting"],
)
def test_gui_prompt_is_sent_when_the_closed_subagents_record_fails(
    acp_native, tmp_path, monkeypatch, failing, error, blocks
):
    def broken(*_):
        raise error

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            session_id = await chat_with_finished_subagents(bridge, tmp_path, "idle")
            with monkeypatch.context() as patch:
                patch.setattr(subagents, failing, broken)
                result = await bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            assert result == {"stopReason": "end_turn"}
            assert len(prompt_texts(acp_native)[-1]) == blocks
            # Nothing was forgotten, so the next prompt still says it.
            await bridge.dispatch("session/prompt", prompt_params(session_id, "again"))
            text, note = prompt_texts(acp_native)[-1]
            assert text == "again" and '"Title f1" (f1)' in note

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["auto-switch-off", "no-login-left"])
def test_gui_relay_returns_quota_unchanged_when_it_cannot_rotate(
    acp_native, relayed, tmp_path, monkeypatch, case
):
    store = acp_native.store
    left = {"ansuman-1"} if case == "auto-switch-off" else {"ansuman-1", "ansuman-2", "ansuman-3"}
    monkeypatch.setattr(usage, "refresh_account", fresh_usage(exhausted=left))
    if case == "auto-switch-off":
        acp.handoff.set_auto_switch(store, False)
    relayed.exhausted = {"ansuman-1"}

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            with pytest.raises(acp.RpcError) as raised:
                await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            assert raised.value.code == -32011
            assert relayed.inference() == [("ansuman-1", "ansuman-1")]
            assert bridge.chats[session_id].backend.account.name == "ansuman-1"
            assert store.selected().name == "ansuman-1"
            assert acp_state.bound_account(store, session_id).name == "ansuman-1"
            assert prompts(acp_native) == [("ansuman-1", "infer")]

    asyncio.run(scenario())


def test_gui_quota_that_reaches_the_agent_continues_in_the_same_process(
    acp_native, relayed, tmp_path, monkeypatch
):
    store = acp_native.store
    monkeypatch.setattr(usage, "refresh_account", fresh_usage(exhausted={"ansuman-1"}))
    # The relay cannot replay this request, so the agent sees the quota error itself.
    monkeypatch.setattr(acp.relay, "ROTATION_LIMIT", 0)
    relayed.exhausted = {"ansuman-1"}

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            result = await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            assert result == {"stopReason": "end_turn"}
            assert bridge.chats[session_id].backend is backend
            assert backend.account.name == backend.relay.active.account.name == "ansuman-2"
            assert acp_state.bound_account(store, session_id).name == "ansuman-2"
            await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            assert relayed.inference()[-1] == ("ansuman-2", "ansuman-2")
            calls = [call for call in logged(acp_native) if call["text"] is not None]
            assert [call["text"] for call in calls] == ["infer", "continue", "infer"]
            assert len({call["pid"] for call in calls}) == 1
            text = json.dumps(messages)
            assert "Out of usage on ansuman-1. Continuing on ansuman-2 in the same agent" in text
            log = (store.root / "logs" / "relay.log").read_text()
            assert "quota passed to the agent" in log and "recover ansuman-1 -> ansuman-2" in log
            assert all(relay_key not in log for relay_key in relayed.keys)

    asyncio.run(scenario())


def test_gui_relay_issues_each_handoff_destination_its_own_login(acp_native, relayed, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            first = bridge.chats[session_id].backend
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/switch ansuman-2"))
            assert first.relay is None
            await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            assert relayed.inference() == [("ansuman-2", "ansuman-2")]

    asyncio.run(scenario())


def test_gui_failed_host_login_keeps_saved_credentials(acp_native, relayed, tmp_path):
    relayed.close()

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            assert bridge.chats[session_id].backend.relay is None
            assert await bridge.dispatch("session/prompt", prompt_params(session_id, "work")) == {
                "stopReason": "end_turn"
            }

    asyncio.run(scenario())


def test_gui_relay_login_reaches_a_cli_that_checks_its_api_server(
    acp_native, relayed, tmp_path, monkeypatch
):
    """CLI 3000.14 refuses a host login for another server than the one it was started on."""
    monkeypatch.setenv("FAKE_PROCESS_MODEL", "1")
    monkeypatch.setattr(usage, "refresh_account", fresh_usage(exhausted={"ansuman-3"}))
    relayed.exhausted = {"ansuman-1"}

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            backend = bridge.chats[session_id].backend
            assert backend.relay is not None
            assert await bridge.dispatch("session/prompt", prompt_params(session_id, "infer")) == {
                "stopReason": "end_turn"
            }
            # Usage ran out, and the same native went on with the next login.
            assert bridge.chats[session_id].backend is backend
            assert relayed.inference()[-1] == ("ansuman-2", "ansuman-2")

    asyncio.run(scenario())


def test_gui_failed_host_login_goes_through_the_relay_the_cli_was_started_on(
    acp_native, relayed, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_PROCESS_MODEL", "1")
    monkeypatch.setattr(acp.Backend, "request", refuse_host_login(acp.Backend.request))

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            session_id = created["sessionId"]
            # Closing it would cut the native off: its saved login uses the relay's address too.
            assert bridge.chats[session_id].backend.relay is not None
            await bridge.dispatch("session/prompt", prompt_params(session_id, "infer"))
            assert relayed.inference() == [("ansuman-1", "ansuman-1")]

    asyncio.run(scenario())


def refuse_host_login(request):
    async def refusing(backend, method, params, *arguments):
        if method == "authenticate":
            raise acp.RpcError("Authentication failed", -32603)
        return await request(backend, method, params, *arguments)

    return refusing


def test_gui_chats_without_a_saved_api_login_skip_the_relay(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            created = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            assert bridge.chats[created["sessionId"]].backend.relay is None
            assert "authenticate" not in [call["method"] for call in logged(acp_native)]

    asyncio.run(scenario())


def test_bridge_shares_idle_logins_tool_folders_in_the_background(acp_native, tmp_path):
    store = acp_native.store
    idle = store.directory("ansuman-2") / "cache/uv"
    idle.mkdir()
    (idle / "a.whl").write_text("built once")
    # The bridge's own listing native holds the default login, like any running launch.
    held = store.directory("ansuman-1") / "cache/pip"
    held.mkdir()

    async def scenario():
        async with connect(acp_native) as client:
            for _ in range(200):
                if idle.is_symlink():
                    break
                await asyncio.sleep(0.05)
            assert (store.tools("cache") / "uv/a.whl").read_text() == "built once"
            assert (idle / "a.whl").read_text() == "built once"
            assert not held.is_symlink() and not (store.tools("cache") / "pip").exists()
            # The default login was probed, never locked: its chats still open.
            session_id = await client.new(tmp_path)
            assert (await client.prompt(session_id, "work"))["result"]["stopReason"] == "end_turn"
            assert client.agent_replies()[-1]["account"] == "ansuman-1"

    asyncio.run(scenario())


def test_bridge_prunes_what_is_never_deleted_in_the_background(acp_native):
    store = acp_native.store
    week = sessions.PRUNE_AGE
    logs = store.root / "shared/cli/logs"
    logs.mkdir()
    old, running = logs / "finished.log", logs / "running.log"
    for path, age in ((old, week + 3600), (running, 0)):
        path.write_text("native log")
        os.utime(path, (time.time() - age, time.time() - age))
    # An agent browser copy that is never opened again keeps its last web cache.
    copy = store.root / "browser/profiles/profile-3/user-data"
    (copy / "Default/Cache/Cache_Data").mkdir(parents=True)
    (copy / "Default/Cache/Cache_Data/f_000001").write_text("web cache")
    (copy / "Default/Cookies").write_text("logins")

    async def scenario():
        async with connect(acp_native):
            for _ in range(200):
                if not old.exists() and not (copy / "Default/Cache").exists():
                    break
                await asyncio.sleep(0.05)
            assert not old.exists()
            assert running.read_text() == "native log"
            assert not (copy / "Default/Cache").exists()
            assert (copy / "Default/Cookies").read_text() == "logins"

    asyncio.run(scenario())
