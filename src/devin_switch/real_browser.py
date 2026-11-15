"""Real mode: agents drive the owner's own Chrome through the Browser MCP extension.

Websites can tell the agent browser's headless copy is a robot, so real mode works in the
Chrome the owner is signed into, on any site. The owner's switch is the extension's own
Stop/Resume button.

`ds browser real-mcp` is a per-chat stdio proxy around the pinned `@browsermcp/mcp` server. It
splits a chat's batch and sends upstream one command per call, so it learns the id of every
tab the chat opens before the next command runs: agents work only in tabs they opened, and a
request's headers and body are never shown.
"""

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from devin_switch.agent_browser import (
    LINE_LIMIT,
    messages,
    package_command,
    save_settings,
    send,
    settings,
    stdin_reader,
    tool_result,
    write,
)
from devin_switch.store import Store, SwitchError

MCP_PACKAGE = "@browsermcp/mcp@2.4.0"
OVERRIDE = "DS_REAL_BROWSER_MCP"
TOOL = "browser"
ACTIONS = ("allow", "disallow", "sites", "real-mcp")
ECHO_LIMIT = 300
# What JavaScript's trim() and \s count as white space: the upstream parser splits on these.
SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
EDGES = re.compile(rf"^[{SPACE}]+|[{SPACE}]+$")
GAP = re.compile(rf"[{SPACE}]")
WORD = re.compile(rf'"(?:[^"\\]|\\.)*"(?=[{SPACE}]|$)|[^{SPACE}]+')
WEB = re.compile(
    rf"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)*)(?::[0-9]+)?(?:[/?#][^{SPACE}]*)?",
    re.ASCII | re.IGNORECASE,
)
TAB_ID = re.compile(r"[0-9]+")
PAGE = re.compile(r'Page: .* — ".*"')
FIRST_STEP = re.compile(r"^✗ step 1:", re.MULTILINE)
NOTHING_RAN = "Nothing ran: fix this line and send the call again"
NO_SITES = "No site is allowed yet, so real mode opens nothing. Add one: ds browser allow SITE"
OWN_TABS = "Real mode works only in tabs it opened, never in the owner's own tabs."
NOT_LISTED = "The owner's own tabs are never listed."
TAB_USAGE = (
    'Real mode takes: tab list, tab new [<url>] [--browser "<name>"], tab select <id> for a '
    "tab it opened, and tab close [<id>]."
)
ONE_ADDRESS = "goto takes one address, like: goto https://example.com"
NO_REQUEST = (
    "Real mode never shows a request's headers or body, because they hold the owner's cookies "
    "and tokens. `requests` lists the requests."
)
STOPPED = "The Browser MCP server behind real mode stopped."
NOTE = (
    "This is the owner's real Chrome, signed in as them, not the test browser. Use it only when "
    "the task says to use the real browser or needs a site the test browser cannot do. It can "
    "open any site, but only in tabs you open: `tab list` shows just those, and the owner's own "
    "tabs cannot be selected. Several Chrome profiles can be connected: `browser list` names "
    'them and `browser use "<name>"` chooses where your next tabs open; a profile is connected '
    "only while it is open in Chrome and has the extension. Before paying, sending, posting, "
    "deleting or changing account settings, ask the owner, unless the task itself asked for "
    "exactly that action. If a call says the owner paused Browser MCP, stop and wait for them. "
    "Never sign in or out. Never read or print cookies, tokens or passwords. Close your tabs "
    "when the work is done."
)


def web_host(url: str) -> str | None:
    """The host of a plain http(s) URL; None for every other scheme and anything unusual.

    Narrow on purpose (no user name, no escapes, only letters, digits, dots and hyphens in the
    host), so this reading of a URL and Chrome's can never name different hosts.
    """
    found = WEB.fullmatch(url)
    return found[1].lower() if found else None


def sites(store: Store) -> list[str]:
    value = settings(store).get("real_sites")
    return [site for site in value if isinstance(site, str)] if isinstance(value, list) else []


