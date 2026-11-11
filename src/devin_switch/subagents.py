"""Remember a GUI chat's running subagents so their work can resume after an interruption.

The native keeps subagents only in its process: a restart, crash or handoff ends them, and a
new process cannot reattach to their IDs. It does save each subagent's transcript in shared
history (`subagent_heads` points at the latest node of its chain). Switch records which
subagents were running for a conversation; when a replacement process opens the conversation,
those are interrupted, and the parent agent can start fresh subagents that continue from the
saved progress instead of redoing it.

A finished subagent stays readable and resumable only in the process that ran it. Switch
records those too: once a replacement process opens the conversation they are closed, and the
parent agent is told so it does not ask for IDs that no longer exist. They live in their own
file: older bridges still running in other windows rewrite the first one with only the lists
they know.
"""

import json
import re
import sqlite3
import time
from pathlib import Path

from devin_switch import acp_state
from devin_switch.store import Store, SwitchError, private_directory, write_json

STARTED = "cognition.ai/subagent_started"
COMPLETED = "cognition.ai/subagent_completed"
QUOTA_FAILURE = re.compile(r"usage quota has been exhausted|quota exhausted", re.IGNORECASE)
TASK_LIMIT = 4000
PROGRESS_LIMIT = 2000
PROGRESS_STEPS = 6
FINISHED_LIMIT = 100
NOTE_LIMIT = 20


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


def ended_path(store: Store, session_id: str) -> Path:
    return store.root / "acp" / "closed-subagents" / f"{acp_state.session_key(session_id)}.json"


def load_ended(store: Store, session_id: str) -> dict:
    """Subagents that finished in the live process, and ones closed with an earlier one.

    Not in the file above: older bridges in other windows save it with only its two lists.
    """
    state = {"session_id": session_id, "finished": {}, "closed": {}}
    try:
        value = json.loads(ended_path(store, session_id).read_text())
        if value.get("session_id") != session_id:
            raise ValueError
        return state | {name: dict(value.get(name) or {}) for name in ("finished", "closed")}
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        return state


def save_ended(store: Store, state: dict) -> None:
    target = ended_path(store, state["session_id"])
    if not state["finished"] and not state["closed"]:
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


def newest(agents: dict) -> dict:
    """The FINISHED_LIMIT subagents that ended last, oldest first."""
    ordered = sorted(agents.items(), key=lambda item: item[1].get("ended_at", 0))
    return dict(ordered[-FINISHED_LIMIT:])


def record(store: Store, session_id: str, key: str, event: dict) -> None:
    """Apply one live subagent event: started, finished, or failed because usage ran out."""
    agent = event.get("agentId")
    if not isinstance(agent, str) or not agent:
        return
    with store.lock("subagents.lock", timeout=5):
        state, ended = load(store, session_id), load_ended(store, session_id)
        if key == STARTED:
            state["running"][agent] = describe(event)
            ended["finished"].pop(agent, None)
        elif key == COMPLETED and (info := state["running"].pop(agent, None)) is not None:
            summary = str(event.get("summary") or "")
            if event.get("success") is False and QUOTA_FAILURE.search(summary):
                state["interrupted"][agent] = {**info, "reason": "usage ran out"}
            else:
                kept = {name: info.get(name) for name in ("title", "profile", "background")}
                ended["finished"][agent] = {**kept, "ended_at": time.time()}
                ended["finished"] = newest(ended["finished"])
        save(store, state)
        save_ended(store, ended)


def interrupt_running(store: Store, session_id: str) -> list[str]:
    """Mark subagents a previous process left running as interrupted; return their IDs.

    The ones that finished in that process are closed: their IDs ended with it.
    """
    with store.lock("subagents.lock", timeout=5):
        state = load(store, session_id)
        moved = list(state["running"])
        for agent in moved:
            state["interrupted"][agent] = {
                **state["running"].pop(agent),
                "reason": "the agent restarted",
            }
        save(store, state)
        ended = load_ended(store, session_id)
        ended["closed"] = newest({**ended["closed"], **ended["finished"]})
        ended["finished"] = {}
        save_ended(store, ended)
    return moved


def interrupted(store: Store, session_id: str) -> dict:
    return load(store, session_id)["interrupted"]


def closed(store: Store, session_id: str) -> dict:
    return load_ended(store, session_id)["closed"]


def forget(store: Store, session_id: str, agents) -> None:
    with store.lock("subagents.lock", timeout=5):
        state = load(store, session_id)
        for agent in agents:
            state["interrupted"].pop(agent, None)
        save(store, state)


def forget_closed(store: Store, session_id: str, agents) -> None:
    with store.lock("subagents.lock", timeout=5):
        ended = load_ended(store, session_id)
        for agent in agents:
            ended["closed"].pop(agent, None)
        save_ended(store, ended)


def closed_note(agents: dict) -> str:
    """Tell the parent agent which finished subagents it can no longer read or resume."""
    listed = list(agents.items())[-NOTE_LIMIT:]
    lines = [
        "[Note from Devin Switch, not typed by the user] The agent process that ran these "
        "subagents has ended:"
    ]
    for agent, info in listed:
        title = info.get("title") or info.get("profile") or "subagent"
        lines.append(f'- "{title}" ({agent})')
    if len(agents) > len(listed):
        lines.append(f"- and {len(agents) - len(listed)} more")
    lines.append(
        "Their results were already delivered earlier in this conversation. read_subagent and "
        'run_subagent with resume no longer work for these IDs (they return "No subagent '
        'found"). If more work is wanted, start a new subagent with the context it needs.'
    )
    return "\n".join(lines)


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


def progress(store: Store, session_id: str, agent: str) -> str:
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
            while node is not None and len(steps) < PROGRESS_STEPS * 3:
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
    text = "\n".join(reversed(steps[:PROGRESS_STEPS]))
    return text[-PROGRESS_LIMIT:]


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
