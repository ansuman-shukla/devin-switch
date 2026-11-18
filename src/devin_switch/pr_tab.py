"""The PR tab: the pull requests seen in a GUI chat, in a Browser tab of the chat.

Desktop's own PR tab loads pull requests through Devin's cloud, which cannot see private
repositories. This one is local. The bridge notes every GitHub pull request link that shows up
in a chat: in what the agent and the user wrote, and in what a `gh pr` command was given and
printed. `/pr` typed in the chat opens the page that lists them, served by `side.Server` like
the side page. Everything is kept in memory only.
"""

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from devin_switch import subagent_tab

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


@dataclass
class Pull:
    """One pull request of a chat's tab."""

    url: str
    repo: str
    number: int
    loading: bool = True
    error: str | None = None
    busy: str | None = None

    def shown(self) -> dict:
        return {
            "url": self.url,
            "repo": self.repo,
            "number": self.number,
            "loading": self.loading,
            "error": self.error,
            "busy": self.busy,
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


class Pulls:
    """Each chat's pull requests, and the pages watching them."""

    def __init__(self) -> None:
        self.tabs: dict[str, Tab] = {}

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
            self.tell(tab)

    def snapshot(self, session_id: str) -> dict:
        """What the page shows."""
        tab = self.tabs.get(session_id) or Tab()
        return {"pulls": [pull.shown() for pull in tab.pulls.values()], "notice": tab.notice}

    def showing(self, session_id: str) -> bool:
        return session_id in self.tabs and bool(self.tabs[session_id].listeners)

    @contextmanager
    def listen(self, session_id: str) -> Iterator[asyncio.Queue]:
        """A connected page's queue. A token in it says: send the page a fresh snapshot."""
        tab = self.tabs.setdefault(session_id, Tab())
        queue: asyncio.Queue = asyncio.Queue()
        # The page shows the pull requests as they are right after connecting.
        queue.put_nowait(True)
        tab.listeners.add(queue)
        try:
            yield queue
        finally:
            tab.listeners.discard(queue)

    def tell(self, tab: Tab) -> None:
        # One waiting token covers every change until it is sent, so a stalled page holds one.
        for queue in tab.listeners:
            if queue.empty():
                queue.put_nowait(True)

    def close(self) -> None:
        """End every connected page's stream."""
        for tab in self.tabs.values():
            for queue in tab.listeners:
                queue.put_nowait(None)
