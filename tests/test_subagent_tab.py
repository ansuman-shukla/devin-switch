import asyncio
import json
import re
import time

import pytest
from test_acp import acp_native as acp_native  # noqa: F401 (a fixture)
from test_acp import in_process, prompt_params, subagent_events, until

from devin_switch import acp, subagent_tab
from devin_switch.store import SwitchError

AGENT = "acb63708"
STARTED = "cognition.ai/subagent_started"
COMPLETED = "cognition.ai/subagent_completed"
PAUSED = "cognition.ai/subagent_paused"
CONTEXT = "cognition.ai/subagent_context"
ROOT = {CONTEXT: {"parentAgentId": "root"}}
# A subagent's steps, as captured from CLI 3000.12.43.
TOOL = {
    "sessionUpdate": "tool_call",
    "toolCallId": "call_ab18",
    "title": "Running command",
    "kind": "execute",
}
RAN = {"sessionUpdate": "tool_call_update", "toolCallId": "call_ab18", "title": "Ran sleep"}
THOUGHT = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}}
MESSAGE = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done"}}
USAGE = {"sessionUpdate": "usage_update", "used": 1, "size": 10}


def started(agent=AGENT, **details):
    event = {
        "agentId": agent,
        "title": "Sleeper",
        "task": "Run sleep 30, then report the secret plan",
        "profile": "General",
        "depth": 1,
        "isBackground": True,
        "model": "SWE-1.6",
        **details,
    }
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "status": "in_progress",
        "_meta": {**ROOT, STARTED: event},
    }


def completed(agent=AGENT, success=True, summary="Slept for 30 seconds."):
    event = {"agentId": agent, "success": success, "summary": summary, "depth": 1}
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "status": "completed",
        "_meta": {**ROOT, COMPLETED: event},
    }


def paused(agent, value):
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "_meta": {**ROOT, PAUSED: {"agentId": agent, "paused": value}},
    }


def by(agent, change):
    """An update produced by `agent` ("root" is the chat's own agent)."""
    return {**change, "_meta": {CONTEXT: {"parentAgentId": agent}}}


def rows(roster, session_id="chat"):
    return roster.snapshot(session_id)["subagents"]


def row(roster, agent=AGENT, session_id="chat"):
    (found,) = [item for item in rows(roster, session_id) if item["id"] == agent]
    return found


def states(roster, session_id="chat"):
    return {item["id"]: item["status"] for item in rows(roster, session_id)}


def test_a_started_subagent_is_a_running_row_that_never_carries_its_task():
    roster = subagent_tab.Roster()
    before = time.time()
    roster.apply("chat", started())
    (only,) = rows(roster)
    began = only.pop("started")
    assert isinstance(began, float) and before <= began <= time.time()
    assert only == {
        "id": AGENT,
        "title": "Sleeper",
        "profile": "General",
        "background": True,
        "status": "running",
        "ended": None,
        "step": "",
        "note": "",
    }
    assert "secret plan" not in json.dumps(roster.snapshot("chat"))
    assert rows(roster, "other-chat") == []

    roster.apply("chat", started("b2", title="Second", isBackground=False))
    assert [(item["id"], item["title"], item["background"]) for item in rows(roster)] == [
        (AGENT, "Sleeper", True),
        ("b2", "Second", False),
    ]
    # Started again (the native resumes a subagent under its ID): one row, from the new start.
    roster.apply("chat", completed())
    roster.apply("chat", started(title="Sleeper again"))
    assert [(item["id"], item["status"], item["ended"]) for item in rows(roster)] == [
        ("b2", "running", None),
        (AGENT, "running", None),
    ]
    assert row(roster)["title"] == "Sleeper again" and row(roster)["started"] >= began


