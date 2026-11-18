import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
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
    until,
    wait_for_text,
)
from test_side import fetch, lines, opened, request
from test_subagent_tab import get, new_chat

from devin_switch import acp, pr_tab, side

STREAM = "cognition.ai/streamingMessageId"
CONTEXT = "cognition.ai/subagent_context"
THOUGHT = "agent_thought_chunk"
USER = "user_message_chunk"
# The head commit of the stand-in GitHub's pull request #596 (each has its own).
OID = f"{596:040x}"
# Exactly what the tab asks `gh pr view` and `gh repo view` for.
FIELDS = (
    "number,title,state,isDraft,url,headRefName,baseRefName,headRefOid,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,additions,deletions,changedFiles,"
    "author,updatedAt,latestReviews,reviewRequests"
)
ALLOWED = "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed,viewerDefaultMergeMethod"
ALLOWS = {
    "mergeCommitAllowed": True,
    "squashMergeAllowed": True,
    "rebaseMergeAllowed": False,
    "viewerDefaultMergeMethod": "SQUASH",
}


def link(number, repo="acme/app"):
    return f"https://github.com/{repo}/pull/{number}"


def said(text, stream="m1", kind="agent_message_chunk", agent=None):
    """One chunk of a message, as CLI 3000.12.43 streams it (a subagent's names its writer)."""
    meta = {STREAM: stream}
    if agent:
        meta[CONTEXT] = {"parentAgentId": agent}
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}, "_meta": meta}


def step(call, kind="tool_call_update", **fields):
    return {"sessionUpdate": kind, "toolCallId": call, **fields}


def printed(text):
    """A command's output so far, as CLI 3000.12.43 sends it while the command runs."""
    return [{"type": "content", "content": {"type": "text", "text": text}}]


def preview(command):
    """The command being written, as CLI 3000.12.43 shows it beside the tool call's input."""
    resource = {"mimeType": "text/x-shellscript", "text": command, "uri": "tool://preview"}
    return [{"type": "content", "content": {"type": "resource", "resource": resource}}]


def waiting(number, repo="acme/app"):
    """A pull request the tab has not looked up yet."""
    return {
        "url": link(number, repo),
        "repo": repo,
        "number": number,
        "loading": True,
        "error": None,
        "busy": None,
    }


def shown(pulls, session_id="chat"):
    return pulls.snapshot(session_id)["pulls"]


def urls(pulls, session_id="chat"):
    return [pull["url"] for pull in shown(pulls, session_id)]


def check_run(name, conclusion="SUCCESS", status="COMPLETED", run=7001, workflow="CI"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/acme/app/actions/runs/{run}/job/{run}9",
        "startedAt": "2026-11-18T09:50:00Z",
        "completedAt": "2026-11-18T09:55:00Z",
    }


def commit_status(context, state):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": f"https://ci.example/{context}",
        "startedAt": "2026-11-18T09:50:00Z",
    }


def reviewed(author, state):
    return {
        "id": f"PRR_{author}",
        "author": {"login": author},
        "authorAssociation": "MEMBER",
        "body": "",
        "submittedAt": "2026-11-18T09:58:00Z",
        "state": state,
    }


def view(number=596, repo="acme/app", **changes):
    """What `gh pr view --json` prints for a pull request that is ready to merge."""
    return {
        "number": number,
        "title": "Add the thing",
        "state": "OPEN",
        "isDraft": False,
        "url": link(number, repo),
        "headRefName": "feature/thing",
        "baseRefName": "main",
        "headRefOid": f"{number:040x}",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [check_run("test")],
        "additions": 120,
        "deletions": 7,
        "changedFiles": 4,
        "author": {"id": "U_1", "is_bot": False, "login": "octocat", "name": "Octo Cat"},
        "updatedAt": "2026-11-18T10:00:00Z",
        "latestReviews": [reviewed("hubot", "APPROVED")],
        "reviewRequests": [],
        **changes,
    }


class Gh:
    """What the stand-in GitHub CLI knows, and what it was run with."""

    def __init__(self, home):
        self.home = home
        kinds = ("pulls", "behind", "repos", "numbers", "branches", "fails", "gates")
        self.write({**{kind: {} for kind in kinds}, "hangs": []})

    def read(self):
        return json.loads((self.home / "state.json").read_text())

    def write(self, state):
        # Whole at once: a running stand-in may read it at any moment.
        scratch = self.home / "state-test.json"
        scratch.write_text(json.dumps(state))
        scratch.replace(self.home / "state.json")

    def put(self, **changes):
        self.write({**self.read(), **changes})

    def pull(self, number=596, repo="acme/app", behind=0, **changes):
        """Give GitHub a pull request, `behind` commits behind its base; its link."""
        state, url = self.read(), link(number, repo)
        state["pulls"][url] = view(number, repo, **changes)
        state["behind"][url] = behind
        state["repos"].setdefault(repo, ALLOWS)
        self.write(state)
        return url

    def change(self, url, **changes):
        state = self.read()
        state["pulls"][url].update(changes)
        self.write(state)

    def calls(self):
        log = self.home / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def runs(self, *command):
        """The arguments of each run so far (of one command, when named)."""
        runs = [call["args"] for call in self.calls()]
        return [run for run in runs if run[: len(command)] == list(command)]


@pytest.fixture(autouse=True)
def gh(tmp_path, monkeypatch):
    """A stand-in `gh`, first on PATH for the test and any bridge it starts: never the real one."""
    home = tmp_path / "gh"
    script = home / "bin/gh"
    script.parent.mkdir(parents=True)
    script.write_text(f"#!{sys.executable}\n" + Path(__file__).with_name("fake_gh.py").read_text())
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_HOME", str(home))
    monkeypatch.setattr(pr_tab, "FALLBACKS", ())
    # And never a real browser window: a test that opens a link brings its own opener.
    monkeypatch.setattr(pr_tab.webbrowser, "open", lambda target: pytest.fail(f"opened {target}"))
    return Gh(home)


@pytest.fixture
def pulls(store):
    return pr_tab.Pulls(store)


@pytest.fixture
def unwatched(monkeypatch):
    """Pages hear only of what a test itself does: nothing is looked up on its own."""

    async def idle(self, tab):
        await asyncio.Event().wait()

    monkeypatch.setattr(pr_tab.Pulls, "watch", idle)


def row(pulls, url, session_id="chat"):
    (found,) = [pull for pull in shown(pulls, session_id) if pull["url"] == url]
    return found


async def looked(pulls, url, session_id="chat"):
    """A pull request's row once the tab has it and has just looked it up."""
    pulls.said(session_id, url)
    tab = pulls.tabs[session_id]
    await pulls.refresh(tab, list(tab.pulls.values()))
    return row(pulls, url, session_id)


def test_links_the_agent_and_the_user_wrote_are_listed_in_first_seen_order(pulls):
    assert pulls.snapshot("chat") == {"pulls": [], "notice": None}
    pulls.feed("chat", said(f"Opened {link(596)} and {link(7, 'Acme-AI/web.site')}, see both."))
    pulls.feed("chat", said(f"what about {link(12)}?", kind=USER))
    assert shown(pulls) == [waiting(596), waiting(7, "Acme-AI/web.site"), waiting(12)]
    assert pulls.snapshot("chat")["notice"] is None

    # The same pull request again is still one row, whatever follows its number.
    again = (
        f"{link(596)} {link(12)}/files {link(596)}?diff=split {link(12)}#issuecomment-9 "
        f"https://github.com/ACME/App/pull/596 https://github.com/acme/app/pull/0012 "
    )
    pulls.feed("chat", said(again, stream="m2"))
    assert urls(pulls) == [link(596), link(7, "Acme-AI/web.site"), link(12)]

    # Only the pull request itself is kept of a longer link, however it is written.
    forms = f"({link(30)}) [x]({link(31)}/commits/abc) <{link(32)}> _{link(33)}_ {link(34)}. "
    pulls.feed("chat", said(forms, stream="m3"))
    assert urls(pulls)[3:] == [link(number) for number in range(30, 35)]
    assert urls(pulls, "other-chat") == []


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/acme/app/issues/5",
        "https://github.com/acme/app/pulls",
        "https://github.com/acme/app/pulls/5",
        "https://github.com/acme/app/blob/main/pull/5",
        "https://github.com/acme/app/pull/new/feature",
        "https://github.com/acme/app/pull/",
        "https://github.com/acme/pull/5",
        "http://github.com/acme/app/pull/5",
        "github.com/acme/app/pull/5",
        "https://github.com.evil.example/acme/app/pull/5",
        "https://gitlab.com/acme/app/pull/5",
        "https://api.github.com/repos/acme/app/pulls/5",
        "https://github.com/acme/app/pull/0",
        "https://github.com/acme/app/pull/12abc",
        "https://github.com/acme/app/pull/12345678901",
    ],
)
def test_only_links_to_a_pull_request_itself_count(pulls, text):
    pulls.feed("chat", said(f"before {text} after"))
    pulls.feed("chat", said(text, stream="m2", kind=USER))
    pulls.said("chat", text)
    pulls.flush("chat")
    assert urls(pulls) == []


def test_a_link_split_across_chunks_counts_once_and_whole(pulls):
    for text in ("See https://github.com/acme/app/pull/1", "2", "3"):
        pulls.feed("chat", said(text))
        # "…/pull/1" may still grow: nothing is added while the number ends the text.
        assert urls(pulls) == []
    pulls.feed("chat", said(" for details"))
    assert urls(pulls) == [link(123)]


def test_a_link_counts_wherever_the_chunks_divide_it(store):
    text = "x" * (2 * pr_tab.TAIL) + f" merged {link(596)} today"
    for cut in range(len(text) - 60, len(text)):
        pulls = pr_tab.Pulls(store)
        pulls.feed("chat", said(text[:cut]))
        pulls.feed("chat", said(text[cut:]))
        assert urls(pulls) == [link(596)], cut
    pulls = pr_tab.Pulls(store)
    for letter in text:
        pulls.feed("chat", said(letter))
    assert urls(pulls) == [link(596)]


@pytest.mark.parametrize(
    "ending",
    [
        said("Next message", stream="m2"),
        said("a question", kind=USER),
        step("call_1", "tool_call", title="Running command"),
        step("call_1", status="completed"),
        {"sessionUpdate": "tool_call"},
    ],
)
def test_a_link_that_ends_a_message_counts_once_the_message_has_ended(pulls, ending):
    pulls.feed("chat", said(f"Opened {link(41)}"))
    assert urls(pulls) == []
    pulls.feed("chat", ending)
    assert urls(pulls) == [link(41)]


@pytest.mark.parametrize(
    "between",
    [
        {"sessionUpdate": "usage_update", "used": 1, "size": 10},
        {"sessionUpdate": "usage_update", "_meta": {CONTEXT: {"parentAgentId": "root"}}},
        {"sessionUpdate": "plan", "entries": []},
        {"sessionUpdate": "current_mode_update", "currentModeId": "ask"},
        {"sessionUpdate": "available_commands_update", "availableCommands": []},
        {"sessionUpdate": "session_info_update", "title": "A chat"},
        {"sessionUpdate": "config_option_update", "configOptions": []},
        said("hm", kind=THOUGHT),
        said("hm", stream="t1", kind=THOUGHT),
        {"sessionUpdate": "something_new"},
        {},
    ],
)
def test_what_arrives_in_the_middle_of_a_message_does_not_end_it(pulls, between):
    # As a native streams: usage, plans, thoughts and settings come between two chunks.
    pulls.feed("chat", said("See https://github.com/acme/app/pull/1"))
    pulls.feed("chat", between)
    assert urls(pulls) == []
    pulls.feed("chat", said("23 for details"))
    assert urls(pulls) == [link(123)]
    # Nor does it say that a message which ends with a link is over: the turn's end does.
    pulls.feed("chat", said(f" and {link(41)}"))
    pulls.feed("chat", between)
    assert urls(pulls) == [link(123)]
    pulls.flush("chat")
    assert urls(pulls) == [link(123), link(41)]


def test_the_end_of_a_turn_counts_the_link_its_last_message_ended_with(pulls):
    pulls.feed("chat", said(f"Opened {link(41)}"))
    pulls.flush("other-chat")
    assert urls(pulls) == []
    pulls.flush("chat")
    assert urls(pulls) == [link(41)]
    # What ended is gone: a later chunk is the start of something new.
    pulls.feed("chat", said("5 more"))
    pulls.flush("chat")
    assert urls(pulls) == [link(41)]


def test_another_writers_updates_leave_a_message_still_being_written_alone(pulls):
    pulls.feed("chat", said("See https://github.com/acme/app/pull/1"))
    # A background subagent and another chat write meanwhile.
    theirs = {CONTEXT: {"parentAgentId": "sub1"}}
    pulls.feed("chat", said("hm", kind=THOUGHT, agent="sub1"))
    pulls.feed("chat", step("call_9", "tool_call", title="Reading", _meta=theirs))
    pulls.feed("chat", said(f"mine is {link(77)}", stream="s1", agent="sub1"))
    pulls.feed("other-chat", said(" elsewhere"))
    pulls.feed("other-chat", step("call_1", "tool_call", title="Running command"))
    assert urls(pulls) == []
    pulls.feed("chat", said("23 is up"))
    assert urls(pulls) == [link(123)]
    # The subagent's own next tool call ends its message; its usage says nothing.
    pulls.feed("chat", {"sessionUpdate": "usage_update", "_meta": theirs})
    assert urls(pulls) == [link(123)]
    pulls.feed("chat", step("call_9", status="completed", _meta=theirs))
    assert urls(pulls) == [link(123), link(77)]
    assert urls(pulls, "other-chat") == []


