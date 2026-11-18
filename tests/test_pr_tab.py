import pytest

from devin_switch import pr_tab

STREAM = "cognition.ai/streamingMessageId"
CONTEXT = "cognition.ai/subagent_context"
THOUGHT = "agent_thought_chunk"
USER = "user_message_chunk"


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


@pytest.fixture
def pulls():
    return pr_tab.Pulls()


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


def test_a_link_counts_wherever_the_chunks_divide_it():
    text = "x" * (2 * pr_tab.TAIL) + f" merged {link(596)} today"
    for cut in range(len(text) - 60, len(text)):
        pulls = pr_tab.Pulls()
        pulls.feed("chat", said(text[:cut]))
        pulls.feed("chat", said(text[cut:]))
        assert urls(pulls) == [link(596)], cut
    pulls = pr_tab.Pulls()
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


def test_a_page_gets_one_token_per_change_and_closing_ends_its_stream(pulls):
    assert not pulls.showing("chat")
    with pulls.listen("chat") as page, pulls.listen("chat") as second, pulls.listen("b") as other:
        assert pulls.showing("chat") and pulls.showing("b") and not pulls.showing("c")
        for queue in (page, second, other):
            # The pull requests as they are, right after connecting.
            assert queue.get_nowait() is True and queue.empty()
        pulls.feed("chat", said(f"{link(1)} and {link(2)} "))
        for queue in (page, second):
            assert queue.get_nowait() is True and queue.empty()
        # A link the tab already has changes nothing, and another chat's page hears nothing.
        pulls.feed("chat", said(f"{link(1)} again "))
        pulls.feed("chat", said("no link here"))
        assert page.empty() and second.empty() and other.empty()
        # One waiting token covers every change until the page takes it.
        pulls.feed("chat", said(f"{link(3)} "))
        pulls.feed("chat", said(f"{link(4)} "))
        assert page.qsize() == 1
        pulls.close()
        assert [page.get_nowait(), page.get_nowait()] == [True, None]
        assert second.get_nowait() is True and second.get_nowait() is None
        assert other.get_nowait() is None
    assert not pulls.showing("chat") and not pulls.showing("b")
    assert urls(pulls) == [link(number) for number in range(1, 5)]
