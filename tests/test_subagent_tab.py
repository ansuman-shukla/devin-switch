import asyncio
import copy
import json
import re
import time
import types
from urllib.parse import quote, urlsplit

import pytest
from test_acp import acp_native as acp_native  # noqa: F401 (a fixture)
from test_acp import (
    connect,
    in_process,
    logged,
    prompt_params,
    prompts,
    replayed,
    replayed_texts,
    subagent_events,
    until,
    wait_for_text,
)
from test_side import fetch, lines, opened, request

from devin_switch import acp, side, subagent_tab, subagents
from devin_switch.store import SwitchError

AGENT = "acb63708"
# The foreground subagent of an earlier turn that the fake replays on a load.
PAST = "9d2e41aa"
# The foreground subagent the fake's "foreground" turn reports (FAKE_FOREGROUND_SUBAGENT).
WORKER = "5e1f0c2a"
# The foreground subagent the fake's "rerun" turns run, and then run again with `resume`.
RESUMED = "d29b75b1"
SECOND, THIRD, FOURTH = f"{RESUMED}-2", f"{RESUMED}-3", f"{RESUMED}-4"
# What the fake's delegate subagent says, in two messages, before its first command.
REPLY = "I'll run the command.\n\n## Plan\nSleep, then **report**."
STARTED = "cognition.ai/subagent_started"
COMPLETED = "cognition.ai/subagent_completed"
PAUSED = "cognition.ai/subagent_paused"
CONTEXT = "cognition.ai/subagent_context"
STREAM = "cognition.ai/streamingMessageId"
STAMP = {"cognition.ai/timestamp": "2026-11-11T07:22:21.637022+00:00"}
# The payloads that can name the run they belong to, and their field for the subagent.
RUN_EVENTS = {STARTED: "agentId", COMPLETED: "agentId", PAUSED: "agentId", CONTEXT: "parentAgentId"}
LISTED = {
    "id",
    "title",
    "profile",
    "background",
    "status",
    "started",
    "ended",
    "step",
    "note",
    "reply_chars",
}
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
        "_meta": {STARTED: event},
    }


def completed(agent=AGENT, success=True, summary="Slept for 30 seconds."):
    event = {"agentId": agent, "success": success, "summary": summary, "depth": 1}
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "status": "completed",
        "_meta": {COMPLETED: event},
    }


def paused(agent, value):
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "_meta": {PAUSED: {"agentId": agent, "paused": value}},
    }


def by(agent, change):
    """An update produced by `agent` ("root": the chat's own agent, on its usage updates)."""
    return {**change, "_meta": {CONTEXT: {"parentAgentId": agent}}}


def said(text, stream="m1", agent=AGENT):
    """One live chunk of a subagent's own message, as captured from CLI 3000.12.43."""
    meta = {STREAM: stream, CONTEXT: {"parentAgentId": agent}, **STAMP}
    return {**MESSAGE, "content": {"type": "text", "text": text}, "_meta": meta}


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
        "reply_chars": 0,
    }
    assert "secret plan" not in json.dumps(roster.snapshot("chat"))
    assert rows(roster, "other-chat") == []

    roster.apply("chat", started("b2", title="Second", isBackground=False))
    assert [(item["id"], item["title"], item["background"]) for item in rows(roster)] == [
        (AGENT, "Sleeper", True),
        ("b2", "Second", False),
    ]
    # The same ID started again is still one row, counted from its new start.
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

    # Stopped is final: neither a later event nor another interrupt changes it.
    before = rows(roster)
    roster.interrupt("chat")
    roster.apply("chat", completed("running"))
    roster.apply("chat", paused("paused", False))
    assert rows(roster) == before
    assert states(roster, "other-chat") == {"elsewhere": "running"}

    # Another chat's rows stop only with that chat's own native.
    roster.interrupt("other-chat")
    elsewhere = row(roster, "elsewhere", "other-chat")
    assert (elsewhere["status"], elsewhere["note"]) == ("stopped", "the agent restarted")
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


def test_a_row_keeps_what_its_subagent_said_and_lists_only_how_much():
    roster = subagent_tab.Roster()
    assert roster.reply("chat", AGENT) is None
    roster.apply("chat", started())
    roster.apply("chat", started("b2"))
    assert roster.reply("chat", AGENT) == "" and row(roster)["reply_chars"] == 0

    # A message arrives in chunks; a later message of the same subagent has another ID.
    roster.apply("chat", said("Looking ", "m1"))
    roster.apply("chat", said("into it.", "m1"))
    assert roster.reply("chat", AGENT) == "Looking into it."
    # Its other work is not something it said.
    for change in (THOUGHT, TOOL, RAN, USAGE):
        roster.apply("chat", by(AGENT, change))
    for text, stream in (("## Report\n", "m2"), ("All **good**.", "m2"), ("Bye", "m3")):
        roster.apply("chat", said(text, stream))
    reply = "Looking into it.\n\n## Report\nAll **good**.\n\nBye"
    assert roster.reply("chat", AGENT) == reply
    assert row(roster)["reply_chars"] == len(reply) == 46
    assert row(roster)["step"] == "Writing"

    # Each subagent has its own, and the list never holds the text itself.
    roster.apply("chat", said("Second's own.", "m1", "b2"))
    assert roster.reply("chat", "b2") == "Second's own." and roster.reply("chat", AGENT) == reply
    assert [item["reply_chars"] for item in rows(roster)] == [46, 13]
    listed = roster.snapshot("chat")
    assert [set(item) for item in listed["subagents"]] == [LISTED, LISTED]
    assert not any(word in json.dumps(listed) for word in ("Looking", "Report", "Bye", "own"))
    assert roster.reply("chat", "gone") is None and roster.reply("other-chat", AGENT) is None


def test_only_the_text_of_a_subagents_own_messages_joins_its_reply():
    roster = subagent_tab.Roster()
    roster.apply("chat", started())
    roster.apply("chat", said("Kept."))
    before = rows(roster)
    image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}
    with roster.listen("chat") as queue:
        queue.get_nowait()
        for content in (
            None,
            "text",
            ["text"],
            {},
            image,
            {"text": "No type"},
            {"type": "resource_link", "text": "Not text"},
            {"type": "text"},
            *({"type": "text", "text": odd} for odd in (None, "", 7, ["x"], {"text": "x"})),
        ):
            roster.apply("chat", {**said("x"), "content": content})
        roster.apply("chat", by(AGENT, {"sessionUpdate": "agent_message_chunk"}))
        # The chat's own agent, unknown agents and other chats add nothing.
        roster.apply("chat", said("Root's", agent="root"))
        roster.apply("chat", said("Nobody's", agent="gone"))
        roster.apply("chat", {**MESSAGE, "_meta": {STREAM: "m1", **STAMP}})
        roster.apply("chat", MESSAGE)
        roster.apply("other-chat", said("Elsewhere"))
        for context in (None, "root", [AGENT], {}, {"parentAgentId": None}):
            roster.apply("chat", {**MESSAGE, "_meta": {CONTEXT: context}})
        assert roster.reply("chat", AGENT) == "Kept." and rows(roster) == before
        assert queue.empty()

    # An odd message ID still keeps the text.
    for odd in (None, 7, ["m"], {"m": 1}):
        roster.apply("chat", said("!", odd))
    assert roster.reply("chat", AGENT).replace("\n", "") == "Kept.!!!!"
    roster.apply("chat", {**said("?"), "_meta": {CONTEXT: {"parentAgentId": AGENT}}})
    assert roster.reply("chat", AGENT).endswith("?")


def test_a_long_reply_keeps_its_newest_text():
    roster, limit = subagent_tab.Roster(), subagent_tab.REPLY_LIMIT
    assert limit == 50_000
    roster.apply("chat", started())
    roster.apply("chat", said("START" + "a" * limit))
    assert roster.reply("chat", AGENT) == "a" * limit
    roster.apply("chat", said("b" * 10 + "END"))
    reply = roster.reply("chat", AGENT)
    assert reply == "a" * (limit - 13) + "b" * 10 + "END"
    assert len(reply) == limit == row(roster)["reply_chars"]
    # A later message counts its blank line too.
    roster.apply("chat", said("NEXT", "m2"))
    assert roster.reply("chat", AGENT) == "a" * (limit - 19) + "b" * 10 + "END\n\nNEXT"
    assert row(roster)["reply_chars"] == limit


def test_a_reply_goes_when_its_row_does():
    roster, cap = subagent_tab.Roster(), subagent_tab.MAX_ROWS
    roster.apply("chat", started())
    roster.apply("chat", said("First run."))
    roster.apply("chat", completed())
    assert roster.reply("chat", AGENT) == "First run."
    # The same ID started again has said nothing yet.
    roster.apply("chat", started())
    assert roster.reply("chat", AGENT) == "" and row(roster)["reply_chars"] == 0
    roster.apply("chat", said("Second run."))
    assert roster.reply("chat", AGENT) == "Second run."

    roster.apply("other-chat", started("elsewhere"))
    roster.apply("other-chat", said("Stays.", agent="elsewhere"))
    roster.forget("chat")
    assert roster.reply("chat", AGENT) is None
    assert roster.reply("other-chat", "elsewhere") == "Stays."

    # A row the cap drops takes its reply along.
    roster.apply("chat", started("first"))
    roster.apply("chat", said("An old report.", agent="first"))
    roster.apply("chat", completed("first"))
    for number in range(cap):
        roster.apply("chat", started(f"job-{number}"))
        roster.apply("chat", said(f"Report {number}", agent=f"job-{number}"))
        roster.apply("chat", completed(f"job-{number}"))
    assert roster.reply("chat", "first") is None and len(rows(roster)) == cap
    assert roster.reply("chat", "job-0") == "Report 0"


