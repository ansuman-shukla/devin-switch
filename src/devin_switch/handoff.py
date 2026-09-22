import json
import os
import re
import shlex
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from devin_switch import sessions, usage
from devin_switch.native import Native
from devin_switch.store import Account, Store, SwitchError, private_directory, write_json

HOOK_COMMAND = (
    'if [ -n "${DS_RUN_ID:-}" ] && [ -n "${DS_EXECUTABLE:-}" ]; then '
    'if [ "${DS_FROZEN:-0}" = 1 ]; then "$DS_EXECUTABLE" _session-end; '
    'else "$DS_EXECUTABLE" -m devin_switch.cli _session-end; fi; fi'
)
HOOK = {"matcher": "", "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}


def json_source(text: str) -> str:
    text = re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/',
        lambda match: match[0] if match[0].startswith('"') else re.sub(r"[^\r\n]", " ", match[0]),
        text,
    )
    return re.sub(
        r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])',
        lambda match: match[0] if match[0].startswith('"') else " ",
        text,
    )


def member_start(source: str, start: int, name: str) -> int | None:
    decoder = json.JSONDecoder()
    index = start + 1
    found = None
    while True:
        index += len(source[index:]) - len(source[index:].lstrip())
        if source[index] == "}":
            return found
        key, index = decoder.raw_decode(source, index)
        index = source.index(":", index) + 1
        index += len(source[index:]) - len(source[index:].lstrip())
        value_start = index
        _, index = decoder.raw_decode(source, index)
        if key == name:
            found = value_start
        index += len(source[index:]) - len(source[index:].lstrip())
        if source[index] == ",":
            index += 1


def enable(store: Store, account: Account) -> None:
    path = store.directory(account.name) / "config/devin/config.json"
    if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise SwitchError(
            "Refusing to change linked profile config; existing settings are unchanged."
        )
    original = path.read_text() if path.exists() else "{}"
    try:
        source = json_source(original)
        config = json.loads(source)
        hooks = config.get("hooks")
        events = hooks.get("SessionEnd", []) if hooks is not None else []
        if not isinstance(events, list):
            raise ValueError("Invalid event list")
        if HOOK in events:
            return
        root = source.index("{")
        hook_start = member_start(source, root, "hooks")
        event_start = member_start(source, hook_start, "SessionEnd") if hooks is not None else None
        removed = 0
        if hook_start is None:
            index, addition = root + 1, '"hooks":' + json.dumps({"SessionEnd": [HOOK]})
            nonempty = bool(config)
        elif hooks is None:
            index, addition = hook_start, json.dumps({"SessionEnd": [HOOK]})
            nonempty, removed = False, 4
        elif event_start is None:
            index, addition = hook_start + 1, '"SessionEnd":' + json.dumps([HOOK])
            nonempty = bool(hooks)
        else:
            index, addition = event_start + 1, json.dumps(HOOK)
            nonempty = bool(events)
        updated = (
            original[:index] + addition + ("," if nonempty else "") + original[index + removed :]
        )
        json.loads(json_source(updated))
    except (ValueError, AttributeError, TypeError, IndexError) as exc:
        raise SwitchError(
            "Cannot extend this profile's config; existing settings were preserved."
        ) from exc
    private_directory(path.parent)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
            if path.exists() and path.read_text() != original:
                raise SwitchError("The profile config changed during launch; retry ds run.")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def choose_best(native: Native, current_account: str) -> tuple[Account, float]:
    store = native.store
    with store.lock():
        accounts = [account for account in store.accounts() if account.name != current_account]

    def check(account: Account) -> tuple[Account, float] | None:
        try:
            with store.account_lock(account, shared=True):
                reading = usage.refresh_account(store, account, force=True)
                remaining = usage.remaining_allowance(reading)
                return (account, remaining) if remaining is not None and remaining > 0 else None
        except SwitchError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        available = [result for result in executor.map(check, accounts) if result is not None]
    for account, remaining in sorted(available, key=lambda result: (-result[1], result[0].name)):
        try:
            with store.account_lock(account, shared=True):
                if native.authenticated(account):
                    return account, remaining
        except SwitchError:
            continue
    raise SwitchError(
        "No other saved login has confirmed remaining usage. This chat is unchanged. "
        "If usage is unavailable, choose an account explicitly: !ds switch ALIAS."
    )


def controller_available(store: Store, run_id: str) -> bool:
    return store.locked(f"controller-{run_id}.lock")


def restart_options(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    result = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            break
        option, equals, _ = value.partition("=")
        if option == "--config":
            return None
        if option in (
            "--model",
            "--prompt-file",
            "--resume",
            "-r",
            "--permission-mode",
        ):
            end = index + (1 if equals else 2)
            if end > len(arguments) or (not equals and arguments[index + 1].startswith("-")):
                return None
            if option == "--permission-mode":
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
                and controller_available(store, run_id)
            ):
                return run
    raise SwitchError(
        "Use !ds switch inside a terminal chat started with the updated ds run. "
        "Older open CLIs need one restart; no project setup is required."
    )


def state_path(store: Store, run_id: str, kind: str) -> Path:
    return store.root / "handoffs" / f"{run_id}.{kind}.json"


def queue(store: Store, run: dict, account: Account) -> None:
    if account.name == run["account"]:
        raise SwitchError(f"This chat already uses {account.name}. Choose a different account.")
    path = state_path(store, run["id"], "request")
    private_directory(path.parent)
    state_path(store, run["id"], "exit").unlink(missing_ok=True)
    write_json(
        path, {"account": account.name, "requester_pid": os.getpid(), "created_at": time.time()}
    )


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
    run_id = os.environ.get("DS_RUN_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        return
    if state_path(store, run_id, "request").exists():
        run = current(store)
        write_json(state_path(store, run["id"], "exit"), {"session_id": session_id})


def finish(
    native: Native, code: int, options: tuple[str, ...] | None
) -> tuple[str, tuple[str, ...]] | None:
    if native.run_id is None or options is None:
        return None
    store = native.store
    with store.lock(timeout=5):
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
