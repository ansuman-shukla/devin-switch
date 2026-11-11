"""The Subagents tab: a GUI chat's subagents, live, in a Browser tab of the chat.

The native reports subagents in `session/update` metadata: `cognition.ai/subagent_started`,
`_completed` and `_paused` events, and on a subagent's own updates `cognition.ai/subagent_context`,
whose `parentAgentId` is that subagent's ID (the chat's own agent sends it only on usage updates,
as "root"). The bridge feeds each live update to a `Roster`, kept only in memory, which also
keeps what each subagent said: the bridge leaves those messages out of the chat, where Desktop
would show them as the chat's own agent's. `/subagents` typed in a chat opens the page that shows
it, served by `side.Server` like the side page. It is a view: nothing here reaches the agent or
the chat's transcript.
"""

import asyncio
import html
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from devin_switch.subagents import COMPLETED, STARTED

PAUSED = "cognition.ai/subagent_paused"
CONTEXT = "cognition.ai/subagent_context"
STREAM = "cognition.ai/streamingMessageId"
PAGE = Path(__file__).parent / "subagent_page"
ASSETS = {
    "subagents.css": "text/css",
    "subagents-view.js": "text/javascript",
    "subagents.js": "text/javascript",
}
# The side page's Markdown renderer (`window.SideMarkdown`), which `side.Server` serves as well.
MARKDOWN = "/side/assets/markdown.js"
ACTIVE = ("running", "paused")
RESTARTED = "the agent restarted"
RELOADED = "the chat was reloaded"
MAX_ROWS = 200
TEXT_LIMIT = 200
REPLY_LIMIT = 50_000
REPLY_PAUSE = 1
SHOWN = ("id", "title", "profile", "background", "status", "started", "ended", "step", "note")


def named(event: object) -> str | None:
    """The subagent an event is about, when it names one."""
    agent = event.get("agentId") if isinstance(event, dict) else None
    return agent if isinstance(agent, str) and agent else None


def author(update: object) -> str | None:
    """The subagent an update is the own work of (None: the chat's own agent, or unsaid)."""
    meta = update.get("_meta") if isinstance(update, dict) else None
    context = meta.get(CONTEXT) if isinstance(meta, dict) else None
    parent = context.get("parentAgentId") if isinstance(context, dict) else None
    return parent if isinstance(parent, str) and parent and parent != "root" else None


def own_message(update: object) -> bool:
    """Whether an update is a subagent's own message, not one of the chat's own agent.

    Never raises on what a native might send: the caller is the chat's read loop.
    """
    return author(update) is not None and update.get("sessionUpdate") == "agent_message_chunk"


def line(value: object) -> str:
    """A short one-line display text; anything but text shows as nothing."""
    lines = value.strip().splitlines() if isinstance(value, str) else []
    return lines[0].strip()[:TEXT_LIMIT] if lines else ""


def step(update: dict) -> str:
    """What a subagent's own update says it is doing now ("": nothing new)."""
    kind = update.get("sessionUpdate")
    if kind in ("tool_call", "tool_call_update"):
        return line(update.get("title"))
    if kind == "agent_thought_chunk":
        return "Thinking"
    return "Writing" if kind == "agent_message_chunk" else ""


def said(update: dict) -> str:
    """The text of a message chunk ("": it is not one, or holds something else)."""
    content = update.get("content")
    if update.get("sessionUpdate") != "agent_message_chunk" or not isinstance(content, dict):
        return ""
    text = content.get("text")
    return text if content.get("type") == "text" and isinstance(text, str) else ""


def trim(rows: dict[str, dict]) -> None:
    """Bound a long chat's list: the oldest finished rows go first, active ones never."""
    extra = len(rows) - MAX_ROWS
    if extra > 0:
        finished = [agent for agent, row in rows.items() if row["status"] not in ACTIVE]
        for agent in finished[:extra]:
            del rows[agent]


