"""Remember a GUI chat's running subagents so their work can resume after an interruption.

The native keeps subagents only in its process: a restart, crash or handoff ends them, and a
new process cannot reattach to their IDs. It does save each subagent's transcript in shared
history (`subagent_heads` points at the latest node of its chain). Switch records which
subagents were running for a conversation; when a replacement process opens the conversation,
those are interrupted, and the parent agent can start fresh subagents that continue from the
saved progress instead of redoing it.
"""

import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from devin_switch import acp_state, sessions
from devin_switch.store import Store, SwitchError, private_directory, write_json

STARTED = "cognition.ai/subagent_started"
COMPLETED = "cognition.ai/subagent_completed"
QUOTA_FAILURE = re.compile(r"usage quota has been exhausted|quota exhausted", re.IGNORECASE)
TASK_LIMIT = 4000
PROGRESS_LIMIT = 2000
PROGRESS_STEPS = 6
STEP_LIMIT = 240


def path(store: Store, session_id: str) -> Path:
    return store.root / "acp" / "subagents" / f"{acp_state.session_key(session_id)}.json"


def load(store: Store, session_id: str) -> dict:
    try:
        value = json.loads(path(store, session_id).read_text())
        if value.get("session_id") != session_id:
            raise ValueError
        return {
            "session_id": session_id,
            "running": dict(value.get("running") or {}),
            "interrupted": dict(value.get("interrupted") or {}),
        }
    except FileNotFoundError:
        return {"session_id": session_id, "running": {}, "interrupted": {}}
    except (ValueError, TypeError, AttributeError):
        return {"session_id": session_id, "running": {}, "interrupted": {}}


def save(store: Store, state: dict) -> None:
    target = path(store, state["session_id"])
    if not state["running"] and not state["interrupted"]:
        target.unlink(missing_ok=True)
        return
    private_directory(target.parent)
    write_json(target, state)


def describe(event: dict) -> dict:
    return {
        "title": str(event.get("title") or "")[:200],
        "task": str(event.get("task") or "")[:TASK_LIMIT],
        "profile": str(event.get("profile") or ""),
        "background": bool(event.get("isBackground")),
        "started_at": time.time(),
    }


def record(store: Store, session_id: str, key: str, event: dict) -> None:
    """Apply one live subagent event: started, finished, or failed because usage ran out."""
    agent = event.get("agentId")
    if not isinstance(agent, str) or not agent:
        return
    with store.lock("subagents.lock", timeout=5):
        state = load(store, session_id)
        if key == STARTED:
            state["running"][agent] = describe(event)
        elif key == COMPLETED:
            info = state["running"].pop(agent, None)
            summary = str(event.get("summary") or "")
            if info is not None and event.get("success") is False and QUOTA_FAILURE.search(summary):
                state["interrupted"][agent] = {**info, "reason": "usage ran out"}
        save(store, state)


def interrupt_running(store: Store, session_id: str) -> list[str]:
    """Mark subagents a previous process left running as interrupted; return their IDs."""
    with store.lock("subagents.lock", timeout=5):
        state = load(store, session_id)
        moved = list(state["running"])
        for agent in moved:
            state["interrupted"][agent] = {
                **state["running"].pop(agent),
                "reason": "the agent restarted",
            }
        save(store, state)
    return moved


def interrupted(store: Store, session_id: str) -> dict:
    return load(store, session_id)["interrupted"]


def forget(store: Store, session_id: str, agents) -> None:
    with store.lock("subagents.lock", timeout=5):
        state = load(store, session_id)
        for agent in agents:
            state["interrupted"].pop(agent, None)
        save(store, state)


def card(agent: str, info: dict) -> dict:
    """The update Desktop draws a subagent card from, as the native sends when one starts."""
    started = datetime.fromtimestamp(info.get("started_at") or time.time(), UTC)
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "status": "in_progress",
        "_meta": {
            STARTED: {
                "agentId": agent,
                "title": info.get("title") or "",
                "task": info.get("task") or "",
                "profile": info.get("profile") or "",
                "depth": 0,
                "isBackground": bool(info.get("background")),
            },
            "cognition.ai/timestamp": started.isoformat(),
        },
    }