def test_the_step_follows_only_that_subagents_own_activity():
    roster = subagent_tab.Roster()
    roster.apply("chat", started())
    roster.apply("chat", started("b2"))
    finished = {"sessionUpdate": "tool_call_update", "toolCallId": "call_ab18", "status": "done"}
    for change, step in (
        (THOUGHT, "Thinking"),
        (TOOL, "Running command"),
        (USAGE, "Running command"),
        (RAN, "Ran sleep"),
        ({**RAN, "title": "  "}, "Ran sleep"),
        (finished, "Ran sleep"),
        (MESSAGE, "Writing"),
        ({**TOOL, "title": "  Reading notes.md \nsecond line"}, "Reading notes.md"),
        ({**TOOL, "title": "x" * 5000}, "x" * subagent_tab.TEXT_LIMIT),
    ):
        roster.apply("chat", by(AGENT, change))
        assert row(roster)["step"] == step
    roster.apply("chat", by(AGENT, MESSAGE))

    # The chat's own agent, unknown agents and other chats never move it.
    roster.apply("chat", by("root", TOOL))
    roster.apply("chat", by("gone", TOOL))
    roster.apply("chat", TOOL)
    roster.apply("other-chat", by(AGENT, TOOL))
    assert (row(roster)["step"], row(roster, "b2")["step"]) == ("Writing", "")
    assert list(states(roster)) == [AGENT, "b2"] and rows(roster, "other-chat") == []
    roster.apply("chat", by("b2", TOOL))
    assert (row(roster)["step"], row(roster, "b2")["step"]) == ("Writing", "Running command")
    assert states(roster) == {AGENT: "running", "b2": "running"}


def test_a_subagent_pauses_and_resumes():
    roster = subagent_tab.Roster()
    roster.apply("chat", started())
    roster.apply("chat", started("b2"))
    roster.apply("chat", paused(AGENT, True))
    assert states(roster) == {AGENT: "paused", "b2": "running"}
    assert row(roster)["ended"] is None
    roster.apply("chat", paused(AGENT, False))
    assert states(roster) == {AGENT: "running", "b2": "running"}
    # A paused one can still finish; a finished one is never paused back to life.
    roster.apply("chat", paused(AGENT, True))
    roster.apply("chat", completed())
    roster.apply("chat", paused(AGENT, True))
    assert states(roster) == {AGENT: "done", "b2": "running"}


def test_a_finished_subagent_is_done_or_failed_with_the_first_line_of_why():
    roster = subagent_tab.Roster()
    for agent in ("ok", "bad", "silent"):
        roster.apply("chat", started(agent))
    roster.apply("chat", by("bad", RAN))
    roster.apply("chat", completed("ok", True, "All good\nmore"))
    roster.apply("chat", completed("bad", False, "\n[Error] Canceled by user\nTraceback: …"))
    # No verdict is not a success.
    roster.apply("chat", {"_meta": {COMPLETED: {"agentId": "silent"}}})
    assert [(item["id"], item["status"], item["note"]) for item in rows(roster)] == [
        ("ok", "done", ""),
        ("bad", "failed", "[Error] Canceled by user"),
        ("silent", "failed", ""),
    ]
    assert row(roster, "bad")["step"] == "Ran sleep"
    for item in rows(roster):
        assert isinstance(item["ended"], float) and item["started"] <= item["ended"] <= time.time()

    # A repeated completion never rewrites the first one.
    before = rows(roster)
    roster.apply("chat", completed("bad", True, "fine after all"))
    roster.apply("chat", completed("ok", False, "[Error] late"))
    assert rows(roster) == before