def test_a_users_prompt_is_a_whole_message(pulls):
    pulls.said("chat", "nothing here")
    assert "chat" not in pulls.tabs
    pulls.said("chat", f"please review {link(5)} and\n{link(6)}")
    assert urls(pulls) == [link(5), link(6)]


def test_a_chat_keeps_its_newest_pull_requests(pulls):
    for number in range(1, pr_tab.MAX_PULLS + 4):
        pulls.feed("chat", said(f"{link(number)} "))
    assert pr_tab.MAX_PULLS == 20
    assert urls(pulls) == [link(number) for number in range(4, 24)]
    # One that was dropped and shows up again is the newest.
    pulls.feed("chat", said(f"{link(1)} "))
    assert urls(pulls) == [*(link(number) for number in range(5, 24)), link(1)]


def test_a_link_is_read_once_however_its_message_goes_on(pulls):
    pulls.feed("chat", said(f"First {link(1)} "))
    assert urls(pulls) == [link(1)]
    # Twenty newer ones push it out; the rest of its message does not bring it back.
    pulls.said("chat", " ".join(link(number) for number in range(2, 22)))
    pulls.feed("chat", said("and that is all"))
    pulls.flush("chat")
    assert urls(pulls) == [link(number) for number in range(2, 22)]


def test_only_a_gh_pr_commands_input_and_output_are_read(pulls):
    # As CLI 3000.12.43 streams a command: its input grows, then it runs, then it ends.
    pulls.feed("chat", step("c1", "tool_call", title="Running command", rawInput=""))
    partial = "gh pr view https://github.com/acme/app/pull/12"
    writing = step("c1", title="Running command", rawInput={"command": partial})
    pulls.feed("chat", {**writing, "content": preview(partial)})
    assert urls(pulls) == []
    whole = f"gh pr view {link(123)} --json url"
    ran = step("c1", "tool_call", title="Ran gh", rawInput={"command": whole})
    pulls.feed("chat", {**ran, "content": preview(whole)})
    assert urls(pulls) == [link(123)]
    # The output so far can end inside a link too.
    so_far = "https://github.com/acme/app/pull/45"
    pulls.feed("chat", step("c1", status="in_progress", content=printed(so_far)))
    assert urls(pulls) == [link(123)]
    more = f"{link(456)}\nnext https://github.com/acme/app/pull/7"
    pulls.feed("chat", step("c1", status="in_progress", content=printed(more)))
    assert urls(pulls) == [link(123), link(456)]
    pulls.feed("chat", step("c1", status="completed"))
    assert urls(pulls) == [link(123), link(456), link(7)]
    # The native sends the whole output once more after the call has ended.
    pulls.feed("chat", step("c1", status="completed", content=printed(f"{more}\n{link(8)}")))
    assert urls(pulls) == [link(123), link(456), link(7), link(8)]


def test_a_command_that_ends_with_a_link_counts_once_it_runs(pulls):
    command = f"cd app && gh pr merge {link(9)}"
    pulls.feed("chat", step("c2", "tool_call", title="Ran cd", rawInput={"command": command}))
    assert urls(pulls) == []
    pulls.feed("chat", step("c2", status="in_progress"))
    assert urls(pulls) == [link(9)]

    # A replayed call arrives whole, and one named only by its title counts as well.
    create = {"title": "Ran gh", "rawInput": {"command": "gh pr create --fill"}}
    pulls.feed("chat", step("c3", "tool_call", status="completed", content=printed(link(60))))
    pulls.feed("chat", step("c4", "tool_call", **create, status="failed", rawOutput=link(61)))
    assert urls(pulls) == [link(9), link(61)]
    pulls.feed("chat", step("c5", "tool_call", title="gh pr view", rawOutput={"out": [link(62)]}))
    assert urls(pulls) == [link(9), link(61)]
    # A call that never says it ended is counted when its turn ends.
    pulls.flush("chat")
    assert urls(pulls) == [link(9), link(61), link(62)]
    # Input given as text, and links in the order the output holds them.
    both = [*printed(f"{link(71)}\n"), {"deep": {"a": [f"{link(72)} ", [f"{link(73)} "]]}}]
    pulls.feed("chat", step("c6", "tool_call", rawInput='{"command": "gh pr list"}'))
    pulls.feed("chat", step("c6", status="completed", content=both, rawOutput=f"{link(74)} "))
    assert urls(pulls)[3:] == [link(number) for number in (71, 72, 73, 74)]


def test_other_tool_calls_say_nothing_about_pull_requests(pulls):
    seen = f"see {link(99)} and more\n"
    for call, title, command in (
        ("c1", "Ran cat", "cat notes.md"),
        ("c2", "Ran gh", "gh issue view 5"),
        ("c3", "Ran git", "git log --grep pr"),
    ):
        pulls.feed("chat", step(call, "tool_call", title=title, rawInput={"command": command}))
        pulls.feed("chat", step(call, status="completed", content=printed(seen)))
    # What a file says about `gh pr` does not make reading or writing it a `gh pr` command.
    read = step("c4", "tool_call", title="Read notes.md", status="completed")
    pulls.feed("chat", {**read, "content": printed(f"gh pr view {link(98)} \n")})
    about = {"file_path": "notes.md", "content": f"Run gh pr view {link(97)} to see it.\n"}
    pulls.feed("chat", step("c6", "tool_call", title="Wrote notes.md", rawInput=about))
    pulls.feed("chat", step("c6", status="completed", content=printed(seen)))
    pulls.feed("chat", step("c7", "tool_call", title="Ran x", rawInput={"command": ["gh pr"]}))
    pulls.feed("chat", step("c7", status="completed", content=printed(seen)))
    pulls.feed("chat", step(5, "tool_call", title="gh pr view", content=printed(seen)))
    pulls.flush("chat")
    assert urls(pulls) == []
    assert pulls.tabs["chat"].calls == {}


def test_a_long_chat_remembers_only_its_latest_gh_pr_calls(pulls):
    for number in range(pr_tab.MAX_CALLS + 5):
        pulls.feed("chat", step(f"c{number}", "tool_call", title="gh pr list"))
    assert list(pulls.tabs["chat"].calls) == [f"c{number}" for number in range(5, 55)]