def named_site(value: str) -> str:
    """The host the owner named, given as a host or as a full address."""
    text = value.strip()
    host = web_host(text if "://" in text else f"https://{text}")
    if host is None:
        raise SwitchError(
            f"{value!r} is not a site. Name a host such as example.com (its subdomains are "
            "included) or paste an http(s) address."
        )
    return host


def split(commands: object) -> list[str]:
    """The command lines of one call, split the way the upstream server splits them."""
    entries = [commands] if isinstance(commands, str) else commands
    if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
        raise SwitchError("commands is a string of command lines, one per line.")
    lines = (EDGES.sub("", line) for entry in entries for line in re.split(r"\r?\n", entry))
    return [line for line in lines if line and not line.startswith("#")]


def words(text: str) -> list[str]:
    """A command's arguments; one in double quotes is the JSON string upstream reads it as."""
    found = []
    for word in WORD.findall(text):
        try:
            found.append(json.loads(word) if word.startswith('"') else word)
        except ValueError:
            found.append(word)
    return found


def echo(command: str) -> str:
    """A command as upstream echoes it in its answer: cut after ECHO_LIMIT characters."""
    return command if len(command) <= ECHO_LIMIT else f"{command[: ECHO_LIMIT - 1]}…"


@dataclass(frozen=True)
class Plan:
    """What to do with one command: what goes upstream (one call each), a reply made here
    without upstream, or a refusal."""

    sends: tuple[str, ...] = ()
    reply: str = ""
    refusal: str = ""


def listing(tabs: Sequence[str], current: str | None) -> str:
    """What `tab list` shows in real mode: the tabs this chat opened and still has."""
    if not tabs:
        return f"This chat has no tab open. Open one with: tab new <url>\n{NOT_LISTED}"
    lines = [f"{'*' if tab == current else ' '} {tab}" for tab in tabs]
    return "\n".join([*lines, f"These are the tabs this chat opened; * is current. {NOT_LISTED}"])


def tab(arguments: list[str], tabs: Sequence[str], current: str | None) -> Plan:
    flag = ""
    if "--browser" in arguments:
        at = arguments.index("--browser")
        if at + 1 == len(arguments):
            return Plan(refusal=TAB_USAGE)
        flag = f" --browser {json.dumps(arguments[at + 1])}"
        arguments = arguments[:at] + arguments[at + 2 :]
    action, rest = arguments[:1], arguments[1:]
    if action == ["list"]:
        return Plan(reply=listing(tabs, current))
    if action == ["new"] and not rest:
        return Plan(sends=(f"tab new{flag}",))
    if action == ["new"] and len(rest) == 1:
        # Blank first: upstream names a tab only once its page has loaded, so a load that
        # fails would leave the chat with a tab it cannot name. The address goes as a JSON
        # string, which upstream reads back as the one word that was read here.
        return Plan(sends=(f"tab new{flag}", f"goto {json.dumps(rest[0])}"))
    if flag or len(rest) > 1 or not all(TAB_ID.fullmatch(word) for word in rest):
        return Plan(refusal=TAB_USAGE)
    if action == ["close"]:
        return Plan(sends=(" ".join(["tab close", *rest]),))
    if action == ["select"] and rest:
        if rest[0] in tabs:
            return Plan(sends=(f"tab select {rest[0]}",))
        return Plan(refusal=f"Tab {rest[0]} is not one this chat opened. {OWN_TABS}")
    return Plan(refusal=TAB_USAGE)


def plan(command: str, tabs: Sequence[str], current: str | None) -> Plan:
    """What to do with one command, given the chat's own open tabs and its current one.

    `tab` is rebuilt from what was understood and anything else about it is refused, so the
    chat can only be in a tab whose id is known here. A `goto` with no current tab of the
    chat's own opens a blank one first, for the same reason. Every other command but
    `request` goes upstream as the agent wrote it.
    """
    name = GAP.split(command, maxsplit=1)[0]
    arguments = words(command[len(name) :])
    if name == "goto":
        # Upstream takes one address as well. Refused here, so that a goto which cannot run
        # never opens the blank tab first.
        if len(arguments) != 1:
            return Plan(refusal=ONE_ADDRESS)
        return Plan(sends=(command,) if current else ("tab new", command))
    if name == "tab":
        return tab(arguments, tabs, current)
    if name == "request":
        return Plan(refusal=NO_REQUEST)
    return Plan(sends=(command,))