def test_interrupt_stops_only_the_rows_still_active():
    roster = subagent_tab.Roster()
    for agent in ("running", "paused", "done", "failed"):
        roster.apply("chat", started(agent))
    roster.apply("other-chat", started("elsewhere"))
    roster.apply("chat", paused("paused", True))
    roster.apply("chat", completed("done"))
    roster.apply("chat", completed("failed", False, "[Error] Out of usage"))
    finished = [row(roster, agent) for agent in ("done", "failed")]

    roster.interrupt("chat")
    assert states(roster) == {
        "running": "stopped",
        "paused": "stopped",
        "done": "done",
        "failed": "failed",
    }
    for agent in ("running", "paused"):
        stopped = row(roster, agent)
        assert stopped["note"] == "the agent restarted"
        assert stopped["started"] <= stopped["ended"] <= time.time()
    assert [row(roster, agent) for agent in ("done", "failed")] == finished
    assert states(roster, "other-chat") == {"elsewhere": "running"}

    # Stopped is final: nothing a later event says changes it.
    before = rows(roster)
    roster.interrupt("chat")
    roster.apply("chat", completed("running"))
    roster.apply("chat", paused("paused", False))
    assert rows(roster) == before


def test_forget_drops_only_that_chats_rows():
    roster = subagent_tab.Roster()
    roster.apply("chat", started())
    roster.apply("other-chat", started("elsewhere"))
    roster.forget("chat")
    roster.forget("never-seen")
    assert rows(roster) == [] and states(roster, "other-chat") == {"elsewhere": "running"}
    # Its later steps find nothing to attach to.
    roster.apply("chat", by(AGENT, TOOL))
    roster.apply("chat", completed())
    assert rows(roster) == []


def test_a_long_chat_keeps_its_active_rows_and_the_newest_finished_ones():
    roster, cap = subagent_tab.Roster(), subagent_tab.MAX_ROWS
    roster.apply("chat", started("first"))
    for number in range(cap + 50):
        roster.apply("chat", started(f"job-{number}"))
        roster.apply("chat", completed(f"job-{number}"))
    roster.apply("chat", started("last"))
    kept = list(states(roster))
    assert len(kept) == cap
    assert kept == ["first", *(f"job-{number}" for number in range(52, cap + 50)), "last"]
    assert states(roster)["first"] == states(roster)["last"] == "running"

    # Active rows are never dropped, even past the cap.
    busy = subagent_tab.Roster()
    for number in range(cap + 5):
        busy.apply("chat", started(f"job-{number}"))
    assert len(rows(busy)) == cap + 5 and set(states(busy).values()) == {"running"}
    busy.apply("chat", completed("job-3"))
    busy.apply("chat", started("one-more"))
    assert len(rows(busy)) == cap + 5 and "job-3" not in states(busy)
    assert "job-0" in states(busy) and "one-more" in states(busy)


def test_malformed_updates_are_ignored_without_raising():
    roster = subagent_tab.Roster()
    roster.apply("chat", started())
    roster.apply("chat", by(AGENT, TOOL))
    before = rows(roster)
    odd_ids = (None, "", 7, ["a"], {"a": 1})
    with roster.listen("chat") as queue:
        queue.get_nowait()
        for meta in (
            None,
            "text",
            [STARTED],
            {},
            *({key: value} for key in (STARTED, COMPLETED, PAUSED) for value in (None, "x", [1])),
            *({key: {"agentId": odd}} for key in (STARTED, COMPLETED, PAUSED) for odd in odd_ids),
            {key: {"title": "No ID"} for key in (STARTED, COMPLETED, PAUSED)},
            {COMPLETED: {"agentId": "nobody", "success": True}},
            {PAUSED: {"agentId": "nobody", "paused": True}},
            {PAUSED: {"agentId": AGENT}},
            {PAUSED: {"agentId": AGENT, "paused": "yes"}},
            *({CONTEXT: value} for value in (None, "root", [AGENT], {})),
            *({CONTEXT: {"parentAgentId": odd}} for odd in odd_ids),
        ):
            roster.apply("chat", {"sessionUpdate": "tool_call", "title": "Bad", "_meta": meta})
        roster.apply("chat", {"sessionUpdate": "tool_call", "title": "No metadata"})
        for title in (None, 7, ["Running"], {"text": "Running"}):
            roster.apply("chat", by(AGENT, {"sessionUpdate": "tool_call", "title": title}))
        for kind in (None, 7, ["tool_call"], {"kind": "tool_call"}):
            roster.apply("chat", by(AGENT, {"sessionUpdate": kind, "title": "Odd kind"}))
        roster.apply("chat", by(AGENT, {"title": "No kind"}))
        assert rows(roster) == before and queue.empty()

    # Odd details still give a plain row.
    roster.apply("chat", started("odd", title=["x"], profile=None, isBackground="yes"))
    odd = row(roster, "odd")
    assert (odd["title"], odd["profile"], odd["background"]) == ("", "", False)


