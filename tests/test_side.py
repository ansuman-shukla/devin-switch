import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from subprocess import PIPE

import pytest
from test_acp import acp_native as acp_native  # noqa: F401 (a fixture)
from test_acp import (
    connect,
    in_process,
    logged,
    replayed,
    replayed_texts,
    wait_for_text,
)

from devin_switch import acp, side
from devin_switch.store import SwitchError

WINDOW = 4242


def command_names(messages):
    (*_, latest) = replayed(messages, "available_commands_update")
    return {item["name"] for item in latest["availableCommands"]}


class Panel:
    """A Desktop window's side panel, talking to `ds side view` like the extension does."""

    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.pending = {}
        self.events = []
        self.pump = asyncio.create_task(self.read())

    @classmethod
    async def open(cls, store, host_pid=WINDOW):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "devin_switch",
            "side",
            "view",
            "--host-pid",
            str(host_pid),
            env={
                **os.environ,
                "DS_HOME": str(store.root),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        return cls(process)

    async def read(self):
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if "ok" in message and (future := self.pending.pop(message["id"], None)):
                future.set_result(message)
            else:
                self.events.append(message)

    def start(self, op, **fields):
        self.sequence += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.sequence] = future
        line = json.dumps({"id": self.sequence, "op": op, **fields}) + "\n"
        self.process.stdin.write(line.encode())
        return self.sequence, future

    async def ask(self, op, **fields):
        _, future = self.start(op, **fields)
        return await asyncio.wait_for(future, 15)

    def updates(self, identifier):
        return [
            event["update"]
            for event in self.events
            if event.get("id") == identifier and event.get("event") == "update"
        ]

    def answers(self, identifier):
        return [
            json.loads(update["content"]["text"])
            for update in self.updates(identifier)
            if update["sessionUpdate"] == "agent_message_chunk"
        ]

    async def close(self):
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 10)
        await self.pump


@pytest.fixture
def window(monkeypatch):
    """The bridge runs as an agent Desktop started from this window's extension host."""
    monkeypatch.setenv(side.HOST_VARIABLE, str(WINDOW))


def test_prompt_text_carries_the_earlier_side_turns_the_native_forgets():
    assert side.prompt_text([], "Why?") == "Why?"
    history = [{"user": "> quoted\n\nwhat is this?", "assistant": "A cache."}]
    assert side.prompt_text(history, "Is it shared?") == (
        "<side_chat_history>\nUser: > quoted\n\nwhat is this?\nYou: A cache.\n"
        "</side_chat_history>\n\nIs it shared?"
    )
    for history, text in (([], "  "), ([{"user": "q"}], "x"), ("q", "x"), ([], None)):
        with pytest.raises(SwitchError):
            side.prompt_text(history, text)