JUNK = [
    {},
    {"sessionUpdate": None},
    {"sessionUpdate": ["agent_message_chunk"]},
    {"sessionUpdate": {"agent_message_chunk": 1}},
    {"sessionUpdate": "agent_message_chunk"},
    {"sessionUpdate": "agent_message_chunk", "content": None},
    {"sessionUpdate": "agent_message_chunk", "content": link(90) + " "},
    {"sessionUpdate": "agent_message_chunk", "content": [link(90) + " "]},
    {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": 5}},
    {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": None}},
    {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "text": link(90) + " "}},
    {**said("x"), "_meta": "meta"},
    {**said("x"), "_meta": {STREAM: {"a": [1]}}},
    {**said("x"), "_meta": {STREAM: ["m"], CONTEXT: "sub"}},
    {**said("x"), "_meta": {CONTEXT: {"parentAgentId": 5}}},
    {**said("x"), "_meta": {CONTEXT: {"parentAgentId": ["sub"]}}},
    {"sessionUpdate": "tool_call"},
    {"sessionUpdate": "tool_call", "toolCallId": None, "title": "gh pr view"},
    {"sessionUpdate": "tool_call", "toolCallId": ["c"], "title": "gh pr view"},
    {"sessionUpdate": "tool_call", "toolCallId": {"c": 1}, "title": "gh pr view"},
    step("j1", "tool_call", title=5, rawInput=7),
    step("j2", "tool_call", title=["gh pr"], rawInput=None, status=["completed"]),
    step("j3", "tool_call", title="gh pr view", status={"completed": 1}, content=5),
    step("j3", status="completed", content={"a": [None, 5, {"b": [[], {}]}]}, rawOutput=1.5),
    step("j3", rawInput=[[[["gh pr"]]]], _meta=[]),
    {"sessionUpdate": "plan", "entries": "none"},
]


def test_whatever_a_native_sends_is_read_without_raising(pulls):
    tab = pr_tab.Tab()
    for update in JUNK:
        pulls.read(tab, update)
        pulls.feed("chat", update)
    for update in (None, 5, "text", [], [said("x")]):
        pulls.feed("chat", update)
    for session_id in (None, 5, ["chat"], {"chat": 1}):
        pulls.feed(session_id, said(f"{link(91)} "))
    pulls.flush("chat")
    assert urls(pulls) == [] and tab.pulls == {}
    assert list(pulls.tabs) == ["chat"]
    # The chat is still read as before.
    pulls.feed("chat", said(f"{link(92)} ", stream="m9"))
    assert urls(pulls) == [link(92)]


def test_a_page_gets_one_token_per_change_and_closing_ends_its_stream(pulls, unwatched):
    async def scenario():
        assert not pulls.showing("chat")
        with pulls.listen("chat") as page, pulls.listen("chat") as second, pulls.listen("b") as b:
            assert pulls.showing("chat") and pulls.showing("b") and not pulls.showing("c")
            for queue in (page, second, b):
                # The pull requests as they are, right after connecting.
                assert queue.get_nowait() is True and queue.empty()
            pulls.feed("chat", said(f"{link(1)} and {link(2)} "))
            for queue in (page, second):
                assert queue.get_nowait() is True and queue.empty()
            # A link the tab already has changes nothing; another chat's page hears nothing.
            pulls.feed("chat", said(f"{link(1)} again "))
            pulls.feed("chat", said("no link here"))
            assert page.empty() and second.empty() and b.empty()
            # One waiting token covers every change until the page takes it.
            pulls.feed("chat", said(f"{link(3)} "))
            pulls.feed("chat", said(f"{link(4)} "))
            assert page.qsize() == 1
            await pulls.close()
            assert [page.get_nowait(), page.get_nowait()] == [True, None]
            assert second.get_nowait() is True and second.get_nowait() is None
            assert b.get_nowait() is None
        assert not pulls.showing("chat") and not pulls.showing("b")
        assert urls(pulls) == [link(number) for number in range(1, 5)]

    asyncio.run(scenario())


def test_gh_runs_as_the_user_would_with_nothing_to_read(gh, store, tmp_path, monkeypatch):
    # A bridge started inside one login's isolated environment still uses the user's own login.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(store.root / "accounts/ansuman-1/config"))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    url = gh.pull(596)
    asked = ("pr", "view", url, "--json", "url,title")
    ran = asyncio.run(pr_tab.run(store, tmp_path, *asked))
    assert ran == pr_tab.Ran(0, json.dumps({"url": url, "title": "Add the thing"}) + "\n", "")
    (call,) = gh.calls()
    assert call["args"] == list(asked)
    assert Path(call["cwd"]) == tmp_path.resolve()
    assert call["stdin"] == ""
    assert call["env"] == {
        "GH_CONFIG_DIR": str(Path.home() / ".config/gh"),
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
    }
    assert call["path"] == os.environ["PATH"]

    failed = asyncio.run(pr_tab.run(store, tmp_path, "pr", "view", link(1), "--json", "url"))
    assert (failed.code, failed.out) == (1, "")
    assert failed.err.startswith("GraphQL: Could not resolve to a PullRequest")
    assert pr_tab.trouble(failed) == "GraphQL: Could not resolve to a PullRequest with that number."
    gone = asyncio.run(pr_tab.run(store, tmp_path / "no-such-folder", *asked))
    assert gone.code != 0 and gone.err.startswith("gh could not be started")
    assert len(gh.calls()) == 2


def test_a_missing_gh_is_told_apart_and_homebrews_is_found_off_the_path(
    gh, store, tmp_path, monkeypatch
):
    url = gh.pull(596)
    asked = ("pr", "view", url, "--json", "url")
    bare = tmp_path / "empty-bin"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    assert asyncio.run(pr_tab.run(store, tmp_path, *asked)) is None
    assert gh.calls() == []
    # An app started from the Dock has no shell PATH: gh is then looked for where Homebrew is.
    monkeypatch.setattr(pr_tab, "FALLBACKS", (str(bare / "gh"), str(gh.home / "bin/gh")))
    assert asyncio.run(pr_tab.run(store, tmp_path, *asked)).out == json.dumps({"url": url}) + "\n"
    (call,) = gh.calls()
    assert call["path"] == f"{gh.home / 'bin'}{os.pathsep}{bare}"


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_gh_that_does_not_answer_is_killed(gh, store, tmp_path, monkeypatch):
    monkeypatch.setattr(pr_tab, "TIMEOUT", 1)
    gh.put(hangs=["pr view"])

    async def scenario():
        began = time.monotonic()
        ran = await pr_tab.run(store, tmp_path, "pr", "view", link(1), "--json", "url")
        assert ran == pr_tab.Ran(1, "", "gh did not answer within 1 seconds")
        assert 1 <= time.monotonic() - began < 5
        (call,) = gh.calls()
        assert gone(call["pid"])

        # Nothing is left running either when the bridge stops waiting for it.
        waiting = asyncio.create_task(pr_tab.run(store, tmp_path, "pr", "view", link(2)))
        await until(lambda: len(gh.calls()) == 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert gone(gh.calls()[1]["pid"])

    asyncio.run(scenario())


def test_an_open_pull_request_is_shown_as_github_describes_it(gh, pulls):
    url = gh.pull(596, baseRefName="release/1.0", behind=3)
    shown_row = asyncio.run(looked(pulls, url))
    assert shown_row == {
        "url": url,
        "repo": "acme/app",
        "number": 596,
        "loading": False,
        "error": None,
        "busy": None,
        "title": "Add the thing",
        "state": "open",
        "draft": False,
        "head": "feature/thing",
        "base": "release/1.0",
        "author": "octocat",
        "updated": "2026-11-18T10:00:00Z",
        "additions": 120,
        "deletions": 7,
        "files": 4,
        "behind": 3,
        "conflicts": False,
        "mergeState": "clean",
        "review": {
            "decision": "approved",
            "reviews": [{"author": "hubot", "state": "approved"}],
            "requested": [],
        },
        "checks": [
            {
                "name": "test",
                "workflow": "CI",
                "status": "pass",
                "url": "https://github.com/acme/app/actions/runs/7001/job/70019",
            }
        ],
        "methods": ["squash", "merge"],
        "can": {"merge": False, "update": True, "ready": False, "draft": True, "rerun": False},
        "why": {"merge": "3 commits behind release/1.0"},
    }
    # Each by its link from the user's home; the base is one path segment of the comparison.
    assert gh.runs() == [
        ["pr", "view", url, "--json", FIELDS],
        ["api", f"repos/acme/app/compare/release%2F1.0...{OID}", "--jq", ".behind_by"],
        ["repo", "view", "acme/app", "--json", ALLOWED],
    ]
    assert {Path(call["cwd"]) for call in gh.calls()} == {Path.home().resolve()}
    assert pulls.snapshot("chat") == {"pulls": [shown_row], "notice": None}


@pytest.mark.parametrize(
    ("state", "draft", "shown_state", "shown_draft"),
    [
        ("OPEN", False, "open", False),
        ("OPEN", True, "open", True),
        ("MERGED", False, "merged", False),
        ("CLOSED", False, "closed", False),
        ("CLOSED", True, "closed", True),
    ],
)
def test_a_pull_request_is_open_merged_or_closed(gh, pulls, state, draft, shown_state, shown_draft):
    url = gh.pull(596, state=state, isDraft=draft, behind=2)
    shown_row = asyncio.run(looked(pulls, url))
    assert (shown_row["state"], shown_row["draft"]) == (shown_state, shown_draft)
    # Only an open one is compared with its base.
    assert shown_row["behind"] == (2 if state == "OPEN" else None)
    assert len(gh.runs("api")) == (1 if state == "OPEN" else 0)
    assert shown_row["methods"] == ["squash", "merge"]


@pytest.mark.parametrize(
    ("mergeable", "conflicts"),
    [("CONFLICTING", True), ("MERGEABLE", False), ("UNKNOWN", None), ("", None), (None, None)],
)
def test_conflicts_are_what_github_says_or_unknown(mergeable, conflicts):
    assert pr_tab.describe(view(mergeable=mergeable), 0, [])["conflicts"] is conflicts


@pytest.mark.parametrize(
    ("status", "merge_state"),
    [
        ("CLEAN", "clean"),
        ("BEHIND", "behind"),
        ("BLOCKED", "blocked"),
        ("DIRTY", "dirty"),
        ("DRAFT", "draft"),
        ("UNSTABLE", "unstable"),
        ("HAS_HOOKS", "has_hooks"),
        ("UNKNOWN", "unknown"),
        ("SOMETHING_NEW", "unknown"),
        (None, "unknown"),
    ],
)
def test_the_merge_state_is_githubs_in_lower_case(status, merge_state):
    assert pr_tab.describe(view(mergeStateStatus=status), 0, [])["mergeState"] == merge_state


@pytest.mark.parametrize(
    ("status", "conclusion", "shown_status"),
    [
        ("COMPLETED", "SUCCESS", "pass"),
        ("COMPLETED", "SKIPPED", "skipped"),
        ("COMPLETED", "NEUTRAL", "skipped"),
        ("COMPLETED", "FAILURE", "fail"),
        ("COMPLETED", "CANCELLED", "fail"),
        ("COMPLETED", "TIMED_OUT", "fail"),
        ("COMPLETED", "ACTION_REQUIRED", "fail"),
        ("COMPLETED", "STARTUP_FAILURE", "fail"),
        ("COMPLETED", "STALE", "fail"),
        ("COMPLETED", "", "fail"),
        ("IN_PROGRESS", "", "pending"),
        ("QUEUED", "", "pending"),
        ("PENDING", None, "pending"),
        ("WAITING", "SUCCESS", "pending"),
    ],
)
def test_a_check_run_passes_fails_is_skipped_or_pending(status, conclusion, shown_status):
    assert pr_tab.check(check_run("lint", conclusion, status, run=42, workflow="Checks")) == {
        "name": "lint",
        "workflow": "Checks",
        "status": shown_status,
        "url": "https://github.com/acme/app/actions/runs/42/job/429",
    }


@pytest.mark.parametrize(
    ("state", "shown_status"),
    [
        ("SUCCESS", "pass"),
        ("PENDING", "pending"),
        ("EXPECTED", "pending"),
        ("FAILURE", "fail"),
        ("ERROR", "fail"),
    ],
)
def test_a_commit_status_passes_fails_or_is_pending(state, shown_status):
    assert pr_tab.check(commit_status("ci/build", state)) == {
        "name": "ci/build",
        "workflow": None,
        "status": shown_status,
        "url": "https://ci.example/ci/build",
    }


def test_checks_keep_their_order_and_lack_what_github_left_out():
    rollup = [
        commit_status("deploy/preview", "PENDING"),
        check_run("unit", "FAILURE"),
        {"__typename": "CheckRun", "name": "bare", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "StatusContext", "context": "old", "state": "SUCCESS", "targetUrl": ""},
        "not a check",
    ]
    checks = pr_tab.describe(view(statusCheckRollup=rollup), 0, [])["checks"]
    assert [(item["name"], item["status"]) for item in checks] == [
        ("deploy/preview", "pending"),
        ("unit", "fail"),
        ("bare", "pass"),
        ("old", "pass"),
    ]
    assert checks[2] == {"name": "bare", "workflow": None, "status": "pass", "url": None}
    assert checks[3]["url"] is None
    assert pr_tab.describe(view(statusCheckRollup=None), 0, [])["checks"] == []


@pytest.mark.parametrize(
    ("decision", "shown_decision"),
    [
        ("APPROVED", "approved"),
        ("CHANGES_REQUESTED", "changes_requested"),
        ("REVIEW_REQUIRED", "review_required"),
        ("", "none"),
        (None, "none"),
    ],
)
def test_the_review_decision_is_githubs_or_none(decision, shown_decision):
    assert pr_tab.review(view(reviewDecision=decision))["decision"] == shown_decision


def test_reviews_name_who_reviewed_and_who_was_asked():
    seen = view(
        latestReviews=[
            reviewed("ann", "APPROVED"),
            reviewed("bob", "CHANGES_REQUESTED"),
            reviewed("cy", "COMMENTED"),
            reviewed("dee", "DISMISSED"),
            reviewed("eve", "PENDING"),
        ],
        reviewRequests=[
            {"__typename": "User", "login": "fay"},
            {"__typename": "Team", "name": "Core", "slug": "acme/core"},
            {"__typename": "Bot", "login": "review-bot"},
            {"__typename": "Mannequin"},
        ],
    )
    assert pr_tab.review(seen) == {
        "decision": "approved",
        "reviews": [
            {"author": "ann", "state": "approved"},
            {"author": "bob", "state": "changes_requested"},
            {"author": "cy", "state": "commented"},
            {"author": "dee", "state": "dismissed"},
            {"author": "eve", "state": "pending"},
        ],
        "requested": ["fay", "Core", "review-bot"],
    }


@pytest.mark.parametrize(
    ("merge", "squash", "rebase", "default", "methods"),
    [
        (True, True, True, "MERGE", ["merge", "squash", "rebase"]),
        (True, True, True, "SQUASH", ["squash", "merge", "rebase"]),
        (True, True, True, "REBASE", ["rebase", "merge", "squash"]),
        (True, False, True, "REBASE", ["rebase", "merge"]),
        (False, True, False, "SQUASH", ["squash"]),
        # A default the repository does not allow is not offered.
        (True, False, True, "SQUASH", ["merge", "rebase"]),
        (False, False, False, "MERGE", []),
        (True, True, False, None, ["merge", "squash"]),
        # What GitHub did not say is not allowed.
        (None, "yes", 1, "MERGE", []),
    ],
)
def test_merge_methods_are_those_the_repository_allows_its_default_first(
    merge, squash, rebase, default, methods
):
    repository = {
        "mergeCommitAllowed": merge,
        "squashMergeAllowed": squash,
        "rebaseMergeAllowed": rebase,
        "viewerDefaultMergeMethod": default,
    }
    assert pr_tab.allowed(repository) == methods


def test_whatever_gh_prints_about_a_pull_request_is_described_without_raising():
    blank = pr_tab.describe({}, None, [])
    assert blank == {
        "title": "",
        "state": "closed",
        "draft": False,
        "head": "",
        "base": "",
        "author": "",
        "updated": "",
        "additions": 0,
        "deletions": 0,
        "files": 0,
        "behind": None,
        "conflicts": None,
        "mergeState": "unknown",
        "review": {"decision": "none", "reviews": [], "requested": []},
        "checks": [],
        "methods": [],
        "can": {"merge": False, "update": False, "ready": False, "draft": False, "rerun": False},
        "why": {},
    }
    odd = {key: [{"x": 1}] for key in FIELDS.split(",")}
    assert pr_tab.describe(odd, None, []) == {
        **blank,
        "review": {
            "decision": "none",
            "reviews": [{"author": "", "state": "commented"}],
            "requested": [],
        },
        "checks": [{"name": "", "workflow": None, "status": "pending", "url": None}],
    }
    for value in (None, 5, True, "text", {"a": {"b": []}}):
        assert pr_tab.describe({key: value for key in FIELDS.split(",")}, None, []) is not None
    odd = pr_tab.describe(view(additions=True, deletions="7", changedFiles=2.5), 0, [])
    assert (odd["additions"], odd["deletions"], odd["files"]) == (0, 0, 0)


def test_a_repositorys_merge_methods_are_asked_once(gh, pulls):
    first, second, third = gh.pull(1), gh.pull(2), gh.pull(3)
    other = gh.pull(4, "acme/web")
    gh.put(repos={"acme/app": ALLOWS, "acme/web": {**ALLOWS, "viewerDefaultMergeMethod": "MERGE"}})

    async def scenario():
        for url in (first, second, third, other):
            pulls.said("chat", url)
        tab = pulls.tabs["chat"]
        # Looked up together, and then again.
        await pulls.refresh(tab, list(tab.pulls.values()))
        await pulls.refresh(tab, list(tab.pulls.values()))

    asyncio.run(scenario())
    assert sorted(run[2] for run in gh.runs("repo", "view")) == ["acme/app", "acme/web"]
    assert len(gh.runs("pr", "view")) == len(gh.runs("api")) == 8
    assert [row(pulls, url)["methods"] for url in (first, third, other)] == [
        ["squash", "merge"],
        ["squash", "merge"],
        ["merge", "squash"],
    ]


def test_what_github_does_not_say_is_unknown_not_a_failure(gh, pulls):
    url = gh.pull(596, behind=2)
    compared = f"api repos/acme/app/compare/main...{OID}"
    gh.put(fails={compared: "gh: Not Found (HTTP 404)", "repo view": "GraphQL: timeout"})

    async def scenario():
        shown_row = await looked(pulls, url)
        assert (shown_row["behind"], shown_row["methods"]) == (None, [])
        assert (shown_row["error"], shown_row["loading"], shown_row["state"]) == (
            None,
            False,
            "open",
        )
        assert pulls.snapshot("chat")["notice"] is None
        # GitHub answers the next time: the methods were not remembered as none.
        gh.put(fails={})
        shown_row = await looked(pulls, url)
        assert (shown_row["behind"], shown_row["methods"]) == (2, ["squash", "merge"])

    asyncio.run(scenario())
    assert len(gh.runs("repo", "view")) == 2


@pytest.mark.parametrize(
    ("printed_number", "behind"), [("0", 0), ("17", 17), ("", None), ("null", None), ("-1", None)]
)
def test_behind_is_the_number_github_gives(gh, pulls, printed_number, behind):
    url = gh.pull(596)
    gh.put(behind={url: printed_number})
    assert asyncio.run(looked(pulls, url))["behind"] == behind


def test_a_pull_request_gh_fails_on_says_why_and_leaves_the_others_alone(gh, pulls):
    good, later = gh.pull(1), gh.pull(3)
    missing = link(2)

    async def scenario():
        for url in (good, missing, later):
            pulls.said("chat", url)
        tab = pulls.tabs["chat"]
        await pulls.refresh(tab, list(tab.pulls.values()))
        assert row(pulls, missing) == {
            **waiting(2),
            "loading": False,
            # The first line of what gh wrote, not the rest.
            "error": "GraphQL: Could not resolve to a PullRequest with that number.",
        }
        assert [row(pulls, url)["title"] for url in (good, later)] == ["Add the thing"] * 2
        assert [row(pulls, url)["error"] for url in (good, later)] == [None, None]
        assert pulls.snapshot("chat")["notice"] is None

        # A later failure keeps what was known and says what went wrong, cut to a short line.
        gh.put(fails={"pr view": "  \n" + "e" * 500 + "\nsecond line"})
        await pulls.refresh(tab, list(tab.pulls.values()))
        assert row(pulls, good) == {
            **row(pulls, good),
            "error": "e" * 200,
            "title": "Add the thing",
        }
        gh.put(fails={"pr view": ""})
        await pulls.refresh(tab, [tab.pulls[good]])
        assert row(pulls, good)["error"] == "gh ended with code 1"
        # And it clears once gh answers again.
        gh.put(fails={})
        gh.change(good, title="Renamed")
        await pulls.refresh(tab, [tab.pulls[good]])
        assert (row(pulls, good)["error"], row(pulls, good)["title"]) == (None, "Renamed")

    asyncio.run(scenario())


def test_an_answer_that_is_not_a_pull_request_is_an_error(gh, pulls):
    for printed_text in ("[]", "not json", ""):
        assert pr_tab.answer(pr_tab.Ran(0, printed_text)) is None
    assert pr_tab.answer(pr_tab.Ran(1, '{"url": "x"}')) is None
    assert pr_tab.answer(pr_tab.Ran(0, '{"url": "x"}\n')) == {"url": "x"}
    url = gh.pull(596)
    state = gh.read()
    state["pulls"][url] = {key: [] for key in FIELDS.split(",")}
    gh.write(state)
    # gh answered, with nothing usable: the row is described as far as that goes.
    shown_row = asyncio.run(looked(pulls, url))
    assert (shown_row["error"], shown_row["state"], shown_row["title"]) == (None, "closed", "")


def test_without_gh_the_tab_says_so_until_it_is_found(gh, pulls, tmp_path, monkeypatch):
    url = gh.pull(596)
    found = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path))

    async def scenario():
        missing = await looked(pulls, url)
        assert missing == {
            **waiting(596),
            "loading": False,
            "error": "GitHub CLI (gh) was not found",
        }
        assert pulls.snapshot("chat")["notice"] == "GitHub CLI (gh) was not found"
        assert pulls.snapshot("other-chat") == {
            "pulls": [],
            "notice": "GitHub CLI (gh) was not found",
        }
        monkeypatch.setenv("PATH", found)
        assert (await looked(pulls, url))["title"] == "Add the thing"
        assert pulls.snapshot("chat")["notice"] is None

    asyncio.run(scenario())