def test_only_a_subagents_own_message_is_told_from_the_chats_own():
    own = subagent_tab.own_message
    # Live, the chunks of one message share a streaming ID; a load replays it whole, by event.
    assert own(said("Report")) and own(said("", "m2", "never-started"))
    saved = {"cognition.ai/eventId": "45", **STAMP, CONTEXT: {"parentAgentId": AGENT}}
    assert own({**MESSAGE, "_meta": saved})
    assert own(by(AGENT, {"sessionUpdate": "agent_message_chunk"}))

    # The chat's own agent's messages are not, nor is a subagent's other work.
    assert not own(MESSAGE)
    assert not own({**MESSAGE, "_meta": {STREAM: "m1", **STAMP}})
    assert not own({**MESSAGE, "_meta": {"cognition.ai/eventId": "72", **STAMP}})
    assert not own(by("root", MESSAGE))
    for change in (THOUGHT, TOOL, RAN, USAGE, {"title": "No kind"}):
        assert not own(by(AGENT, change))
    for change in (started(), completed(), paused(AGENT, True)):
        assert not own(change)

    # Whatever else a native sends is not one either, and never raises.
    for odd in (None, "", "text", 7, ["agent_message_chunk"], {}):
        assert not own(odd)
        assert not own({**MESSAGE, "_meta": odd})
        assert not own({**MESSAGE, "_meta": {CONTEXT: odd}})
    for odd in (None, "", 7, [AGENT], {"id": AGENT}):
        assert not own(by(odd, MESSAGE))
        assert not own(by(AGENT, {"sessionUpdate": odd}))


@pytest.fixture
def clock(monkeypatch):
    """The roster's monotonic clock, moved by hand."""
    now = [5000.0]
    stand_in = types.SimpleNamespace(time=time.time, monotonic=lambda: now[0])
    monkeypatch.setattr(subagent_tab, "time", stand_in)
    return now


def test_a_growing_reply_alone_tells_the_page_once_a_second(clock):
    roster = subagent_tab.Roster()
    roster.apply("chat", started())
    roster.apply("chat", started("b2"))
    with roster.listen("chat") as queue:
        queue.get_nowait()

        def told():
            return not queue.empty() and queue.get_nowait() is True and queue.empty()

        # Its first words are a new step, told at once.
        roster.apply("chat", said("a"))
        assert told()
        # Chunks arrive many times a second: the page is not told of each.
        for _ in range(20):
            clock[0] += 0.04
            roster.apply("chat", said("a"))
        clock[0] = 5000.99
        roster.apply("chat", said("a"))
        assert not told()
        # The list read meanwhile is current all the same.
        assert row(roster)["reply_chars"] == 22
        clock[0] = 5001.0
        roster.apply("chat", said("a"))
        assert told()
        clock[0] = 5001.5
        roster.apply("chat", said("a"))
        assert not told()

        # Each row waits for itself.
        roster.apply("chat", {**said("", agent="b2"), "content": {"type": "image"}})
        assert told() and row(roster, "b2")["step"] == "Writing"
        roster.apply("chat", said("b", agent="b2"))
        assert told()
        clock[0] = 5001.6
        roster.apply("chat", said("b", agent="b2"))
        assert not told()

        # A new step or the end is told at once, with the reply's length as it is by then.
        roster.apply("chat", by(AGENT, TOOL))
        assert told()
        clock[0] = 5001.7
        roster.apply("chat", said("a", "m2"))
        assert told() and row(roster)["reply_chars"] == 27
        clock[0] = 5001.8
        roster.apply("chat", said("a", "m2"))
        assert not told()
        roster.apply("chat", completed())
        assert told()
        (ended, _) = rows(roster)
        assert (ended["status"], ended["reply_chars"]) == ("done", 28)
        assert roster.reply("chat", AGENT) == "a" * 24 + "\n\naa"


def test_the_page_shell_names_its_chat_token_and_files():
    shell = subagent_tab.page('chat-1<"&>', 'tok<en>&"x')
    token = "tok&lt;en&gt;&amp;&quot;x"
    assert "<title>Subagents</title>" in shell
    assert f'<body data-session="chat-1&lt;&quot;&amp;&gt;" data-token="{token}">' in shell
    assert re.findall(r'<link rel="stylesheet" href="([^"]*)">', shell) == [
        f"/subagents/assets/subagents.css?t={token}"
    ]
    # The side page's Markdown renderer comes first: the page's scripts show replies with it.
    assert re.findall(r'<script src="([^"]*)"></script>', shell) == [
        f"/side/assets/markdown.js?t={token}",
        f"/subagents/assets/subagents-view.js?t={token}",
        f"/subagents/assets/subagents.js?t={token}",
    ]
    assert shell.count("<script") == 3 and shell.endswith("</script></body></html>")
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
        monkeypatch.setattr(acp.Bridge, "interrupt_subagents", lambda self, backend: None)

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
            bridge.interrupt_subagents(old)
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


def authored(messages, agent):
    """The updates Desktop was sent that name `agent` as the one whose work they are."""
    return [
        update
        for message in messages
        if message.get("method") == "session/update"
        and ((update := message["params"]["update"]).get("_meta") or {}).get(CONTEXT)
        == {"parentAgentId": agent}
    ]


def kinds(updates):
    return [update["sessionUpdate"] for update in updates]


