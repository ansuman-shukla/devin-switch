import asyncio

from test_acp import acp_native as acp_native  # noqa: F401 (a fixture)
from test_acp import (
    connect,
    logged,
    replayed,
    replayed_texts,
    wait_for_text,
)


def command_names(messages):
    (*_, latest) = replayed(messages, "available_commands_update")
    return {item["name"] for item in latest["availableCommands"]}


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
            "_meta" in message.get("params", {})
            and "cognition.ai/chain" in message["params"]["_meta"]
            for message in client.messages
        )
        assert replayed_texts(replay, "user_message_chunk") == ["side-noise", "busy"]
        assert "aside" not in replayed_texts(replay, "agent_message_chunk")
        # The native's own /btw would answer where Desktop can't show it.
        assert "btw" not in command_names(client.messages)
        assert "help" in command_names(client.messages)

    asyncio.run(scenario())