async def loaded(pulls, url, session_id="chat"):
    """Wait until a page's watcher has looked a pull request up."""
    await until(lambda: not row(pulls, url, session_id)["loading"])
    return row(pulls, url, session_id)


def test_pull_requests_are_looked_up_only_while_a_page_shows_them(gh, pulls):
    first, second = gh.pull(1), gh.pull(2)

    async def scenario():
        pulls.feed("chat", said(f"Opened {first} today"))
        pulls.feed("idle-chat", said(f"Opened {second} today"))
        await asyncio.sleep(0.3)
        assert gh.calls() == [] and row(pulls, first)["loading"]

        with pulls.listen("chat") as page:
            assert page.get_nowait() is True
            assert (await loaded(pulls, first))["title"] == "Add the thing"
            # The page was told on connecting, and again when the answer came.
            assert page.get_nowait() is True and page.empty()
            # A link that shows up while the page is open is looked up at once.
            before = len(gh.runs("pr", "view"))
            pulls.feed("chat", said(f"and {second} too"))
            assert await page.get() is True
            assert row(pulls, second)["loading"]
            assert (await loaded(pulls, second))["number"] == 2
            assert await page.get() is True
            assert gh.runs("pr", "view")[before:] == [["pr", "view", second, "--json", FIELDS]]
            assert row(pulls, second, "idle-chat")["loading"]
        # The page is gone: nothing more is asked, whatever the chat says.
        await asyncio.sleep(0)
        asked = len(gh.calls())
        pulls.feed("chat", said(f"and {gh.pull(3)} too"))
        await asyncio.sleep(0.3)
        assert len(gh.calls()) == asked and row(pulls, link(3))["loading"]
        assert pulls.tabs["chat"].watcher is None

    asyncio.run(scenario())


def test_an_open_page_sees_github_change(gh, pulls, monkeypatch):
    monkeypatch.setattr(pr_tab, "REFRESH", 0.2)
    # More than are looked up at a time: every one of them is looked up again.
    opened = [gh.pull(number) for number in range(1, 7)]
    url = opened[-1]

    async def scenario():
        pulls.said("chat", " ".join(opened))
        with pulls.listen("chat") as page:
            assert (await loaded(pulls, url))["title"] == "Add the thing"
            gh.change(url, title="Renamed on GitHub", state="MERGED")
            await until(lambda: row(pulls, url)["title"] == "Renamed on GitHub")
            assert row(pulls, url)["state"] == "merged"
            # Every pass tells the page, whether or not anything changed.
            while not page.empty():
                page.get_nowait()
            assert await asyncio.wait_for(page.get(), 5) is True

    asyncio.run(scenario())


def test_only_pull_requests_that_are_not_settled_are_looked_up_again(gh, pulls, monkeypatch):
    monkeypatch.setattr(pr_tab, "REFRESH", 0.2)
    opened, merged, closed = gh.pull(1), gh.pull(2, state="MERGED"), gh.pull(3, state="CLOSED")
    # GitHub does not know this one yet: every look at it fails.
    failing = link(4)

    def looks(url):
        return len([run for run in gh.runs("pr", "view") if run[2] == url])

    async def scenario():
        pulls.said("chat", f"{opened} {merged} {closed} {failing}")
        with pulls.listen("chat"):
            # The first pass looks at every one; the next ones leave the finished ones alone.
            await until(lambda: looks(opened) >= 4 and looks(failing) >= 4)
            assert (looks(merged), looks(closed)) == (1, 1)
            assert row(pulls, failing)["error"] and not row(pulls, failing)["loading"]
            # What GitHub says of a finished one later comes with Refresh only.
            gh.change(merged, title="Renamed after the merge")
            seen = looks(opened)
            await until(lambda: looks(opened) >= seen + 2)
            assert (row(pulls, merged)["title"], looks(merged)) == ("Add the thing", 1)
            assert await ask(pulls, "refresh") == {"ok": True}
            assert row(pulls, merged)["title"] == "Renamed after the merge"
            assert (looks(merged), looks(closed)) == (2, 2)

            # One that gh failed on is tried again until it answers.
            gh.pull(4)
            await until(lambda: row(pulls, failing)["error"] is None)
            assert row(pulls, failing)["state"] == "open"
            # One that gets merged is seen merged, and from then on left alone too.
            gh.change(opened, state="MERGED")
            await until(lambda: row(pulls, opened)["state"] == "merged")
            settled_at, seen = looks(opened), looks(failing)
            await until(lambda: looks(failing) >= seen + 3)
            assert looks(opened) == settled_at
            assert (looks(merged), looks(closed)) == (2, 2)
        # A page that connects looks at every one once more.
        with pulls.listen("chat"):
            every = (merged, closed, opened)
            await until(lambda: [looks(url) for url in every] == [3, 3, settled_at + 1])

    asyncio.run(scenario())


def test_an_open_page_keeps_looking_after_its_second_page_leaves(gh, pulls, monkeypatch):
    monkeypatch.setattr(pr_tab, "REFRESH", 0.2)
    url = gh.pull(596)

    async def scenario():
        pulls.said("chat", url)
        with pulls.listen("chat"):
            with pulls.listen("chat"):
                await loaded(pulls, url)
            asked = len(gh.runs("pr", "view"))
            await until(lambda: len(gh.runs("pr", "view")) >= asked + 2)
        await asyncio.sleep(0)
        asked = len(gh.runs("pr", "view"))
        await asyncio.sleep(0.6)
        assert len(gh.runs("pr", "view")) == asked

    asyncio.run(scenario())


STOPPED = (
    "Stopped checking GitHub: RuntimeError: GitHub answered nonsense. Press Refresh to try again."
)


class Faulty:
    """Looks pull requests up as the tab does, or fails the way a bug would, and counts."""

    def __init__(self, pulls):
        self.broken, self.tries, self.look = True, 0, pulls.look
        pulls.look = self

    async def __call__(self, tab, pull):
        self.tries += 1
        if self.broken:
            raise RuntimeError("GitHub answered nonsense.\nmore detail")
        await self.look(tab, pull)


def test_a_watcher_that_fails_says_so_and_refresh_starts_it_again(gh, pulls):
    url = gh.pull(596)
    faulty = Faulty(pulls)

    async def scenario():
        pulls.said("chat", url)
        with pulls.listen("chat") as page:
            tab = pulls.tabs["chat"]
            assert page.get_nowait() is True
            await until(lambda: tab.watcher is None)
            # The page is told why nothing is looked up any more: it must not look live.
            assert pulls.snapshot("chat") == {"pulls": [waiting(596)], "notice": STOPPED}
            assert page.get_nowait() is True and page.empty()
            assert faulty.tries == 1
            assert pulls.snapshot("other-chat")["notice"] is None

            # Refresh starts a watcher again. While the fault lasts, it says so again.
            assert await ask(pulls, "refresh") == {"ok": True}
            await until(lambda: faulty.tries == 2 and tab.watcher is None)
            assert pulls.snapshot("chat")["notice"] == STOPPED
            while not page.empty():
                page.get_nowait()
            faulty.broken = False
            assert await ask(pulls, "refresh") == {"ok": True}
            assert pulls.snapshot("chat")["notice"] is None
            assert page.get_nowait() is True
            assert (await loaded(pulls, url))["title"] == "Add the thing"
            assert tab.watcher is not None and not tab.watcher.done()
            assert pulls.snapshot("chat")["notice"] is None
            # With a watcher running, Refresh looks them up itself and starts no second one.
            running, asked = tab.watcher, len(gh.runs("pr", "view"))
            assert await ask(pulls, "refresh") == {"ok": True}
            assert tab.watcher is running and len(gh.runs("pr", "view")) == asked + 1
        # A page that leaves ends the watcher without a word.
        await asyncio.sleep(0.05)
        assert running.cancelled() and pulls.snapshot("chat")["notice"] is None

    asyncio.run(scenario())


def test_a_watcher_that_fails_later_keeps_what_was_shown(gh, pulls, monkeypatch):
    monkeypatch.setattr(pr_tab, "REFRESH", 0.2)
    url = gh.pull(596)
    faulty = Faulty(pulls)
    faulty.broken = False

    async def scenario():
        pulls.said("chat", url)
        with pulls.listen("chat") as page:
            tab = pulls.tabs["chat"]
            assert (await loaded(pulls, url))["title"] == "Add the thing"
            tab.notice = "No pull request #77 in my-project"
            faulty.broken = True
            await until(lambda: tab.watcher is None)
            # What stopped comes first, and the pull request stays as it was last seen.
            assert pulls.snapshot("chat")["notice"] == STOPPED
            assert row(pulls, url)["title"] == "Add the thing"
            tries = faulty.tries
            await asyncio.sleep(0.5)
            assert faulty.tries == tries
            while not page.empty():
                page.get_nowait()

            # A page that connects starts a watcher again, and so does /pr.
            faulty.broken = False
            with pulls.listen("chat"):
                await until(lambda: faulty.tries > tries)
                assert tab.watcher is not None and not tab.watcher.done()
                # The earlier notice is back: only what stopped is over.
                assert pulls.snapshot("chat")["notice"] == "No pull request #77 in my-project"
            faulty.broken = True
            await until(lambda: tab.watcher is None)
            faulty.broken, tries = False, faulty.tries
            pulls.show("chat", [], Path.home())
            assert tab.watcher is not None
            await until(lambda: faulty.tries > tries)
            assert pulls.snapshot("chat")["notice"] is None

    asyncio.run(scenario())


def test_pull_requests_are_looked_up_a_few_at_a_time(gh, pulls, tmp_path):
    assert pr_tab.BATCH == 4
    opened = [gh.pull(number) for number in range(1, 7)]
    gate = tmp_path / "answer"
    gh.put(gates={"pr view": str(gate)})

    async def scenario():
        for url in opened:
            pulls.said("chat", url)
        with pulls.listen("chat"):
            await until(lambda: len(gh.runs("pr", "view")) == 4)
            await asyncio.sleep(0.3)
            assert sorted(run[2] for run in gh.runs("pr", "view")) == opened[:4]
            gate.touch()
            await until(lambda: not any(pull["loading"] for pull in shown(pulls)))
            assert sorted(run[2] for run in gh.runs("pr", "view")) == opened

    asyncio.run(scenario())


def test_closing_ends_the_streams_and_the_looking(gh, pulls, tmp_path):
    url = gh.pull(596)
    gh.put(gates={"pr view": str(tmp_path / "never")})

    async def scenario():
        pulls.said("chat", url)
        with pulls.listen("chat") as page:
            await until(lambda: len(gh.calls()) == 1)
            watcher = pulls.tabs["chat"].watcher
            await pulls.close()
            assert watcher.cancelled()
            assert [page.get_nowait(), page.get_nowait()] == [True, None]
            # The gh that was running went with it.
            assert gone(gh.calls()[0]["pid"])

    asyncio.run(scenario())


def described(behind=0, **changes):
    return pr_tab.describe(view(**changes), behind, ["squash", "merge"])