def test_what_a_subagent_says_reaches_its_row_but_never_the_chat(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster = chat.session_id, bridge.roster
            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            await until(lambda: row(roster, AGENT, session_id)["step"] == "Running command")

            # Desktop got the chat's own message as sent, and the subagent's card and steps...
            (progress,) = replayed(messages, "agent_message_chunk")
            assert progress["content"] == {"type": "text", "text": "live progress"}
            assert set(progress["_meta"]) == {STREAM, "cognition.ai/timestamp"}
            assert [event["agentId"] for event in subagent_events(messages, STARTED)] == [AGENT]
            assert kinds(authored(messages, AGENT)) == [
                "agent_thought_chunk",
                "usage_update",
                "tool_call",
            ]
            assert kinds(authored(messages, "root")) == ["usage_update"]
            # ...but nothing of what the subagent said: that is its row's to show.
            assert not any(words in json.dumps(messages) for words in ("I'll run", "## Plan"))
            assert roster.reply(session_id, AGENT) == REPLY
            assert row(roster, AGENT, session_id)["reply_chars"] == len(REPLY)

            # A reload during the turn replays the bridge's own record, which never held it.
            start = len(messages)
            await bridge.dispatch("session/load", chat.setup)
            replay = messages[start:]
            assert native_loads(acp_native) == []
            assert replayed_texts(replay, "user_message_chunk") == ["delegate"]
            assert replayed_texts(replay, "agent_message_chunk") == ["live progress"]
            assert [event["agentId"] for event in subagent_events(replay, STARTED)] == [AGENT]
            assert kinds(authored(replay, AGENT)) == [
                "agent_thought_chunk",
                "tool_call",
                "usage_update",
            ]

            await stop(bridge, chat, turn)
            assert states(roster, session_id) == {AGENT: "failed"}
            assert roster.reply(session_id, AGENT) == REPLY
            assert replayed_texts(messages, "agent_message_chunk") == ["live progress"] * 2

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["session/load", "session/resume"])
def test_a_reopened_chat_shows_a_past_subagents_steps_but_not_what_it_said(
    acp_native, tmp_path, monkeypatch, method
):
    monkeypatch.setenv("FAKE_REPLAY_SUBAGENT", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster = chat.session_id, bridge.roster
            start = len(messages)
            assert "configOptions" in await bridge.dispatch(method, chat.setup)
            assert native_loads(acp_native) == [chat.backend.process.pid]
            shown = messages[start:]
            if method == "session/load":
                assert replayed_texts(shown, "agent_message_chunk") == ["historical replay"]
                assert [update["title"] for update in authored(shown, PAST)] == ["Read notes.md"]
                assert [event["agentId"] for event in subagent_events(shown, STARTED)] == [PAST]
                ended = subagent_events(shown, COMPLETED)
                assert [event["summary"] for event in ended] == ["Read the notes."]
            else:
                # A resume asks for no replay at all.
                assert replayed(shown, "agent_message_chunk") == []
                assert authored(shown, PAST) == [] and subagent_events(shown, STARTED) == []
            assert rows(roster, session_id) == [] and roster.reply(session_id, PAST) is None

            # Either way the bridge recorded that load for a reload during a later turn.
            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            await until(lambda: row(roster, AGENT, session_id)["step"] == "Running command")
            start = len(messages)
            await bridge.dispatch("session/load", chat.setup)
            replay = messages[start:]
            assert replayed_texts(replay, "agent_message_chunk") == [
                "historical replay",
                "live progress",
            ]
            assert [update["title"] for update in authored(replay, PAST)] == ["Read notes.md"]
            assert [event["agentId"] for event in subagent_events(replay, STARTED)] == [
                PAST,
                AGENT,
            ]
            sent = json.dumps(messages)
            assert not any(words in sent for words in ("notes say hello", "I'll run", "## Plan"))
            assert list(states(roster, session_id)) == [AGENT]
            assert roster.reply(session_id, AGENT) == REPLY
            await stop(bridge, chat, turn)

    asyncio.run(scenario())


def about(messages, agent=RESUMED):
    """The updates Desktop was sent that are a subagent's start, end, pause or own work."""
    return [
        update
        for message in messages
        if message.get("method") == "session/update"
        and isinstance(meta := (update := message["params"]["update"]).get("_meta"), dict)
        and any(
            isinstance(event := meta.get(key), dict) and event.get(field) == agent
            for key, field in RUN_EVENTS.items()
        )
    ]


def run_ids(messages, agent=RESUMED):
    """Each of those updates in order: what it is, and the run it says it belongs to."""
    found = []
    for update in about(messages, agent):
        for key, field in RUN_EVENTS.items():
            event = update["_meta"].get(key)
            if isinstance(event, dict) and event.get(field) == agent:
                what = update["sessionUpdate"] if key == CONTEXT else key.rpartition("_")[2]
                found.append((what, event.get("runId")))
    return found


def one_run(run=None, *between):
    """What Desktop is shown of one run of the fake's resumed subagent, each part in `run`."""
    parts = ("started", "agent_thought_chunk", "usage_update", *between, "completed")
    return [(part, run) for part in parts]


def in_run(update, run):
    """An update as it reads once each of its run payloads names `run`."""
    update = copy.deepcopy(update)
    for key in RUN_EVENTS.keys() & update["_meta"].keys():
        update["_meta"][key]["runId"] = run
    return update


def reported(task, *between, **named):
    """One run as the fake's "rerun" turns send it, bar its thought (which carries a time)."""
    start = started(RESUMED, task=task, isBackground=False, **named)
    return [start, by(RESUMED, USAGE), *between, completed(RESUMED, True, f"Did: {task}")]


def unthinking(updates):
    return [update for update in updates if update["sessionUpdate"] != "agent_thought_chunk"]


def named_by(ids, update, chat="chat"):
    """An update as `ids` sends it on, the one given left as it was."""
    update = copy.deepcopy(update)
    ids.stamp(chat, update)
    return update


def ask(bridge, chat, text):
    return bridge.dispatch("session/prompt", prompt_params(chat.session_id, text))


def test_a_resumed_subagents_run_reaches_desktop_under_a_run_id_of_its_own(acp_native, tmp_path):
    """The native reports a resumed run like the first: the same agent ID and no run ID. Desktop
    3.14 then files the second start under a row nothing finds again, and counts it as running
    for good ("N subagents running")."""

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            await ask(bridge, chat, "rerun")
            # The first run is as the native sent it. The resumed one names itself on its
            # start, on its own updates and on its end.
            assert run_ids(messages) == one_run() + one_run(SECOND)
            shown = about(messages)
            resumed = [in_run(update, SECOND) for update in reported("Now the rest")]
            assert unthinking(shown) == reported("Read notes.md") + resumed
            thoughts = [
                update for update in shown if update["sessionUpdate"] == "agent_thought_chunk"
            ]
            assert [(update["content"]["text"], set(update["_meta"])) for update in thoughts] == [
                ("…", {CONTEXT, STREAM, "cognition.ai/timestamp"})
            ] * 2
            assert [update["_meta"][CONTEXT] for update in thoughts] == [
                {"parentAgentId": RESUMED},
                {"parentAgentId": RESUMED, "runId": SECOND},
            ]

            # Every further run gets the next number.
            start = len(messages)
            await ask(bridge, chat, "rerun-again")
            assert run_ids(messages[start:]) == one_run(THIRD)
            assert unthinking(about(messages[start:])) == [
                in_run(update, THIRD) for update in reported("Read on")
            ]
            # What the subagent said stays out of the chat all the same: its row has it.
            assert "On it:" not in json.dumps(messages)
            assert bridge.roster.reply(chat.session_id, RESUMED) == "On it: Read on"

    asyncio.run(scenario())


def test_a_resumed_run_that_is_paused_says_which_run_was_paused(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            await ask(bridge, chat, "rerun-paused")
            assert run_ids(messages) == one_run() + one_run(SECOND, "paused", "paused")
            parked = [paused(RESUMED, True), paused(RESUMED, False)]
            resumed = [in_run(update, SECOND) for update in reported("Now the rest", *parked)]
            assert unthinking(about(messages)) == reported("Read notes.md") + resumed
            assert states(bridge.roster, chat.session_id) == {RESUMED: "done"}

    asyncio.run(scenario())


def test_a_run_id_of_the_natives_own_reaches_desktop_as_sent(acp_native, tmp_path, monkeypatch):
    """A native that names its runs needs no help: nothing is renamed, nothing is added."""
    monkeypatch.setenv("FAKE_RUN_IDS", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            await ask(bridge, chat, "rerun")
            await ask(bridge, chat, "rerun-again")
            tasks = ("Read notes.md", "Now the rest", "Read on")
            sent = [
                update
                for number, task in enumerate(tasks, 1)
                for update in reported(task, runId=f"native-run-{number}")
            ]
            assert unthinking(about(messages)) == sent
            # The fake names a run on its start alone: the rest of the run stays unnamed.
            named = [run for _, run in run_ids(messages) if run]
            assert named == ["native-run-1", "native-run-2", "native-run-3"]

    asyncio.run(scenario())


def test_a_reload_during_a_turn_replays_a_resumed_run_as_it_went_out(
    acp_native, tmp_path, monkeypatch
):
    gate = tmp_path / "may-finish"
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            turn = asyncio.create_task(ask(bridge, chat, "rerun"))
            # The resumed run is working: only its end is still to come.
            await until(lambda: len(about(messages)) == 7)
            live = about(messages)
            assert run_ids(messages) == one_run() + one_run(SECOND)[:-1]

            # The bridge's own record answers a reload during a turn. Asked twice, it reads
            # the same: a replay is no new start.
            for _ in range(2):
                start = len(messages)
                await bridge.dispatch("session/load", chat.setup)
                replay = about(messages[start:])
                assert run_ids(messages[start:]) == [
                    ("started", None),
                    ("agent_thought_chunk", None),
                    ("completed", None),
                    ("started", SECOND),
                    ("agent_thought_chunk", SECOND),
                    # A record keeps only the latest usage update, and sends it last.
                    ("usage_update", SECOND),
                ]
                steps = [update for update in live if update["sessionUpdate"] != "usage_update"]
                assert replay == [*steps, live[-1]]
            assert native_loads(acp_native) == []

            start = len(messages)
            gate.touch()
            assert await turn == {"stopReason": "end_turn"}
            assert run_ids(messages[start:]) == [("completed", SECOND)]
            start = len(messages)
            await ask(bridge, chat, "rerun-again")
            assert run_ids(messages[start:]) == one_run(THIRD)

    asyncio.run(scenario())


@pytest.mark.parametrize("reopened", ["open", "released", "closed"])
def test_the_natives_own_replay_numbers_a_resumed_subagents_runs_from_the_first(
    acp_native, tmp_path, monkeypatch, reopened
):
    """Desktop builds its transcript anew from what a load replays, so the runs in it are
    numbered anew as well, however many the bridge counted before."""
    monkeypatch.setenv("FAKE_REPLAY_RESUMED", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            await ask(bridge, chat, "rerun")
            assert run_ids(messages) == one_run() + one_run(SECOND)
            if reopened == "released":
                chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
                assert await bridge.release_idle(chat)
            if reopened == "closed":
                await bridge.dispatch("session/close", {"sessionId": chat.session_id})
                # A closed chat's runs are not kept.
                end = completed(RESUMED)
                assert named_by(bridge.run_ids, end, chat.session_id) == end

            for loads in (1, 2):
                start = len(messages)
                await bridge.dispatch("session/load", chat.setup)
                assert len(native_loads(acp_native)) == loads
                assert run_ids(messages[start:]) == one_run() + one_run(SECOND)

            # A run after the replay carries on from the replay's numbers.
            start = len(messages)
            await ask(bridge, chat, "rerun-again")
            assert run_ids(messages[start:]) == one_run(THIRD)

    asyncio.run(scenario())


def test_a_load_under_a_running_turn_numbers_the_runs_like_any_replay_of_the_native(
    acp_native, tmp_path, monkeypatch
):
    """From CLI 3000.14 on the native itself answers a load during a turn."""
    gate = tmp_path / "may-finish"
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))
    for switch in ("PROCESS_MODEL", "REPLAY_RESUMED"):
        monkeypatch.setenv(f"FAKE_{switch}", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            turn = asyncio.create_task(ask(bridge, chat, "rerun"))
            await until(lambda: len(about(messages)) == 7)
            start = len(messages)
            await bridge.dispatch("session/load", chat.setup)
            assert native_loads(acp_native) == [chat.backend.process.pid]
            assert run_ids(messages[start:]) == one_run() + one_run(SECOND)

            # The working run's end names the run its replayed start named.
            start = len(messages)
            gate.touch()
            assert await turn == {"stopReason": "end_turn"}
            assert run_ids(messages[start:]) == [("completed", SECOND)]
            start = len(messages)
            await ask(bridge, chat, "rerun-again")
            assert run_ids(messages[start:]) == one_run(THIRD)

    asyncio.run(scenario())


def recorded(first, second, working):
    """A record replay of two finished runs and one at work, each with the run ID it names.

    A record keeps only the latest usage update, and sends it last.
    """
    finished = [
        (part, run)
        for run in (first, second)
        for part in ("started", "agent_thought_chunk", "completed")
    ]
    return [*finished, *one_run(working)[:2], ("usage_update", working)]


@pytest.mark.parametrize("unseen", ["handoff", "release"])
def test_a_replay_desktop_never_sees_is_numbered_for_the_record_alone(
    acp_native, tmp_path, monkeypatch, unseen
):
    """Desktop keeps its transcript over a handoff and over an idle release: the new native's
    replay of the chat is not shown, so its starts are none Desktop has rows for. The bridge's
    record holds that replay, though, and a reload during a turn shows Desktop the record."""
    gate = tmp_path / "may-finish"
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))
    monkeypatch.setenv("FAKE_REPLAY_RESUMED", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            old = chat.backend
            gate.touch()
            await ask(bridge, chat, "rerun")
            await ask(bridge, chat, "rerun-again")
            assert run_ids(messages) == one_run() + one_run(SECOND) + one_run(THIRD)

            start = len(messages)
            if unseen == "handoff":
                await ask(bridge, chat, "/switch ansuman-2")
            else:
                old.last_activity -= acp.IDLE_TIMEOUT + 1
                assert await bridge.release_idle(chat)
            gate.unlink()
            turn = asyncio.create_task(ask(bridge, chat, "rerun-again"))
            await until(lambda: len(about(messages[start:])) == 3)
            # The new native replayed the two runs it has saved, to the bridge alone. Desktop
            # was sent three, so the run now at work is its fourth.
            assert chat.backend is not old
            assert native_loads(acp_native) == [chat.backend.process.pid]
            assert run_ids(messages[start:]) == one_run(FOURTH)[:-1]

            # The record's replay is numbered from its own first run, like one of the
            # native's that Desktop is shown; the run at work reads as it went out.
            start = len(messages)
            await bridge.dispatch("session/load", chat.setup)
            assert run_ids(messages[start:]) == recorded(None, SECOND, FOURTH)
            assert len(native_loads(acp_native)) == 1
            start = len(messages)
            gate.touch()
            assert await turn == {"stopReason": "end_turn"}
            assert run_ids(messages[start:]) == [("completed", FOURTH)]

    asyncio.run(scenario())


def test_a_run_never_takes_a_number_the_record_holds_from_a_replay_desktop_never_saw(
    acp_native, tmp_path, monkeypatch
):
    """A resume asks for no replay, so Desktop is sent none of the chat's earlier runs, while
    the record holds them. The next run must be told apart from those in both."""
    gate = tmp_path / "may-finish"
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))
    monkeypatch.setenv("FAKE_REPLAY_RESUMED", "1")

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            # Each such replay is numbered from its own first run.
            for loads in (1, 2):
                await bridge.dispatch("session/resume", chat.setup)
                assert len(native_loads(acp_native)) == loads
            assert about(messages) == []

            turn = asyncio.create_task(ask(bridge, chat, "rerun-again"))
            await until(lambda: len(about(messages)) == 3)
            assert run_ids(messages) == one_run(THIRD)[:-1]
            start = len(messages)
            await bridge.dispatch("session/load", chat.setup)
            assert run_ids(messages[start:]) == recorded(None, SECOND, THIRD)
            gate.touch()
            assert await turn == {"stopReason": "end_turn"}

    asyncio.run(scenario())