@dataclass
class Answer:
    """What one command produced: its text without the closing Page line, and that line."""

    text: str
    page: str | None = None
    images: list[dict] = field(default_factory=list)
    failed: bool = False


def answer(message: dict) -> Answer:
    """The upstream server's reply to one command."""
    if isinstance(error := message.get("error"), dict):
        return Answer(str(error.get("message")), failed=True)
    result = message.get("result") or {}
    content = result.get("content") or []
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    body, _, last = text.rpartition("\n")
    page = last if PAGE.fullmatch(last) else None
    images = [item for item in content if item.get("type") != "text"]
    return Answer(body if page else text, page, images, result.get("isError") is True)


def opened_tab(sent: str, text: str) -> str | None:
    """The id of the tab a `tab new` opened, read from upstream's own note after its echo."""
    found = re.match(re.escape(f"✓ {echo(sent)} — tab ") + r"([0-9]+)", text)
    return found[1] if found else None


def renumbered(text: str, number: int) -> str:
    """An upstream failure as step `number` of the chat's batch, not step 1 of its own call."""
    if number == 1:
        return text
    lines = [line for line in text.split("\n") if line != NOTHING_RAN]
    return FIRST_STEP.sub(f"✗ step {number}:", "\n".join(lines))


def joined(parts: list[Answer], ran: int, total: int) -> dict:
    """One result for the chat, laid out like the upstream result of the whole batch."""
    failed = parts[-1].failed
    lines = [part.text for part in parts if part.text]
    if ran < total:
        lines.append(
            f"Step {total} not run" if ran + 1 == total else f"Steps {ran + 1}–{total} not run"
        )
    lines += [part.page for part in parts if part.page][-1:]
    images = [image for part in parts for image in part.images]
    return {"content": [{"type": "text", "text": "\n".join(lines)}, *images], "isError": failed}


def upstream_command() -> tuple[list[str], dict[str, str]]:
    return package_command(OVERRIDE, (MCP_PACKAGE,))