READY = {"merge": True, "update": False, "ready": False, "draft": True, "rerun": False}
NOTHING = dict.fromkeys(READY, False)


@pytest.mark.parametrize("merge_state", ["CLEAN", "UNSTABLE", "HAS_HOOKS"])
def test_a_pull_request_github_can_merge_that_has_all_of_its_base_can_be_merged(merge_state):
    details = described(mergeStateStatus=merge_state)
    assert details["can"] == READY and details["why"] == {}


@pytest.mark.parametrize(
    ("behind", "changes", "why"),
    [
        (0, {"isDraft": True}, "Still a draft"),
        (3, {"isDraft": True, "mergeable": "CONFLICTING"}, "Still a draft"),
        (0, {"mergeable": "CONFLICTING"}, "Has merge conflicts"),
        (3, {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, "Has merge conflicts"),
        (1, {}, "1 commit behind main"),
        (2, {"baseRefName": "release/1.0"}, "2 commits behind release/1.0"),
        (12, {"mergeStateStatus": "BLOCKED"}, "12 commits behind main"),
        (4, {"mergeable": "UNKNOWN"}, "4 commits behind main"),
        (
            0,
            {"mergeStateStatus": "BLOCKED"},
            "Blocked by branch rules (reviews or required checks)",
        ),
        (
            None,
            {"mergeStateStatus": "BLOCKED", "mergeable": "UNKNOWN"},
            "Blocked by branch rules (reviews or required checks)",
        ),
        (0, {"mergeable": "UNKNOWN"}, "GitHub is still checking if it can merge"),
        (0, {"mergeStateStatus": "UNKNOWN"}, "GitHub is still checking if it can merge"),
        (None, {"mergeStateStatus": "UNKNOWN"}, "GitHub is still checking if it can merge"),
        (None, {"mergeable": "UNKNOWN"}, "GitHub is still checking if it can merge"),
        (None, {}, "Can't tell if it is behind main"),
        (None, {"mergeStateStatus": "DIRTY"}, "Can't tell if it is behind main"),
        (0, {"mergeStateStatus": "DIRTY"}, "GitHub says it can't merge yet"),
        (0, {"mergeStateStatus": "BEHIND"}, "GitHub says it can't merge yet"),
        (0, {"mergeStateStatus": "DRAFT"}, "GitHub says it can't merge yet"),
    ],
)
def test_an_open_pull_request_that_cannot_be_merged_says_the_first_reason(behind, changes, why):
    details = described(behind, **changes)
    assert details["can"]["merge"] is False
    assert details["why"] == {"merge": why}


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_nothing_can_be_done_to_a_pull_request_that_is_not_open(state):
    failing = [check_run("unit", "FAILURE")]
    for draft, behind in ((False, None), (True, None), (False, 2), (True, 0)):
        details = described(behind, state=state, isDraft=draft, statusCheckRollup=failing)
        assert details["can"] == NOTHING and details["why"] == {}


@pytest.mark.parametrize(
    ("behind", "changes", "can"),
    [
        (0, {}, READY),
        (2, {}, {**READY, "merge": False, "update": True}),
        (None, {}, {**READY, "merge": False}),
        (0, {"isDraft": True}, {**NOTHING, "ready": True}),
        (2, {"isDraft": True}, {**NOTHING, "ready": True, "update": True}),
        (1, {"mergeable": "CONFLICTING"}, {**NOTHING, "draft": True, "update": True}),
        (0, {"statusCheckRollup": [check_run("unit", "FAILURE")]}, {**READY, "rerun": True}),
        (0, {"statusCheckRollup": [check_run("unit", "CANCELLED")]}, {**READY, "rerun": True}),
        # Only a failed check of a GitHub Actions run can be run again.
        (0, {"statusCheckRollup": [commit_status("ci/build", "FAILURE")]}, READY),
        (0, {"statusCheckRollup": [check_run("unit", "SKIPPED")]}, READY),
        (0, {"statusCheckRollup": [check_run("unit", "", "IN_PROGRESS")]}, READY),
        (0, {"statusCheckRollup": [{"name": "x", "status": "COMPLETED"}]}, READY),
        (
            0,
            {
                "statusCheckRollup": [
                    {"name": "x", "status": "COMPLETED", "detailsUrl": "/actions/runs/"}
                ]
            },
            READY,
        ),
    ],
)
def test_what_can_be_done_to_an_open_pull_request(behind, changes, can):
    assert described(behind, **changes)["can"] == can


def ask(pulls, action, url=None, session_id="chat", **more):
    return pulls.act({"session": session_id, "url": url, "action": action, **more})


def acted(gh):
    """What gh was run for, besides looking pull requests up."""
    lookups = (["pr", "view"], ["repo", "view"])
    return [run for run in gh.runs() if run[:2] not in lookups and run[0] != "api"]


@pytest.mark.parametrize("method", ["squash", "merge"])
def test_merge_names_the_commit_after_the_pull_request_and_pins_what_was_shown(gh, pulls, method):
    title = "--admin: fix the “thing” & more"
    url = gh.pull(596, title=title)

    async def scenario():
        assert (await looked(pulls, url))["can"]["merge"] is True
        asked = len(gh.calls())
        assert await ask(pulls, "merge", url, method=method) == {"ok": True}
        # No branch is deleted, nothing waits for checks, no branch rule is overridden.
        assert acted(gh) == [
            ["pr", "merge", url, f"--{method}", "--subject", title, "--match-head-commit", OID]
        ]
        # The page then shows it as GitHub has it now.
        assert gh.runs()[asked:][1] == ["pr", "view", url, "--json", FIELDS]
        assert (row(pulls, url)["state"], row(pulls, url)["busy"]) == ("merged", None)
        assert row(pulls, url)["can"] == NOTHING
        assert await ask(pulls, "merge", url, method=method) == {
            "error": "This pull request is merged"
        }
        assert len(acted(gh)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["rebase", "admin", "--admin", "", None, ["squash"], 5])
def test_a_merge_method_the_repository_does_not_allow_is_refused(gh, pulls, method):
    url = gh.pull(596)

    async def scenario():
        assert (await looked(pulls, url))["methods"] == ["squash", "merge"]
        refused = {"error": "This repository does not allow that merge method"}
        assert await ask(pulls, "merge", url, method=method) == refused
        assert await ask(pulls, "merge", url) == refused
        gh.put(fails={"repo view": "GraphQL: timeout"})
        pulls.methods.clear()
        assert (await looked(pulls, url))["methods"] == []
        assert await ask(pulls, "merge", url, method="squash") == {
            "error": "Can't tell which merge methods this repository allows"
        }
        assert acted(gh) == [] and row(pulls, url)["state"] == "open"

    asyncio.run(scenario())


def test_update_brings_the_branch_up_to_date_and_then_it_can_be_merged(gh, pulls):
    url = gh.pull(596, behind=2)

    async def scenario():
        before = await looked(pulls, url)
        assert (before["can"]["merge"], before["can"]["update"]) == (False, True)
        assert await ask(pulls, "merge", url, method="squash") == {"error": "2 commits behind main"}
        assert acted(gh) == []
        assert await ask(pulls, "update", url) == {"ok": True}
        assert acted(gh) == [["pr", "update-branch", url]]
        after = row(pulls, url)
        assert (after["behind"], after["can"]["merge"], after["can"]["update"]) == (0, True, False)
        assert after["why"] == {}
        assert await ask(pulls, "update", url) == {"error": "Already up to date with main"}
        # The update made a new commit: that one is what a merge now pins.
        assert await ask(pulls, "merge", url, method="merge") == {"ok": True}
        assert acted(gh)[1:] == [
            ["pr", "merge", url, "--merge", "--subject", "Add the thing"]
            + ["--match-head-commit", "0" * 40]
        ]

    asyncio.run(scenario())


def test_ready_and_draft_switch_a_pull_request_between_the_two(gh, pulls):
    url = gh.pull(596, isDraft=True)

    async def scenario():
        assert (await looked(pulls, url))["why"] == {"merge": "Still a draft"}
        assert await ask(pulls, "draft", url) == {"error": "Already a draft"}
        assert await ask(pulls, "merge", url, method="squash") == {"error": "Still a draft"}
        assert await ask(pulls, "ready", url) == {"ok": True}
        assert acted(gh) == [["pr", "ready", url]]
        assert (row(pulls, url)["draft"], row(pulls, url)["can"]["merge"]) == (False, True)
        assert await ask(pulls, "ready", url) == {"error": "Already ready for review"}
        assert await ask(pulls, "draft", url) == {"ok": True}
        assert acted(gh) == [["pr", "ready", url], ["pr", "ready", url, "--undo"]]
        assert row(pulls, url)["draft"] is True and row(pulls, url)["can"]["ready"] is True

    asyncio.run(scenario())


def test_rerun_restarts_each_failed_actions_run_once(gh, pulls):
    rollup = [
        check_run("unit", "FAILURE", run=7001),
        check_run("lint", "TIMED_OUT", run=7001),
        check_run("build", "SUCCESS", run=7003),
        commit_status("ci/external", "FAILURE"),
        check_run("e2e", "CANCELLED", run=7002),
        check_run("docs", "SKIPPED", run=7004),
    ]
    url = gh.pull(596, statusCheckRollup=rollup)
    quiet = gh.pull(597)

    async def scenario():
        assert (await looked(pulls, url))["can"]["rerun"] is True
        assert (await looked(pulls, quiet))["can"]["rerun"] is False
        assert await ask(pulls, "rerun", quiet) == {
            "error": "No failed GitHub Actions run to re-run"
        }
        assert acted(gh) == []
        assert await ask(pulls, "rerun", url) == {"ok": True}
        assert acted(gh) == [
            ["run", "rerun", "7001", "--failed", "-R", "acme/app"],
            ["run", "rerun", "7002", "--failed", "-R", "acme/app"],
        ]
        statuses = {item["name"]: item["status"] for item in row(pulls, url)["checks"]}
        assert statuses == {
            "unit": "pending",
            "lint": "pending",
            "build": "pass",
            "ci/external": "fail",
            "e2e": "pending",
            "docs": "skipped",
        }
        assert row(pulls, url)["can"]["rerun"] is False

    asyncio.run(scenario())


def test_a_run_that_cannot_be_rerun_does_not_stop_the_next(gh, pulls):
    rollup = [check_run("unit", "FAILURE", run=7001), check_run("e2e", "FAILURE", run=7002)]
    url = gh.pull(596, statusCheckRollup=rollup)
    gh.put(fails={"run rerun": "run 7001 cannot be rerun; its workflow file may be broken\nhint"})

    async def scenario():
        await looked(pulls, url)
        assert await ask(pulls, "rerun", url) == {
            "error": "run 7001 cannot be rerun; its workflow file may be broken"
        }
        assert [run[2] for run in acted(gh)] == ["7001", "7002"]
        assert row(pulls, url)["busy"] is None

    asyncio.run(scenario())


def test_what_gh_says_when_an_action_fails_comes_back_and_the_row_is_looked_up_again(gh, pulls):
    url = gh.pull(596)
    gh.put(fails={"pr merge": "GraphQL: Merging is blocked (mergePullRequest)\nmore text"})

    async def scenario():
        await looked(pulls, url)
        asked = len(gh.calls())
        gh.change(url, mergeStateStatus="BLOCKED")
        assert await ask(pulls, "merge", url, method="squash") == {
            "error": "GraphQL: Merging is blocked (mergePullRequest)"
        }
        assert [run[:2] for run in gh.runs()[asked:]][:2] == [["pr", "merge"], ["pr", "view"]]
        shown_row = row(pulls, url)
        assert (shown_row["state"], shown_row["busy"], shown_row["error"]) == ("open", None, None)
        assert shown_row["why"] == {"merge": "Blocked by branch rules (reviews or required checks)"}

    asyncio.run(scenario())


def test_an_action_is_refused_for_a_pull_request_that_is_not_known_well_enough(gh, pulls):
    url, closed = gh.pull(596, behind=None), gh.pull(7, state="CLOSED")
    gh.put(behind={})

    async def scenario():
        pulls.said("chat", f"{url} {closed} {link(8)}")
        loading = {"error": "This pull request is still loading"}
        for action in pr_tab.ACTIONS:
            assert await ask(pulls, action, url, method="squash") == loading
        tab = pulls.tabs["chat"]
        await pulls.refresh(tab, list(tab.pulls.values()))
        assert await ask(pulls, "update", url) == {"error": "Can't tell if it is behind main"}
        assert await ask(pulls, "merge", url, method="squash") == {
            "error": "Can't tell if it is behind main"
        }
        for action in pr_tab.ACTIONS:
            assert await ask(pulls, action, closed, method="squash") == {
                "error": "This pull request is closed"
            }
            # One gh could not look up says what gh said.
            assert await ask(pulls, action, link(8), method="squash") == {
                "error": "GraphQL: Could not resolve to a PullRequest with that number."
            }
        assert await ask(pulls, "close", url) == {"error": "Unknown action"}
        assert await ask(pulls, "", url) == {"error": "Unknown action"}
        assert acted(gh) == []

    asyncio.run(scenario())


def test_an_action_needs_a_pull_request_of_its_own_chat(gh, pulls):
    url, theirs = gh.pull(596, isDraft=True), gh.pull(7, isDraft=True)
    opened = []
    pulls.browse = opened.append

    async def scenario():
        await looked(pulls, url)
        await looked(pulls, theirs, "other-chat")
        stranger = {"error": "That pull request is not in this chat's tab"}
        for action in (*pr_tab.ACTIONS, "open", "dismiss", "close"):
            for target in (theirs, link(99), None, "", 5, ["x"]):
                assert await ask(pulls, action, target, method="squash") == stranger
        assert acted(gh) == [] and opened == []
        assert urls(pulls, "other-chat") == [theirs]
        # A link is the same pull request however its owner and repository are capitalised.
        assert await ask(pulls, "ready", url.replace("acme/app", "ACME/App")) == {"ok": True}
        assert acted(gh) == [["pr", "ready", url]]

        # A chat without a tab has nothing to act on, and a request must say what it wants.
        assert await ask(pulls, "ready", theirs, "no-such-chat") is None
        assert await ask(pulls, "refresh", session_id="no-such-chat") is None
        for unclear in ({"session": 5, "action": "refresh"}, {"session": "chat"}, {}):
            with pytest.raises(ValueError):
                await pulls.act(unclear)
        with pytest.raises(ValueError):
            await pulls.act({"session": "chat", "action": ["refresh"]})

    asyncio.run(scenario())


def test_a_running_action_is_shown_and_holds_the_others_off(gh, pulls, tmp_path, unwatched):
    url, other = gh.pull(596, behind=2), gh.pull(7)
    gate = tmp_path / "merged"
    gh.put(gates={"pr update-branch": str(gate)})
    opened = []
    pulls.browse = lambda target: opened.append(target) or True

    async def scenario():
        await looked(pulls, url)
        await looked(pulls, other)
        with pulls.listen("chat") as page:
            assert page.get_nowait() is True and page.empty()
            running = asyncio.create_task(ask(pulls, "update", url))
            await until(lambda: len(acted(gh)) == 1)
            # The page was told at once, before gh answered.
            assert page.get_nowait() is True and page.empty()
            assert (row(pulls, url)["busy"], row(pulls, other)["busy"]) == ("update", None)
            busy = {"error": "Another action is still running on this pull request"}
            for action in pr_tab.ACTIONS:
                assert await ask(pulls, action, url, method="squash") == busy
            assert acted(gh) == [["pr", "update-branch", url]]
            # Looking, opening and another pull request go on meanwhile.
            assert await ask(pulls, "refresh") == {"ok": True}
            assert await ask(pulls, "open", url) == {"ok": True}
            assert await ask(pulls, "merge", other, method="squash") == {"ok": True}
            assert row(pulls, url)["busy"] == "update" and not running.done()
            told = []

            async def read():
                while await page.get():
                    told.append((row(pulls, url)["busy"], row(pulls, url)["behind"]))

            reading = asyncio.create_task(read())
            gate.touch()
            assert await running == {"ok": True}
            after = row(pulls, url)
            assert (after["busy"], after["behind"], after["can"]["merge"]) == (None, 0, True)
            # It stays busy until GitHub's new state is in: no page is told the old one, idle.
            await until(lambda: told[-1:] == [(None, 0)])
            assert (None, 2) not in told
            reading.cancel()
        assert opened == [url]

    asyncio.run(scenario())


def test_an_action_that_is_given_up_leaves_the_pull_request_free(gh, pulls, tmp_path, unwatched):
    url = gh.pull(596, isDraft=True)
    gh.put(gates={"pr ready": str(tmp_path / "never")})

    async def scenario():
        await looked(pulls, url)
        with pulls.listen("chat") as page:
            running = asyncio.create_task(ask(pulls, "ready", url))
            await until(lambda: len(acted(gh)) == 1)
            assert row(pulls, url)["busy"] == "ready"
            while not page.empty():
                page.get_nowait()
            # The bridge closing, say: the row must not stay busy for good.
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert row(pulls, url)["busy"] is None
            assert page.get_nowait() is True
            (ready,) = [call for call in gh.calls() if call["args"][:2] == ["pr", "ready"]]
            assert gone(ready["pid"])

    asyncio.run(scenario())


def test_open_shows_a_pull_request_or_one_of_its_checks_in_the_browser(gh, store, monkeypatch):
    odd = commit_status("local", "FAILURE") | {"targetUrl": "file:///etc/passwd"}
    rollup = [check_run("unit", run=8001), commit_status("ci/x", "PENDING"), odd]
    url, other = gh.pull(596, statusCheckRollup=rollup), gh.pull(7)
    opened, works = [], True

    def browse(target):
        opened.append(target)
        return works

    # The system's own opener, unless another is given.
    monkeypatch.setattr(pr_tab.webbrowser, "open", browse)
    pulls = pr_tab.Pulls(store)
    given = pr_tab.Pulls(store, opened.append)
    assert given.browse == opened.append and pulls.browse is browse

    async def scenario():
        nonlocal works
        pulls.said("chat", f"{url} {other}")
        # Its own link opens before anything is known about it.
        assert await ask(pulls, "open", url) == {"ok": True}
        tab = pulls.tabs["chat"]
        await pulls.refresh(tab, list(tab.pulls.values()))
        job = "https://github.com/acme/app/actions/runs/8001/job/80019"
        for target in (job, "https://ci.example/ci/x", url, None, ""):
            assert await ask(pulls, "open", url, target=target) == {"ok": True}
        assert opened == [url, job, "https://ci.example/ci/x", url, url, url]
        refused = {"error": "That link is not this pull request's"}
        # Not another pull request's link or check, and nothing but a web page.
        strange = (other, "https://evil.example/", f"{url}/files", "file:///etc/passwd", 5, [url])
        for target in strange:
            assert await ask(pulls, "open", url, target=target) == refused
        assert await ask(pulls, "open", other, target=job) == refused
        assert len(opened) == 6
        works = False
        assert await ask(pulls, "open", url) == {"error": "The browser could not be opened"}
        assert acted(gh) == []

    asyncio.run(scenario())


def test_a_dismissed_pull_request_stays_away_until_the_user_asks_for_it(
    gh, pulls, tmp_path, unwatched
):
    url, kept = gh.pull(596), gh.pull(7)

    async def scenario():
        await looked(pulls, url)
        await looked(pulls, kept)
        await looked(pulls, url, "other-chat")
        with pulls.listen("chat") as page:
            assert page.get_nowait() is True and page.empty()
            assert await ask(pulls, "dismiss", url) == {"ok": True}
            assert page.get_nowait() is True and page.empty()
        assert urls(pulls) == [kept] and urls(pulls, "other-chat") == [url]
        assert await ask(pulls, "dismiss", url) == {
            "error": "That pull request is not in this chat's tab"
        }
        # The chat saying the link again, in any way it is read, does not bring it back.
        pulls.feed("chat", said(f"As said, {url} and https://github.com/ACME/app/pull/596 ."))
        pulls.feed("chat", said(f"see {url} ", kind=USER))
        pulls.said("chat", f"what about {url}")
        pulls.feed("chat", step("c1", "tool_call", title="gh pr view", status="completed"))
        pulls.feed("chat", step("c1", status="completed", content=printed(f"{url}\n")))
        pulls.flush("chat")
        assert urls(pulls) == [kept]
        # Asking for it does, as new as any other.
        pulls.show("chat", [url], tmp_path)
        assert urls(pulls) == [kept, url]
        assert row(pulls, url) == waiting(596)
        # And it is then an ordinary one again: pushed out by newer ones, a link brings it back.
        pulls.said("chat", " ".join(link(number) for number in range(100, 120)))
        assert url not in urls(pulls)
        pulls.feed("chat", said(f"once more {url} ", stream="m7"))
        assert urls(pulls)[-1] == url
        assert await ask(pulls, "dismiss", url) == {"ok": True}
        pulls.feed("chat", said(f"and again {url} ", stream="m8"))
        assert url not in urls(pulls)
        assert acted(gh) == []

    asyncio.run(scenario())


def test_refresh_looks_every_pull_request_of_the_chat_up_now(gh, pulls):
    first, second = gh.pull(1), gh.pull(2)
    elsewhere = gh.pull(3)

    async def scenario():
        pulls.said("chat", f"{first} {second}")
        pulls.said("other-chat", elsewhere)
        assert await ask(pulls, "refresh") == {"ok": True}
        assert [pull["loading"] for pull in shown(pulls)] == [False, False]
        assert [pull["title"] for pull in shown(pulls)] == ["Add the thing"] * 2
        assert row(pulls, elsewhere, "other-chat")["loading"]
        assert sorted(run[2] for run in gh.runs("pr", "view")) == [first, second]
        gh.change(second, title="Renamed")
        # The link is not needed, and one given is not looked at.
        assert await ask(pulls, "refresh", link(99)) == {"ok": True}
        assert [pull["title"] for pull in shown(pulls)] == ["Add the thing", "Renamed"]
        assert len(gh.runs("pr", "view")) == 4

    asyncio.run(scenario())


async def settled(pulls):
    """Wait for what a `/pr` left gh to find."""
    await until(lambda: not pulls.tasks)


def test_pr_adds_the_links_and_numbers_it_is_given(gh, pulls, tmp_path):
    folder = tmp_path / "my-project"
    folder.mkdir()
    by_link, by_number, hashed = gh.pull(596), gh.pull(12), gh.pull(13)
    gh.put(numbers={"12": by_number, "13": hashed})

    async def scenario():
        pulls.show("chat", [by_link, "12", "#013", f"<{link(7)}/files>,"], folder)
        # Links are there at once; numbers once gh has found them in the chat's folder.
        assert urls(pulls) == [by_link, link(7)]
        await settled(pulls)
        assert urls(pulls) == [by_link, link(7), by_number, hashed]
        assert gh.runs() == [
            ["pr", "view", "12", "--json", "url"],
            ["pr", "view", "13", "--json", "url"],
        ]
        assert {Path(call["cwd"]) for call in gh.calls()} == {folder.resolve()}
        assert pulls.snapshot("chat")["notice"] is None
        # Nothing new: nothing changes, and gh is asked about a number only.
        pulls.show("chat", [by_link], folder)
        await settled(pulls)
        assert len(gh.calls()) == 2 and len(urls(pulls)) == 4

    asyncio.run(scenario())


def test_pr_says_which_number_or_word_it_could_not_use(gh, pulls, tmp_path, unwatched):
    folder = tmp_path / "my-project"
    folder.mkdir()
    found = gh.pull(12)
    gh.put(numbers={"12": found})

    async def scenario():
        with pulls.listen("chat") as page:
            pulls.show("chat", ["77"], folder)
            assert page.get_nowait() is True and page.empty()
            await settled(pulls)
            # The open page is told what gh could not find.
            assert page.get_nowait() is True and page.empty()
            assert pulls.snapshot("chat") == {
                "pulls": [],
                "notice": "No pull request #77 in my-project",
            }
            # A refresh clears it, and says so even with nothing to look up.
            assert await ask(pulls, "refresh") == {"ok": True}
            assert page.get_nowait() is True and page.empty()
            assert pulls.snapshot("chat") == {"pulls": [], "notice": None}
            pulls.show("chat", ["77", "12"], folder)
            await settled(pulls)
            assert pulls.snapshot("chat")["notice"] == "No pull request #77 in my-project"
            assert urls(pulls) == [found]
            # The next /pr starts clean, and so does a refresh.
            pulls.show("chat", [], folder)
            assert pulls.snapshot("chat")["notice"] is None
            pulls.show("chat", ["soon", "#12"], folder)
            assert pulls.snapshot("chat")["notice"] == "Not a pull request link or number: soon"
            assert await ask(pulls, "refresh") == {"ok": True}
            assert pulls.snapshot("chat")["notice"] is None
            pulls.show("chat", ["0", "12abc", "https://github.com/acme/app/issues/5"], folder)
            await settled(pulls)
            assert pulls.snapshot("chat")["notice"] == (
                "Not a pull request link or number: https://github.com/acme/app/issues/5"
            )
        numbers = [run[2] for run in gh.runs("pr", "view") if run[2].isdigit()]
        assert numbers == ["77", "77", "12", "12"]

    asyncio.run(scenario())


def test_pr_alone_finds_the_pull_request_of_the_folders_branch_for_an_empty_tab(
    gh, pulls, tmp_path
):
    folder, bare = tmp_path / "my-project", tmp_path / "no-pull-request"
    folder.mkdir()
    bare.mkdir()
    mine = gh.pull(596)
    gh.put(branches={"my-project": mine})

    async def scenario():
        # A folder whose branch has none says so, in gh's words.
        pulls.show("quiet-chat", [], bare)
        await settled(pulls)
        assert pulls.snapshot("quiet-chat") == {
            "pulls": [],
            "notice": "No pull request found for no-pull-request: no pull requests found",
        }
        assert gh.runs() == [["pr", "view", "--json", "url"]]
        assert Path(gh.calls()[0]["cwd"]) == bare.resolve()

        pulls.show("chat", [], folder)
        await settled(pulls)
        assert urls(pulls) == [mine]
        assert Path(gh.calls()[1]["cwd"]) == folder.resolve()
        # A tab that has pull requests is shown as it is.
        pulls.show("chat", [], folder)
        await settled(pulls)
        assert len(gh.calls()) == 2
        # Dismissed and asked for again with an empty tab, it is back.
        assert await ask(pulls, "dismiss", mine) == {"ok": True}
        pulls.show("chat", [], folder)
        await settled(pulls)
        assert urls(pulls) == [mine] and len(gh.calls()) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("written_by_gh", "why"),
    [
        (
            "failed to run git: fatal: not a git repository (or any of the parent directories)\n",
            "failed to run git: fatal: not a git repository (or any of the parent directories)",
        ),
        (
            "To get started with GitHub CLI, please run:  gh auth login\nOr set GH_TOKEN",
            "To get started with GitHub CLI, please run:  gh auth login",
        ),
        ('no pull requests found for branch "main"', 'no pull requests found for branch "main"'),
        ("  \n" + "e" * 300, "e" * 200),
        ("", "gh ended with code 1"),
    ],
)
def test_pr_alone_says_why_gh_found_no_pull_request_for_the_folder(
    gh, pulls, tmp_path, unwatched, written_by_gh, why
):
    folder = tmp_path / "my-project"
    folder.mkdir()
    gh.put(fails={"pr view": written_by_gh})

    async def scenario():
        with pulls.listen("chat") as page:
            pulls.show("chat", [], folder)
            assert page.get_nowait() is True and page.empty()
            await settled(pulls)
            assert pulls.snapshot("chat") == {
                "pulls": [],
                "notice": f"No pull request found for my-project: {why}",
            }
            # The open page is told.
            assert page.get_nowait() is True and page.empty()
            assert gh.runs() == [["pr", "view", "--json", "url"]]
            # A number that gh does not find keeps its own words.
            pulls.show("chat", ["12"], folder)
            await settled(pulls)
            assert pulls.snapshot("chat")["notice"] == "No pull request #12 in my-project"

    asyncio.run(scenario())


