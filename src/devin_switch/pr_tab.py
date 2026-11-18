"""The PR tab: the pull requests seen in a GUI chat, in a Browser tab of the chat.

Desktop's own PR tab loads pull requests through Devin's cloud, which cannot see private
repositories. This one is local. The bridge notes every GitHub pull request link that shows up
in a chat: in what the agent and the user wrote, and in what a `gh pr` command was given and
printed. `/pr` typed in the chat opens the page that lists them, served by `side.Server` like
the side page. While a page is open the bridge looks them up with the `gh` CLI, under the
user's own GitHub login. Everything is kept in memory only.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from devin_switch import native, subagent_tab
from devin_switch.store import Store

# Only a pull request itself: not an issue, the list of pulls or a file. A number followed by
# a letter or by more digits than GitHub numbers have is not one.
LINK = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)/pull/"
    r"0*([1-9][0-9]{0,8})(?![A-Za-z0-9])"
)
CHUNKS = ("agent_message_chunk", "user_message_chunk")
STEPS = ("tool_call", "tool_call_update")
COMMAND = "gh pr"
# A tool call's command is written once the call runs; its output is whole once it has ended.
RAN = ("in_progress", "completed", "failed")
ENDED = ("completed", "failed")
MAX_PULLS = 20
MAX_CALLS = 50
MAX_WRITERS = 50
# Longer than any link: the end of a text that may hold one still being written.
TAIL = 200
FIELDS = (
    "number,title,state,isDraft,url,headRefName,baseRefName,headRefOid,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,additions,deletions,changedFiles,"
    "author,updatedAt,latestReviews,reviewRequests"
)
ALLOWED = "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed,viewerDefaultMergeMethod"
# Where Homebrew puts gh: a bridge started by an app from the Dock has no shell PATH.
FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
TIMEOUT = 30
REFRESH = 30
BATCH = 4
ERROR_LIMIT = 200
MISSING = "GitHub CLI (gh) was not found"
UNREADABLE = "gh printed something that is not a pull request"
STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
CONFLICTS = {"CONFLICTING": True, "MERGEABLE": False}
MERGE_STATES = ("clean", "behind", "blocked", "dirty", "draft", "unstable", "has_hooks")
DECISIONS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "REVIEW_REQUIRED": "review_required",
}
REVIEWS = ("approved", "changes_requested", "commented", "dismissed", "pending")
# How a finished check run ended: any other ending (failure, cancelled, timed out) failed.
CONCLUSIONS = {"SUCCESS": "pass", "SKIPPED": "skipped", "NEUTRAL": "skipped"}
# A commit status: any other state (pending, expected) is still pending.
STATUSES = {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail"}
# Each merge method, in the order shown, with what says that a repository allows it.
METHODS = {
    "merge": ("MERGE", "mergeCommitAllowed"),
    "squash": ("SQUASH", "squashMergeAllowed"),
    "rebase": ("REBASE", "rebaseMergeAllowed"),
}


def links(text: str, ended: bool = False) -> tuple[list[str], str]:
    """The pull request links in a text, and the end of it that may hold one more.

    A link that ends the text counts only once the text has `ended`: while it is still being
    written, "…/pull/1" can become "…/pull/123". Links come without what follows the number.
    """
    found, start = [], 0
    for match in LINK.finditer(text):
        if match.end() == len(text) and not ended:
            break
        found.append(f"https://github.com/{match[1]}/{match[2]}/pull/{int(match[3])}")
        start = match.end()
    return found, "" if ended else text[max(start, len(text) - TAIL) :]


def texts(value: object) -> Iterator[str]:
    """Every text inside a tool call's input or output, however it is nested."""
    waiting = [value]
    while waiting:
        item = waiting.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            waiting.extend(item.values())
        elif isinstance(item, list):
            waiting.extend(item)


def written(update: dict) -> str:
    """The text of a message chunk ("": it holds something else)."""
    content = update.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    return text if isinstance(text, str) and content.get("type") == "text" else ""


@dataclass(frozen=True)
class Ran:
    """How one `gh` command ended."""

    code: int
    out: str = ""
    err: str = ""


