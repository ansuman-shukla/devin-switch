import asyncio
import json
import sys
from pathlib import Path

import pytest
from test_agent_browser import Client

from devin_switch import agent_browser, cli, real_browser
from devin_switch.store import Store, SwitchError

ALLOWED = ["example.com", "localhost"]
PAUSED = "The user paused Browser MCP. Ask them to click Resume in its popup"
NOT_CONNECTED = "No browser is connected. Install the Browser MCP extension, then retry"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/",
        "http://example.com/path?next=https://other.test/#top",
        "https://EXAMPLE.com:8443/a",
        "https://studio.example.com/watch?v=1",
        "https://a.b.example.com",
        "http://localhost:3000/",
        "about:blank",
    ],
)
def test_allowed_sites_cover_the_host_and_its_subdomains(url: str) -> None:
    assert real_browser.permitted(url, ALLOWED)


@pytest.mark.parametrize(
    "url",
    [
        "https://notexample.com/",
        "https://example.com.evil.test/",
        "https://evil.test/example.com",
        "https://evil.test/?u=https://example.com",
        "https://evil.test#.example.com",
        "https://example.com@evil.test/",
        "https://user:pass@example.com/",
        "https://evil.test\\@example.com/",
        "https://evil.test%23.example.com/",
        "https://example.com%2eevil.test/",
        "https://example.com./",
        "https://example.com:/",
        "https://example.com:80@evil.test/",
        "https://example.com\t.evil.test/",
        "https://\u212a.example.com/",
        "https://[::1]/",
        "https://com/",
        "https:example.com",
        "//example.com/",
        "example.com",
        "ftp://example.com/",
        "file:///etc/hosts",
        "javascript:alert(1)//example.com",
        "data:text/html,https://example.com/",
        "view-source:https://example.com/",
        "chrome://settings/",
        "chrome-extension://example.com/popup.html",
        "blob:https://example.com/1",
        "about:blank#example.com",
        "about:srcdoc",
        "",
    ],
)
def test_other_hosts_schemes_and_look_alikes_are_not_allowed(url: str) -> None:
    assert not real_browser.permitted(url, ALLOWED)


def test_nothing_but_a_blank_tab_is_allowed_until_a_site_is_added() -> None:
    assert not real_browser.permitted("https://example.com/", [])
    assert real_browser.permitted("about:blank", [])


def run(store: Store, *arguments: str) -> int:
    return cli.execute(cli.parser().parse_args(["browser", *arguments]), store)


def test_owner_adds_lists_and_removes_sites(store: Store, capsys: pytest.CaptureFixture) -> None:
    assert run(store, "sites") == 0
    assert "No site is allowed yet" in capsys.readouterr().out
    assert real_browser.sites(store) == []

    assert run(store, "allow", "Studio.Example.COM", "https://example.org:8443/a?b#c") == 0
    assert "studio.example.com" in capsys.readouterr().out
    assert run(store, "allow", "example.org", "localhost:3000") == 0
    assert real_browser.sites(store) == ["example.org", "localhost", "studio.example.com"]
    saved = json.loads((store.root / "browser/settings.json").read_text())
    assert saved["real_sites"] == ["example.org", "localhost", "studio.example.com"]

    capsys.readouterr()
    assert run(store, "sites") == 0
    assert capsys.readouterr().out == "example.org\nlocalhost\nstudio.example.com\n"

    assert run(store, "disallow", "http://EXAMPLE.org/", "never-added.test") == 0
    out = capsys.readouterr().out
    assert "no longer use example.org" in out and "never-added.test was not" in out
    assert real_browser.sites(store) == ["localhost", "studio.example.com"]


def test_allowing_sites_keeps_the_other_browser_settings(store: Store) -> None:
    real_browser.save_settings(store, installed=True, default="Profile 3")
    assert run(store, "allow", "example.com") == 0
    assert real_browser.settings(store) == {
        "installed": True,
        "default": "Profile 3",
        "real_sites": ["example.com"],
    }


@pytest.mark.parametrize(
    "value",
    ["", "   ", "https://", "*.example.com", "exa mple.com", "ftp://example.com", "file:///tmp/x"],
)
def test_allow_rejects_what_names_no_site(store: Store, value: str) -> None:
    with pytest.raises(SwitchError, match="is not a site"):
        run(store, "allow", "example.com", value)
    assert real_browser.sites(store) == []