def test_a_resumed_subagent_stays_one_row_known_by_its_agent_id(acp_native, tmp_path, monkeypatch):
    gate = tmp_path / "may-finish"
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster, server = chat.session_id, bridge.roster, bridge.side_server
            store = acp_native.store
            turn = asyncio.create_task(ask(bridge, chat, "rerun"))
            await until(lambda: len(about(messages)) == 7)
            (running,) = rows(roster, session_id)
            assert set(running) == LISTED
            assert (running["id"], running["title"], running["status"], running["step"]) == (
                RESUMED,
                "Sleeper",
                "running",
                "Writing",
            )
            assert chat.backend.running == {RESUMED}
            assert list(subagents.load(store, session_id)["running"]) == [RESUMED]

            gate.touch()
            assert await turn == {"stopReason": "end_turn"}
            # Desktop was told of two runs, the page and the saved records of one subagent.
            assert run_ids(messages) == one_run() + one_run(SECOND)
            (done,) = rows(roster, session_id)
            assert done == {**running, "status": "done", "ended": done["ended"]}
            assert running["started"] <= done["ended"] <= time.time()
            _, reader, writer = await request(server.port, "GET", events_url(server, session_id))
            assert await lines(reader, 1) == [{"subagents": [done]}]
            writer.close()
            assert roster.reply(session_id, RESUMED) == "On it: Now the rest"
            assert not chat.backend.running
            assert subagents.load(store, session_id)["running"] == {}
            assert list(subagents.load_ended(store, session_id)["finished"]) == [RESUMED]

    asyncio.run(scenario())


def test_a_run_the_native_named_is_left_alone_among_runs_it_did_not_name():
    ids = subagents.RunIds()
    run = [started(), by(AGENT, THOUGHT), paused(AGENT, True), completed()]
    assert [named_by(ids, update) for update in run] == run
    second = [in_run(update, f"{AGENT}-2") for update in run]
    assert [named_by(ids, update) for update in run] == second
    # Named on its start alone, or on everything: either way nothing is added or renamed.
    for own in ([started(runId="r3"), *run[1:]], [in_run(update, "r4") for update in run]):
        assert [named_by(ids, update) for update in own] == own
    # Every start counted, and a name left empty is no name.
    assert named_by(ids, started(runId=None)) == started(runId=f"{AGENT}-5")
    assert named_by(ids, completed()) == in_run(completed(), f"{AGENT}-5")
    # A name of the native's on a later event of a run that the bridge named stays as well.
    assert named_by(ids, in_run(by(AGENT, TOOL), "own")) == in_run(by(AGENT, TOOL), "own")


def test_runs_are_numbered_per_chat_and_subagent_until_the_chat_is_forgotten():
    ids = subagents.RunIds()
    for update in (started(), started(), started("b2")):
        ids.stamp("chat", update)
    assert named_by(ids, completed()) == in_run(completed(), f"{AGENT}-2")
    assert named_by(ids, completed("b2")) == completed("b2")
    assert named_by(ids, by("root", USAGE)) == by("root", USAGE)
    assert named_by(ids, started(), "other") == started()
    assert named_by(ids, by(AGENT, TOOL), "other") == by(AGENT, TOOL)
    # One update can be a subagent's own work and another one's start: each names its own.
    nested = {**started("b2"), "_meta": {**started("b2")["_meta"], **by(AGENT, {})["_meta"]}}
    stamped = named_by(ids, nested)["_meta"]
    assert (stamped[STARTED]["runId"], stamped[CONTEXT]["runId"]) == ("b2-2", f"{AGENT}-2")

    ids.forget("chat")
    assert named_by(ids, completed()) == completed()
    assert named_by(ids, started()) == started()
    assert named_by(ids, started(), "other") == started(runId=f"{AGENT}-2")


def test_a_replay_kept_from_desktop_is_numbered_on_its_own_from_its_first_run():
    ids = subagents.RunIds()
    run = [started(), by(AGENT, THOUGHT), paused(AGENT, True), completed()]
    third = [in_run(update, f"{AGENT}-3") for update in run]
    for _ in range(3):
        shown = [named_by(ids, update) for update in run]
    assert shown == third

    def kept(update):
        update = copy.deepcopy(update)
        ids.stamp("chat", update, shown=False)
        return update

    # Each replay Desktop is not shown starts the record anew: so are its runs numbered.
    for _ in range(2):
        ids.forget("chat", shown=False)
        assert [kept(update) for update in run] == run
        assert [kept(update) for update in run] == [in_run(update, f"{AGENT}-2") for update in run]
        assert kept(started("b2")) == started("b2")
    # Desktop's rows are as they were: the run it was last sent is still the third...
    assert named_by(ids, completed()) == in_run(completed(), f"{AGENT}-3")
    assert named_by(ids, started(), "other") == started()
    # ...and the next one is the fourth.
    assert named_by(ids, started()) == started(runId=f"{AGENT}-4")
    assert kept(completed()) == in_run(completed(), f"{AGENT}-2")

    # Where the record holds more runs than Desktop was sent, the next run takes none of
    # their numbers: a reload during a turn shows Desktop the record.
    assert named_by(ids, started("b2")) == started("b2", runId="b2-2")
    ids.forget("chat", shown=False)
    for _ in range(6):
        ids.stamp("chat", started(), shown=False)
    assert named_by(ids, started()) == started(runId=f"{AGENT}-7")
    # A replay that Desktop is shown starts both anew.
    ids.forget("chat")
    assert named_by(ids, started()) == started()
    assert kept(started()) == started()


def test_naming_runs_never_raises_on_odd_updates():
    ids = subagents.RunIds()
    for _ in range(2):
        ids.stamp("chat", started())
    odd_ids = (None, "", 7, ["a"], {"a": 1})
    for meta in (
        None,
        "text",
        [STARTED],
        *({key: value} for key in RUN_EVENTS for value in (None, "x", [1])),
        *({key: {field: odd}} for key, field in RUN_EVENTS.items() for odd in odd_ids),
        {key: {"title": "No ID"} for key in RUN_EVENTS},
    ):
        update = {"sessionUpdate": "tool_call_update", "_meta": meta}
        assert named_by(ids, update) == update
        ids.stamp("chat", sent := copy.deepcopy(update), shown=False)
        assert sent == update
    ids.stamp("chat", {"sessionUpdate": "usage_update"})
    # None of them was a start.
    assert named_by(ids, started()) == started(runId=f"{AGENT}-3")


# A second subagent, and the rows a history page covers: its runs are named after `endRowId`.
OTHER = "b7e2c9d4"
ROWS = {"startRowId": 120, "endRowId": 125}
RUN = [started(), by(AGENT, THOUGHT), completed()]


def page_of(*updates, chat="chat", **rows):
    """An answer to `_cognition.ai/session/loadPage` that holds these updates, oldest first."""
    covered = {**ROWS, **rows}
    return {
        "updates": [{"sessionId": chat, "update": copy.deepcopy(update)} for update in updates],
        "coveredRange": covered,
        "nextEndRowId": covered["startRowId"],
        "exhausted": False,
        "replayFilterVersion": "1",
    }


def paged(ids, *updates, chat="chat", **rows):
    """The updates of that page as Desktop gets them; nothing else of the page may change."""
    page = page_of(*updates, chat=chat, **rows)
    ids.stamp_page(chat, page)
    assert {**page, "updates": []} == {**page_of(chat=chat, **rows), "updates": []}
    assert all(set(row) == {"sessionId", "update"} for row in page["updates"])
    assert {row["sessionId"] for row in page["updates"]} <= {chat}
    return [row["update"] for row in page["updates"]]


def in_page(run, *updates):
    return [in_run(update, run) for update in updates]


def test_every_run_that_starts_in_a_history_page_is_named_after_the_page():
    """Desktop folds a page on its own: unnamed, a resumed run's row there never ends, and the
    page's first run shares a row with the tail's first."""
    ids = subagents.RunIds()
    other = [started(OTHER), completed(OTHER)]
    assert paged(ids, *RUN, *RUN, *other) == [
        *in_page(f"{AGENT}-h125-1", *RUN),
        *in_page(f"{AGENT}-h125-2", *RUN),
        *in_page(f"{OTHER}-h125-1", *other),
    ]
    # A pause belongs to the run before the next start, like an end.
    parked = [started(), paused(AGENT, True), started(), completed()]
    assert paged(ids, *parked) == [
        *in_page(f"{AGENT}-h125-1", *parked[:2]),
        *in_page(f"{AGENT}-h125-2", *parked[2:]),
    ]
    # Another page's runs are told apart by its rows, whatever chat or order it comes in.
    assert paged(ids, *RUN, chat="other", startRowId=90, endRowId=120) == in_page(
        f"{AGENT}-h120-1", *RUN
    )
    # One update can be a subagent's own work and another one's start: each names its own.
    nested = {**started(OTHER), "_meta": {**started(OTHER)["_meta"], **by(AGENT, {})["_meta"]}}
    stamped = paged(ids, started(), nested)[1]["_meta"]
    assert stamped[STARTED]["runId"] == f"{OTHER}-h125-1"
    assert stamped[CONTEXT]["runId"] == f"{AGENT}-h125-1"


