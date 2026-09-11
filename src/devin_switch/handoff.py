import json
import os
import re
import shlex
from pathlib import Path

from devin_switch import sessions
from devin_switch.native import Native
from devin_switch.store import Account, Store, SwitchError, private_directory, write_json

HOOK_COMMAND = (
    'if [ -n "${DS_RUN_ID:-}" ] && [ -n "${DS_EXECUTABLE:-}" ]; then '
    'if [ "${DS_FROZEN:-0}" = 1 ]; then "$DS_EXECUTABLE" _session-end; '
    'else "$DS_EXECUTABLE" -m devin_switch.cli _session-end; fi; fi'
)
HOOK = {"matcher": "", "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}


def hook_config(project: Path) -> tuple[Path, dict]:
    directory = project / ".devin"
    path = directory / "hooks.v1.json"
    if directory.is_symlink() or path.is_symlink():
        raise SwitchError(
            "Refusing to change linked .devin hooks. Use a regular project directory."
        )
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError) as exc:
        raise SwitchError(
            "Cannot read .devin/hooks.v1.json; existing hooks were preserved."
        ) from exc
    if not isinstance(config, dict) or not isinstance(config.get("SessionEnd", []), list):
        raise SwitchError("Invalid .devin/hooks.v1.json; existing hooks were preserved.")
    return path, config


def install(project: Path) -> None:
    path, config = hook_config(project)
    hooks = config.setdefault("SessionEnd", [])
    if HOOK not in hooks:
        hooks.append(HOOK)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        write_json(path, config)


def enabled(project: Path) -> bool:
    _, config = hook_config(project)
    return HOOK in config.get("SessionEnd", [])


def restart_options(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    result = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            break
        option, equals, _ = value.partition("=")
        if option in (
            "--model",
            "--prompt-file",
            "--resume",
            "-r",
            "--config",
            "--permission-mode",
        ):
            end = index + (1 if equals else 2)
            if end > len(arguments) or (not equals and arguments[index + 1].startswith("-")):
                return None
            if option in ("--config", "--permission-mode"):
                result.extend(arguments[index:end])
            index = end
        elif option in ("--export", "--respect-workspace-trust"):
            end = index + 1
            if not equals and end < len(arguments) and not arguments[end].startswith("-"):
                end += 1
            result.extend(arguments[index:end])
            index = end
        elif value == "--sandbox":
            result.append(value)
            index += 1
        elif value in ("--continue", "-c") or (value.startswith("-r") and len(value) > 2):
            index += 1
        else:
            return None
    return tuple(result)


def current(store: Store) -> dict:
    run_id = os.environ.get("DS_RUN_ID", "")
    if re.fullmatch(r"[0-9a-f]{32}", run_id):
        for run in sessions.runs(store):
            if (
                run["id"] == run_id
                and run["active"]
                and run["ended_at"] is None
                and run["kind"] == "chat"
            ):
                return run
    raise SwitchError(
        "Use !ds switch inside an interactive chat started with the updated ds run. "
        "It cannot attach to an older, unmanaged, or non-interactive CLI."
    )


def state_path(store: Store, run_id: str, kind: str) -> Path:
    return store.root / "handoffs" / f"{run_id}.{kind}.json"


def queue(store: Store, run: dict, account: Account) -> None:
    if account.name == run["account"]:
        raise SwitchError(f"This chat already uses {account.name}. Choose a different account.")
    if not enabled(Path(run["project"])):
        raise SwitchError(
            "Enable the exit hook first: run ds switch --setup in this chat's project folder, "
            "then restart ds run before switching."
        )
    path = state_path(store, run["id"], "request")
    private_directory(path.parent)
    write_json(path, {"account": account.name})
    state_path(store, run["id"], "exit").unlink(missing_ok=True)


def cancel(store: Store, run: dict) -> None:
    state_path(store, run["id"], "request").unlink(missing_ok=True)
    state_path(store, run["id"], "exit").unlink(missing_ok=True)


def record_exit(store: Store, event: object) -> None:
    if not isinstance(event, dict):
        raise SwitchError("Invalid session-end hook payload.")
    if event.get("hook_event_name") != "SessionEnd" or event.get("reason") != "prompt_input_exit":
        return
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", session_id
    ):
        raise SwitchError("The exit hook did not provide a valid conversation ID.")
    with store.lock():
        run = current(store)
        if state_path(store, run["id"], "request").exists():
            write_json(state_path(store, run["id"], "exit"), {"session_id": session_id})


def finish(
    native: Native, code: int, options: tuple[str, ...] | None
) -> tuple[str, tuple[str, ...]] | None:
    if native.run_id is None or options is None:
        return None
    store = native.store
    with store.lock():
        path = state_path(store, native.run_id, "request")
        if not path.exists():
            return None
        if code:
            raise SwitchError(
                f"The CLI exited with code {code}; the queued switch was not resumed. "
                "Saved history and the default account are unchanged."
            )
        try:
            account = store.account(json.loads(path.read_text())["account"])
            session_id = json.loads(state_path(store, native.run_id, "exit").read_text())[
                "session_id"
            ]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SwitchError(
                "No confirmed exit conversation was captured; nothing was resumed. "
                "Check /hooks and update Devin CLI if needed. Recover with ds sessions, then "
                "ds run --account ALIAS -- --resume SESSION_ID."
            ) from exc
        chat = sessions.resumable(store, session_id)
        if chat["project"] != str(Path.cwd().resolve()):
            raise SwitchError(
                "The exit conversation belongs to another folder; nothing was resumed."
            )
        return account.name, (*options, "--resume", session_id)


def recovery_command(account: str, arguments: tuple[str, ...]) -> str:
    return shlex.join(("ds", "run", "--account", account, "--", *arguments))