@pytest.mark.parametrize(
    ("written", "opened"),
    [
        ("example.com", "https://example.com"),
        ("example.com/a?b=1#c", "https://example.com/a?b=1#c"),
        ("localhost", "http://localhost"),
        ("localhost/x", "http://localhost/x"),
        ("127.0.0.1:8000/x", "http://127.0.0.1:8000/x"),
        ("[::1]:8000", "http://[::1]:8000"),
        ("http://example.com", "http://example.com"),
        ("HTTPS://example.com", "HTTPS://example.com"),
        ("about:blank", "about:blank"),
        ("localhost:3000", "localhost:3000"),
        ("javascript:alert(1)", "javascript:alert(1)"),
        ("//example.com", "https:////example.com"),
    ],
)
def test_addresses_are_completed_the_way_upstream_completes_them(written: str, opened: str) -> None:
    assert real_browser.address(written) == opened


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
    return real_browser.plan(command, ALLOWED, tabs, current)


@pytest.mark.parametrize(
    ("command", "sends"),
    [
        ("goto example.com", ['goto "https://example.com"']),
        ("goto   https://studio.example.com/a(b", ['goto "https://studio.example.com/a(b"']),
        ('goto "https://example.com/q?x=\\"1\\""', ['goto "https://example.com/q?x=\\"1\\""']),
        ("goto https://example.com/é", ['goto "https://example.com/\\u00e9"']),
        ("goto about:blank", ['goto "about:blank"']),
        ("tab new", ["tab new"]),
        ("tab new localhost/x", ["tab new", 'goto "http://localhost/x"']),
        ('tab new --browser "Work Chrome"', ['tab new --browser "Work Chrome"']),
        (
            "tab new example.com --browser b2",
            ['tab new --browser "b2"', 'goto "https://example.com"'],
        ),
        (
            "tab --browser b2 new example.com",
            ['tab new --browser "b2"', 'goto "https://example.com"'],
        ),
        ("tab close", ["tab close"]),
        ("tab close 12", ["tab close 12"]),
        ("tab close 99", ["tab close 99"]),
        ("tab select 12", ["tab select 12"]),
        ('tab "select" "12"', ["tab select 12"]),
    ],
)
def test_addresses_and_tab_commands_are_rebuilt_for_upstream(
    command: str, sends: list[str]
) -> None:
    assert plan(command) == real_browser.Plan(sends=tuple(sends))


def test_a_goto_with_no_current_tab_of_the_chats_own_opens_one_first() -> None:
    opened = ("tab new", 'goto "https://example.com/a"')
    assert plan("goto example.com/a", tabs=(), current=None).sends == opened
    assert plan("goto example.com/a", tabs=("12",), current=None).sends == opened
    assert plan("goto example.com/a", tabs=("12",), current="12").sends == opened[1:]
    for off_list in ("goto other.test", "goto file:///etc/hosts"):
        refused = plan(off_list, tabs=(), current=None)
        assert refused.refusal and not refused.sends


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
        "tab new other.test example.com",
        "tab new --browser",
        "tab new --browser=b2 other.test",
        "tab new (https://other.test )",
        "tab new 'https://other.test'",
        "tab select 7",
        "tab select 012",
        "tab select 12 7",
        "tab select",
        "tab select --browser b2 12",
        "tab close 12 7",
        "tab close all",
        "tab close --browser b2",
        "goto",
        "goto other.test",
        "goto example.com other.test",
        "goto --wait example.com",
        "goto https://example.com@other.test",
        "goto https://other.test\\@example.com",
        "goto https://example.com/a b",
        "goto 'example.com'",
        "goto javascript:alert(1)",
        "goto file:///etc/hosts",
        "goto view-source:https://example.com",
        "goto localhost:3000",
        "goto HTTPS://other.test",
        "tab new localhost:3000/x",
        "request",
        "request 5",
        "request 5 --body",
    ],
)
def test_other_addresses_tab_commands_and_request_details_are_refused(command: str) -> None:
    decided = plan(command)
    assert decided.refusal and not decided.sends and not decided.reply


def test_refusals_tell_the_agent_what_the_owner_can_do() -> None:
    assert "ds browser allow other.test" in plan("goto https://Other.test/a").refusal
    assert "ds browser allow other.test" in plan("tab new other.test").refusal
    assert "only http and https" in plan("goto file:///etc/hosts").refusal
    assert "not one this chat opened" in plan("tab select 7").refusal
    assert "cookies and tokens" in plan("request 5").refusal


@pytest.mark.parametrize(
    "command", ["back", "forward", "reload", "help", "help tab", "browser list", 'browser use "b2"']
)
def test_commands_that_touch_no_page_content_are_sent_as_they_are(command: str) -> None:
    assert plan(command) == real_browser.Plan(sends=(command,))


