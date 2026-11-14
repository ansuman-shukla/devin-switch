"""Real mode: agents drive the owner's own Chrome through the Browser MCP extension.

Websites can tell the agent browser's headless copy is a robot, so real mode works in the
Chrome the owner is signed into. The owner keeps two locks: the extension's own Stop/Resume
button, and the site allowlist kept here, which starts empty.

`ds browser real-mcp` is a per-chat stdio proxy around the pinned `@browsermcp/mcp` server. It
splits a chat's batch and sends upstream one command per call, so every command passes the
policy first: only allowed sites are opened, read or acted on, and only in tabs the chat opened.
"""

import asyncio
import json
import re
from collections.abc import Collection, Sequence
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
BLANK = "about:blank"
PROBE = "get url"
# Commands that neither read nor act on a page's content, so they need no allowed page.
FREE = ("back", "forward", "reload", "help")
ECHO_LIMIT = 300
# What JavaScript's trim() and \s count as white space: the upstream parser splits on these.
SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
EDGES = re.compile(rf"^[{SPACE}]+|[{SPACE}]+$")
GAP = re.compile(rf"[{SPACE}]")
WORD = re.compile(rf'"(?:[^"\\]|\\.)*"(?=[{SPACE}]|$)|[^{SPACE}]+')
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
LOCAL = ("localhost", "127.", "[::1]")
WEB = re.compile(
    rf"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)*)(?::[0-9]+)?(?:[/?#][^{SPACE}]*)?",
    re.ASCII | re.IGNORECASE,
)
TAB_ID = re.compile(r"[0-9]+")
PAGE = re.compile(r'Page: (.*?) — ".*"')
FIRST_STEP = re.compile(r"^✗ step 1:", re.MULTILINE)
NOTHING_RAN = "Nothing ran: fix this line and send the call again"
NO_SITES = "No site is allowed yet, so real mode opens nothing. Add one: ds browser allow SITE"
OWN_TABS = "Real mode works only in tabs it opened, never in the owner's own tabs."
NOT_LISTED = "The owner's own tabs are never listed."
TAB_USAGE = (
    'Real mode takes: tab list, tab new [<url>] [--browser "<name>"], tab select <id> for a '
    "tab it opened, and tab close [<id>]."
)
NO_REQUEST = (
    "Real mode never shows a request's headers or body, because they hold the owner's cookies "
    "and tokens. `requests` lists the requests."
)
STOPPED = "The Browser MCP server behind real mode stopped."
NOTE = (
    "This is the owner's real Chrome, signed in as them, not the test browser. Use it only when "
    "the task says to use the real browser or needs a site the test browser cannot do. It works "
    "only on sites the owner allowed (`ds browser allow SITE`) and only in tabs you open: `tab "
    "list` shows just those, and the owner's own tabs cannot be selected. Before paying, "
    "sending, posting, deleting or changing account settings, ask the owner, unless the task "
    "itself asked for exactly that action. If a call says the owner paused Browser MCP, stop "
    "and wait for them. Never sign in or out. Never read or print cookies, tokens or "
    "passwords. Close your tabs when the work is done."
)


def web_host(url: str) -> str | None:
    """The host of a plain http(s) URL; None for every other scheme and anything unusual.

    Narrow on purpose (no user name, no escapes, only letters, digits, dots and hyphens in the
    host), so this reading of a URL and Chrome's can never name different hosts.
    """
    found = WEB.fullmatch(url)
    return found[1].lower() if found else None


def permitted(url: str, allowed: Collection[str]) -> bool:
    """Whether real mode may open, read and act on this URL: a blank tab or an allowed site."""
    if url == BLANK:
        return True
    host = web_host(url)
    return host is not None and any(host == site or host.endswith(f".{site}") for site in allowed)


def sites(store: Store) -> list[str]:
    value = settings(store).get("real_sites")
    return [site for site in value if isinstance(site, str)] if isinstance(value, list) else []


def note(allowed: Sequence[str]) -> str:
    """The real-mode note for a new chat, naming the sites it may use so every agent knows."""
    listed = (
        f"Sites allowed when this chat started: {', '.join(allowed)} (their subdomains too)."
        if allowed
        else "No site was allowed when this chat started."
    )
    return f"{NOTE} {listed} `ds browser sites` shows the list as it is now."


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


def address(value: str) -> str:
    """The URL the upstream server opens for what follows `goto` or `tab new`."""
    if SCHEME.match(value):
        return value
    return f"{'http' if value.startswith(LOCAL) else 'https'}://{value}"


def echo(command: str) -> str:
    """A command as upstream echoes it in its answer: cut after ECHO_LIMIT characters."""
    return command if len(command) <= ECHO_LIMIT else f"{command[: ECHO_LIMIT - 1]}…"


def off_site(url: str) -> str:
    """Why a tab that left the allowed sites is closed to reads and actions."""
    host = web_host(url)
    owner = f", or ask the owner to allow it with: ds browser allow {host}" if host else ""
    return (
        f"The tab is on {host or url}, which is not an allowed site, so reads and actions are "
        f"blocked there. Go `back`, close it with `tab close`{owner}"
    )


@dataclass(frozen=True)
class Plan:
    """The policy's answer for one command: what goes upstream (one call each), a reply made
    here without upstream, or a refusal."""

    sends: tuple[str, ...] = ()
    reply: str = ""
    refusal: str = ""
    page: bool = False