async def run(store: Store, folder: Path, *arguments: str) -> Ran | None:
    """Run `gh` in a folder as the user would (None: gh is not installed).

    The user's own GitHub login applies, even when the bridge was started inside one login's
    isolated environment. A command that takes too long is killed with all it started.
    """
    found = shutil.which("gh")
    gh = found or next((path for path in FALLBACKS if os.access(path, os.X_OK)), None)
    if gh is None:
        return None
    environment = {
        **os.environ,
        "GH_CONFIG_DIR": native.github_config_directory(store),
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
    }
    if not found:
        # gh runs git, and both sit in Homebrew's folder.
        environment["PATH"] = f"{Path(gh).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    try:
        process = await asyncio.create_subprocess_exec(
            gh,
            *arguments,
            cwd=folder,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return Ran(1, err=f"gh could not be started: {exc.strerror or exc}")
    try:
        out, err = await asyncio.wait_for(process.communicate(), TIMEOUT)
    except TimeoutError:
        return Ran(1, err=f"gh did not answer within {TIMEOUT} seconds")
    finally:
        if process.returncode is None:
            # Its own session, so whatever it started (git, a credential helper) goes with it.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 5)
    return Ran(process.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


def trouble(ran: Ran) -> str:
    """Why a `gh` command failed, in one short line."""
    lines = [line.strip() for line in ran.err.splitlines() if line.strip()]
    return (lines[0] if lines else f"gh ended with code {ran.code}")[:ERROR_LIMIT]


def answer(ran: Ran) -> dict | None:
    """The JSON object a `gh` command printed (None: it failed, or printed something else)."""
    if ran.code:
        return None
    try:
        value = json.loads(ran.out)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def word(value: object) -> str:
    return value if isinstance(value, str) else ""


def count(value: object) -> int:
    return value if type(value) is int else 0


def rows(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def login(value: object) -> str:
    return word(value.get("login")) if isinstance(value, dict) else ""


def check(item: dict) -> dict:
    """One entry of gh's `statusCheckRollup`: a check run, or a commit status."""
    if item.get("__typename") == "StatusContext":
        name, workflow, url = item.get("context"), None, item.get("targetUrl")
        status = STATUSES.get(word(item.get("state")), "pending")
    else:
        name, workflow, url = item.get("name"), item.get("workflowName"), item.get("detailsUrl")
        ended = item.get("status") == "COMPLETED"
        status = CONCLUSIONS.get(word(item.get("conclusion")), "fail") if ended else "pending"
    return {
        "name": word(name),
        "workflow": word(workflow) or None,
        "status": status,
        "url": word(url) or None,
    }


def review(view: dict) -> dict:
    """Who reviewed a pull request and who was asked to, from gh's review fields."""
    reviews = []
    for item in rows(view.get("latestReviews")):
        state = word(item.get("state")).lower()
        reviews.append(
            {
                "author": login(item.get("author")),
                "state": state if state in REVIEWS else "commented",
            }
        )
    # A requested person has a login, a requested team a name.
    asked = (
        word(item.get("login")) or word(item.get("name"))
        for item in rows(view.get("reviewRequests"))
    )
    return {
        "decision": DECISIONS.get(word(view.get("reviewDecision")), "none"),
        "reviews": reviews,
        "requested": [name for name in asked if name],
    }


def allowed(repository: dict) -> list[str]:
    """The merge methods a repository allows, its default first."""
    default = repository.get("viewerDefaultMergeMethod")
    methods = [method for method, (_, allows) in METHODS.items() if repository.get(allows) is True]
    return sorted(methods, key=lambda method: METHODS[method][0] != default)


def describe(view: dict, behind: int | None, methods: list[str]) -> dict:
    """What the page shows of a pull request, from what `gh pr view` printed."""
    merge_state = word(view.get("mergeStateStatus")).lower()
    return {
        "title": word(view.get("title")),
        "state": STATES.get(word(view.get("state")), "closed"),
        "draft": view.get("isDraft") is True,
        "head": word(view.get("headRefName")),
        "base": word(view.get("baseRefName")),
        "author": login(view.get("author")),
        "updated": word(view.get("updatedAt")),
        "additions": count(view.get("additions")),
        "deletions": count(view.get("deletions")),
        "files": count(view.get("changedFiles")),
        "behind": behind,
        "conflicts": CONFLICTS.get(word(view.get("mergeable"))),
        "mergeState": merge_state if merge_state in MERGE_STATES else "unknown",
        "review": review(view),
        "checks": [check(item) for item in rows(view.get("statusCheckRollup"))],
        "methods": methods,
    }


@dataclass
class Pull:
    """One pull request of a chat's tab: what the page shows, and what only actions need."""

    url: str
    repo: str
    number: int
    loading: bool = True
    error: str | None = None
    busy: str | None = None
    details: dict = field(default_factory=dict)
    oid: str = ""
    # One look at a time, so an older answer never replaces a newer one.
    asking: asyncio.Lock = field(default_factory=asyncio.Lock)

    def shown(self) -> dict:
        return {
            "url": self.url,
            "repo": self.repo,
            "number": self.number,
            "loading": self.loading,
            "error": self.error,
            "busy": self.busy,
            **self.details,
        }


@dataclass
class Tab:
    """One chat's pull requests in first-seen order, the text still being read, and its pages."""

    pulls: dict[str, Pull] = field(default_factory=dict)
    dismissed: set[str] = field(default_factory=set)
    notice: str | None = None
    # Per writer (None: the chat's own agent, or the user): the message being written and its
    # end, which may hold a link still being written.
    tails: dict[str | None, tuple[tuple, str]] = field(default_factory=dict)
    # The chat's `gh pr` tool calls, each with the links its input and output ended with so far.
    calls: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    listeners: set[asyncio.Queue] = field(default_factory=set)
    watcher: asyncio.Task | None = None
    added: asyncio.Event = field(default_factory=asyncio.Event)


class Pulls:
    """Each chat's pull requests, and the pages watching them."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.tabs: dict[str, Tab] = {}
        # Whether gh was missing the last time it was needed.
        self.missing = False
        self.methods: dict[str, list[str]] = {}
        self.asking: dict[str, asyncio.Lock] = {}

    def feed(self, session_id: object, update: object) -> None:
        """Note the links in one `session/update` of a chat, live or replayed.

        Never raises and never waits (no `gh` runs here): the caller is the chat's read loop.
        """
        try:
            if isinstance(session_id, str) and isinstance(update, dict):
                self.read(self.tabs.setdefault(session_id, Tab()), update)
        except Exception:
            # Whatever a native sends, a tab beside the chat must never end the chat's read loop.
            pass

    def read(self, tab: Tab, update: dict) -> None:
        kind, writer = update.get("sessionUpdate"), subagent_tab.author(update)
        if kind not in CHUNKS:
            # Anything else from the same writer: its message has ended.
            self.end(tab, writer)
            if kind in STEPS and isinstance(call := update.get("toolCallId"), str):
                self.step(tab, call, update)
            return
        meta = update.get("_meta")
        message = (kind, meta.get(subagent_tab.STREAM) if isinstance(meta, dict) else None)
        if writer in tab.tails and tab.tails[writer][0] != message:
            # The writer's next message begins.
            self.end(tab, writer)
        found, tail = links(tab.tails.get(writer, (message, ""))[1] + written(update))
        tab.tails[writer] = (message, tail)
        self.keep(tab, found)
        if len(tab.tails) > MAX_WRITERS:
            for other in list(tab.tails):
                self.end(tab, other)

    def end(self, tab: Tab, writer: str | None) -> None:
        """A writer's message has ended, so a link it ended with is whole."""
        if writer in tab.tails:
            self.keep(tab, links(tab.tails.pop(writer)[1], ended=True)[0])

    def step(self, tab: Tab, call: str, update: dict) -> None:
        """Read a `gh pr` command's input and output; other tool calls say nothing here.

        The native streams a command while it is written and its output while it runs, each
        time whole so far. A link that ends such a text waits until the text is known whole.
        """
        if call not in tab.calls:
            named = texts([update.get("title"), update.get("rawInput")])
            if not any(COMMAND in text for text in named):
                return
            tab.calls[call] = {"input": [], "output": []}
            for oldest in list(tab.calls)[:-MAX_CALLS]:
                del tab.calls[oldest]
        held, found = tab.calls[call], []
        for part, keys in (("input", ("rawInput",)), ("output", ("content", "rawOutput"))):
            if any(key in update for key in keys):
                held[part] = []
                for text in texts([update.get(key) for key in keys]):
                    whole, tail = links(text)
                    found += whole
                    held[part] += links(tail, ended=True)[0]
        status = update.get("status")
        for part, states in (("input", RAN), ("output", ENDED)):
            if status in states:
                found += held[part]
                held[part] = []
        self.keep(tab, found)

    def flush(self, session_id: str) -> None:
        """Count what the chat's messages and commands ended with: its turn is over."""
        if tab := self.tabs.get(session_id):
            for writer in list(tab.tails):
                self.end(tab, writer)
            for held in tab.calls.values():
                for part in held:
                    self.keep(tab, held[part])
                    held[part] = []

    def said(self, session_id: str, text: str) -> None:
        """Note the links in a prompt the user sent: a whole message."""
        if found := links(text, ended=True)[0]:
            self.keep(self.tabs.setdefault(session_id, Tab()), found)

    def keep(self, tab: Tab, urls: list[str], asked: bool = False) -> None:
        """Add links to a chat's tab. One the user dismissed returns only when `asked` for."""
        new = False
        for url in urls:
            key = url.lower()
            if key in tab.pulls or (key in tab.dismissed and not asked):
                continue
            tab.dismissed.discard(key)
            owner, name, _, number = url.split("/")[3:]
            tab.pulls[key] = Pull(url, f"{owner}/{name}", int(number))
            new = True
        for oldest in list(tab.pulls)[:-MAX_PULLS]:
            del tab.pulls[oldest]
        if new:
            # A page that is open shows it at once, and its watcher looks it up.
            tab.added.set()
            self.tell(tab)

    async def gh(self, *arguments: str, folder: Path | None = None) -> Ran:
        """Run `gh`: in the user's home, or in a chat's folder for what depends on its repo."""
        ran = await run(self.store, folder or Path.home(), *arguments)
        self.missing = ran is None
        return ran or Ran(127, err=MISSING)

    async def behind(self, repo: str, base: str, oid: str) -> int | None:
        """How many commits of its base a pull request lacks (None: GitHub did not say)."""
        path = f"repos/{repo}/compare/{quote(base, safe='')}...{oid}"
        ran = await self.gh("api", path, "--jq", ".behind_by")
        text = ran.out.strip()
        return int(text) if not ran.code and text.isascii() and text.isdigit() else None

    async def allowed(self, repo: str) -> list[str]:
        """The merge methods a repository allows, asked once ([]: GitHub has not said yet)."""
        async with self.asking.setdefault(repo, asyncio.Lock()):
            if repo not in self.methods:
                seen = answer(await self.gh("repo", "view", repo, "--json", ALLOWED))
                if seen is None:
                    return []
                self.methods[repo] = allowed(seen)
            return self.methods[repo]

    async def look(self, tab: Tab, pull: Pull) -> None:
        """Ask GitHub about one pull request. What goes wrong is told on that pull request."""
        async with pull.asking:
            ran = await self.gh("pr", "view", pull.url, "--json", FIELDS)
            view = answer(ran)
            if view is None:
                pull.error = trouble(ran) if ran.code else UNREADABLE
            else:
                base, oid = word(view.get("baseRefName")), word(view.get("headRefOid"))
                # Only an open pull request can be merged, so only then does its base matter.
                behind = (
                    await self.behind(pull.repo, base, oid) if view.get("state") == "OPEN" else None
                )
                pull.details = describe(view, behind, await self.allowed(pull.repo))
                pull.error, pull.oid = None, oid
            pull.loading = False
        self.tell(tab)

    async def refresh(self, tab: Tab, pulls: list[Pull]) -> None:
        """Look pull requests up, a few at a time."""
        for start in range(0, len(pulls), BATCH):
            await asyncio.gather(*(self.look(tab, pull) for pull in pulls[start : start + BATCH]))

    async def watch(self, tab: Tab) -> None:
        """Keep a chat's pull requests fresh while a page shows them.

        All of them every `REFRESH` seconds, and one that was just added at once.
        """
        while True:
            again = time.monotonic() + REFRESH
            await self.refresh(tab, list(tab.pulls.values()))
            while (left := again - time.monotonic()) > 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(tab.added.wait(), left)
                tab.added.clear()
                await self.refresh(tab, [pull for pull in tab.pulls.values() if pull.loading])

    def snapshot(self, session_id: str) -> dict:
        """What the page shows."""
        tab = self.tabs.get(session_id)
        return {
            "pulls": [pull.shown() for pull in tab.pulls.values()] if tab else [],
            "notice": MISSING if self.missing else tab.notice if tab else None,
        }

    def showing(self, session_id: str) -> bool:
        return session_id in self.tabs and bool(self.tabs[session_id].listeners)

    @contextmanager
    def listen(self, session_id: str) -> Iterator[asyncio.Queue]:
        """A connected page's queue. A token in it says: send the page a fresh snapshot.

        A chat's pull requests are looked up only while a page of it is connected.
        """
        tab = self.tabs.setdefault(session_id, Tab())
        queue: asyncio.Queue = asyncio.Queue()
        # The page shows the pull requests as they are right after connecting.
        queue.put_nowait(True)
        tab.listeners.add(queue)
        if tab.watcher is None:
            tab.watcher = asyncio.create_task(self.watch(tab))
        try:
            yield queue
        finally:
            tab.listeners.discard(queue)
            if not tab.listeners and tab.watcher:
                tab.watcher.cancel()
                tab.watcher = None

    def tell(self, tab: Tab) -> None:
        # One waiting token covers every change until it is sent, so a stalled page holds one.
        for queue in tab.listeners:
            if queue.empty():
                queue.put_nowait(True)

    async def close(self) -> None:
        """End every connected page's stream, and stop looking pull requests up."""
        watchers = [tab.watcher for tab in self.tabs.values() if tab.watcher]
        for tab in self.tabs.values():
            tab.watcher = None
            for queue in tab.listeners:
                queue.put_nowait(None)
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