@pytest.mark.parametrize(
    "command",
    [
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
def test_every_other_command_needs_an_allowed_page(command: str) -> None:
    assert plan(command) == real_browser.Plan(sends=(command,), page=True)


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


def allow(store: Store, *names: str) -> None:
    real_browser.save_settings(store, real_sites=list(names))


def control(upstream: Path, **settings: object) -> None:
    (upstream / "control.json").write_text(json.dumps(settings))


def test_instructions_carry_the_real_mode_note_and_everything_else_passes_through(
    chat: Chat, upstream: Path
) -> None:
    assert chat.instructions == (f"{real_browser.NOTE}\n\nUpstream instructions: act in batches.")
    assert "real Chrome" in real_browser.NOTE and "ds browser allow" in real_browser.NOTE
    assert "Never read or print cookies, tokens or passwords" in real_browser.NOTE
    assert chat.request("tools/list")["result"] == {
        "tools": [{"name": "browser", "inputSchema": {"type": "object"}}]
    }
    assert chat.request("ping")["result"] == {"answered": "ping"}
    assert (upstream / "notifications").read_text() == "notifications/initialized\n"
    text, error = chat.run("upload report.pdf")
    assert not error and text == "✓ upload report.pdf\nroot file:///work"


def test_addresses_off_the_allowlist_never_reach_the_browser(
    chat: Chat, store: Store, upstream: Path
) -> None:
    for command in ("goto example.com", "tab new https://example.com/watch"):
        text, error = chat.run(command)
        assert error and "ds browser allow example.com" in text
        assert text.startswith(f"✗ step 1: {command} — example.com is not an allowed site")
    allow(store, "example.com")
    for command in (
        "goto other.test",
        "tab new https://notexample.com",
        "tab new https://example.com.other.test/",
        "goto https://example.com@other.test/",
        "goto file:///etc/hosts",
        "tab new javascript:alert(1)",
        'goto "https://other.test/"',
        "tab --browser b2 new other.test",
    ):
        text, error = chat.run(command)
        assert error and text.startswith("✗ step 1: "), command
    assert sent(upstream) == []

    text, error = chat.run("goto studio.example.com/watch?v=1")
    assert not error
    assert text == (
        "✓ tab new — tab 501\n"
        '✓ goto "https://studio.example.com/watch?v=1"\n'
        'Page: https://studio.example.com/watch?v=1 — "Title of studio.example.com"'
    )
    text, error = chat.run("tab new example.com")
    assert not error
    assert text == (
        "✓ tab new — tab 502\n"
        '✓ goto "https://example.com"\n'
        'Page: https://example.com — "Title of example.com"'
    )
    assert sent(upstream) == [
        "tab new",
        'goto "https://studio.example.com/watch?v=1"',
        "tab new",
        'goto "https://example.com"',
    ]


def test_a_blank_tab_needs_no_allowed_site(chat: Chat, upstream: Path) -> None:
    text, error = chat.run("tab new\nget url\ngoto about:blank")
    assert not error
    assert text.split("\n")[:3] == ["✓ tab new — tab 501", "✓ get url", "about:blank"]
    assert sent(upstream) == ["tab new", "get url", "get url", 'goto "about:blank"']


def test_the_owners_tabs_are_never_listed_or_selected(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
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
    assert sent(upstream)[len(before) :] == ["tab select 501", "get url", "get url"]

    other = Chat(store)
    listed, _ = other.run("tab list")
    text, error = other.run("tab select 501")
    other.close()
    assert listed.startswith("✓ tab list\nThis chat has no tab open.")
    assert error and "not one this chat opened" in text


def test_a_chat_can_always_get_back_to_a_tab_it_opened(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    assert chat.run("goto example.com") == (
        "✓ tab new — tab 501\n"
        '✓ goto "https://example.com"\n'
        'Page: https://example.com — "Title of example.com"',
        False,
    )
    assert not chat.run("goto example.com/next")[1]
    assert sent(upstream) == [
        "tab new",
        'goto "https://example.com"',
        'goto "https://example.com/next"',
    ]
    assert not chat.run("tab new example.com/\ntab close")[1]
    assert chat.run("tab list") == (f"✓ tab list\n  501\n{LISTED}", False)
    before = sent(upstream)
    text, error = chat.run("tab select 501\ngoto example.com/again\nget url")
    assert not error
    assert text == (
        '✓ tab select 501\n✓ goto "https://example.com/again"\n'
        "✓ get url\nhttps://example.com/again\n"
        'Page: https://example.com/again — "Title of example.com"'
    )
    assert sent(upstream)[len(before) :] == [
        "tab select 501",
        'goto "https://example.com/again"',
        "get url",
        "get url",
    ]


def test_a_closed_tab_is_forgotten_and_the_next_goto_opens_a_new_one(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
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
        f'✓ tab new — tab 504\n✓ goto "https://example.com/d"\n✓ tab list\n  502\n* 504\n{LISTED}'
    )
    assert sent(upstream)[len(before) :] == ["tab new", 'goto "https://example.com/d"']


def test_a_tab_whose_page_fails_to_load_is_still_known(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    for command in ("goto down.example.com", "tab new down.example.com"):
        text, error = chat.run(f"{command}\nget title")
        assert error and "✓ tab new — tab " in text
        assert text.split("\n")[1:] == [
            '✗ step 1: goto "https://down.example.com" — net::ERR_CONNECTION_REFUSED at '
            "https://down.example.com",
            "Step 2 not run",
            'Page: about:blank — ""',
        ]
    assert chat.run("tab list") == (f"✓ tab list\n  501\n* 502\n{LISTED}", False)
    before = sent(upstream)
    assert chat.run("goto example.com") == (
        '✓ goto "https://example.com"\nPage: https://example.com — "Title of example.com"',
        False,
    )
    assert sent(upstream)[len(before) :] == ['goto "https://example.com"']


def test_tab_list_never_reaches_upstream_and_the_batch_goes_on(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    text, error = chat.run("tab list\ntab new example.com\ntab list\nget title")
    assert not error
    assert text == (
        "✓ tab list\nThis chat has no tab open. Open one with: tab new <url>\n"
        "The owner's own tabs are never listed.\n"
        '✓ tab new — tab 501\n✓ goto "https://example.com"\n'
        f"✓ tab list\n* 501\n{LISTED}\n"
        "✓ get title\nTitle of example.com\n"
        'Page: https://example.com — "Title of example.com"'
    )
    assert "owner.test" not in text
    assert sent(upstream) == ["tab new", 'goto "https://example.com"', "get url", "get title"]


def test_a_requests_headers_and_body_are_never_shown(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    chat.run("tab new example.com")
    before = sent(upstream)
    for command in ("request 5", "request 5 --body"):
        assert chat.run(command) == (
            f"✗ step 1: {command} — {real_browser.NO_REQUEST}",
            True,
        )
    assert sent(upstream) == before
    assert chat.run("requests") == ("✓ requests\n5 GET 200 https://example.com/api", False)
    assert sent(upstream)[len(before) :] == ["get url", "requests"]


def test_a_click_that_leaves_the_allowed_sites_stops_the_batch_and_blocks_the_tab(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    control(upstream, moves={"click": "https://other.test/landing"})
    text, error = chat.run("tab new example.com\nclick e1\nget text\nsnapshot")
    assert error
    assert text == (
        '✓ tab new — tab 501\n✓ goto "https://example.com"\n'
        "✓ click e1\n"
        f"✗ after step 2: {real_browser.off_site('https://other.test/landing')}\n"
        "Steps 3–4 not run\n"
        'Page: https://other.test/landing — "Title of other.test"'
    )
    assert "The tab is on other.test, which is not an allowed site" in text
    assert "ds browser allow other.test" in text and "`back`" in text and "tab close" in text
    assert sent(upstream) == ["tab new", 'goto "https://example.com"', "get url", "click e1"]

    for command in ("snapshot", "get text", 'eval "document.title"', "click e2", "screenshot"):
        result = chat.browser(command)
        text = result["content"][0]["text"]
        assert result["isError"] and len(result["content"]) == 1
        assert text == f"✗ step 1: {command} — {real_browser.off_site('https://other.test/x')}"
    assert sent(upstream)[4:] == ["get url"] * 5

    text, error = chat.run("tab close 7\nhelp")
    assert error and text.split("\n") == [
        "✗ step 1: tab close 7 — Tab 7 isn't one you opened, so it stays open. Switch away "
        "with `tab select <id>`",
        f"✗ after step 1: {real_browser.off_site('https://other.test/landing')}",
        "Step 2 not run",
        'Page: https://other.test/landing — "Title of other.test"',
    ]
    text, error = chat.run("back\nsnapshot")
    assert not error and "snapshot of https://example.com" in text
    assert sent(upstream)[9:] == ["tab close 7", "back", "get url", "snapshot"]


def test_allowing_the_site_unblocks_the_same_chat_without_a_restart(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    control(upstream, moves={"click": "https://app.other.test/landing"})
    chat.run("tab new example.com\nclick e1")
    text, error = chat.run("snapshot")
    assert error and "ds browser allow app.other.test" in text
    assert cli.execute(cli.parser().parse_args(["browser", "allow", "other.test"]), store) == 0
    text, error = chat.run("snapshot")
    assert not error and text == "✓ snapshot\nsnapshot of https://app.other.test/landing"
    assert cli.execute(cli.parser().parse_args(["browser", "disallow", "other.test"]), store) == 0
    assert chat.run("snapshot")[1]


def test_a_batch_reaches_upstream_one_command_per_call_in_order(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    result = chat.browser(
        "# open it\r\ntab new example.com\r\n\r\n  get title  \nhelp\nscreenshot\nget url\n"
    )
    assert result["isError"] is False
    assert result["content"] == [
        {
            "type": "text",
            "text": (
                '✓ tab new — tab 501\n✓ goto "https://example.com"\n'
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
        'goto "https://example.com"',
        "get url",
        "get title",
        "help",
        "get url",
        "screenshot",
        "get url",
        "get url",
    ]
    assert chat.browser(["goto example.com/b\nget title", "back"])["isError"] is False
    assert sent(upstream)[9:] == ['goto "https://example.com/b"', "get url", "get title", "back"]


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


def test_only_the_last_page_line_is_kept(chat: Chat, store: Store, upstream: Path) -> None:
    allow(store, "example.com")
    text, _ = chat.run("tab new example.com\ngoto example.com/b\nget title\ntab close")
    assert text.count("Page: ") == 1
    assert text.endswith('✓ tab close\nPage: https://example.com/b — "Title of example.com"')


def test_failures_are_numbered_by_their_place_in_the_batch(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    text, error = chat.run("tab new example.com\nget title\nclick missing\nget url\nsnapshot")
    assert error
    assert text == (
        '✓ tab new — tab 501\n✓ goto "https://example.com"\n'
        "✓ get title\nTitle of example.com\n"
        "✗ step 3: click missing — No element matches missing\n"
        "Steps 4–5 not run\n"
        'Page: https://example.com — "Title of example.com"'
    )
    assert sent(upstream)[-2:] == ["get url", "click missing"]

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
    text, error = chat.run("get title\ngoto other.test")
    assert error and "✗ step 2: goto other.test — other.test is not" in text
    assert "not run" not in text


@pytest.mark.parametrize("message", [PAUSED, NOT_CONNECTED])
def test_upstream_errors_come_back_unchanged(
    chat: Chat, store: Store, upstream: Path, message: str
) -> None:
    allow(store, "example.com")
    control(upstream, error=message)
    for command in ("snapshot", "tab new example.com", "back"):
        assert chat.run(command) == (message, True)
    assert chat.run("get title\nsnapshot") == (f"{message}\nStep 2 not run", True)
    assert sent(upstream)[-2:] == ["get url", "get title"]


def test_a_late_redirect_is_seen_before_the_next_command_reads_the_page(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    control(upstream, moves={"click": "https://other.test/late"}, late=True)
    text, error = chat.run("tab new example.com\nclick e1\nsnapshot\nget text")
    assert error and "snapshot of" not in text
    assert text.split("\n")[2:] == [
        "✓ click e1",
        f"✗ step 3: snapshot — {real_browser.off_site('https://other.test/late')}",
        "Step 4 not run",
        'Page: https://example.com — "Title of example.com"',
    ]
    assert sent(upstream) == [
        "tab new",
        'goto "https://example.com"',
        "get url",
        "click e1",
        "get url",
    ]


def test_what_a_command_read_while_leaving_the_allowed_sites_is_withheld(
    chat: Chat, store: Store, upstream: Path
) -> None:
    allow(store, "example.com")
    moved = "https://other.test/private"
    control(upstream, moves={"eval": moved, "screenshot": moved})
    for command in ('eval "document.body.innerText"', "screenshot"):
        chat.run("tab new example.com")
        result = chat.browser(f"{command}\nget title")
        assert result["isError"] and len(result["content"]) == 1
        lines = result["content"][0]["text"].split("\n")
        assert lines[0].startswith(f"✓ {command}")
        assert lines[1:] == [
            f"✗ after step 1: {real_browser.off_site(moved)}",
            "Step 2 not run",
            'Page: https://other.test/private — "Title of other.test"',
        ]


def test_a_probe_answer_that_names_no_page_blocks_the_command(store: Store) -> None:
    proxy = real_browser.Proxy(store)
    asked = []

    async def ask(command: str) -> real_browser.Answer:
        asked.append(command)
        return real_browser.Answer("✓ get url")

    proxy.ask = ask
    [blocked] = asyncio.run(proxy.step(1, "snapshot", ["example.com"]))
    assert blocked.failed and blocked.text.startswith("✗ step 1: snapshot — The tab is on")
    assert asked == ["get url"]


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