def visit(opening: tuple[str, ...], target: str, allowed: Collection[str]) -> Plan:
    """Open an allowed address, after `opening` a blank tab for it when one is needed.

    The URL goes as a JSON string, so upstream reads exactly the URL that was checked. A new
    tab is opened blank first: upstream names a tab only once its page has loaded, so a load
    that fails would leave the chat with a tab it cannot name.
    """
    url = address(target)
    if permitted(url, allowed):
        return Plan(sends=(*opening, f"goto {json.dumps(url)}"))
    host = web_host(url)
    if host is None:
        return Plan(refusal=f"Real mode opens only http and https pages, not {url}")
    return Plan(
        refusal=f"{host} is not an allowed site. The owner can allow it with: "
        f"ds browser allow {host}"
    )


def listing(tabs: Sequence[str], current: str | None) -> str:
    """What `tab list` shows in real mode: the tabs this chat opened and still has."""
    if not tabs:
        return f"This chat has no tab open. Open one with: tab new <url>\n{NOT_LISTED}"
    lines = [f"{'*' if tab == current else ' '} {tab}" for tab in tabs]
    return "\n".join([*lines, f"These are the tabs this chat opened; * is current. {NOT_LISTED}"])


def tab(
    arguments: list[str], allowed: Collection[str], tabs: Sequence[str], current: str | None
) -> Plan:
    flag = ""
    if "--browser" in arguments[:-1]:
        at = arguments.index("--browser")
        flag = f" --browser {json.dumps(arguments[at + 1])}"
        arguments = arguments[:at] + arguments[at + 2 :]
    action, rest = arguments[:1], arguments[1:]
    if action == ["list"]:
        return Plan(reply=listing(tabs, current))
    if action == ["new"] and not rest:
        return Plan(sends=(f"tab new{flag}",))
    if action == ["new"] and len(rest) == 1:
        return visit((f"tab new{flag}",), rest[0], allowed)
    if flag or len(rest) > 1 or not all(TAB_ID.fullmatch(word) for word in rest):
        return Plan(refusal=TAB_USAGE)
    if action == ["close"]:
        return Plan(sends=(" ".join(["tab close", *rest]),))
    if action == ["select"] and rest:
        if rest[0] in tabs:
            return Plan(sends=(f"tab select {rest[0]}",))
        return Plan(refusal=f"Tab {rest[0]} is not one this chat opened. {OWN_TABS}")
    return Plan(refusal=TAB_USAGE)


def plan(command: str, allowed: Collection[str], tabs: Sequence[str], current: str | None) -> Plan:
    """What to do with one command, given the chat's own open tabs and its current one.

    `goto` and `tab` are rebuilt from what was understood, so the check and upstream can never
    read them differently; anything about them that is not understood is refused. A `goto`
    with no current tab of the chat's own opens one first, so its id is known.
    """
    name = GAP.split(command, maxsplit=1)[0]
    arguments = words(command[len(name) :])
    if name == "goto":
        if len(arguments) != 1:
            return Plan(refusal="goto takes one address, like: goto https://example.com")
        return visit(() if current else ("tab new",), arguments[0], allowed)
    if name == "tab":
        return tab(arguments, allowed, tabs, current)
    if name == "request":
        return Plan(refusal=NO_REQUEST)
    if name in FREE or (name == "browser" and arguments[:1] in (["list"], ["use"])):
        return Plan(sends=(command,))
    return Plan(sends=(command,), page=True)


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
    """Serve @browsermcp/mcp to one chat, behind the owner's site allowlist."""

    def __init__(self, store: Store) -> None:
        self.store = store
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

    async def current(self) -> str | None:
        """The URL of the tab the next command acts on; None when upstream has none to name.

        Asked before every page command: upstream reports a new page only when it changed
        during a call, so a late redirect or a tab the owner handed over would go unseen.
        """
        probe = await self.ask(PROBE)
        if probe.failed:
            return None
        lines = probe.text.split("\n")
        return lines[1] if len(lines) > 1 else probe.text

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

    async def forward(self, number: int, sent: str, page: bool, allowed: Collection[str]) -> Answer:
        """Run one command upstream, then check where it left the tab."""
        result = await self.ask(sent)
        if result.failed:
            result.text = renumbered(result.text, number)
        else:
            self.track(sent, result.text)
        landed = PAGE.fullmatch(result.page)[1] if result.page else None
        if landed is None or permitted(landed, allowed):
            return result
        kept = result.text.split("\n")
        if page:
            # What it read may be the page it left the allowed sites for: only its ✓ line stays.
            kept = [line for line in kept[:1] if line.startswith("✓ ")]
        note = f"✗ after step {number}: {off_site(landed)}"
        return Answer("\n".join([*kept, note]), result.page, failed=True)

    async def step(self, number: int, command: str, allowed: Collection[str]) -> list[Answer]:
        decided = plan(command, allowed, self.tabs, self.tab)
        refusal = decided.refusal
        if decided.page:
            url = await self.current()
            if url is not None and not permitted(url, allowed):
                refusal = off_site(url)
        if refusal:
            return [Answer(f"✗ step {number}: {echo(command)} — {refusal}", failed=True)]
        if decided.reply:
            return [Answer(f"✓ {echo(command)}\n{decided.reply}")]
        parts: list[Answer] = []
        for sent in decided.sends:
            parts.append(await self.forward(number, sent, decided.page, allowed))
            if parts[-1].failed:
                break
        return parts

    async def batch(self, commands: list[str]) -> dict:
        if not commands:
            return tool_result("No commands. Run `help`", error=True)
        allowed = sites(self.store)
        parts: list[Answer] = []
        for number, command in enumerate(commands, 1):
            parts += await self.step(number, command, allowed)
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
                head, upstream = note(sites(self.store)), result.get("instructions")
                result["instructions"] = f"{head}\n\n{upstream}" if upstream else head
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
        asyncio.run(Proxy(store).run())
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