class Proxy:
    """Serve @browsermcp/mcp to one chat, which works only in the tabs it opened."""

    def __init__(self) -> None:
        self.child: asyncio.subprocess.Process | None = None
        self.stopped = False
        self.pending: dict[object, str] = {}
        self.child_requests: set[object] = set()
        self.replies: dict[str, asyncio.Future] = {}
        self.asked = 0
        self.lock = asyncio.Lock()
        self.tabs: list[str] = []
        self.tab: str | None = None
        self.tasks: set[asyncio.Task] = set()

    def background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def ask(self, command: str) -> Answer:
        """Run one command upstream, in a call of its own."""
        if self.stopped:
            raise SwitchError(STOPPED)
        self.asked += 1
        key = f"ds-real-{self.asked}"
        reply = self.replies[key] = asyncio.get_running_loop().create_future()
        params = {"name": TOOL, "arguments": {"commands": command}}
        await write(
            self.child, {"jsonrpc": "2.0", "id": key, "method": "tools/call", "params": params}
        )
        return answer(await reply)

    def track(self, sent: str, text: str) -> None:
        """Follow a tab command that worked, so every tab the chat has open is known by id."""
        if not sent.startswith("tab "):
            return
        action, _, rest = sent.removeprefix("tab ").partition(" ")
        if action == "new":
            if opened := opened_tab(sent, text):
                self.tabs.append(opened)
                self.tab = opened
        elif action == "select":
            self.tab = rest
        elif action == "close":
            closed = rest or self.tab
            self.tabs = [tab for tab in self.tabs if tab != closed]
            if closed == self.tab:
                self.tab = None

    async def step(self, number: int, command: str) -> list[Answer]:
        decided = plan(command, self.tabs, self.tab)
        if decided.refusal:
            return [Answer(f"✗ step {number}: {echo(command)} — {decided.refusal}", failed=True)]
        if decided.reply:
            return [Answer(f"✓ {echo(command)}\n{decided.reply}")]
        parts: list[Answer] = []
        for sent in decided.sends:
            result = await self.ask(sent)
            parts.append(result)
            if result.failed:
                result.text = renumbered(result.text, number)
                break
            self.track(sent, result.text)
        return parts

    async def batch(self, commands: list[str]) -> dict:
        if not commands:
            return tool_result("No commands. Run `help`", error=True)
        parts: list[Answer] = []
        for number, command in enumerate(commands, 1):
            parts += await self.step(number, command)
            if parts[-1].failed:
                break
        return joined(parts, number, len(commands))

    async def call(self, message: dict) -> None:
        params = message.get("params") or {}
        if params.get("name") != TOOL:
            error = {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}
            send({"jsonrpc": "2.0", "id": message["id"], "error": error})
            return
        try:
            commands = split((params.get("arguments") or {}).get("commands"))
            async with self.lock:
                result = await self.batch(commands)
        except SwitchError as exc:
            result = tool_result(str(exc), error=True)
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def from_chat(self, message: dict) -> None:
        method = message.get("method")
        if method == "tools/call":
            if "id" in message:
                self.background(self.call(message))
            return
        if method is None:
            if message.get("id") not in self.child_requests:
                return
            self.child_requests.discard(message["id"])
        elif "id" in message:
            self.pending[message["id"]] = method
        await write(self.child, message)

    def from_child(self, message: dict) -> None:
        key = message.get("id")
        if "method" in message:
            if "id" in message:
                self.child_requests.add(key)
        elif isinstance(key, str) and key in self.replies:
            self.replies.pop(key).set_result(message)
            return
        elif self.pending.pop(key, None) == "initialize":
            result = message.get("result")
            if isinstance(result, dict):
                upstream = result.get("instructions")
                result["instructions"] = f"{NOTE}\n\n{upstream}" if upstream else NOTE
        send(message)

    def fail(self) -> None:
        self.stopped = True
        for reply in self.replies.values():
            reply.set_exception(SwitchError(STOPPED))
        self.replies.clear()
        for key in self.pending:
            send({"jsonrpc": "2.0", "id": key, "error": {"code": -32603, "message": STOPPED}})
        self.pending.clear()

    async def read_chat(self, reader: asyncio.StreamReader) -> None:
        async for message in messages(reader):
            await self.from_chat(message)

    async def read_child(self, reader: asyncio.StreamReader) -> None:
        async for message in messages(reader):
            self.from_child(message)

    async def run(self) -> None:
        reader = await stdin_reader()
        command, environment = upstream_command()
        child = self.child = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
            env=environment,
        )
        assert child.stdin is not None and child.stdout is not None
        chat = asyncio.create_task(self.read_chat(reader))
        upstream = asyncio.create_task(self.read_child(child.stdout))
        try:
            await asyncio.wait({chat, upstream}, return_when=asyncio.FIRST_COMPLETED)
            if upstream.done():
                self.fail()
                await asyncio.gather(*tuple(self.tasks))
                upstream.result()
                raise SwitchError(STOPPED)
            chat.result()
        finally:
            child.stdin.close()
            try:
                await asyncio.wait_for(child.wait(), 5)
            except TimeoutError:
                child.terminate()


def command(args, store: Store) -> int:
    if args.action == "real-mcp":
        asyncio.run(Proxy().run())
        return 0
    if args.action == "sites":
        print("\n".join(sites(store)) or NO_SITES)
        return 0
    named = list(dict.fromkeys(named_site(value) for value in args.sites))
    current = sites(store)
    if args.action == "allow":
        save_settings(store, real_sites=sorted({*current, *named}))
        for site in named:
            print(f"Real mode may use {site} and its subdomains.")
    else:
        save_settings(store, real_sites=[site for site in current if site not in named])
        for site in named:
            print(
                f"Real mode may no longer use {site}."
                if site in current
                else f"{site} was not an allowed site."
            )
    return 0
