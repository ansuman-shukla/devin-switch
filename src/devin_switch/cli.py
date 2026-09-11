"""The ds command."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from devin_switch import browser, handoff, sessions
from devin_switch.native import Native, find_binary
from devin_switch.store import Account, Store, SwitchError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="ds", description="Switch saved Devin CLI accounts and retain local conversations."
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("gui", help="Open the native Mac account manager")
    add = commands.add_parser("add", help="Register an account; does not sign in")
    add.add_argument("account")
    add.add_argument("--chrome-profile", help='Chrome directory identifier, e.g. "Profile 1"')
    remove = commands.add_parser("remove", help="Remove a local profile, keeping shared chats")
    remove.add_argument("account")
    remove.add_argument(
        "--yes", action="store_true", help="Confirm removal of saved login and settings"
    )
    login = commands.add_parser(
        "login", help="Sign into an account once using native authentication"
    )
    login.add_argument("account")
    login.add_argument("--default-browser", action="store_true", help="Use native browser login")
    use = commands.add_parser("use", help="Select an account with saved credentials")
    use.add_argument("account")
    commands.add_parser("next", help="Cycle to another saved login; does not measure quota")
    switch = commands.add_parser(
        "switch",
        help="Queue an in-chat account handoff; exit with Ctrl+D to resume here",
        description=(
            "Inside a ds run chat, type !ds switch [ALIAS], then Ctrl+D on an empty input. "
            "The exact saved conversation reopens here; the default account is unchanged. "
            "Without ALIAS, cycle from this chat's account (not a quota check). "
            "First enable the project hook with ds switch --setup, then start ds run."
        ),
    )
    switch.add_argument("account", nargs="?")
    switch_mode = switch.add_mutually_exclusive_group()
    switch_mode.add_argument(
        "--setup",
        action="store_true",
        help="Add the exit hook to this project's .devin/hooks.v1.json",
    )
    switch_mode.add_argument(
        "--cancel", action="store_true", help="Cancel this chat's queued switch"
    )
    commands.add_parser("list", help="List registered accounts and the current selection")
    commands.add_parser("profiles", help="List existing Chrome profile identifiers")
    status = commands.add_parser("status", help="Check the selected or specified saved login")
    status.add_argument("account", nargs="?")
    run = commands.add_parser("run", help="Run Devin; pass its arguments after --")
    run.add_argument("--account", help="Use this account without changing the saved selection")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    sessions = commands.add_parser("sessions", help="List shared sessions in the current directory")
    sessions.add_argument("--account")
    verify = commands.add_parser(
        "verify", help="Check A → B → A saved logins and session visibility"
    )
    verify.add_argument("first")
    verify.add_argument("second")
    return root


def selected_name(store: Store) -> str | None:
    if not (store.root / "selected").exists():
        return None
    return store.selected().name


def choose_next(store: Store, native: Native, *, current: str | None = None) -> Account:
    accounts = store.accounts()
    current = selected_name(store) if current is None else current
    names = tuple(account.name for account in accounts)
    start = names.index(current) + 1 if current in names else 0
    ordered = accounts[start:] + accounts[:start]
    for account in ordered:
        if account.name != current and native.authenticated(account):
            return account
    raise SwitchError("No other account has a saved login. Run: ds login <alias>")


def verify(native: Native, first: Account, second: Account) -> None:
    if first.name == second.name:
        raise SwitchError("Choose two different accounts.")
    for account in (first, second):
        native.require_login(account)
    fingerprints = tuple(
        hashlib.sha256(native.store.credentials(account).read_bytes()).digest()
        for account in (first, second)
    )
    if fingerprints[0] == fingerprints[1]:
        raise SwitchError("Both profiles contain identical credentials. Enroll different accounts.")
    initial_sessions = native.sessions(first)
    for account in (first, second, first):
        native.require_login(account)
        if native.sessions(account) != initial_sessions:
            raise SwitchError("Local session visibility differs between accounts.")
        print(f"{account.name}: saved login accepted; shared session list matches")
    print("A → B → A passed without a browser login. No model request was made.")
    print("Continuing a paid conversation across accounts still needs a live prompt test.")


def execute(args: argparse.Namespace, store: Store) -> int:
    if args.command == "gui":
        app = next(
            (
                directory / "Devin Switch.app"
                for directory in (Path.home() / "Applications", Path("/Applications"))
                if (directory / "Devin Switch.app").is_dir()
            ),
            None,
        )
        if app is None:
            raise SwitchError("Install the Mac app from a release, or run make app from source.")
        subprocess.run(("/usr/bin/open", str(app)), check=True, timeout=15)
        return 0
    if args.command == "profiles":
        for profile in browser.profiles():
            print(f"{profile.directory:<14} {profile.name:<24} {profile.email}")
        return 0
    if args.command in {"run", "login"}:
        native = Native(store, find_binary())
        arguments = tuple(args.arguments) if args.command == "run" else ("auth", "login")
        if arguments[:1] == ("--",):
            arguments = arguments[1:]
        kind = "login" if args.command == "login" else "chat"
        if kind == "chat" and arguments[:1] in (("auth",), ("mcp",), ("list",), ("doctor",)):
            kind = "command"
        options = handoff.restart_options(arguments) if kind == "chat" else None
        name = args.account
        while True:
            with sessions.managed(
                native, name, arguments, kind=kind, can_handoff=options is not None
            ) as (runner, account, arguments):
                if args.command == "login":
                    if runner.authenticated(account):
                        print(f"{account.name} already has a saved login; no browser needed.")
                        return 0
                    if account.chrome_profile and not args.default_browser:
                        print(
                            f"Opening {account.chrome_profile}. Sign in, then paste the token into "
                            "Devin's terminal prompt. The token is not saved by this wrapper.",
                            flush=True,
                        )
                        browser.open_login(account.chrome_profile)
                        arguments += ("--force-manual-token-flow",)
                else:
                    print(f"Using {account.name}", file=sys.stderr, flush=True)
                    if options is not None:
                        print(
                            "Switch accounts here: !ds switch [alias], then Ctrl+D. "
                            "One-time project setup: ds switch --setup",
                            file=sys.stderr,
                            flush=True,
                        )
                code = runner.interactive(account, arguments)
                if args.command == "login" and not code:
                    runner.require_login(account)
                    print(f"Login saved for {account.name}. Select it with: ds use {account.name}")
            next_launch = handoff.finish(runner, code, options)
            if next_launch is None:
                return code
            name, arguments = next_launch
            print(
                f"Reopening the saved conversation with {name}; default account unchanged.\n"
                "No prompt is replayed. Send your next message when ready.\n"
                f"If reopening fails: {handoff.recovery_command(name, arguments)}",
                file=sys.stderr,
                flush=True,
            )
    with store.lock():
        if args.command == "switch":
            if args.account and (args.setup or args.cancel):
                raise SwitchError("Use an account alias, --setup, or --cancel, not both.")
            if args.setup:
                handoff.install(Path.cwd().resolve())
                print("Exit hook enabled in .devin/hooks.v1.json. Start a fresh ds run to use it.")
                return 0
            run = handoff.current(store)
            if args.cancel:
                handoff.cancel(store, run)
                print("Queued switch canceled. This chat's account and the default are unchanged.")
                return 0
            native = Native(store, find_binary())
            account = (
                store.account(args.account)
                if args.account
                else choose_next(store, native, current=run["account"])
            )
            with store.account_lock(account, shared=True):
                native.require_login(account)
                handoff.queue(store, run, account)
            print(
                f"Switch queued: {run['account']} → {account.name} (quota not checked).\n"
                "Press Ctrl+D on an empty input to exit and reopen this conversation here.\n"
                "If bash mode remains open, press Esc first. Cancel with !ds switch --cancel.\n"
                "The saved default is unchanged; your last prompt will not be replayed."
            )
            return 0
        if args.command == "remove":
            if not args.yes:
                raise SwitchError("Confirm profile removal with ds remove <alias> --yes.")
            store.remove(store.account(args.account))
            print(f"Removed {args.account}. Shared conversations and project files were preserved.")
            return 0
        if args.command == "add":
            if args.chrome_profile and args.chrome_profile not in {
                item.directory for item in browser.profiles()
            }:
                raise SwitchError("Unknown Chrome profile. Run: ds profiles")
            account = store.add(args.account, args.chrome_profile)
            print(f"Added {account.name}. Sign in once: ds login {account.name}")
            return 0
        if args.command == "list":
            current = selected_name(store)
            accounts = store.accounts()
            if not accounts:
                print("No accounts yet. Run: ds add <alias> --chrome-profile 'Profile 1'")
            for account in accounts:
                saved = store.credentials(account).is_file()
                marker = "*" if account.name == current else " "
                state = "credentials saved" if saved else "needs login"
                print(
                    f"{marker} {account.name:<20} {state:<20} {account.chrome_profile or 'default'}"
                )
            return 0
        native = Native(store, find_binary())
        if args.command == "verify":
            verify(native, store.account(args.first), store.account(args.second))
            return 0
        if args.command == "next":
            account = choose_next(store, native)
            store.select(account)
            print(f"Selected {account.name}. Run: ds run")
            return 0
        account = store.account(args.account) if args.account else store.selected()
        if args.command == "status":
            logged_in = native.authenticated(account)
            print(f"{account.name}: {'saved login accepted' if logged_in else 'needs login'}")
            return 0 if logged_in else 1
        elif args.command == "use":
            native.require_login(account)
            store.select(account)
            print(f"Selected {account.name}. Run: ds run")
        elif args.command == "sessions":
            print(json.dumps(native.sessions(account), indent=2))
    return 0


def main() -> int:
    root = Path(os.environ.get("DS_HOME", "~/.local/share/devin-switch")).expanduser().resolve()
    try:
        if sys.argv[1:] == ["_session-end"]:
            try:
                event = json.loads(sys.stdin.read(65536))
            except ValueError as exc:
                raise SwitchError("Invalid session-end hook payload.") from exc
            handoff.record_exit(Store(root), event)
            return 0
        return execute(parser().parse_args(), Store(root))
    except SwitchError as exc:
        print(f"ds: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        # Avoid echoing subprocess arguments/output, which may contain secrets.
        print(
            f"ds: Operation failed ({type(exc).__name__}); check access and retry.", file=sys.stderr
        )
        return 1
    except KeyboardInterrupt:
        print("\nds: Interrupted. Saved account selection is unchanged.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