def test_a_history_page_and_the_tail_number_their_runs_apart():
    """The tail is named as before: its first run not at all, a resumed one "<id>-<n>"."""
    ids = subagents.RunIds()
    twice = [*RUN, *RUN]
    tail = [*RUN, *in_page(f"{AGENT}-2", *RUN)]
    assert [named_by(ids, update) for update in twice] == tail
    assert paged(ids, *twice) == [
        *in_page(f"{AGENT}-h125-1", *RUN),
        *in_page(f"{AGENT}-h125-2", *RUN),
    ]
    # A page's starts are not the tail's: the next live run is the tail's third.
    assert [named_by(ids, update) for update in RUN] == in_page(f"{AGENT}-3", *RUN)


def test_a_history_page_leaves_what_started_in_an_older_page_unnamed():
    ids = subagents.RunIds()
    leading = [by(AGENT, THOUGHT), paused(AGENT, True), completed()]
    other = [started(OTHER), by(OTHER, TOOL), completed(OTHER)]
    assert paged(ids, *leading, *other, *RUN) == [
        *leading,
        *in_page(f"{OTHER}-h125-1", *other),
        *in_page(f"{AGENT}-h125-1", *RUN),
    ]


def test_a_history_page_leaves_a_run_the_native_named_alone():
    ids = subagents.RunIds()
    own = [started(runId="r1"), by(AGENT, THOUGHT), paused(AGENT, True), completed()]
    whole = in_page("r2", *RUN)
    # Named on its start alone, or on everything: nothing is added or renamed. The start
    # still counts, and a name left empty is no name.
    assert paged(ids, *own, *whole, started(runId=None), completed()) == [
        *own,
        *whole,
        *in_page(f"{AGENT}-h125-3", started(), completed()),
    ]
    # After a run that the page named, too: its name ends with it.
    assert paged(ids, *RUN, *own) == [*in_page(f"{AGENT}-h125-1", *RUN), *own]
    # A name of the native's on a later event of a run that the page named stays as well.
    step = in_run(by(AGENT, TOOL), "own")
    assert paged(ids, started(), step) == [in_run(started(), f"{AGENT}-h125-1"), step]


def test_a_history_page_without_subagents_comes_out_as_it_went_in():
    ids = subagents.RunIds()
    plain = [MESSAGE, THOUGHT, TOOL, RAN, by("root", USAGE), {"sessionUpdate": "plan"}]
    page = page_of(*plain)
    given = json.dumps(page)
    ids.stamp_page("chat", page)
    assert json.dumps(page) == given


# Updates that name no subagent, whatever they hold where one is named.
ODD_UPDATES = [
    {},
    {"sessionUpdate": "tool_call_update"},
    *({"_meta": meta} for meta in (None, "text", [STARTED])),
    *({"_meta": {key: value}} for key in RUN_EVENTS for value in (None, "x", [1])),
    *(
        {"_meta": {key: {field: odd}}}
        for key, field in RUN_EVENTS.items()
        for odd in (None, "", 7, ["a"], {"a": 1})
    ),
    {"_meta": {key: {"title": "No ID"} for key in RUN_EVENTS}},
]
ODD_PAGES = [
    {},
    {"exhausted": True, "nextEndRowId": None},
    # No row range to name the runs after.
    {"updates": [{"update": update} for update in RUN]},
    *(
        {"updates": [{"update": update} for update in RUN], "coveredRange": covered}
        for covered in (
            None,
            "120-125",
            [120, 125],
            {},
            {"startRowId": 120},
            {"endRowId": 125},
            {"startRowId": "120", "endRowId": 125},
            {"startRowId": 120, "endRowId": None},
            {"startRowId": 120, "endRowId": 125.5},
            {"startRowId": 120, "endRowId": True},
        )
    ),
    # No rows.
    *({"updates": rows, "coveredRange": ROWS} for rows in (None, "none", 7, {"0": RUN[0]})),
    # Rows that are none, and updates that name no subagent.
    {"updates": [None, 7, "row", [], {}, {"sessionId": "any"}], "coveredRange": ROWS},
    {
        "updates": [{"update": odd} for odd in (None, "text", [started()], *ODD_UPDATES)],
        "coveredRange": ROWS,
    },
]


@pytest.mark.parametrize("page", ODD_PAGES)
def test_naming_a_history_page_never_raises_on_an_odd_one(page):
    """It runs on a chat's answer to Desktop, where an error fails the request."""
    ids = subagents.RunIds()
    given = json.dumps(page)
    for _ in range(2):
        ids.stamp_page("chat", page)
        assert json.dumps(page) == given


def test_a_history_page_names_the_runs_beside_a_row_that_is_none():
    ids = subagents.RunIds()
    odd = [None, {"update": "text"}, {"update": {"_meta": {STARTED: {"agentId": 7}}}}]
    given = {"updates": [*odd, *({"update": update} for update in RUN)], "coveredRange": ROWS}
    page = copy.deepcopy(given)
    ids.stamp_page("chat", page)
    named = [{"update": update} for update in in_page(f"{AGENT}-h125-1", *RUN)]
    assert page == {**given, "updates": [*odd, *named]}


OLDER = {"startRowId": 90, "endRowId": 120}


def ended(agent=AGENT, where="the tail"):
    """An end told apart from every other one: the one copy of it must be this one."""
    return completed(agent, summary=f"Ended in {where}.")


def test_an_end_that_leads_the_tail_is_copied_to_the_page_that_holds_its_start():
    """Desktop applies an end as its own segment arrives, to a row of that segment. An end
    whose start sits in an older page finds none and is dropped for good, so the page that
    brings the start brings a copy of the end too, under the name the page gave the run."""
    ids = subagents.RunIds()
    end = ended()
    tail = [by(AGENT, THOUGHT), end]
    # Nothing in the tail started this run: it goes out as sent.
    sent = copy.deepcopy(tail)
    for update in sent:
        ids.stamp("chat", update)
    assert sent == tail
    assert paged(ids, started(), completed(), started()) == [
        *in_page(f"{AGENT}-h125-1", started(), completed()),
        *in_page(f"{AGENT}-h125-2", started(), end),
    ]
    # Only the copy is named: what the tail was sent is not touched after the fact.
    assert sent == tail
    # The copy ended that run: nothing that follows is taken for it.
    assert named_by(ids, by(AGENT, TOOL)) == by(AGENT, TOOL)


def test_an_end_that_leads_a_page_is_copied_to_the_older_page_that_holds_its_start():
    ids = subagents.RunIds()
    end = ended(where="the newer page")
    other = [started(OTHER), completed(OTHER)]
    # The newer page comes first. What leads it stays as sent, and nothing is added to it.
    newer = page_of(by(AGENT, THOUGHT), end, *other)
    ids.stamp_page("chat", newer)
    answer = page_of(by(AGENT, THOUGHT), end, *in_page(f"{OTHER}-h125-1", *other))
    assert newer == answer
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started(), end)
    assert newer == answer
    # An end that the native named is not the bridge's to copy.
    ids.forget("chat")
    theirs = in_run(completed(), "r9")
    assert paged(ids, theirs) == [theirs]
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started())


def test_each_end_reaches_its_own_run_across_three_segments():
    ids = subagents.RunIds()
    third, second = ended(), ended(where="the newer page")
    assert named_by(ids, third) == third
    # The newer page holds the end of a run from the older page, then the start of the run
    # that ends in the tail. A pause leaves that run open; only an end closes it.
    assert paged(ids, second, started(), paused(AGENT, True)) == [
        second,
        *in_page(f"{AGENT}-h125-1", started(), paused(AGENT, True), third),
    ]
    assert paged(ids, *RUN, started(), **OLDER) == [
        *in_page(f"{AGENT}-h120-1", *RUN),
        *in_page(f"{AGENT}-h120-2", started(), second),
    ]


def test_an_end_is_carried_past_a_page_that_holds_only_the_runs_own_work():
    ids = subagents.RunIds()
    end = ended()
    tail = [by(AGENT, THOUGHT), end]
    assert [named_by(ids, update) for update in tail] == tail
    assert paged(ids, by(AGENT, TOOL)) == [by(AGENT, TOOL)]
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started(), end)


def test_pauses_and_the_end_are_copied_in_the_order_they_happened():
    ids = subagents.RunIds()
    end = ended()
    assert named_by(ids, end) == end
    parked = [paused(AGENT, True), paused(AGENT, False)]
    # An older segment's events happened before a newer one's, whichever arrived first.
    assert paged(ids, *parked) == parked
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started(), *parked, end)


def test_a_run_that_a_page_left_open_is_named_on_what_comes_live():
    ids = subagents.RunIds()
    other = [started(OTHER), completed(OTHER)]
    assert paged(ids, started(), by(AGENT, THOUGHT), *other) == [
        *in_page(f"{AGENT}-h125-1", started(), by(AGENT, THOUGHT)),
        *in_page(f"{OTHER}-h125-1", *other),
    ]
    # Only that chat's, and only the run that is open: the other one ended in the page.
    assert named_by(ids, by(AGENT, TOOL), "other") == by(AGENT, TOOL)
    assert named_by(ids, by(OTHER, TOOL)) == by(OTHER, TOOL)
    live = [by(AGENT, TOOL), paused(AGENT, True), paused(AGENT, False), completed()]
    assert [named_by(ids, update) for update in live] == in_page(f"{AGENT}-h125-1", *live)
    # The end closed it: nothing after it is that run's.
    for update in (by(AGENT, TOOL), paused(AGENT, True), completed()):
        assert named_by(ids, update) == update


def test_a_run_still_open_after_its_copied_pauses_is_named_on_what_comes_live():
    ids = subagents.RunIds()
    parked = paused(AGENT, True)
    assert named_by(ids, parked) == parked
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started(), parked)
    # Of two pages that leave the subagent open, the newer one holds the run that goes on.
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started())
    assert named_by(ids, completed()) == in_run(completed(), f"{AGENT}-h125-1")