def test_pr_alone_says_when_gh_answers_with_no_pull_request(gh, pulls, tmp_path):
    folder = tmp_path / "my-project"
    folder.mkdir()
    odd = gh.pull(596, url="https://github.com/acme/app/issues/596")
    gh.put(branches={"my-project": odd})

    async def scenario():
        pulls.show("chat", [], folder)
        await settled(pulls)
        assert pulls.snapshot("chat") == {
            "pulls": [],
            "notice": "No pull request found for my-project: "
            "gh printed something that is not a pull request",
        }

    asyncio.run(scenario())


def test_pr_leaves_a_missing_gh_its_own_notice(gh, pulls, tmp_path, monkeypatch, unwatched):
    folder = tmp_path / "my-project"
    folder.mkdir()
    url = gh.pull(596)
    found = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path))

    async def scenario():
        with pulls.listen("chat") as page:
            for words in ([], ["12"]):
                while not page.empty():
                    page.get_nowait()
                pulls.show("chat", words, folder)
                assert page.get_nowait() is True and page.empty()
                await settled(pulls)
                assert pulls.snapshot("chat")["notice"] == "GitHub CLI (gh) was not found"
                # The open page is told that gh is missing.
                assert page.get_nowait() is True and page.empty()
            # Once gh is found, nothing is left that blames the folder or the number.
            monkeypatch.setenv("PATH", found)
            pulls.said("other-chat", url)
            assert (await looked(pulls, url, "other-chat"))["title"] == "Add the thing"
            assert pulls.snapshot("chat") == {"pulls": [], "notice": None}

    asyncio.run(scenario())