def test_a_page_gets_one_token_per_change_and_none_when_nothing_changed():
    roster = subagent_tab.Roster()
    assert not roster.showing("chat")
    with roster.listen("chat") as queue, roster.listen("other-chat") as other:
        assert roster.showing("chat") and roster.showing("other-chat")
        # Its first snapshot is waiting as soon as it connects.
        assert queue.get_nowait() is True and queue.empty()
        other.get_nowait()

        for change in (started(), by(AGENT, THOUGHT), by(AGENT, TOOL), paused(AGENT, True)):
            roster.apply("chat", change)
            assert queue.get_nowait() is True and queue.empty()

        # Chunks arrive many times a second: the same step again is not a change.
        for change in (TOOL, TOOL, USAGE):
            roster.apply("chat", by(AGENT, change))
        roster.apply("chat", by(AGENT, THOUGHT))
        queue.get_nowait()
        for _ in range(20):
            roster.apply("chat", by(AGENT, THOUGHT))
        roster.apply("chat", paused(AGENT, True))
        roster.apply("chat", by("root", TOOL))
        roster.apply("chat", by("gone", TOOL))
        roster.forget("never-seen")
        assert queue.empty()

        # A page that is behind holds one token, whatever happened meanwhile.
        for change in (by(AGENT, MESSAGE), paused(AGENT, False), by(AGENT, RAN), completed()):
            roster.apply("chat", change)
        assert queue.qsize() == 1
        queue.get_nowait()

        roster.interrupt("chat")
        assert queue.empty()
        roster.apply("chat", started("b2"))
        queue.get_nowait()
        roster.interrupt("chat")
        assert queue.get_nowait() is True and queue.empty()
        roster.forget("chat")
        assert queue.get_nowait() is True and queue.empty()
        roster.forget("chat")
        assert queue.empty() and other.empty()
    assert not roster.showing("chat") and not roster.showing("other-chat")
    assert roster.listeners == {}


def test_every_page_of_a_chat_is_told_and_closing_ends_their_streams():
    roster = subagent_tab.Roster()
    with roster.listen("chat") as first:
        with roster.listen("chat") as second:
            first.get_nowait()
            roster.apply("chat", started())
            assert first.qsize() == second.qsize() == 1
        # One page left: the chat still counts as showing.
        assert roster.showing("chat")
        roster.close()
        assert [first.get_nowait(), first.get_nowait()] == [True, None]
        assert second.qsize() == 1
    assert not roster.showing("chat")


def test_the_page_shell_names_its_chat_token_and_three_files():
    shell = subagent_tab.page('chat-1<"&>', 'tok<en>&"x')
    token = "tok&lt;en&gt;&amp;&quot;x"
    assert "<title>Subagents</title>" in shell
    assert f'<body data-session="chat-1&lt;&quot;&amp;&gt;" data-token="{token}">' in shell
    assert re.findall(r'<link rel="stylesheet" href="([^"]*)">', shell) == [
        f"/subagents/assets/subagents.css?t={token}"
    ]
    assert re.findall(r'<script src="([^"]*)"></script>', shell) == [
        f"/subagents/assets/subagents-view.js?t={token}",
        f"/subagents/assets/subagents.js?t={token}",
    ]
    assert 'tok<en>&"x' not in shell and 'chat-1<"&>' not in shell


async def new_chat(bridge, folder):
    created = await bridge.dispatch("session/new", {"cwd": str(folder), "mcpServers": []})
    return bridge.chats[created["sessionId"]]