def test_a_live_end_that_arrives_before_its_page_is_copied_to_it():
    ids = subagents.RunIds()
    end = ended(where="a live turn")
    assert named_by(ids, end) == end
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started(), end)
    # Copied once: an older page that left the subagent cut off gets no end.
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started())


def test_what_follows_a_start_in_the_stream_belongs_to_no_page():
    ids = subagents.RunIds()
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started())
    # The stream's own run: as before, its first is unnamed and the next "<id>-2".
    stream = [*RUN, *RUN]
    assert [named_by(ids, update) for update in stream] == [
        *RUN,
        *in_page(f"{AGENT}-2", *RUN),
    ]
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started())


def test_an_end_is_never_carried_past_a_page_that_starts_its_subagent():
    """It can only belong to that page's last run, so an older run cut off stays cut off."""
    ids = subagents.RunIds()
    assert named_by(ids, ended()) == ended()
    assert paged(ids, *RUN) == in_page(f"{AGENT}-h125-1", *RUN)
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started())


def test_carrying_leaves_a_run_the_native_named_alone():
    ids = subagents.RunIds()
    assert named_by(ids, ended()) == ended()
    theirs = started(runId="r1")
    # Nothing is added to a run the native named, nothing live is named after it, and the
    # end that led the tail was that run's: no older page gets it.
    assert paged(ids, theirs) == [theirs]
    assert named_by(ids, by(AGENT, TOOL)) == by(AGENT, TOOL)
    assert paged(ids, started(), **OLDER) == in_page(f"{AGENT}-h120-1", started())
    # An end that the native named is neither renamed nor taken for the open run's.
    named = in_run(completed(), "r9")
    assert named_by(ids, named) == named
    assert named_by(ids, in_run(by(AGENT, TOOL), "r9")) == in_run(by(AGENT, TOOL), "r9")
    assert named_by(ids, completed()) == in_run(completed(), f"{AGENT}-h120-1")
    # Nor is it copied to a page.
    ids.forget("chat")
    assert named_by(ids, named) == named
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started())


def test_a_copied_end_is_named_like_a_row_of_its_page_and_starts_nothing_again():
    ids = subagents.RunIds()
    # One update can end a subagent, be its parent's own work and start another subagent.
    end = {
        **completed(OTHER),
        "_meta": {
            **completed(OTHER)["_meta"],
            **by(AGENT, {})["_meta"],
            **started("c3")["_meta"],
        },
    }
    assert named_by(ids, end) == end
    copied = {**completed(OTHER), "_meta": {**completed(OTHER)["_meta"], **by(AGENT, {})["_meta"]}}
    assert paged(ids, started(), started(OTHER)) == [
        in_run(started(), f"{AGENT}-h125-1"),
        in_run(started(OTHER), f"{OTHER}-h125-1"),
        {
            **copied,
            "_meta": {
                COMPLETED: {**completed(OTHER)["_meta"][COMPLETED], "runId": f"{OTHER}-h125-1"},
                CONTEXT: {"parentAgentId": AGENT, "runId": f"{AGENT}-h125-1"},
            },
        },
    ]


def test_a_history_page_asked_for_again_gets_the_same_answer():
    """Desktop may ask twice: what a page was given is kept, not worked out again from what
    its first answer used up."""
    ids = subagents.RunIds()
    end = ended()
    assert named_by(ids, end) == end
    newer = [paused(AGENT, True), started(OTHER)]
    first = [paused(AGENT, True), in_run(started(OTHER), f"{OTHER}-h125-1")]
    for _ in range(2):
        assert paged(ids, *newer) == first
    answer = in_page(f"{AGENT}-h120-1", started(), paused(AGENT, True), end)
    for _ in range(3):
        assert paged(ids, started(), **OLDER) == answer
    # Asking again changed nothing that is still owed: the other run is open as it was, and
    # its end, when it comes, is the newer page's alone.
    assert named_by(ids, completed(OTHER)) == in_run(completed(OTHER), f"{OTHER}-h125-1")
    assert paged(ids, *newer) == [*first, in_run(completed(OTHER), f"{OTHER}-h125-1")]
    assert paged(ids, started(), **OLDER) == answer


def test_what_comes_live_of_a_run_that_a_page_left_open_joins_that_pages_answer():
    """Desktop asks for a page again on a retry, and for every page after a reload that the
    bridge answered from its record. There the live end comes before any row of its run and
    is dropped, so the page that holds the start must bring the end from then on."""
    ids = subagents.RunIds()
    name = f"{AGENT}-h125-1"
    parked = paused(AGENT, True)
    assert named_by(ids, parked) == parked
    given = in_page(name, started(), parked)
    assert paged(ids, started()) == given
    older = in_page(f"{OTHER}-h120-1", started(OTHER), completed(OTHER))
    assert paged(ids, started(OTHER), completed(OTHER), **OLDER) == older

    end = ended(where="a live turn")
    live = [by(AGENT, TOOL), paused(AGENT, False), by(AGENT, THOUGHT), end]
    sent = copy.deepcopy(live)
    for update in sent:
        ids.stamp("chat", update)
    assert sent == in_page(name, *live)
    # The pause and the end follow the page's own rows and what it was given before, in the
    # order they happened, under the run's name. The run's own work belongs to no page.
    answer = [*given, *in_page(name, paused(AGENT, False), end)]
    for _ in range(3):
        assert paged(ids, started()) == answer
    # They are copies: what went out live is as it was, and no other page gains anything.
    assert sent == in_page(name, *live)
    assert paged(ids, started(OTHER), completed(OTHER), **OLDER) == older
    # The end closed the run: what follows it is not that page's.
    for update in (completed(), paused(AGENT, True), by(AGENT, TOOL)):
        assert named_by(ids, update) == update
    assert paged(ids, started()) == answer


def test_a_live_copy_is_named_like_a_row_of_its_page_and_starts_nothing_again():
    ids = subagents.RunIds()
    names = {AGENT: f"{AGENT}-h125-1", OTHER: f"{OTHER}-h125-1"}
    opened = [in_run(started(), names[AGENT]), in_run(started(OTHER), names[OTHER])]
    assert paged(ids, started(), started(OTHER)) == opened
    # One update can end a subagent, be its parent's own work and start another subagent.
    end = completed(OTHER)["_meta"][COMPLETED]
    parts = {COMPLETED: end, CONTEXT: {"parentAgentId": AGENT}, **started("c3")["_meta"]}
    named = {
        COMPLETED: {**end, "runId": names[OTHER]},
        CONTEXT: {"parentAgentId": AGENT, "runId": names[AGENT]},
    }
    # Live, both open runs are named, and the stream's own first run of the third is not.
    assert named_by(ids, {**completed(OTHER), "_meta": parts}) == {
        **completed(OTHER),
        "_meta": {**named, **started("c3")["_meta"]},
    }
    assert paged(ids, started(), started(OTHER)) == [
        *opened,
        {**completed(OTHER), "_meta": named},
    ]


def test_a_page_takes_only_so_many_copies_of_what_comes_live():
    ids = subagents.RunIds()
    name = f"{AGENT}-h125-1"
    first = paused(AGENT, True)
    assert named_by(ids, first) == first
    assert paged(ids, started()) == in_page(name, started(), first)
    parked = [paused(AGENT, number % 2 == 1) for number in range(subagents.CARRIED_LIMIT + 1)]
    end = ended(where="a live turn")
    # Every one of them is named as it goes out, whatever the page keeps.
    for update in (*parked, end):
        assert named_by(ids, update) == in_run(update, name)
    # The page keeps that many copies, the one it was given before among them; a pause past
    # the limit is not copied. The end always is: it is what ends the run in that page.
    kept = parked[: subagents.CARRIED_LIMIT - 1]
    for _ in range(2):
        assert paged(ids, started()) == in_page(name, started(), first, *kept, end)


def test_a_load_that_desktop_is_shown_forgets_what_came_live_of_an_open_run():
    ids = subagents.RunIds()
    name = f"{AGENT}-h125-1"
    end = ended(where="a live turn")
    assert paged(ids, started()) == in_page(name, started())
    assert named_by(ids, end) == in_run(end, name)
    assert paged(ids, started()) == in_page(name, started(), end)
    # A replay kept from Desktop is no new tail: the page answers as it did.
    ids.forget("chat", shown=False)
    assert paged(ids, started()) == in_page(name, started(), end)
    # A replay Desktop is shown is one. Its tail or a newer page holds that end now; the
    # page starts over, and so does what it gains of the run it leaves open this time.
    ids.forget("chat")
    assert paged(ids, started()) == in_page(name, started())
    assert named_by(ids, paused(AGENT, True)) == in_run(paused(AGENT, True), name)
    assert paged(ids, started()) == in_page(name, started(), paused(AGENT, True))


def test_a_load_that_desktop_is_shown_starts_the_pages_over():
    ids = subagents.RunIds()
    end = ended()
    open_run = in_run(started(OTHER), f"{OTHER}-h125-1")
    assert named_by(ids, end) == end
    assert paged(ids, started(OTHER)) == [open_run]

    # A replay kept from Desktop (a handoff's, a resume's) is no new tail: nothing it holds
    # is carried, and what Desktop holds is owed as before.
    ids.forget("chat", shown=False)
    for update in (ended(where="a kept replay"), started(), started(OTHER)):
        ids.stamp("chat", sent := copy.deepcopy(update), shown=False)
        assert sent == update
    assert named_by(ids, by(OTHER, TOOL)) == in_run(by(OTHER, TOOL), f"{OTHER}-h125-1")
    assert paged(ids, started(OTHER)) == [open_run]

    # A replay Desktop is shown is a new tail, and its pages come again: the run a page of
    # the old round left open, the end that led the old tail and what that page was given
    # are all forgotten.
    ids.forget("chat")
    assert named_by(ids, by(OTHER, TOOL)) == by(OTHER, TOOL)
    assert named_by(ids, end) == end
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started(), end)
    # Another chat's pages are its own.
    assert named_by(ids, end, "other") == end
    ids.forget("chat")
    assert paged(ids, started(), chat="other") == in_page(f"{AGENT}-h125-1", started(), end)