def test_pr_counts_the_link_a_message_was_ending_with(gh, pulls, tmp_path):
    async def scenario():
        pulls.feed("chat", step("c1", "tool_call", title="gh pr create", rawOutput=link(42)))
        pulls.feed("chat", said(f"Opened {link(41)}"))
        assert urls(pulls) == []
        pulls.show("chat", [], tmp_path)
        assert urls(pulls) == [link(41), link(42)]
        await settled(pulls)
        # The tab was not empty, so the folder's branch was not asked for.
        assert gh.calls() == []

    asyncio.run(scenario())


def test_forgetting_a_chat_drops_its_tab_and_ends_its_pages(gh, pulls, tmp_path):
    url, kept = gh.pull(596), gh.pull(7)
    gh.put(gates={"pr view": str(tmp_path / "never")})

    async def scenario():
        pulls.said("chat", f"{url} {kept}")
        pulls.said("other-chat", kept)
        pulls.tabs["chat"].notice = "No pull request #77 in my-project"
        assert await ask(pulls, "dismiss", kept) == {"ok": True}
        pulls.feed("chat", said(f"It ends with {link(41)}"))
        with pulls.listen("chat") as page, pulls.listen("chat") as second:
            with pulls.listen("other-chat") as other:
                await until(lambda: len(gh.calls()) == 2)
                watcher, theirs = pulls.tabs["chat"].watcher, pulls.tabs["other-chat"].watcher
                for queue in (page, second, other):
                    assert queue.get_nowait() is True and queue.empty()
                pulls.forget("never-seen")
                pulls.forget("chat")
                # Nothing of the chat is kept, and its pages' streams end.
                assert list(pulls.tabs) == ["other-chat"]
                assert pulls.snapshot("chat") == {"pulls": [], "notice": None}
                assert not pulls.showing("chat")
                assert page.get_nowait() is None and second.get_nowait() is None
                # Nothing looks its pull requests up any more: the gh that was running is gone.
                with pytest.raises(asyncio.CancelledError):
                    await watcher
                mine = [call for call in gh.calls() if call["args"][2] == url]
                assert [gone(call["pid"]) for call in mine] == [True]
                # Another chat's tab, its page and its watcher go on.
                assert other.empty() and pulls.showing("other-chat") and not theirs.done()
                assert urls(pulls, "other-chat") == [kept]
        # What the chat says later starts a new tab: what it dismissed was not kept either.
        pulls.feed("chat", said(f"Again {kept} and more"))
        assert urls(pulls) == [kept]
        await pulls.close()

    asyncio.run(scenario())


def test_closing_a_chat_forgets_its_pull_requests(gh, acp_native, tmp_path):
    url = gh.pull(596)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            chat, other = await new_chat(bridge, tmp_path), await new_chat(bridge, tmp_path)
            pulls, server = bridge.pulls, bridge.side_server
            pulls.said(chat.session_id, url)
            pulls.said(other.session_id, url)
            address = events_url(server, chat.session_id)
            status, reader, writer = await request(server.port, "GET", address)
            assert status == 200
            await streamed(reader, lambda shown: not shown["pulls"][0]["loading"])
            await bridge.dispatch("session/close", {"sessionId": chat.session_id})
            assert chat.session_id not in pulls.tabs
            assert urls(pulls, chat.session_id) == []
            # The page that was open is told nothing more: its stream ends.
            assert b"data:" not in await asyncio.wait_for(reader.read(), 5)
            assert not pulls.showing(chat.session_id)
            assert urls(pulls, other.session_id) == [url]
            writer.close()

    asyncio.run(scenario())


def test_closing_stops_what_a_pr_command_left_running(gh, pulls, tmp_path):
    gh.put(gates={"pr view": str(tmp_path / "never")})

    async def scenario():
        pulls.show("chat", ["12"], tmp_path)
        await until(lambda: len(gh.calls()) == 1)
        (finding,) = pulls.tasks
        await pulls.close()
        assert finding.cancelled() and gone(gh.calls()[0]["pid"])

    asyncio.run(scenario())


def test_the_page_shell_names_its_chat_token_and_files():
    shell = pr_tab.page('chat-1<"&>', 'tok<en>&"x')
    token = "tok&lt;en&gt;&amp;&quot;x"
    assert "<title>Pull requests</title>" in shell
    assert f'<body data-session="chat-1&lt;&quot;&amp;&gt;" data-token="{token}">' in shell
    assert re.findall(r'<link rel="stylesheet" href="([^"]*)">', shell) == [
        f"/pr/assets/pr.css?t={token}"
    ]
    # The view comes first: the page's own script draws with it.
    assert re.findall(r'<script src="([^"]*)"></script>', shell) == [
        f"/pr/assets/pr-view.js?t={token}",
        f"/pr/assets/pr.js?t={token}",
    ]
    assert shell.count("<script") == 2 and shell.endswith("</script></body></html>")
    assert shell.startswith("<!doctype html>")
    assert 'tok<en>&"x' not in shell and 'chat-1<"&>' not in shell


def events_url(server, session_id):
    return f"/pr/events?session={quote(session_id, safe='')}&t={server.token}"


async def post(server, body, token=None, host=None):
    """Status and answer of an action a page asks for."""
    target = f"/pr/act?t={server.token if token is None else token}"
    status, answer = await fetch(server.port, "POST", target, body, host)
    return status, json.loads(answer) if answer.startswith("{") else answer


def test_the_pull_requests_page_needs_its_token_and_a_loopback_host(
    gh, acp_native, tmp_path, monkeypatch
):
    # The real page files are not this test's business: serve stand-ins.
    folder = tmp_path / "page"
    folder.mkdir()
    kinds = {"pr.css": "text/css", "pr-view.js": "text/javascript", "pr.js": "text/javascript"}
    for name in (*kinds, "notes.js"):
        (folder / name).write_text(f"/* stand-in {name} */")
    (tmp_path / "outside.js").write_text("/* outside */")
    monkeypatch.setattr(pr_tab, "PAGE", folder)
    url = gh.pull(596, isDraft=True)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            server = bridge.side_server
            port, token = server.port, server.token
            page = server.path('chat-1<"&>', "pr")
            assert page == f"/pr/chat-1%3C%22%26%3E?t={token}"
            bridge.pulls.said("c", url)
            ready = {"session": "c", "url": url, "action": "ready"}
            for route in (page, f"/pr/assets/pr.js?t={token}", events_url(server, "c")):
                assert (await get(port, route.replace(token, "wrong")))[0] == 403
                assert (await get(port, route.replace(f"t={token}", "")))[0] == 403
                assert (await get(port, route, host="evil.example"))[0] == 403
            assert (await post(server, ready, token="wrong"))[0] == 403
            assert (await post(server, ready, token=""))[0] == 403
            assert (await post(server, ready, host="evil.example"))[0] == 403
            assert (await fetch(port, "POST", "/pr/act", ready))[0] == 403
            # Nothing was connected, looked up or done for any of those.
            assert not bridge.pulls.showing("c") and gh.calls() == []

            for host in (None, f"localhost:{port}"):
                status, headers, shell = await get(port, page, host)
                assert status == 200 and headers["Content-Type"].startswith("text/html")
                assert shell == pr_tab.page('chat-1<"&>', token)
                # Every file the shell names is one this server serves.
                files = re.findall(r'(?:src|href)="([^"]*)"', shell)
                assert len(files) == 3
                for source in files:
                    assert (await get(port, source, host))[0] == 200
            for name, kind in kinds.items():
                status, headers, body = await get(port, f"/pr/assets/{name}?t={token}")
                assert (status, body) == (200, f"/* stand-in {name} */")
                assert headers["Content-Type"] == f"{kind}; charset=utf-8"
            # Only the page's three files, whatever else its folder (or another page) holds.
            for name in ("notes.js", "../outside.js", "side.js", "subagents.js", "", "pr.js/x"):
                assert (await get(port, f"/pr/assets/{name}?t={token}"))[0] == 404
            assert (await get(port, f"/side/assets/pr.js?t={token}"))[0] == 404
            assert (await get(port, f"/subagents/assets/pr.js?t={token}"))[0] == 404
            assert (await fetch(port, "POST", page, ready))[0] == 404

    asyncio.run(scenario())


def test_the_pull_requests_page_streams_a_chats_pull_requests_as_they_change(gh, acp_native):
    first, second = gh.pull(596), gh.pull(7, behind=1)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            pulls, server = bridge.pulls, bridge.side_server
            pulls.said("chat", first)
            assert not pulls.showing("chat")
            status, reader, writer = await request(server.port, "GET", events_url(server, "chat"))
            assert status == 200
            _, idle, other = await request(server.port, "GET", events_url(server, "another-chat"))
            # The pull requests as they are right after connecting, then as gh describes them.
            (connected,) = await lines(reader, 1)
            assert connected == {"pulls": [waiting(596)], "notice": None}
            assert await lines(idle, 1) == [{"pulls": [], "notice": None}]
            assert pulls.showing("chat")
            (looked_up,) = await lines(reader, 1)
            assert looked_up == pulls.snapshot("chat")
            (shown_row,) = looked_up["pulls"]
            assert (shown_row["loading"], shown_row["title"]) == (False, "Add the thing")
            assert shown_row["can"]["merge"] is True

            # A link the chat says next shows up at once, and is then looked up.
            pulls.feed("chat", said(f"and {second} too"))
            (added,) = await lines(reader, 1)
            assert added["pulls"] == [shown_row, waiting(7)]
            async with asyncio.timeout(10):
                while (latest := (await lines(reader, 1))[0])["pulls"][1]["loading"]:
                    pass
            assert latest["pulls"][1]["why"] == {"merge": "1 commit behind main"}

            # Closing the tab is noticed at once, well before the keepalive.
            writer.close()
            await until(lambda: not pulls.showing("chat"), side.KEEPALIVE / 3)
            assert pulls.showing("another-chat")
            # A closing bridge ends the streams still open.
            await server.close()
            await asyncio.wait_for(idle.read(), 5)
            assert not pulls.showing("another-chat")
            other.close()

    asyncio.run(scenario())