def stopped_card(agent: str, info: dict) -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": agent,
        "status": "failed",
        "_meta": {
            COMPLETED: {
                "agentId": agent,
                "success": False,
                "summary": f"Stopped before finishing because {info.get('reason')}. Send "
                "/resume-subagents to have the agent continue it from its saved progress.",
            }
        },
    }


def summarize(message: dict) -> str:
    role = message.get("role")
    parts = []
    if isinstance(message.get("content"), str) and message["content"].strip():
        parts.append(message["content"].strip())
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            arguments = json.dumps(call.get("arguments"), ensure_ascii=False)
            parts.append(f"called {call.get('name')} {arguments}")
    text = " | ".join(parts)
    return f"{role}: {text[:600]}" if text else ""


def progress(store: Store, session_id: str, agent: str, limit: int = PROGRESS_STEPS) -> str:
    """The last steps of a subagent's saved transcript, or "" when none is saved."""
    database = store.root / "shared/cli/sessions.db"
    if not database.exists():
        return ""
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            head = connection.execute(
                "SELECT chain_node_id FROM subagent_heads WHERE session_id = ? AND agent_id = ?",
                (session_id, agent),
            ).fetchone()
            if head is None:
                return ""
            steps, node = [], head[0]
            while node is not None and len(steps) < limit * 3:
                row = connection.execute(
                    "SELECT parent_node_id, chat_message FROM message_nodes "
                    "WHERE session_id = ? AND node_id = ?",
                    (session_id, node),
                ).fetchone()
                if row is None:
                    break
                node = row[0]
                try:
                    message = json.loads(row[1])
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict) and message.get("role") in ("assistant", "tool"):
                    if line := summarize(message):
                        steps.append(line)
    except sqlite3.Error:
        return ""
    text = "\n".join(reversed(steps[:limit]))
    return text[-PROGRESS_LIMIT:]


def listing(store: Store, session_id: str) -> list[dict]:
    """A chat's running and stopped subagents for the Mac app, oldest first."""
    state = load(store, session_id)
    entries = [
        {
            "id": agent,
            "title": info.get("title") or "",
            "profile": info.get("profile") or "",
            "background": bool(info.get("background")),
            "started_at": float(info.get("started_at") or 0),
            "status": status,
            "reason": info.get("reason"),
            "last_step": progress(store, session_id, agent, limit=1)[:STEP_LIMIT],
        }
        for status, group in (("running", state["running"]), ("stopped", state["interrupted"]))
        for agent, info in group.items()
    ]
    return sorted(entries, key=lambda entry: entry["started_at"])


def window(store: Store, host: int) -> list[dict]:
    """Running subagents of the live GUI chats a Desktop window's extension host started."""
    parents = sessions.process_parents()
    try:
        titles = {chat["id"]: chat["title"] or "" for chat in sessions.history(store)}
    except SwitchError:
        titles = {}
    return [
        {
            **entry,
            "project": run["project"],
            "chat": titles.get(run["session_id"], ""),
            "recent": progress(store, run["session_id"], entry["id"]),
        }
        for run in acp_state.live(store)
        if sessions.started_by(store, run["id"], host, parents)
        for entry in listing(store, run["session_id"])
        if entry["status"] == "running"
    ]


def resume_text(store: Store, session_id: str, agents: dict) -> str:
    lines = [
        f"{len(agents)} of your subagents stopped before finishing, so Devin Switch is asking "
        "you to resume them. Their IDs no longer exist and cannot be read or resumed directly, "
        "but their transcripts are saved. For each one you still need, start a new subagent "
        "with the original task plus the progress below, and tell it to continue from there "
        "instead of redoing finished work (check the working tree first). Skip any you no "
        "longer need.",
    ]
    for number, (agent, info) in enumerate(agents.items(), 1):
        lines.append("")
        title = info.get("title") or info.get("profile") or "subagent"
        background = " (background)" if info.get("background") else ""
        lines.append(f"{number}. {title}{background}, stopped because {info.get('reason')}")
        lines.append(f"Original task:\n{info.get('task') or '(not reported)'}")
        try:
            done = progress(store, session_id, agent)
        except (OSError, SwitchError):
            done = ""
        lines.append(f"Last saved progress:\n{done or '(none saved)'}")
    return "\n".join(lines)