class Roster:
    """Each chat's subagents in start order, and the pages watching them."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict]] = {}
        self.listeners: dict[str, set[asyncio.Queue]] = {}

    def apply(self, session_id: str, update: dict) -> None:
        """Take one live `session/update` of a chat; most are its own agent's and change nothing.

        Never raises on what a native might send: the caller is the chat's read loop.
        """
        meta = update.get("_meta")
        if not isinstance(meta, dict):
            return
        if agent := named(event := meta.get(STARTED)):
            rows = self.rows.setdefault(session_id, {})
            # The same ID started again counts, and sorts, from its new start.
            rows.pop(agent, None)
            rows[agent] = {
                "id": agent,
                "title": line(event.get("title")),
                "profile": line(event.get("profile")),
                "background": event.get("isBackground") is True,
                "status": "running",
                "started": time.time(),
                "ended": None,
                "step": "",
                "note": "",
                "reply": "",
                "stream": None,
                "told": float("-inf"),
            }
            trim(rows)
            changed = True
        elif rows := self.rows.get(session_id):
            changed = False
        else:
            return
        row = rows.get(named(event := meta.get(COMPLETED)))
        if row and row["status"] in ACTIVE:
            done = event.get("success") is True
            row["status"] = "done" if done else "failed"
            row["note"] = "" if done else line(event.get("summary"))
            row["ended"] = time.time()
            changed = True
        row = rows.get(named(event := meta.get(PAUSED)))
        if row and row["status"] in ACTIVE and isinstance(event.get("paused"), bool):
            status = "paused" if event["paused"] else "running"
            changed, row["status"] = changed or status != row["status"], status
        row = rows.get(author(update))
        # Thought and message chunks arrive many times a second: only a new step is a change.
        if row and (doing := step(update)) and doing != row["step"]:
            row["step"] = doing
            changed = True
        if row and (text := said(update)):
            stream, now = meta.get(STREAM), time.monotonic()
            # A message after an earlier one starts a new paragraph.
            gap = "\n\n" if row["reply"] and stream != row["stream"] else ""
            row["reply"], row["stream"] = (row["reply"] + gap + text)[-REPLY_LIMIT:], stream
            # Chunks arrive many times a second: a longer reply alone is told once in a while.
            changed = changed or now - row["told"] >= REPLY_PAUSE
            if changed:
                row["told"] = now
        if changed:
            self.notify(session_id)

    def interrupt(self, session_id: str, reason: str = RESTARTED) -> None:
        """The chat's native dropped every subagent it was still running, for `reason`."""
        rows, now = self.rows.get(session_id, {}), time.time()
        active = [row for row in rows.values() if row["status"] in ACTIVE]
        for row in active:
            row.update(status="stopped", note=reason, ended=now)
        if active:
            self.notify(session_id)

    def forget(self, session_id: str) -> None:
        if self.rows.pop(session_id, None):
            self.notify(session_id)

    def snapshot(self, session_id: str) -> dict:
        """What the page lists: never a subagent's task, which can hold prompt content, and of
        its reply only the length."""
        rows = self.rows.get(session_id, {}).values()
        return {
            "subagents": [
                {**{key: row[key] for key in SHOWN}, "reply_chars": len(row["reply"])}
                for row in rows
            ]
        }

    def reply(self, session_id: str, agent: str) -> str | None:
        """Everything a subagent said so far, newest `REPLY_LIMIT` characters (None: no row)."""
        row = self.rows.get(session_id, {}).get(agent)
        return row["reply"] if row else None

    def showing(self, session_id: str) -> bool:
        return bool(self.listeners.get(session_id))

    @contextmanager
    def listen(self, session_id: str) -> Iterator[asyncio.Queue]:
        """A connected page's queue. A token in it says: send the page a fresh snapshot."""
        queue: asyncio.Queue = asyncio.Queue()
        # The page shows the rows as they are right after connecting.
        queue.put_nowait(True)
        self.listeners.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            self.listeners[session_id].discard(queue)
            if not self.listeners[session_id]:
                del self.listeners[session_id]

    def notify(self, session_id: str) -> None:
        # One waiting token covers every change until it is sent, so a stalled page holds one.
        for queue in self.listeners.get(session_id, ()):
            if queue.empty():
                queue.put_nowait(True)

    def close(self) -> None:
        """End every connected page's stream."""
        for queues in self.listeners.values():
            for queue in queues:
                queue.put_nowait(None)


def page(session_id: str, token: str) -> str:
    """The page shell: its scripts read the chat and token from <body>."""
    token = html.escape(token)
    links = {name: f"/subagents/assets/{name}?t={token}" for name in ASSETS}
    # A reply is shown as Markdown, so the renderer loads before the page's own scripts.
    scripts = (f"{MARKDOWN}?t={token}", links["subagents-view.js"], links["subagents.js"])
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Subagents</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link rel="stylesheet" href="{links["subagents.css"]}"></head>'
        f'<body data-session="{html.escape(session_id)}" data-token="{token}">'
        + "".join(f'<script src="{source}"></script>' for source in scripts)
        + "</body></html>"
    )