def test_a_replay_kept_from_desktop_starts_no_run_in_what_desktop_holds():
    ids = subagents.RunIds()
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started())
    assert named_by(ids, ended(OTHER)) == ended(OTHER)
    ids.forget("chat", shown=False)
    for update in (*RUN, started(OTHER), completed(OTHER)):
        ids.stamp("chat", copy.deepcopy(update), shown=False)
    # Desktop was sent no start of either: the page's run is the one that goes on, and the
    # end that led its tail still waits for its page.
    assert named_by(ids, by(AGENT, TOOL)) == in_run(by(AGENT, TOOL), f"{AGENT}-h125-1")
    assert paged(ids, started(OTHER), **OLDER) == in_page(
        f"{OTHER}-h120-1", started(OTHER), ended(OTHER)
    )


def test_only_so_many_leading_ends_wait_for_their_page():
    ids = subagents.RunIds()
    agents = [f"agent-{number}" for number in range(subagents.CARRIED_LIMIT + 2)]
    for agent in agents:
        assert named_by(ids, completed(agent)) == completed(agent)
    # One past the limit is not kept, from the stream or from a page: its run stays open.
    assert paged(ids, paused(AGENT, True)) == [paused(AGENT, True)]
    kept = agents[: subagents.CARRIED_LIMIT]
    assert paged(ids, started(), *(started(agent) for agent in agents), **OLDER) == [
        in_run(started(), f"{AGENT}-h120-1"),
        *(in_run(started(agent), f"{agent}-h120-1") for agent in agents),
        *(in_run(completed(agent), f"{agent}-h120-1") for agent in kept),
    ]
    # Once a page took them there is room again.
    assert named_by(ids, completed("late")) == completed("late")
    assert paged(ids, started("late"), startRowId=60, endRowId=90) == in_page(
        "late-h90-1", started("late"), completed("late")
    )


def test_only_so_many_pages_take_part_in_carrying():
    ids = subagents.RunIds()
    last = 1000 + subagents.PAGE_LIMIT
    for update in (ended(), ended(OTHER), ended("c3")):
        assert named_by(ids, update) == update
    # A page without a subagent's start, end or pause leaves nothing to keep.
    for row in range(subagents.PAGE_LIMIT + 1):
        assert paged(ids, MESSAGE, by(AGENT, TOOL), startRowId=row, endRowId=row + 1) == [
            MESSAGE,
            by(AGENT, TOOL),
        ]
    first = in_page(f"{OTHER}-h1001-1", started(OTHER), ended(OTHER))
    assert paged(ids, started(OTHER), startRowId=1000, endRowId=1001) == first
    for row in range(1001, last - 1):
        run = in_page(f"{OTHER}-h{row + 1}-1", started(OTHER), completed(OTHER))
        assert paged(ids, started(OTHER), completed(OTHER), startRowId=row, endRowId=row + 1) == run
    # The last page within the limit still takes part.
    assert paged(ids, started("c3"), startRowId=last - 1, endRowId=last) == in_page(
        f"c3-h{last}-1", started("c3"), ended("c3")
    )
    # One page more: its runs are named, which needs no memory, but nothing is copied to it
    # and nothing live is named after it, however often it is asked for.
    for _ in range(2):
        assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started())
    assert named_by(ids, by(AGENT, TOOL)) == by(AGENT, TOOL)
    # A page that was kept still gets its answer.
    assert paged(ids, started(OTHER), startRowId=1000, endRowId=1001) == first


def test_naming_what_leads_the_stream_never_raises_on_odd_updates():
    ids = subagents.RunIds()
    assert paged(ids, started()) == in_page(f"{AGENT}-h125-1", started())
    assert named_by(ids, ended(OTHER)) == ended(OTHER)
    for update in ODD_UPDATES:
        assert named_by(ids, update) == update
    # None of them ended the open run or was kept for a page.
    assert named_by(ids, completed()) == in_run(completed(), f"{AGENT}-h125-1")
    assert paged(ids, started(OTHER), **OLDER) == in_page(
        f"{OTHER}-h120-1", started(OTHER), ended(OTHER)
    )


