import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from devin_switch.native import Native
from devin_switch.store import Account, Store, SwitchError, private_directory, write_json


@dataclass(frozen=True)
class Run:
    id: str
    account: str
    project: str
    kind: str
    started_at: float
    session_id: str | None = None
    ended_at: float | None = None


def history(store: Store) -> list[dict]:
    database = store.root / "shared/cli/sessions.db"
    if not database.exists():
        return []
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, title, working_directory AS project, last_activity_at "
                    "FROM sessions WHERE hidden = 0 ORDER BY last_activity_at DESC, id"
                )
            ]
    except sqlite3.Error as exc:
        raise SwitchError(
            "Conversation history is unavailable. Check the native CLI and retry."
        ) from exc


def runs(store: Store) -> list[dict]:
    directory = store.root / "runs"
    if not directory.exists():
        return []
    result = []
    for path in directory.glob("*.json"):
        try:
            run = Run(**json.loads(path.read_text()))
            if (
                run.id != path.stem
                or len(run.id) != 32
                or not all(c in "0123456789abcdef" for c in run.id)
            ):
                continue
            result.append({**asdict(run), "active": store.locked(f"run-{run.id}.lock")})
        except (OSError, ValueError, TypeError):
            continue
    return sorted(result, key=lambda run: run["started_at"])


def resume_blocker(chat: dict, running: list[dict]) -> str | None:
    for run in running:
        if (
            run["active"]
            and run["kind"] == "chat"
            and (run["project"] == chat["project"] or run["session_id"] == chat["id"])
        ):
            return "Close the open CLI sessions in this repo before resuming a saved chat."
    return None


def overview(store: Store) -> dict:
    running = runs(store)
    try:
        chats = history(store)
        error = None
    except SwitchError as exc:
        chats, error = [], str(exc)
    for chat in chats:
        launches = [run for run in running if run["session_id"] == chat["id"]]
        chat["account"] = launches[-1]["account"] if launches else None
        chat["active"] = any(run["active"] for run in launches)
        chat["resume_blocked"] = resume_blocker(chat, running)
    return {"runs": running, "sessions": chats, "history_error": error}


def resumable(store: Store, session_id: str) -> dict:
    chat = next((chat for chat in history(store) if chat["id"] == session_id), None)
    if chat is None:
        raise SwitchError(
            "This conversation is no longer in shared history. Refresh and choose another."
        )
    if blocker := resume_blocker(chat, runs(store)):
        raise SwitchError(blocker)
    if not Path(chat["project"]).is_dir():
        raise SwitchError("This conversation's project folder no longer exists.")
    return chat


def resume_arguments(
    store: Store, arguments: tuple[str, ...], project: Path
) -> tuple[tuple[str, ...], str | None]:
    arguments = list(arguments)
    session_id = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            break
        if value in ("-p", "--print", "--export"):
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                index += 1
        elif value in ("--continue", "-c"):
            chat = next((chat for chat in history(store) if chat["project"] == str(project)), None)
            if chat is None:
                raise SwitchError("No conversation in this folder yet. Choose Start new first.")
            session_id = chat["id"]
            arguments[index : index + 1] = ["--resume", session_id]
            index += 1
        elif value in ("--resume", "-r"):
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                raise SwitchError(
                    "Choose an exact conversation with ds sessions, then use --resume ID."
                )
            index += 1
            session_id = arguments[index]
        elif value.startswith("--resume="):
            session_id = value.split("=", 1)[1]
            if not session_id:
                raise SwitchError("Choose an exact conversation with --resume ID.")
        elif value.startswith("-r") and len(value) > 2:
            session_id = value[2:]
        elif value in (
            "--model",
            "--config",
            "--permission-mode",
            "--prompt-file",
            "--respect-workspace-trust",
        ):
            index += 1
        index += 1
    if session_id:
        chat = resumable(store, session_id)
        if chat["project"] != str(project):
            raise SwitchError(
                "Open this conversation from its original project folder or use the app."
            )
    return tuple(arguments), session_id


@contextmanager
def managed(
    native: Native,
    name: str | None,
    arguments: tuple[str, ...],
    *,
    kind: str = "chat",
    can_handoff: bool = False,
) -> Iterator[tuple[Native, Account, tuple[str, ...]]]:
    store = native.store
    with ExitStack() as stack:
        with store.lock():
            account = store.account(name) if name else store.selected()
            exclusive = kind == "login" or arguments[:1] == ("auth",)
            account_fd = stack.enter_context(store.account_lock(account, shared=not exclusive))
            if kind != "login":
                native.require_login(account)
            project = Path.cwd().resolve()
            session_id = None
            if kind == "chat":
                arguments, session_id = resume_arguments(store, arguments, project)
            run = Run(uuid.uuid4().hex, account.name, str(project), kind, time.time(), session_id)
            run_fd = stack.enter_context(store.lock(f"run-{run.id}.lock"))
            directory = store.root / "runs"
            private_directory(directory)
            write_json(directory / f"{run.id}.json", asdict(run))
        try:
            runner = replace(
                native, lock_fds=(account_fd, run_fd), run_id=run.id if can_handoff else None
            )
            yield runner, account, arguments
        finally:
            write_json(directory / f"{run.id}.json", asdict(replace(run, ended_at=time.time())))
