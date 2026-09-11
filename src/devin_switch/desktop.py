"""Private stdin/stdout bridge for the native Mac app; no network listener."""

import fcntl
import json
import os
import shlex
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from devin_switch import browser, enrollment, usage
from devin_switch.cli import choose_next, selected_name
from devin_switch.native import Native, find_binary
from devin_switch.store import Store, SwitchError, private_directory


def is_busy(store: Store) -> bool:
    try:
        handle = (store.root / "lock").open("r")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def snapshot(store: Store) -> dict[str, object]:
    try:
        chrome_profiles = tuple(asdict(profile) for profile in browser.profiles())
        chrome_error = None
    except SwitchError as exc:
        chrome_profiles = ()
        chrome_error = str(exc)
    return {
        "accounts": tuple(
            {
                "name": account.name,
                "chrome_profile": account.chrome_profile,
                "usage": asdict(usage.read_cached(store, account)),
                "saved_login": (
                    not store.credentials(account).is_symlink()
                    and store.credentials(account).is_file()
                    and store.credentials(account).stat().st_size > 0
                ),
            }
            for account in store.accounts()
        ),
        "selected": selected_name(store),
        "profiles": chrome_profiles,
        "chrome_error": chrome_error,
        "busy": is_busy(store),
    }


def string_field(request: dict[str, object], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value:
        raise SwitchError(f"{name.replace('_', ' ').capitalize()} is required.")
    return value


def terminal_launcher(store: Store, arguments: tuple[str, ...], project: Path) -> Path:
    directory = store.root / "launchers"
    private_directory(directory)
    launcher = directory / f"session-{uuid.uuid4().hex}.command"
    command = shlex.join(
        (
            "/usr/bin/env",
            f"DS_HOME={store.root}",
            f"DS_BINARY={find_binary()}",
            sys.executable,
            "-m",
            "devin_switch.cli",
            *arguments,
        )
    )
    content = (
        "#!/bin/zsh\n"
        f"cd -- {shlex.quote(str(project))} || exit 1\n"
        f"{command}\n"
        "ds_result=$?\n"
        "printf '\\nSession closed. You can return to Devin Switch.\\n'\n"
        "exit $ds_result\n"
    )
    descriptor = os.open(launcher, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
    return launcher


def action(store: Store, request: dict[str, object]) -> dict[str, object]:
    operation = string_field(request, "action")
    if operation == "state":
        return {"state": snapshot(store)}
    if operation == "usage":
        usage.refresh_all(store, force=request.get("force") == "true")
        return {"state": snapshot(store)}
    with store.lock():
        if operation == "import":
            added, renamed = enrollment.import_profiles(store, browser.profiles())
            return {
                "message": (
                    f"Added {added} accounts and renamed {renamed}. "
                    "Sign in to each new account to see usage."
                )
            }
        if operation == "add":
            name = string_field(request, "account")
            profile = request.get("chrome_profile")
            if profile is not None and (
                not isinstance(profile, str)
                or profile not in {item.directory for item in browser.profiles()}
            ):
                raise SwitchError("Choose an existing Chrome profile, or use the default browser.")
            account = store.add(name, profile)
            return {"message": f"Added {account.name}. Select Sign in to save its login."}
        native = Native(store, find_binary())
        if operation == "next":
            account = choose_next(store, native)
            store.select(account)
            return {"message": f"{account.name} is now active.", "focus": account.name}
        account = store.account(string_field(request, "account"))
        if operation == "select":
            native.require_login(account)
            store.select(account)
            return {"message": f"{account.name} is now active."}
        if operation == "check":
            native.require_login(account)
            return {"message": f"Saved login accepted for {account.name}."}
        if operation == "login":
            if native.authenticated(account):
                return {"message": f"{account.name} already has a saved login."}
            launcher = terminal_launcher(store, ("login", account.name), Path.home())
            return {
                "launcher": str(launcher),
                "message": "Finish sign-in in Terminal, then return here.",
            }
        if operation in {"start", "resume"}:
            project = Path(string_field(request, "project")).expanduser().resolve()
            if not project.is_dir():
                raise SwitchError("Choose an existing project folder.")
            native.require_login(account)
            if operation == "resume" and not native.sessions(account, cwd=project):
                raise SwitchError(
                    "No conversation in this folder yet. Choose Start new and send a message first."
                )
            arguments = ("run", "--account", account.name)
            if operation == "resume":
                arguments += ("--", "--continue")
            launcher = terminal_launcher(store, arguments, project)
            return {
                "launcher": str(launcher),
                "message": f"Opening {account.name} in Terminal. Exit before switching accounts.",
            }
        raise SwitchError("Unknown desktop action.")


def main() -> int:
    store = Store(
        Path(os.environ.get("DS_HOME", "~/.local/share/devin-switch")).expanduser().resolve()
    )
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise SwitchError("Expected a desktop action object.")
        result = action(store, request)
        print(json.dumps({"ok": True, **result}))
        return 0
    except (SwitchError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "message": str(exc)}))
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "message": f"Operation failed ({type(exc).__name__})."}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