def test_gui_natives_answer_side_questions_that_never_reach_desktop(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "side-noise")
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            start = len(client.messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            await asyncio.wait_for(client.request("session/load", params), 5)
            replay = client.messages[start:]
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            await turn

        initialized = [call for call in logged(acp_native) if call["method"] == "initialize"]
        assert initialized and all(call["chains"] is True for call in initialized)
        assert "aside" not in client.texts()
        assert not any(
            "cognition.ai/chain" in message.get("params", {}).get("_meta", {})
            for message in client.messages
        )
        assert replayed_texts(replay, "user_message_chunk") == ["side-noise", "busy"]
        assert "aside" not in replayed_texts(replay, "agent_message_chunk")
        # The native's own /btw would answer where Desktop can't show it.
        assert "btw" not in command_names(client.messages)
        assert "help" in command_names(client.messages)

    asyncio.run(scenario())


def test_side_panel_answers_from_the_chat_beside_its_running_turn(acp_native, tmp_path, window):
    async def scenario():
        async with connect(acp_native) as client:
            other = await client.new(tmp_path / "other")
            session_id = await client.new(tmp_path)
            await client.prompt(session_id, "hello")
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            panel = await Panel.open(acp_native.store)

            listed = await panel.ask("chats")
            assert listed["ok"], listed
            assert [chat["sessionId"] for chat in listed["chats"]] == [session_id, other]
            assert listed["chats"][0]["title"] == "Test chat"
            assert listed["chats"][0]["folder"] == str(tmp_path)

            history = [{"user": "What is X?", "assistant": "A cache."}]
            identifier, answer = panel.start(
                "ask", sessionId=session_id, history=history, text="Shared?"
            )
            reply = await asyncio.wait_for(answer, 10)
            assert reply == {"id": identifier, "ok": True, "stopReason": "end_turn"}
            (answered,) = panel.answers(identifier)
            assert answered["side"] == side.prompt_text(history, "Shared?")
            assert answered["session"] == session_id
            kinds = [update["sessionUpdate"] for update in panel.updates(identifier)]
            assert kinds == ["agent_thought_chunk", "usage_update", "agent_message_chunk"]
            assert not turn.done()

            start = len(client.messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            await asyncio.wait_for(client.request("session/load", params), 5)
            assert replayed_texts(client.messages[start:], "user_message_chunk") == [
                "hello",
                "busy",
            ]
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"
            await panel.close()

        assert not any("Shared?" in text for text in client.texts())
        sides = [call for call in logged(acp_native) if call["chain"] == "side"]
        assert [(call["method"], call["sessionId"]) for call in sides] == [
            ("session/prompt", session_id)
        ]

    asyncio.run(scenario())


def test_side_panel_stop_ends_only_the_side_answer(acp_native, tmp_path, window):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")
            panel = await Panel.open(acp_native.store)
            identifier, answer = panel.start(
                "ask", sessionId=session_id, history=[], text="side-wait"
            )
            async with asyncio.timeout(10):
                while not panel.answers(identifier):
                    await asyncio.sleep(0.02)
            assert (await panel.ask("cancel", sessionId=session_id))["ok"]
            assert (await asyncio.wait_for(answer, 10))["stopReason"] == "cancelled"
            await asyncio.sleep(0.2)
            assert not turn.done()

            # Closing the panel mid-answer stops that answer too.
            identifier, answer = panel.start(
                "ask", sessionId=session_id, history=[], text="side-wait"
            )
            async with asyncio.timeout(10):
                while not panel.answers(identifier):
                    await asyncio.sleep(0.02)
            await panel.close()
            async with asyncio.timeout(10):
                while sum(1 for c in logged(acp_native) if c["method"] == "session/cancel") < 2:
                    await asyncio.sleep(0.02)
            assert not turn.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"

        cancels = [
            call["chain"] for call in logged(acp_native) if call["method"] == "session/cancel"
        ]
        assert cancels == ["side", "side", None]

    asyncio.run(scenario())


def test_side_panel_reaches_only_its_own_window_with_the_token(acp_native, tmp_path, window):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            (record,) = side.find(acp_native.store, WINDOW)
            path = side.records(acp_native.store) / f"{record['pid']}.json"
            assert path.stat().st_mode & 0o077 == 0
            assert side.find(acp_native.store, WINDOW + 1) == []

            stranger = await Panel.open(acp_native.store, WINDOW + 1)
            refused = await stranger.ask("chats")
            assert refused == {"id": 1, "ok": False, "error": side.NO_BRIDGE}
            await stranger.close()

            reader, writer = await asyncio.open_connection("127.0.0.1", record["port"])
            writer.write(b'{"token": "wrong"}\n{"id": 1, "op": "chats"}\n')
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()

            panel = await Panel.open(acp_native.store)
            missing = await panel.ask("ask", sessionId="not-open", history=[], text="hi")
            assert not missing["ok"] and "isn't open in this window" in missing["error"]
            empty = await panel.ask("ask", sessionId=session_id, history=[], text=" ")
            assert empty["error"] == "Type a side question first."
            await panel.close()
        assert list(side.records(acp_native.store).glob("*.json")) == []

    asyncio.run(scenario())


def write_record(store, pid, host_pid, port, token="t0k"):
    folder = side.records(store)
    folder.mkdir(exist_ok=True)
    record = {"pid": pid, "host_pid": host_pid, "port": port, "token": token}
    (folder / f"{pid}.json").write_text(json.dumps(record))
    return folder / f"{pid}.json"


def test_side_records_of_crashed_bridges_are_dropped(store):
    exited = subprocess.run((sys.executable, "-c", "import os; print(os.getpid())"), stdout=PIPE)
    stale = write_record(store, int(exited.stdout), WINDOW, 1)
    other = write_record(store, os.getpid(), WINDOW + 1, 2)
    assert side.find(store, WINDOW) == []
    assert not stale.exists() and other.exists()
    assert side.find(store, WINDOW + 1) == [json.loads(other.read_text())]


def test_side_panel_hears_when_its_bridge_goes_away_mid_answer(store):
    async def scenario():
        heard = []

        async def vanishing_bridge(reader, writer):
            heard.append(json.loads(await reader.readline()))
            heard.append(json.loads(await reader.readline()))
            writer.close()

        server = await asyncio.start_server(vanishing_bridge, "127.0.0.1", 0)
        write_record(store, os.getpid(), WINDOW, server.sockets[0].getsockname()[1])
        panel = await Panel.open(store)
        reply = await panel.ask("ask", sessionId="s", history=[], text="hi")
        assert reply == {"id": 1, "ok": False, "error": side.BRIDGE_GONE}
        assert heard[0] == {"token": "t0k"}
        assert heard[1] == {"id": 1, "op": "ask", "sessionId": "s", "history": [], "text": "hi"}
        await panel.close()
        server.close()

    asyncio.run(scenario())


def test_side_question_reconnects_a_chat_released_for_idleness(acp_native, tmp_path, window):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            result = await bridge.dispatch("session/new", {"cwd": str(tmp_path), "mcpServers": []})
            chat = bridge.chats[result["sessionId"]]
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            updates = []

            async def sink(update):
                updates.append(update)

            reply = await bridge.side_ask(chat.session_id, "still there?", sink)
            assert reply == {"stopReason": "end_turn"}
            assert not chat.suspended
            (prompt,) = [call for call in logged(acp_native) if call["chain"] == "side"]
            assert prompt["pid"] == chat.backend.process.pid
            assert json.loads(updates[-1]["content"]["text"])["side"] == "still there?"
            assert not any("still there?" in json.dumps(message) for message in messages)

    asyncio.run(scenario())