def delegate(bridge, chat):
    """Start a turn whose background subagent keeps working until the turn is stopped."""
    return asyncio.create_task(
        bridge.dispatch("session/prompt", prompt_params(chat.session_id, "delegate"))
    )


async def stop(bridge, chat, turn):
    await bridge.dispatch("session/cancel", {"sessionId": chat.session_id}, notification=True)
    assert await turn == {"stopReason": "cancelled"}


def test_live_subagents_become_rows_and_load_replays_add_none(acp_native, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_REPLAY_SUBAGENT", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster = chat.session_id, bridge.roster
            # Reloading the open chat replays an earlier turn's subagent in the live shapes.
            await bridge.dispatch("session/load", chat.setup)
            assert [event["agentId"] for event in subagent_events(messages, STARTED)] == [
                "9d2e41aa"
            ]
            assert rows(roster, session_id) == []

            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            await until(lambda: row(roster, AGENT, session_id)["step"] == "Running command")
            live = row(roster, AGENT, session_id)
            assert (live["title"], live["profile"], live["background"]) == (
                "Sleeper",
                "General",
                True,
            )
            await stop(bridge, chat, turn)
            (ended,) = rows(roster, session_id)
            assert (ended["status"], ended["note"]) == ("failed", "[Error] Canceled by user")
            assert "Run sleep 30" not in json.dumps(roster.snapshot(session_id))

            # A handoff's new native replays the chat as well, unseen by Desktop.
            start = len(messages)
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/switch ansuman-2"))
            assert chat.backend.account.name == "ansuman-2"
            assert subagent_events(messages[start:], STARTED) == []
            assert rows(roster, session_id) == [ended]

    asyncio.run(scenario())


@pytest.mark.parametrize("noticed", ["exit", "replacement"])
def test_subagents_a_dead_native_left_running_read_stopped(
    acp_native, tmp_path, monkeypatch, noticed
):
    if noticed == "replacement":
        # Should an exit ever go unnoticed, the native that takes over still knows.
        monkeypatch.setattr(acp.Bridge, "stop_subagents", lambda self, backend: None)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster = chat.session_id, bridge.roster
            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            chat.backend.process.kill()
            with pytest.raises(SwitchError):
                await turn
            if noticed == "exit":
                # At once: the tab must not show a dead subagent as running until a reload.
                await until(lambda: states(roster, session_id) == {AGENT: "stopped"})
            await bridge.dispatch("session/load", chat.setup)
            (stopped,) = rows(roster, session_id)
            assert (stopped["status"], stopped["note"]) == ("stopped", "the agent restarted")
            assert stopped["started"] <= stopped["ended"] <= time.time()

    asyncio.run(scenario())


def test_an_old_natives_late_exit_leaves_its_successors_subagents_alone(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster, old = chat.session_id, bridge.roster, chat.backend
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/switch ansuman-2"))
            assert chat.backend is not old and old.process.poll() is not None
            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            # The old native's cleanup can finish late (it waits on Desktop's pipe).
            bridge.stop_subagents(old)
            assert states(roster, session_id) == {AGENT: "running"}
            await stop(bridge, chat, turn)
            assert states(roster, session_id) == {AGENT: "failed"}

    asyncio.run(scenario())


def test_closing_a_chat_forgets_its_subagents(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat, other = await new_chat(bridge, tmp_path), await new_chat(bridge, tmp_path)
            turn = delegate(bridge, chat)
            await until(lambda: states(bridge.roster, chat.session_id) == {AGENT: "running"})
            bridge.roster.apply(other.session_id, started("elsewhere"))
            await stop(bridge, chat, turn)
            await bridge.dispatch("session/close", {"sessionId": chat.session_id})
            assert rows(bridge.roster, chat.session_id) == []
            assert states(bridge.roster, other.session_id) == {"elsewhere": "running"}

    asyncio.run(scenario())
