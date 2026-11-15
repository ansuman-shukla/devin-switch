import json
import sys
from pathlib import Path

import pytest
from test_agent_browser import Client

from devin_switch import agent_browser, cli, real_browser
from devin_switch.store import Store, SwitchError

PAUSED = "The user paused Browser MCP. Ask them to click Resume in its popup"
NOT_CONNECTED = "No browser is connected. Install the Browser MCP extension, then retry"


@pytest.mark.parametrize(
    "arguments", [["allow", "example.com"], ["disallow", "example.com"], ["sites"]]
)
def test_the_site_list_commands_are_gone(
    arguments: list[str], capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.parser().parse_args(["browser", *arguments])
    assert refused.value.code == 2
    assert f"invalid choice: '{arguments[0]}'" in capsys.readouterr().err
    assert cli.parser().parse_args(["browser", "real-mcp"]).action == "real-mcp"


def test_a_call_is_split_into_commands_like_upstream_splits_it() -> None:
    text = "# open it\r\n  tab new example.com \r\n\n\t#later\n\ufeffget title\u00a0\nclick e1\rx"
    assert real_browser.split(text) == ["tab new example.com", "get title", "click e1\rx"]
    assert real_browser.split(["goto example.com\nsnapshot", " help "]) == [
        "goto example.com",
        "snapshot",
        "help",
    ]
    assert real_browser.split(" \n# nothing\n") == []
    for wrong in (None, 5, {"commands": "help"}, ["help", 5]):
        with pytest.raises(SwitchError, match="command lines"):
            real_browser.split(wrong)


LISTED = "These are the tabs this chat opened; * is current. The owner's own tabs are never listed."


def plan(
    command: str, tabs: tuple[str, ...] = ("12",), current: str | None = "12"
) -> real_browser.Plan:
    return real_browser.plan(command, tabs, current)


@pytest.mark.parametrize(
    "command",
    [
        "goto example.com",
        "goto other.test",
        "goto   https://studio.example.com/a(b",
        'goto "https://example.com/q?x=\\"1\\""',
        "goto 'example.com'",
        "goto https://example.com/é",
        "goto HTTPS://other.test",
        "goto https://example.com@other.test",
        "goto localhost:3000",
        "goto about:blank",
        "goto file:///tmp/page.html",
        "goto view-source:https://example.com",
        "goto chrome://version/",
    ],
)
def test_a_goto_to_any_address_is_sent_as_the_agent_wrote_it(command: str) -> None:
    assert plan(command) == real_browser.Plan(sends=(command,))


@pytest.mark.parametrize(
    ("command", "sends"),
    [
        ("tab new", ["tab new"]),
        ("tab new localhost/x", ["tab new", 'goto "localhost/x"']),
        ("tab new https://other.test/a?b=1#c", ["tab new", 'goto "https://other.test/a?b=1#c"']),
        ('tab new "https://example.com/a b"', ["tab new", 'goto "https://example.com/a b"']),
        ("tab new https://example.com/é", ["tab new", 'goto "https://example.com/\\u00e9"']),
        ("tab new file:///tmp/page.html", ["tab new", 'goto "file:///tmp/page.html"']),
        ('tab new --browser "Work Chrome"', ['tab new --browser "Work Chrome"']),
        ("tab new example.com --browser b2", ['tab new --browser "b2"', 'goto "example.com"']),
        ("tab --browser b2 new example.com", ['tab new --browser "b2"', 'goto "example.com"']),
        ("tab close", ["tab close"]),
        ("tab close 12", ["tab close 12"]),
        ("tab close 99", ["tab close 99"]),
        ("tab select 12", ["tab select 12"]),
        ('tab "select" "12"', ["tab select 12"]),
    ],
)
def test_tab_commands_are_rebuilt_for_upstream(command: str, sends: list[str]) -> None:
    assert plan(command) == real_browser.Plan(sends=tuple(sends))


def test_a_goto_with_no_current_tab_of_the_chats_own_opens_one_first() -> None:
    opened = ("tab new", "goto other.test/a")
    assert plan("goto other.test/a", tabs=(), current=None).sends == opened
    assert plan("goto other.test/a", tabs=("12",), current=None).sends == opened
    assert plan("goto other.test/a", tabs=("12",), current="12").sends == opened[1:]


@pytest.mark.parametrize(
    "command",
    [
        "tab list",
        "tab  list  --max 5",
        'tab "list"',
        "tab\u00a0list",
        "tab\ufefflist",
        "tab --browser b2 list",
    ],
)
def test_tab_list_is_answered_here_with_the_chats_own_tabs(command: str) -> None:
    listed = plan(command, tabs=("12", "15"), current="15")
    assert listed == real_browser.Plan(reply=f"  12\n* 15\n{LISTED}")
    assert plan(command, tabs=("12", "15"), current=None).reply == f"  12\n  15\n{LISTED}"
    nothing = plan(command, tabs=(), current=None)
    assert nothing == real_browser.Plan(
        reply="This chat has no tab open. Open one with: tab new <url>\n"
        "The owner's own tabs are never listed."
    )


@pytest.mark.parametrize(
    "command",
    [
        "tab",
        "tab ls",
        "tab List",
        "tab new example.com other.test",
        "tab new --browser",
        "tab new --browser=b2 other.test",
        "tab new (https://other.test )",
        "tab select 7",
        "tab select 012",
        "tab select 12 7",
        "tab select",
        "tab select --browser b2 12",
        "tab close 12 7",
        "tab close all",
        "tab close --browser b2",
        "goto",
        "goto example.com other.test",
        "goto --wait example.com",
        "goto https://example.com/a b",
        "request",
        "request 5",
        "request 5 --body",
    ],
)
def test_other_tab_commands_gotos_and_request_details_are_refused(command: str) -> None:
    decided = plan(command)
    assert decided.refusal and not decided.sends and not decided.reply
    idle = plan(command, tabs=(), current=None)
    assert idle.refusal and not idle.sends and not idle.reply


def test_refusals_say_what_real_mode_takes() -> None:
    assert "not one this chat opened" in plan("tab select 7").refusal
    assert plan("tab ls").refusal == real_browser.TAB_USAGE
    assert plan("goto").refusal == "goto takes one address, like: goto https://example.com"
    assert "cookies and tokens" in plan("request 5").refusal


@pytest.mark.parametrize(
    "command",
    [
        "back",
        "forward",
        "reload",
        "help",
        "help tab",
        "browser list",
        'browser use "b2"',
        "snapshot -i",
        "click e12",
        'fill getByLabel("Email") "a@example.com"',
        'eval "document.title"',
        "get url",
        "screenshot --full",
        "wait 500",
        "dialog accept",
        "requests",
        "requests --filter api",
        "Request 5",
        "browser",
        "browser close",
        "Tab list",
        "tabs",
        "helper",
        'he"lp"',
    ],
)
def test_every_other_command_is_sent_as_it_is(command: str) -> None:
    assert plan(command) == real_browser.Plan(sends=(command,))
    assert plan(command, tabs=(), current=None) == real_browser.Plan(sends=(command,))


def test_upstream_is_the_pinned_package_with_no_extra_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DS_REAL_BROWSER_MCP", raising=False)
    monkeypatch.setattr(agent_browser.shutil, "which", lambda name: f"/opt/node/bin/{name}")
    command, environment = real_browser.upstream_command()
    assert command == ["/opt/node/bin/npx", "-y", "@browsermcp/mcp@2.4.0"]
    assert environment["PATH"].startswith("/opt/node/bin:")
    monkeypatch.setenv("DS_REAL_BROWSER_MCP", json.dumps(["fake", "server"]))
    assert real_browser.upstream_command()[0] == ["fake", "server"]


def test_the_opened_tab_is_read_only_from_upstreams_own_note() -> None:
    sent = 'tab new "https://example.com" --browser "x \\u2014 tab 7"'
    line = f"✓ {sent} — tab 501 in x — tab 7"
    assert real_browser.opened_tab(sent, line) == "501"
    assert real_browser.opened_tab(sent, f"{line}\nPage: x") == "501"
    assert real_browser.opened_tab("tab new", "✓ tab new — tab 502; opened tab 7 (x)") == "502"
    assert real_browser.opened_tab("tab new", '✓ tab new "x" — tab 7') is None
    assert real_browser.opened_tab("tab new", "✗ step 1: tab new — tab 7") is None
    long = f'tab new "https://example.com/{"a" * 400}"'
    assert real_browser.opened_tab(long, f"✓ {long[:299]}… — tab 503") == "503"


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder with the fake Browser MCP server's log and controls; chats start that server."""
    folder = tmp_path / "upstream"
    folder.mkdir()
    server = folder / "server.py"
    server.write_text(Path(__file__).with_name("fake_browser_mcp.py").read_text())
    monkeypatch.setenv("DS_REAL_BROWSER_MCP", json.dumps([sys.executable, str(server)]))
    monkeypatch.setenv("FAKE_BROWSER_MCP", str(folder))
    return folder


class Chat(Client):
    action = "real-mcp"

    def write(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self.write({"id": self.next_id, "method": method, "params": params or {}})
        while True:
            reply = self.messages.get(timeout=30)
            if "method" in reply and "id" in reply:
                roots = [{"uri": "file:///work", "name": "work"}]
                self.write({"id": reply["id"], "result": {"roots": roots}})
            elif reply.get("id") == self.next_id:
                return reply

    def browser(self, commands: object) -> dict:
        arguments = {"name": "browser", "arguments": {"commands": commands}}
        return self.request("tools/call", arguments)["result"]

    def run(self, commands: str) -> tuple[str, bool]:
        result = self.browser(commands)
        return result["content"][0]["text"], result["isError"]


@pytest.fixture
def chat(store: Store, upstream: Path):
    client = Chat(store)
    yield client
    client.close()


def sent(upstream: Path) -> list[str]:
    log = upstream / "log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def control(upstream: Path, **settings: object) -> None:
    (upstream / "control.json").write_text(json.dumps(settings))


def test_instructions_carry_the_real_mode_note_and_everything_else_passes_through(
    chat: Chat, upstream: Path
) -> None:
    assert chat.instructions == f"{real_browser.NOTE}\n\nUpstream instructions: act in batches."
    for told in (
        "the owner's real Chrome",
        "It can open any site, but only in tabs you open",
        "the owner's own tabs cannot be selected",
        "ask the owner",
        "the owner paused Browser MCP, stop and wait",
        "Never sign in or out",
        "Never read or print cookies, tokens or passwords",
        "Close your tabs",
    ):
        assert told in real_browser.NOTE
    assert "allow" not in chat.instructions
    assert chat.request("tools/list")["result"] == {
        "tools": [{"name": "browser", "inputSchema": {"type": "object"}}]
    }
    assert chat.request("ping")["result"] == {"answered": "ping"}
    assert (upstream / "notifications").read_text() == "notifications/initialized\n"
    text, error = chat.run("upload report.pdf")
    assert not error and text == "✓ upload report.pdf\nroot file:///work"


def test_any_site_opens_with_no_site_ever_allowed(chat: Chat, store: Store, upstream: Path) -> None:
    assert chat.run("goto other.test/watch?v=1") == (
        "✓ tab new — tab 501\n"
        "✓ goto other.test/watch?v=1\n"
        'Page: https://other.test/watch?v=1 — "Title of other.test"',
        False,
    )
    assert chat.run("tab new https://example.com/a") == (
        "✓ tab new — tab 502\n"
        '✓ goto "https://example.com/a"\n'
        'Page: https://example.com/a — "Title of example.com"',
        False,
    )
    assert chat.run("goto file:///tmp/page.html\nget url") == (
        "✓ goto file:///tmp/page.html\n"
        "✓ get url\nfile:///tmp/page.html\n"
        'Page: file:///tmp/page.html — "Title of "',
        False,
    )
    assert sent(upstream) == [
        "tab new",
        "goto other.test/watch?v=1",
        "tab new",
        'goto "https://example.com/a"',
        "goto file:///tmp/page.html",
        "get url",
    ]
    assert not (store.root / "browser/settings.json").exists()


@pytest.mark.parametrize("left", [[], ["example.com"]])
def test_a_site_list_left_in_old_settings_changes_nothing(
    store: Store, upstream: Path, left: list[str]
) -> None:
    agent_browser.save_settings(store, installed=True, real_sites=left)
    saved = (store.root / "browser/settings.json").read_bytes()
    client = Chat(store)
    try:
        assert client.instructions == (
            f"{real_browser.NOTE}\n\nUpstream instructions: act in batches."
        )
        for address in ("example.com", "other.test"):
            assert not client.run(f"goto {address}\nget title")[1]
    finally:
        client.close()
    assert sent(upstream) == [
        "tab new",
        "goto example.com",
        "get title",
        "goto other.test",
        "get title",
    ]
    assert (store.root / "browser/settings.json").read_bytes() == saved


def test_a_goto_that_cannot_run_opens_no_tab(chat: Chat, upstream: Path) -> None:
    for command in ("goto", "goto example.com other.test"):
        assert chat.run(f"{command}\nget title") == (
            f"✗ step 1: {command} — goto takes one address, like: goto https://example.com\n"
            "Step 2 not run",
            True,
        )
    assert chat.run("tab list")[0].startswith("✓ tab list\nThis chat has no tab open.")
    assert sent(upstream) == []


def test_other_chrome_profiles_are_listed_chosen_and_opened_in(chat: Chat, upstream: Path) -> None:
    text, error = chat.run(
        'browser list\nbrowser use "Work"\ntab new example.com --browser "Work 2"\ntab list'
    )
    assert not error
    assert text == (
        '✓ browser list\n* b1 "Work" — 3 tabs\n'
        '✓ browser use "Work"\n* b1 "Work" — 3 tabs\n'
        '✓ tab new --browser "Work 2" — tab 501 in Work 2\n✓ goto "example.com"\n'
        f"✓ tab list\n* 501\n{LISTED}\n"
        'Page: https://example.com — "Title of example.com"'
    )
    assert sent(upstream) == [
        "browser list",
        'browser use "Work"',
        'tab new --browser "Work 2"',
        'goto "example.com"',
    ]


def test_the_owners_tabs_are_never_listed_or_selected(
    chat: Chat, store: Store, upstream: Path
) -> None:
    text, _ = chat.run('tab new example.com\ntab new example.com/b --browser "x — tab 7"')
    assert "✓ tab new — tab 501" in text and "— tab 502 in x — tab 7" in text
    before = sent(upstream)
    for command in ("tab list", 'tab "list"', "tab --browser b2 list"):
        assert chat.run(command) == (f"✓ {command}\n  501\n* 502\n{LISTED}", False)
    for command in ("tab", "tab ls"):
        text, error = chat.run(command)
        assert error and text == f"✗ step 1: {command} — {real_browser.TAB_USAGE}"
    for foreign in ("7", "0501", "503"):
        text, error = chat.run(f"tab select {foreign}")
        assert error and "not one this chat opened" in text
    assert sent(upstream) == before

    text, error = chat.run("tab select 501\nget url\ntab list")
    assert not error
    assert text == (
        "✓ tab select 501\n✓ get url\nhttps://example.com\n"
        f"✓ tab list\n* 501\n  502\n{LISTED}\n"
        'Page: https://example.com — "Title of example.com"'
    )
    assert sent(upstream)[len(before) :] == ["tab select 501", "get url"]

    other = Chat(store)
    listed, _ = other.run("tab list")
    text, error = other.run("tab select 501")
    other.close()
    assert listed.startswith("✓ tab list\nThis chat has no tab open.")
    assert error and "not one this chat opened" in text


def test_a_chat_can_always_get_back_to_a_tab_it_opened(chat: Chat, upstream: Path) -> None:
    assert chat.run("goto example.com") == (
        "✓ tab new — tab 501\n"
        "✓ goto example.com\n"
        'Page: https://example.com — "Title of example.com"',
        False,
    )
    assert not chat.run("goto example.com/next")[1]
    assert sent(upstream) == ["tab new", "goto example.com", "goto example.com/next"]
    assert not chat.run("tab new example.com/\ntab close")[1]
    assert chat.run("tab list") == (f"✓ tab list\n  501\n{LISTED}", False)
    before = sent(upstream)
    text, error = chat.run("tab select 501\ngoto example.com/again\nget url")
    assert not error
    assert text == (
        "✓ tab select 501\n✓ goto example.com/again\n"
        "✓ get url\nhttps://example.com/again\n"
        'Page: https://example.com/again — "Title of example.com"'
    )
    assert sent(upstream)[len(before) :] == [
        "tab select 501",
        "goto example.com/again",
        "get url",
    ]


def test_a_closed_tab_is_forgotten_and_the_next_goto_opens_a_new_one(
    chat: Chat, upstream: Path
) -> None:
    chat.run("tab new example.com\ntab new example.com/b\ntab new example.com/c")
    assert chat.run("tab close 501\ntab list") == (
        f"✓ tab close 501\n✓ tab list\n  502\n* 503\n{LISTED}",
        False,
    )
    text, error = chat.run("tab close 7\ntab list")
    assert error and "Tab 7 isn't one you opened" in text
    control(upstream, error=PAUSED)
    for command in ("tab close 502", "tab select 502", "tab close"):
        assert chat.run(command) == (PAUSED, True)
    control(upstream)
    assert chat.run("tab list") == (f"✓ tab list\n  502\n* 503\n{LISTED}", False)
    assert chat.run("tab close\ntab list") == (f"✓ tab close\n✓ tab list\n  502\n{LISTED}", False)
    text, error = chat.run("tab close")
    assert error and text == "✗ step 1: tab close — There's no current tab"
    before = sent(upstream)
    for closed in ("501", "503"):
        text, error = chat.run(f"tab select {closed}")
        assert error and "not one this chat opened" in text
    assert chat.run("tab list") == (f"✓ tab list\n  502\n{LISTED}", False)
    assert sent(upstream) == before

    text, error = chat.run("goto example.com/d\ntab list")
    assert not error and text.startswith(
        f"✓ tab new — tab 504\n✓ goto example.com/d\n✓ tab list\n  502\n* 504\n{LISTED}"
    )
    assert sent(upstream)[len(before) :] == ["tab new", "goto example.com/d"]


def test_a_tab_whose_page_fails_to_load_is_still_known(chat: Chat, upstream: Path) -> None:
    for command, went in (
        ("goto down.example.com", "goto down.example.com"),
        ("tab new down.example.com", 'goto "down.example.com"'),
    ):
        text, error = chat.run(f"{command}\nget title")
        assert error and "✓ tab new — tab " in text
        assert text.split("\n")[1:] == [
            f"✗ step 1: {went} — net::ERR_CONNECTION_REFUSED at https://down.example.com",
            "Step 2 not run",
            'Page: about:blank — ""',
        ]
    assert chat.run("tab list") == (f"✓ tab list\n  501\n* 502\n{LISTED}", False)
    before = sent(upstream)
    assert chat.run("goto example.com") == (
        '✓ goto example.com\nPage: https://example.com — "Title of example.com"',
        False,
    )
    assert sent(upstream)[len(before) :] == ["goto example.com"]


def test_tab_list_never_reaches_upstream_and_the_batch_goes_on(chat: Chat, upstream: Path) -> None:
    text, error = chat.run("tab list\ntab new example.com\ntab list\nget title")
    assert not error
    assert text == (
        "✓ tab list\nThis chat has no tab open. Open one with: tab new <url>\n"
        "The owner's own tabs are never listed.\n"
        '✓ tab new — tab 501\n✓ goto "example.com"\n'
        f"✓ tab list\n* 501\n{LISTED}\n"
        "✓ get title\nTitle of example.com\n"
        'Page: https://example.com — "Title of example.com"'
    )
    assert "owner.test" not in text
    assert sent(upstream) == ["tab new", 'goto "example.com"', "get title"]


def test_a_requests_headers_and_body_are_never_shown(chat: Chat, upstream: Path) -> None:
    chat.run("tab new example.com")
    before = sent(upstream)
    for command in ("request 5", "request 5 --body"):
        assert chat.run(command) == (
            f"✗ step 1: {command} — {real_browser.NO_REQUEST}",
            True,
        )
    text, error = chat.run("get title\nrequest 5 --body\nget url")
    assert error and "secret" not in text
    assert text == (
        "✓ get title\nTitle of example.com\n"
        f"✗ step 2: request 5 --body — {real_browser.NO_REQUEST}\n"
        "Step 3 not run"
    )
    assert sent(upstream)[len(before) :] == ["get title"]
    assert chat.run("requests") == ("✓ requests\n5 GET 200 https://example.com/api", False)
    assert sent(upstream)[len(before) :] == ["get title", "requests"]


def test_leaving_a_site_by_a_link_or_a_redirect_does_not_stop_the_batch(
    chat: Chat, upstream: Path
) -> None:
    chat.run("tab new example.com")
    control(upstream, moves={"click": "https://other.test/landing"})
    result = chat.browser("click e1\nget text\nsnapshot\nscreenshot")
    assert result["isError"] is False
    assert result["content"] == [
        {
            "type": "text",
            "text": (
                "✓ click e1\n"
                "✓ get text\ntext of https://other.test/landing\n"
                "✓ snapshot\nsnapshot of https://other.test/landing\n"
                "✓ screenshot — 2×2 JPEG\n"
                'Page: https://other.test/landing — "Title of other.test"'
            ),
        },
        {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/jpeg"},
    ]
    control(upstream, moves={"goto": "https://elsewhere.test/moved"})
    assert chat.run("goto example.com/next\nget url") == (
        "✓ goto example.com/next\n"
        "✓ get url\nhttps://elsewhere.test/moved\n"
        'Page: https://elsewhere.test/moved — "Title of elsewhere.test"',
        False,
    )
    assert sent(upstream) == [
        "tab new",
        'goto "example.com"',
        "click e1",
        "get text",
        "snapshot",
        "screenshot",
        "goto example.com/next",
        "get url",
    ]


def test_a_batch_reaches_upstream_one_command_per_call_in_order_and_nothing_else_does(
    chat: Chat, upstream: Path
) -> None:
    result = chat.browser(
        "# open it\r\ntab new example.com\r\n\r\n  get title  \nhelp\nscreenshot\nget url\n"
    )
    assert result["isError"] is False
    assert result["content"] == [
        {
            "type": "text",
            "text": (
                '✓ tab new — tab 501\n✓ goto "example.com"\n'
                "✓ get title\nTitle of example.com\n"
                "✓ help\ngoto <url>\n"
                "✓ screenshot — 2×2 JPEG\n"
                "✓ get url\nhttps://example.com\n"
                'Page: https://example.com — "Title of example.com"'
            ),
        },
        {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/jpeg"},
    ]
    assert sent(upstream) == [
        "tab new",
        'goto "example.com"',
        "get title",
        "help",
        "screenshot",
        "get url",
    ]
    assert chat.browser(["goto example.com/b\nget title", "back"])["isError"] is False
    assert sent(upstream)[6:] == ["goto example.com/b", "get title", "back"]


def test_two_calls_at_once_never_interleave_upstream(chat: Chat, upstream: Path) -> None:
    for name in ("a", "b"):
        commands = "\n".join(f"help {name}{number}" for number in range(1, 5))
        chat.next_id += 1
        arguments = {"name": "browser", "arguments": {"commands": commands}}
        chat.write({"id": chat.next_id, "method": "tools/call", "params": arguments})
    replies = [chat.messages.get(timeout=30) for _ in range(2)]
    assert sorted(reply["id"] for reply in replies) == [chat.next_id - 1, chat.next_id]
    assert all(reply["result"]["isError"] is False for reply in replies)
    assert sent(upstream) == [f"help {name}{number}" for name in "ab" for number in range(1, 5)]


def test_only_the_last_page_line_is_kept(chat: Chat) -> None:
    text, _ = chat.run("tab new example.com\ngoto example.com/b\nget title\ntab close")
    assert text.count("Page: ") == 1
    assert text.endswith('✓ tab close\nPage: https://example.com/b — "Title of example.com"')


def test_failures_are_numbered_by_their_place_in_the_batch(chat: Chat, upstream: Path) -> None:
    text, error = chat.run("tab new example.com\nget title\nclick missing\nget url\nsnapshot")
    assert error
    assert text == (
        '✓ tab new — tab 501\n✓ goto "example.com"\n'
        "✓ get title\nTitle of example.com\n"
        "✗ step 3: click missing — No element matches missing\n"
        "Steps 4–5 not run\n"
        'Page: https://example.com — "Title of example.com"'
    )
    assert sent(upstream)[-2:] == ["get title", "click missing"]

    text, error = chat.run("get title\nfrobnicate now\nsnapshot")
    assert error
    assert text.split("\n")[2:] == [
        "✗ step 2: frobnicate — Unknown command frobnicate. Commands: goto, back, tab, "
        "browser, help, get, snapshot, click, eval, screenshot, upload, crash, requests, request",
        "Step 3 not run",
    ]
    text, error = chat.run("frobnicate now\nsnapshot")
    assert error and text.startswith("✗ step 1: frobnicate — Unknown command frobnicate.")
    assert text.endswith("Nothing ran: fix this line and send the call again\nStep 2 not run")

    text, error = chat.run("get title\ntab ls\nsnapshot\nhelp")
    assert error and text.endswith(
        f"✗ step 2: tab ls — {real_browser.TAB_USAGE}\nSteps 3–4 not run"
    )
    text, error = chat.run("get title\nrequest 5")
    assert error and text.endswith(f"✗ step 2: request 5 — {real_browser.NO_REQUEST}")
    assert "not run" not in text

    text, error = chat.run("get title\ntab new down.example.com\nhelp")
    assert error and text.split("\n")[2:] == [
        "✓ tab new — tab 502",
        '✗ step 2: goto "down.example.com" — net::ERR_CONNECTION_REFUSED at '
        "https://down.example.com",
        "Step 3 not run",
        'Page: about:blank — ""',
    ]


@pytest.mark.parametrize("message", [PAUSED, NOT_CONNECTED])
def test_upstream_errors_come_back_unchanged(chat: Chat, upstream: Path, message: str) -> None:
    control(upstream, error=message)
    for command in ("snapshot", "tab new example.com", "back"):
        assert chat.run(command) == (message, True)
    assert chat.run("get title\nsnapshot") == (f"{message}\nStep 2 not run", True)
    assert sent(upstream) == ["snapshot", "tab new", "back", "get title"]
    control(upstream)
    assert chat.run("tab list")[0].startswith("✓ tab list\nThis chat has no tab open.")


def test_other_tools_and_unreadable_commands_are_answered_without_upstream(
    chat: Chat, upstream: Path
) -> None:
    reply = chat.request("tools/call", {"name": "tab_list", "arguments": {"commands": "help"}})
    assert reply["error"]["code"] == -32602 and "tab_list" in reply["error"]["message"]
    for wrong in (None, 5, ["help", 5]):
        result = chat.browser(wrong)
        assert result["isError"] and "command lines" in result["content"][0]["text"]
    assert chat.run("# nothing to do\n\n") == ("No commands. Run `help`", True)
    unanswerable = {"name": "browser", "arguments": {"commands": "help"}}
    chat.write({"method": "tools/call", "params": unanswerable})
    assert chat.request("ping")["result"] == {"answered": "ping"}
    assert (upstream / "notifications").read_text() == "notifications/initialized\n"
    assert sent(upstream) == []


def test_the_proxy_ends_when_the_upstream_server_does(store: Store, upstream: Path) -> None:
    chat = Chat(store)
    assert chat.run("help\ncrash\nhelp") == (real_browser.STOPPED, True)
    assert chat.process.wait(timeout=15) == 1
    chat.close()