async def get(port, target, host=None):
    """Status, headers and body of a request the server answers and closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n\r\n".encode())
    await writer.drain()
    head, _, body = (await asyncio.wait_for(reader.read(), 10)).decode().partition("\r\n\r\n")
    writer.close()
    status, *fields = head.split("\r\n")
    return int(status.split()[1]), dict(field.split(": ", 1) for field in fields), body


async def quiet(reader):
    """Whether an event stream stays open without sending anything for a moment."""
    try:
        async with asyncio.timeout(0.3):
            while line := await reader.readline():
                if line.startswith(b"data:"):
                    return False
    except TimeoutError:
        return True
    return False


def events_url(server, session_id):
    return f"/subagents/events?session={quote(session_id, safe='')}&t={server.token}"


def reply_url(server, session_id, agent):
    chat, row = quote(session_id, safe=""), quote(agent, safe="")
    return f"/subagents/reply?session={chat}&agent={row}&t={server.token}"


def test_the_subagents_page_streams_a_chats_rows_as_they_change(acp_native, tmp_path, monkeypatch):
    gate = tmp_path / "next-step"
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster, server = chat.session_id, bridge.roster, bridge.side_server
            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            await until(lambda: row(roster, AGENT, session_id)["step"] == "Running command")
            assert not roster.showing(session_id)

            status, reader, writer = await request(
                server.port, "GET", events_url(server, session_id)
            )
            assert status == 200
            _, idle, _ = await request(server.port, "GET", events_url(server, "another-chat"))
            # The rows as they are, right after connecting.
            (first,) = await lines(reader, 1)
            assert first == roster.snapshot(session_id)
            (running,) = first["subagents"]
            assert set(running) == LISTED
            assert (running["id"], running["status"], running["step"]) == (
                AGENT,
                "running",
                "Running command",
            )
            assert await lines(idle, 1) == [{"subagents": []}]
            assert roster.showing(session_id)

            gate.touch()
            (second,) = await lines(reader, 1)
            assert second == {"subagents": [{**running, "step": "Ran sleep"}]}

            await stop(bridge, chat, turn)
            (third,) = await lines(reader, 1)
            (ended,) = third["subagents"]
            assert (ended["status"], ended["note"]) == ("failed", "[Error] Canceled by user")
            assert ended["ended"] >= ended["started"] and ended["step"] == "Ran sleep"
            # Another chat's page heard nothing of it.
            assert await quiet(idle)

            # Closing the tab is noticed at once, well before the keepalive.
            writer.close()
            await until(lambda: not roster.showing(session_id), side.KEEPALIVE / 3)
            assert roster.showing("another-chat")
            # A closing bridge ends the streams still open.
            await server.close()
            await asyncio.wait_for(idle.read(), 5)
            assert not roster.showing("another-chat")

    asyncio.run(scenario())


def test_the_subagents_page_needs_its_token_and_a_loopback_host(acp_native, tmp_path, monkeypatch):
    # The real page files are not this test's business: serve stand-ins.
    folder = tmp_path / "page"
    folder.mkdir()
    kinds = {
        "subagents.css": "text/css",
        "subagents-view.js": "text/javascript",
        "subagents.js": "text/javascript",
    }
    for name in (*kinds, "notes.js"):
        (folder / name).write_text(f"/* stand-in {name} */")
    (tmp_path / "outside.js").write_text("/* outside */")
    monkeypatch.setattr(subagent_tab, "PAGE", folder)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            server = bridge.side_server
            port, token = server.port, server.token
            page = server.path('chat-1<"&>', "subagents")
            assert page == f"/subagents/chat-1%3C%22%26%3E?t={token}"
            routes = (
                page,
                f"/subagents/assets/subagents.js?t={token}",
                events_url(server, "c"),
                reply_url(server, "c", AGENT),
            )
            for route in routes:
                assert (await get(port, route.replace(token, "wrong")))[0] == 403
                assert (await get(port, route.replace(f"t={token}", "")))[0] == 403
                assert (await get(port, route, host="evil.example"))[0] == 403
            assert not bridge.roster.showing("c")

            for host in (None, f"localhost:{port}"):
                status, headers, shell = await get(port, page, host)
                assert status == 200 and headers["Content-Type"].startswith("text/html")
                assert (
                    f'<body data-session="chat-1&lt;&quot;&amp;&gt;" data-token="{token}">' in shell
                )
                assert [f"/subagents/assets/{name}?t={token}" in shell for name in kinds] == [
                    True
                ] * 3
                # Every script the shell names is one this server serves.
                scripts = re.findall(r'<script src="([^"]*)"></script>', shell)
                assert len(scripts) == 3
                for source in scripts:
                    status, headers, _ = await get(port, source, host)
                    assert status == 200
                    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
            status, _, renderer = await get(port, scripts[0])
            assert "SideMarkdown" in renderer and "stand-in" not in renderer

            for name, kind in kinds.items():
                status, headers, body = await get(port, f"/subagents/assets/{name}?t={token}")
                assert (status, body) == (200, f"/* stand-in {name} */")
                assert headers["Content-Type"] == f"{kind}; charset=utf-8"
            # Only the page's three files, whatever else its folder (or the side page) holds.
            for name in ("notes.js", "../outside.js", "side.js", "", "subagents.js/x"):
                assert (await get(port, f"/subagents/assets/{name}?t={token}"))[0] == 404
            assert (await get(port, f"/side/assets/subagents.js?t={token}"))[0] == 404

    asyncio.run(scenario())


def test_the_subagents_page_fetches_what_a_subagent_said(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster, server = chat.session_id, bridge.roster, bridge.side_server
            port, url = server.port, reply_url(server, session_id, AGENT)
            assert (await get(port, url))[0] == 404
            turn = delegate(bridge, chat)
            await until(lambda: states(roster, session_id) == {AGENT: "running"})
            await until(lambda: row(roster, AGENT, session_id)["step"] == "Running command")

            for host in (None, f"localhost:{port}"):
                status, headers, body = await get(port, url, host)
                assert status == 200
                assert headers["Content-Type"] == "application/json; charset=utf-8"
                # The text itself: not the page of a chat that happens to be named "reply".
                assert json.loads(body) == {"reply": REPLY}
            # Only with the token, from a loopback page.
            for refused in (
                get(port, url.replace(server.token, "wrong")),
                get(port, url.replace(f"&t={server.token}", "")),
                get(port, url, "evil.example"),
            ):
                status, _, body = await refused
                assert status == 403 and "Plan" not in body
            # Only a row this chat has, and only by GET.
            for target in (
                reply_url(server, "another-chat", AGENT),
                reply_url(server, session_id, "gone"),
                reply_url(server, session_id, ""),
                f"/subagents/reply?agent={AGENT}&t={server.token}",
                f"/subagents/reply?t={server.token}",
                f"/subagents/reply/{session_id}?agent={AGENT}&t={server.token}",
            ):
                assert (await get(port, target))[::2] == (404, "Not found")
            assert await fetch(port, "POST", url, {}) == (404, "Not found")

            # A row that said nothing has an empty reply; any text comes back as it was said.
            roster.apply(session_id, started("b2"))
            other = reply_url(server, session_id, "b2")
            assert json.loads((await get(port, other))[2]) == {"reply": ""}
            roster.apply(session_id, said('Fertig ✓ – "quoted" <b>\n', agent="b2"))
            assert json.loads((await get(port, other))[2]) == {"reply": 'Fertig ✓ – "quoted" <b>\n'}

            # It outlives the subagent, and goes with the chat.
            await stop(bridge, chat, turn)
            assert json.loads((await get(port, url))[2]) == {"reply": REPLY}
            await bridge.dispatch("session/close", {"sessionId": session_id})
            assert (await get(port, url))[0] == 404

    asyncio.run(scenario())


def native_loads(native):
    return [call["pid"] for call in logged(native) if call["method"] == "session/load"]


@pytest.mark.parametrize("method", ["session/load", "session/resume"])
def test_reloading_an_idle_chat_leaves_its_subagents_rows_alone(
    acp_native, tmp_path, monkeypatch, method
):
    """A native keeps its subagents over a load of its own chat (verified on 3000.12.43): one
    that a Stop parked carries on with the next prompt, and finishes."""
    gate = tmp_path / "may-finish"
    monkeypatch.setenv("FAKE_FOREGROUND_SUBAGENT", "1")
    monkeypatch.setenv("FAKE_SUBAGENT_GATE", str(gate))

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat, other = await new_chat(bridge, tmp_path), await new_chat(bridge, tmp_path)
            session_id, roster, server = chat.session_id, bridge.roster, bridge.side_server
            native = chat.backend

            def listed():
                return [
                    (item["id"], item["status"], item["note"], item["ended"])
                    for item in rows(roster, session_id)
                ]

            # A reload during a turn is answered from the bridge's own record.
            turn = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "foreground"))
            )
            await until(lambda: states(roster, session_id) == {WORKER: "running"})
            await bridge.dispatch(method, chat.setup)
            assert states(roster, session_id) == {WORKER: "running"}
            assert native_loads(acp_native) == []
            # Stop parks a foreground subagent instead of ending it.
            await stop(bridge, chat, turn)
            await until(lambda: states(roster, session_id) == {WORKER: "paused"})

            # The fake has no subagent that works while its chat is idle, so one is fed in.
            roster.apply(session_id, started("b2"))
            roster.apply(other.session_id, started("elsewhere"))
            before = rows(roster, session_id)
            _, reader, writer = await request(server.port, "GET", events_url(server, session_id))
            await lines(reader, 1)

            await bridge.dispatch(method, chat.setup)
            assert chat.backend is native and native_loads(acp_native) == [native.process.pid]
            assert rows(roster, session_id) == before
            assert listed() == [(WORKER, "paused", "", None), ("b2", "running", "", None)]
            assert await quiet(reader)

            # The next prompt un-pauses the parked one, and it finishes.
            following = asyncio.create_task(
                bridge.dispatch("session/prompt", prompt_params(session_id, "next"))
            )
            await until(lambda: states(roster, session_id)[WORKER] == "running")
            assert listed() == [(WORKER, "running", "", None), ("b2", "running", "", None)]
            assert await lines(reader, 1) == [roster.snapshot(session_id)]
            gate.touch()
            assert await following == {"stopReason": "end_turn"}
            done = row(roster, WORKER, session_id)
            assert (done["status"], done["note"]) == ("done", "")
            assert done["started"] <= done["ended"] <= time.time()
            assert await lines(reader, 1) == [roster.snapshot(session_id)]
            assert states(roster, session_id) == {WORKER: "done", "b2": "running"}
            assert states(roster, other.session_id) == {"elsewhere": "running"}
            writer.close()

    asyncio.run(scenario())


def test_a_reload_that_fails_leaves_the_rows_as_they_were(acp_native, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_LOAD_ACCOUNT", "ansuman-1")

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat = await new_chat(bridge, tmp_path)
            session_id, roster, server = chat.session_id, bridge.roster, bridge.side_server
            native = chat.backend
            # The fake's delegate turn keeps its prompt open, so an idle chat's row is fed in.
            roster.apply(session_id, started())
            before = rows(roster, session_id)
            _, reader, writer = await request(server.port, "GET", events_url(server, session_id))
            await lines(reader, 1)

            with pytest.raises(acp.RpcError):
                await bridge.dispatch("session/load", chat.setup)
            assert chat.backend is native and native_loads(acp_native) == [native.process.pid]
            assert rows(roster, session_id) == before and before[0]["status"] == "running"
            assert await quiet(reader)
            writer.close()

    asyncio.run(scenario())


def test_subagents_command_opens_the_tab_without_waiting_for_the_turn(acp_native, tmp_path):
    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "delegate"))
            await wait_for_text(client, "live progress")

            start = len(client.messages)
            asked = await asyncio.wait_for(client.prompt(session_id, " /subagents"), 5)
            assert asked["result"] == {"stopReason": "end_turn"} and not turn.done()
            (preview,) = opened(client.messages[start:])
            assert set(preview) == {"sessionId", "previewUrl"}
            assert preview["sessionId"] == session_id
            url = urlsplit(preview["previewUrl"])
            assert (url.scheme, url.hostname) == ("http", "127.0.0.1")
            assert url.path == f"/subagents/{quote(session_id, safe='')}"
            assert url.query.startswith("t=") and len(url.query) > 20
            # The chat itself shows nothing of it.
            assert replayed(client.messages[start:], "agent_message_chunk") == []

            # The tab loads the page, whose event stream shows the turn's subagent.
            status, _, body = await get(url.port, f"{url.path}?{url.query}")
            assert status == 200 and f'data-session="{session_id}"' in body
            events = f"/subagents/events?session={quote(session_id, safe='')}&{url.query}"
            status, reader, stream = await request(url.port, "GET", events)
            assert status == 200
            async with asyncio.timeout(10):
                while not (shown := (await lines(reader, 1))[0]["subagents"]):
                    pass
            assert [(item["id"], item["status"]) for item in shown] == [(AGENT, "running")]

            # Desktop opens a new tab per notice: a chat whose page is connected gets none.
            start = len(client.messages)
            again = await asyncio.wait_for(client.prompt(session_id, "/subagents"), 5)
            assert again["result"] == {"stopReason": "end_turn"}
            assert opened(client.messages[start:]) == []
            # Closing the tab is noticed at once, so the next /subagents opens it again.
            stream.close()
            async with asyncio.timeout(side.KEEPALIVE / 3):
                while not opened(client.messages[start:]):
                    await asyncio.sleep(0.05)
                    await asyncio.wait_for(client.prompt(session_id, "/subagents"), 5)
            assert len(opened(client.messages[start:])) == 1

            # A reload mid-turn replays the chat: neither the command nor a tab to reopen.
            start = len(client.messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            await asyncio.wait_for(client.request("session/load", params), 5)
            replay = client.messages[start:]
            assert replayed_texts(replay, "user_message_chunk") == ["delegate"]
            assert opened(replay) == []
            assert not turn.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"

        prompts = [call["text"] for call in logged(acp_native) if call["text"] is not None]
        assert prompts == ["delegate"]
        assert not any("subagents" in text for text in client.texts())
        (*_, latest) = replayed(client.messages, "available_commands_update")
        names = [item["name"] for item in latest["availableCommands"]]
        assert names.count("subagents") == 1 and {"help", "btw"} <= set(names)

    asyncio.run(scenario())


def test_subagents_command_does_not_wake_a_chat_released_for_idleness(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            calls = [call for call in logged(acp_native) if call["sessionId"] == chat.session_id]
            reply = await bridge.dispatch(
                "session/prompt", prompt_params(chat.session_id, "/subagents")
            )
            assert reply == {"stopReason": "end_turn"} and chat.suspended
            (preview,) = opened(messages)
            assert f"/subagents/{chat.session_id}?t=" in preview["previewUrl"]
            assert calls == [
                call for call in logged(acp_native) if call["sessionId"] == chat.session_id
            ]

    asyncio.run(scenario())


def test_subagents_command_ignores_what_follows_it_but_is_a_whole_word(acp_native, tmp_path):
    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}
            for blocks in (
                [{"type": "text", "text": "/subagents extra words"}],
                [{"type": "text", "text": "/subagents"}, image],
            ):
                start = len(messages)
                reply = await bridge.dispatch(
                    "session/prompt", {"sessionId": chat.session_id, "prompt": blocks}
                )
                assert reply == {"stopReason": "end_turn"}
                (preview,) = opened(messages[start:])
                assert f"/subagents/{chat.session_id}?t=" in preview["previewUrl"]
            assert prompts(acp_native) == []

            # A longer first word is not this command: the agent gets it as typed.
            start = len(messages)
            reply = await bridge.dispatch(
                "session/prompt", prompt_params(chat.session_id, "/subagents-foo")
            )
            assert reply == {"stopReason": "end_turn"}
            assert opened(messages[start:]) == []
            assert prompts(acp_native) == [("ansuman-1", "/subagents-foo")]

    asyncio.run(scenario())