def test_the_pull_requests_page_runs_the_actions_its_buttons_ask_for(gh, acp_native):
    url = gh.pull(596, isDraft=True)

    async def scenario():
        async with in_process(acp_native) as (bridge, _):
            pulls, server = bridge.pulls, bridge.side_server
            pulls.said("chat", url)
            status, reader, writer = await request(server.port, "GET", events_url(server, "chat"))
            async with asyncio.timeout(10):
                while (await lines(reader, 1))[0]["pulls"][0]["loading"]:
                    pass

            ready = {"session": "chat", "url": url, "action": "ready"}
            assert await post(server, ready) == (200, {"ok": True})
            assert acted(gh) == [["pr", "ready", url]]
            # The open page is shown the action running, then what GitHub made of it.
            async with asyncio.timeout(10):
                while (latest := (await lines(reader, 1))[0]["pulls"][0])["draft"]:
                    assert latest["busy"] in ("ready", None)
            # What may not be done is refused in the answer, not as a failed request.
            assert await post(server, ready) == (200, {"error": "Already ready for review"})
            merge = {**ready, "action": "merge", "method": "rebase"}
            assert await post(server, merge) == (
                200,
                {"error": "This repository does not allow that merge method"},
            )
            assert await post(server, {**merge, "method": "squash"}) == (200, {"ok": True})
            assert acted(gh)[1:] == [
                ["pr", "merge", url, "--squash", "--subject", "Add the thing"]
                + ["--match-head-commit", OID]
            ]
            assert row(pulls, url)["state"] == "merged"

            # A chat without a tab, and a request that does not say what it wants.
            assert (await post(server, {**ready, "session": "no-such-chat"}))[0] == 404
            for body in ([], "ready", {"session": "chat"}, {"action": "ready"}, None):
                assert (await post(server, body))[0] == 400
            status, _ = await fetch(server.port, "GET", f"/pr/act?t={server.token}")
            assert status == 200 and len(acted(gh)) == 2
            writer.close()

    asyncio.run(scenario())


async def streamed(reader, done):
    """Every snapshot a page is sent, up to the first one that `done` accepts."""
    seen = []
    async with asyncio.timeout(10):
        while not seen or not done(seen[-1]):
            seen += await lines(reader, 1)
    return seen


def listed(snapshot):
    return [pull["url"] for pull in snapshot["pulls"]]


def test_pr_command_opens_the_tab_without_waiting_for_the_turn(gh, acp_native, tmp_path):
    url = gh.pull(596)

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            turn = asyncio.create_task(client.prompt(session_id, "busy"))
            await wait_for_text(client, "live progress")

            start = len(client.messages)
            asked = await asyncio.wait_for(client.prompt(session_id, f" /pr {url}"), 5)
            assert asked["result"] == {"stopReason": "end_turn"} and not turn.done()
            (preview,) = opened(client.messages[start:])
            assert set(preview) == {"sessionId", "previewUrl"}
            assert preview["sessionId"] == session_id
            address = urlsplit(preview["previewUrl"])
            assert (address.scheme, address.hostname) == ("http", "127.0.0.1")
            assert address.path == f"/pr/{quote(session_id, safe='')}"
            assert address.query.startswith("t=") and len(address.query) > 20
            # The chat itself shows nothing of it.
            assert replayed(client.messages[start:], "agent_message_chunk") == []

            # The tab loads the page, whose event stream shows the pull request asked for.
            status, _, body = await get(address.port, f"{address.path}?{address.query}")
            assert status == 200 and f'data-session="{session_id}"' in body
            # The page's own files are there to be served, whatever they hold.
            files = re.findall(r'(?:src|href)="([^"]*)"', body)
            assert len(files) == 3
            for source in files:
                assert (await get(address.port, source))[0] == 200
            events = f"/pr/events?session={quote(session_id, safe='')}&{address.query}"
            status, reader, stream = await request(address.port, "GET", events)
            assert status == 200
            *_, latest = await streamed(reader, lambda shown: not shown["pulls"][0]["loading"])
            assert [(pull["url"], pull["title"]) for pull in latest["pulls"]] == [
                (url, "Add the thing")
            ]

            # Desktop opens a new tab per notice: a chat whose page is connected gets none.
            start = len(client.messages)
            again = await asyncio.wait_for(client.prompt(session_id, "/pr"), 5)
            assert again["result"] == {"stopReason": "end_turn"}
            assert opened(client.messages[start:]) == []
            # Closing the tab is noticed at once, so the next /pr opens it again.
            stream.close()
            async with asyncio.timeout(side.KEEPALIVE / 3):
                while not opened(client.messages[start:]):
                    await asyncio.sleep(0.05)
                    await asyncio.wait_for(client.prompt(session_id, "/pr"), 5)
            assert len(opened(client.messages[start:])) == 1

            # A reload mid-turn replays the chat: neither the command nor a tab to reopen.
            start = len(client.messages)
            params = {"sessionId": session_id, "cwd": str(tmp_path), "mcpServers": []}
            await asyncio.wait_for(client.request("session/load", params), 5)
            replay = client.messages[start:]
            assert replayed_texts(replay, "user_message_chunk") == ["busy"]
            assert opened(replay) == []
            assert not turn.done()
            await client.send({"method": "session/cancel", "params": {"sessionId": session_id}})
            assert (await turn)["result"]["stopReason"] == "cancelled"

        # The agent never saw the command, and the chat never showed the pull request.
        assert [call["text"] for call in logged(acp_native) if call["text"] is not None] == ["busy"]
        assert not any("pull" in text for text in client.texts())
        (*_, latest) = replayed(client.messages, "available_commands_update")
        offered = {item["name"]: item for item in latest["availableCommands"]}
        assert len(offered) == len(latest["availableCommands"])
        assert {"help", "btw", "subagents"} <= set(offered)
        assert offered["pr"] == {
            "name": "pr",
            "description": "Show this chat's pull requests in a tab; this chat keeps working",
            "input": {"hint": "optional link or number"},
        }
        # The folder's own branch was not asked for: the tab was given a pull request.
        assert all(run[2] == url for run in gh.runs("pr", "view"))

    asyncio.run(scenario())


def test_pr_command_does_not_wake_a_chat_released_for_idleness(gh, acp_native, tmp_path):
    found = gh.pull(12)
    gh.put(numbers={"12": found})

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            chat.backend.last_activity -= acp.IDLE_TIMEOUT + 1
            assert await bridge.release_idle(chat)
            calls = [call for call in logged(acp_native) if call["sessionId"] == chat.session_id]
            reply = await bridge.dispatch(
                "session/prompt", prompt_params(chat.session_id, "/pr 12")
            )
            assert reply == {"stopReason": "end_turn"} and chat.suspended
            (preview,) = opened(messages)
            assert f"/pr/{chat.session_id}?t=" in preview["previewUrl"]
            # The number is looked for in the chat's folder, beside the answer.
            await settled(bridge.pulls)
            assert urls(bridge.pulls, chat.session_id) == [found]
            (call,) = gh.calls()
            assert call["args"] == ["pr", "view", "12", "--json", "url"]
            assert Path(call["cwd"]) == tmp_path.resolve()
            assert chat.suspended and calls == [
                call for call in logged(acp_native) if call["sessionId"] == chat.session_id
            ]

    asyncio.run(scenario())


def test_pr_command_takes_links_and_numbers_and_is_a_whole_word(gh, acp_native, tmp_path):
    by_link, by_number, mine = gh.pull(596), gh.pull(12), gh.pull(3)
    gh.put(numbers={"12": by_number}, branches={tmp_path.name: mine})

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            session_id, pulls = chat.session_id, bridge.pulls
            start = len(messages)
            reply = await bridge.dispatch("session/prompt", prompt_params(session_id, "/pr"))
            assert reply == {"stopReason": "end_turn"}
            (preview,) = opened(messages[start:])
            assert f"/pr/{session_id}?t=" in preview["previewUrl"]
            # Alone, with nothing listed yet: the pull request of the folder's branch.
            await settled(pulls)
            assert urls(pulls, session_id) == [mine]

            # With links and numbers, whatever else the prompt holds.
            image = {"type": "image", "data": "aGk=", "mimeType": "image/png"}
            blocks = [{"type": "text", "text": f"/pr {by_link} #12 99"}, image]
            reply = await bridge.dispatch(
                "session/prompt", {"sessionId": session_id, "prompt": blocks}
            )
            assert reply == {"stopReason": "end_turn"}
            # No page is connected, so each /pr asks for the tab.
            assert len(opened(messages[start:])) == 2
            assert urls(pulls, session_id) == [mine, by_link]
            await settled(pulls)
            assert urls(pulls, session_id) == [mine, by_link, by_number]
            assert pulls.snapshot(session_id)["notice"] == f"No pull request #99 in {tmp_path.name}"
            assert gh.runs() == [
                ["pr", "view", "--json", "url"],
                ["pr", "view", "12", "--json", "url"],
                ["pr", "view", "99", "--json", "url"],
            ]
            assert {Path(call["cwd"]) for call in gh.calls()} == {tmp_path.resolve()}
            assert prompts(acp_native) == []

            # A longer first word is not this command: the agent gets it as typed.
            start = len(messages)
            for text in ("/pr-foo", "/prs"):
                reply = await bridge.dispatch("session/prompt", prompt_params(session_id, text))
                assert reply == {"stopReason": "end_turn"}
            assert opened(messages[start:]) == []
            assert prompts(acp_native) == [("ansuman-1", "/pr-foo"), ("ansuman-1", "/prs")]

    asyncio.run(scenario())


def test_links_a_chat_shows_reach_its_open_tab(gh, acp_native, tmp_path, monkeypatch):
    gate = tmp_path / "end-the-turn"
    created = "gh pr create --fill"
    turn = [
        said("Opened https://github.com/acme/app/pull/1"),
        # Usage arrives in the middle of the message, as it does from a real native.
        {"sessionUpdate": "usage_update", "used": 1, "size": 10},
        said("23 and one by a command."),
        step("call_1", "tool_call", title="Running command", rawInput={"command": created}),
        step("call_1", status="completed", content=printed(f"{link(77)}\n")),
        step("call_2", "tool_call", title="Ran cat", rawInput={"command": "cat notes.md"}),
        step("call_2", status="completed", content=printed(f"{link(78)}\n")),
        # A subagent's own message never shows in the chat, but its link counts.
        said(f"Mine is {link(55)} too.", stream="s1", agent="sub1"),
        said(f"The last one is {link(45)}", stream="m2"),
    ]
    monkeypatch.setenv("FAKE_SAY", json.dumps(turn))
    monkeypatch.setenv("FAKE_SAY_GATE", str(gate))

    async def scenario():
        async with connect(acp_native) as client:
            session_id = await client.new(tmp_path)
            start = len(client.messages)
            await asyncio.wait_for(client.prompt(session_id, f"/pr {link(9)}"), 5)
            (preview,) = opened(client.messages[start:])
            address = urlsplit(preview["previewUrl"])
            events = f"/pr/events?session={quote(session_id, safe='')}&{address.query}"
            _, reader, stream = await request(address.port, "GET", events)

            running = asyncio.create_task(client.prompt(session_id, "say"))
            seen = await streamed(reader, lambda shown: len(shown["pulls"]) == 4)
            assert listed(seen[-1]) == [link(9), link(123), link(77), link(55)]
            # The turn is still open, so the link its last message ends with is not counted yet.
            await asyncio.sleep(0.3)
            assert not running.done()
            gate.touch()
            assert (await running)["result"] == {"stopReason": "end_turn"}
            seen += await streamed(reader, lambda shown: len(shown["pulls"]) == 5)
            assert listed(seen[-1]) == [link(9), link(123), link(77), link(55), link(45)]

            # What the user writes counts as well.
            await asyncio.wait_for(client.prompt(session_id, f"and what about {link(31)}"), 5)
            seen += await streamed(reader, lambda shown: len(shown["pulls"]) == 6)
            assert listed(seen[-1])[-1] == link(31)
            # No page was ever shown part of a link, or what another command printed.
            every = {url for shown in seen for url in listed(shown)}
            assert every == {link(number) for number in (9, 123, 77, 55, 45, 31)}
            stream.close()

        assert "Mine is" not in "".join(client.texts())
        assert "The last one is" in "".join(client.texts())

    asyncio.run(scenario())


def test_links_a_reopened_chat_replays_are_listed_again(gh, acp_native, tmp_path, monkeypatch):
    history = [
        said(f"please look at {link(5)}", stream=None, kind=USER),
        said(f"Looked at {link(5)}; see {link(6)}/files as well.", stream=None),
        said(f"It ends with {link(7)}", stream=None),
    ]
    monkeypatch.setenv("FAKE_REPLAY_SAY", json.dumps(history))

    async def scenario():
        async with in_process(acp_native) as (bridge, messages):
            chat = await new_chat(bridge, tmp_path)
            session_id, pulls = chat.session_id, bridge.pulls
            assert urls(pulls, session_id) == []
            await bridge.dispatch("session/load", chat.setup)
            # A replay is read like the chat itself: the settings that follow it do not say
            # that its last message is over, so the link that message ends with waits.
            assert urls(pulls, session_id) == [link(5), link(6)]
            assert opened(messages) == []
            # /pr counts it.
            await bridge.dispatch("session/prompt", prompt_params(session_id, "/pr"))
            assert urls(pulls, session_id) == [link(5), link(6), link(7)]
            # One the user dismissed stays away through the next replay.
            assert await ask(pulls, "dismiss", link(6), session_id) == {"ok": True}
            await bridge.dispatch("session/load", chat.setup)
            assert urls(pulls, session_id) == [link(5), link(7)]
            assert gh.calls() == []

    asyncio.run(scenario())
