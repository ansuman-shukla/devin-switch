import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from test_acp import until

from devin_switch import pr_tab

STREAM = "cognition.ai/streamingMessageId"
CONTEXT = "cognition.ai/subagent_context"
THOUGHT = "agent_thought_chunk"
USER = "user_message_chunk"
OID = "1f2e3d4c5b6a79880123456789abcdef01234567"
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
        "headRefOid": OID,
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
    return Gh(home)


@pytest.fixture
def pulls(store):
    return pr_tab.Pulls(store)


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
        said("hm", kind=THOUGHT),
        step("call_1", "tool_call", title="Running command"),
        {"sessionUpdate": "usage_update", "_meta": {CONTEXT: {"parentAgentId": "root"}}},
        {"sessionUpdate": "available_commands_update", "availableCommands": []},
    ],
)
def test_a_link_that_ends_a_message_counts_once_the_message_has_ended(pulls, ending):
    pulls.feed("chat", said(f"Opened {link(41)}"))
    assert urls(pulls) == []
    pulls.feed("chat", ending)
    assert urls(pulls) == [link(41)]


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
    # The subagent's own next step ends its message.
    pulls.feed("chat", {"sessionUpdate": "usage_update", "_meta": theirs})
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


def test_other_tool_calls_say_nothing_about_pull_requests(pulls):
    seen = f"see {link(99)} and more\n"
    for call, title, command in (
        ("c1", "Ran cat", "cat notes.md"),
        ("c2", "Ran gh", "gh issue view 5"),
        ("c3", "Ran git", "git log --grep pr"),
    ):
        pulls.feed("chat", step(call, "tool_call", title=title, rawInput={"command": command}))
        pulls.feed("chat", step(call, status="completed", content=printed(seen)))
    # What a file says about `gh pr` does not make reading it a `gh pr` command.
    read = step("c4", "tool_call", title="Read notes.md", status="completed")
    pulls.feed("chat", {**read, "content": printed(f"gh pr view {link(98)} \n")})
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


def test_a_page_gets_one_token_per_change_and_closing_ends_its_stream(pulls, monkeypatch):
    async def idle(self, tab):
        await asyncio.Event().wait()

    # Looking pull requests up tells pages too; here only what the chat says does.
    monkeypatch.setattr(pr_tab.Pulls, "watch", idle)

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
    assert pr_tab.describe(view(additions=True, deletions="7", changedFiles=2.5), 0, []) | {
        "additions": 0,
        "deletions": 0,
        "files": 0,
    } == pr_tab.describe(view(additions=0, deletions=0, changedFiles=0), 0, [])


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
    url = gh.pull(596)

    async def scenario():
        pulls.said("chat", url)
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
