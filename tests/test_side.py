import asyncio
import json
from urllib.parse import quote

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

QUOTED = (
    '\n<quoted_artifact kind="message" event_id="agent_message:1" author="devin">\n'
    "<caption>/btw and this?</caption>\n<content>\nx = 1\ny &lt; 2\n</content>\n</quoted_artifact>"
)


def previews(messages):
    return [
        update
        for update in replayed(messages, "tool_call")
        if update.get("_meta", {}).get("cognition.ai/eventType") == "browser_preview"
    ]


async def request(port, method, target, body=None, host=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = b"" if body is None else json.dumps(body).encode()
    writer.write(
        f"{method} {target} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode()
        + data
    )
    await writer.drain()
    status = int((await reader.readline()).split()[1])
    while (await reader.readline()).strip():
        pass
    return status, reader, writer


async def fetch(port, method, target, body=None, host=None):
    status, reader, writer = await request(port, method, target, body, host)
    content = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return status, content.decode()


async def lines(reader, count):
    """The next JSON lines of a streamed answer (or `data:` lines of the event stream)."""
    found = []
    async with asyncio.timeout(10):
        while len(found) < count:
            line = (await reader.readline()).decode().strip()
            assert line or not reader.at_eof(), "stream ended early"
            if line.startswith("data: "):
                found.append(json.loads(line[6:]))
            elif line.startswith("{"):
                found.append(json.loads(line))
    return found


class Page:
    """A chat's side page, found through the preview card a /btw puts in the chat."""

    def __init__(self, card):
        meta = card["_meta"]
        self.port = meta["cognition.ai/ports"][0]
        self.path = meta["cognition.ai/path"]
        self.token = self.path.split("?t=")[1]

    def url(self, route, extra=""):
        return f"/side/{route}?t={self.token}{extra}"

    async def ask(self, session_id, text, history=()):
        body = {"sessionId": session_id, "history": list(history), "text": text}
        status, reader, writer = await request(self.port, "POST", self.url("ask"), body)
        assert status == 200
        return reader, writer


async def open_page(client, session_id):
    start = len(client.messages)
    assert (await client.prompt(session_id, "/btw"))["result"] == {"stopReason": "end_turn"}
    (card,) = previews(client.messages[start:])
    return Page(card)


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


def test_btw_questions_come_typed_alone_or_as_a_quotes_comment():
    assert side.btw_question("  /btw why async?  ") == "why async?"
    assert side.btw_question("/btw") == ""
    assert side.btw_question("/btwx no") is None
    assert side.btw_question("why? /btw") is None
    assert side.btw_question(QUOTED) == "> x = 1\n> y < 2\n\nand this?"
    plain = QUOTED.replace("/btw and this?", "keep going")
    assert side.btw_question(plain) is None
    assert (
        side.btw_question(plain + "\n/btw what about it?") == "> x = 1\n> y < 2\n\nwhat about it?"
    )


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
        # The native's own /btw would answer where Desktop can't show it; Switch's replaces it.
        (*_, latest) = replayed(client.messages, "available_commands_update")
        descriptions = {item["name"]: item["description"] for item in latest["availableCommands"]}
        assert descriptions["btw"] != "Side question" and "help" in descriptions
        assert [item["name"] for item in latest["availableCommands"]].count("btw") == 1

    asyncio.run(scenario())


def test_btw_opens_the_side_page_in_the_chat_without_waiting_for_the_turn(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")

            start = len(client.messages)
            asked = await asyncio.wait_for(client.prompt(session_id, " /btw why async?"), 5)
            assert asked["result"] == {"stopReason": "end_turn"}
            (card,) = previews(client.messages[start:])
            page = Page(card)
            assert card["title"] == "Side chat" and card["status"] == "completed"
            meta = card["_meta"]
            assert meta["cognition.ai/name"] == "Side chat"
            assert page.path.startswith(f"/side/{quote(session_id, safe='')}?t=")
            assert meta["cognition.ai/url"] == f"http://127.0.0.1:{page.port}{page.path}"

            status, html = await fetch(page.port, "GET", page.path)
            assert status == 200 and f'data-session="{session_id}"' in html
            status, script = await fetch(page.port, "GET", page.url("assets/side.js"))
            assert status == 200 and "EventSource" in script

            # The page loads after the card opens it: the question waited for it.
            target = page.url("events", f"&session={quote(session_id, safe='')}")
            status, events, stream = await request(page.port, "GET", target)
            assert status == 200
            assert await lines(events, 1) == [{"text": "why async?"}]

            start = len(client.messages)
            await asyncio.wait_for(client.prompt(session_id, QUOTED), 5)
            assert await lines(events, 1) == [{"text": "> x = 1\n> y < 2\n\nand this?"}]
            assert previews(client.messages[start:]) == []
            # A bare /btw shows the open page again without asking anything.
            await asyncio.wait_for(client.prompt(session_id, "/btw"), 5)
            assert len(previews(client.messages[start:])) == 1
            stream.close()

            start = len(client.messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            await asyncio.wait_for(client.request("session/load", params), 5)
            assert len(previews(client.messages[start:])) == 2
            assert not turn.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            await turn

        assert not any("btw" in (call["text"] or "") for call in logged(acp_native))
        assert not any("why async" in text for text in client.texts())

    asyncio.run(scenario())


def test_side_page_answers_beside_the_running_turn(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            page = await open_page(client, session_id)
            await client.prompt(session_id, "hello")
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")

            history = [{"user": "What is X?", "assistant": "A cache."}]
            reader, writer = await page.ask(session_id, "Shared?", history)
            *updates, done = await lines(reader, 4)
            writer.close()
            assert done == {"done": "end_turn"}
            kinds = [line["update"]["sessionUpdate"] for line in updates]
            assert kinds == ["agent_thought_chunk", "usage_update", "agent_message_chunk"]
            answer = json.loads(updates[-1]["update"]["content"]["text"])
            assert answer["side"] == side.prompt_text(history, "Shared?")
            assert answer["session"] == session_id
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

        assert not any("Shared?" in text for text in client.texts())
        sides = [call for call in logged(acp_native) if call["chain"] == "side"]
        assert [(call["method"], call["sessionId"]) for call in sides] == [
            ("session/prompt", session_id)
        ]

    asyncio.run(scenario())


def test_side_page_stop_and_leaving_end_only_the_side_answer(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            page = await open_page(client, session_id)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")

            reader, writer = await page.ask(session_id, "side-wait")
            await lines(reader, 3)
            status, _ = await fetch(
                page.port, "POST", page.url("cancel"), {"sessionId": session_id}
            )
            assert status == 200
            assert await lines(reader, 1) == [{"done": "cancelled"}]
            writer.close()
            await asyncio.sleep(0.2)
            assert not turn.done()

            # Closing the page mid-answer stops that answer too.
            reader, writer = await page.ask(session_id, "side-wait")
            await lines(reader, 3)
            writer.close()
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


def test_side_page_needs_its_token_and_a_loopback_host(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            page = await open_page(client, session_id)
            wrong = page.path.replace(page.token, "wrong")
            assert (await fetch(page.port, "GET", wrong))[0] == 403
            assert (await fetch(page.port, "GET", page.path.split("?")[0]))[0] == 403
            assert (await fetch(page.port, "GET", page.path, host="evil.example"))[0] == 403
            assert (await fetch(page.port, "GET", page.url("assets/x.js")))[0] == 404
            assert (await fetch(page.port, "GET", page.path, host=f"localhost:{page.port}"))[
                0
            ] == 200

            reader, writer = await page.ask("not-open", "hi")
            (refused,) = await lines(reader, 1)
            assert "isn't open in this window" in refused["error"]
            writer.close()
            reader, writer = await page.ask(session_id, " ")
            assert await lines(reader, 1) == [{"error": "Type a side question first."}]
            writer.close()

        assert not any(call["chain"] == "side" for call in logged(acp_native))

    asyncio.run(scenario())


def test_side_question_reconnects_a_chat_released_for_idleness(acp_native, tmp_path):
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
