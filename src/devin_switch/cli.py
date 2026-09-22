"""The ds command."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
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
    acp = commands.add_parser("acp", help="Run the Switch-managed GUI agent over ACP stdio")
    acp.add_argument(
        "--account", help="Override the account for newly opened chats, not the default"
    )
    acp.add_argument(
        "--sandbox", action="store_true", help="Retain native sandboxing across switches"
    )
    acp.add_argument(
        "--print-registry",
        action="store_true",
        help="Print a Desktop ACP registry entry; change no settings",
    )
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
        help="Resume this chat with the account reporting the most remaining usage",
        description=(
            "Inside a ds run chat, type !ds switch. Switch refreshes usage, selects the "
            "other saved login with the most remaining daily/weekly allowance, and reopens "
            "the exact conversation here. No project setup or manual exit is needed. "
            "An optional ALIAS overrides automatic selection. The resumed account also becomes "
            "the app's default for new chats when its CLI process starts."
        ),
    )
    switch.add_argument("account", nargs="?")
    switch.add_argument("--session", help="Target an exact live Switch-managed GUI conversation")
    switch.add_argument("--cancel", action="store_true", help="Cancel a pending switch")
    commands.add_parser("list", help="List registered accounts and the current selection")
    commands.add_parser("profiles", help="List existing Chrome profile identifiers")
    status = commands.add_parser("status", help="Check the selected or specified saved login")
    status.add_argument("account", nargs="?")
    run = commands.add_parser("run", help="Run Devin; pass its arguments after --")
    run.add_argument("--account", help="Use this account without changing the saved selection")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    sessions = commands.add_parser("sessions", help="List shared sessions in the current directory")
    sessions.add_argument("--account")
    sessions.add_argument(
        "--gui", action="store_true", help="List live GUI chats with exact switch IDs"
    )
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
    if args.command == "acp":
        import asyncio

        from devin_switch import acp

        native = Native(store, find_binary())
        if args.print_registry:
            print(json.dumps(acp.registry(native, args.account, args.sandbox), indent=2))
        else:
            asyncio.run(acp.serve(native, account=args.account, sandbox=args.sandbox))
        return 0
    if args.command == "sessions" and args.gui:
        from devin_switch import acp_state

        print(json.dumps(acp_state.live(store), indent=2))
        return 0
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
                        with store.lock(timeout=5):
                            handoff.enable(store, account)
                        print(
                            "Low on usage? !ds switch resumes here with the best available login.",
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
            native = replace(native, select_on_start=True)
            print(
                f"Reopening with {name}; this becomes the default for new chats on launch.\n"
                "No prompt is replayed. Send your next message when ready.\n"
                f"If reopening fails: {handoff.recovery_command(name, arguments)}",
                file=sys.stderr,
                flush=True,
            )
    if args.command == "switch":
        if args.account and args.cancel:
            raise SwitchError("Use an account alias or --cancel, not both.")
        if args.session is not None or os.environ.get("DS_ACP_RUN_ID"):
            from devin_switch import acp_state

            print(
                acp_state.queue(
                    Native(store, find_binary()), args.session, args.account, cancel=args.cancel
                )
            )
            return 0
        with store.lock():
            run = handoff.current(store)
            if args.cancel:
                handoff.cancel(store, run)
                print("Switch canceled. This chat's account and the default are unchanged.")
                return 0
        native = Native(store, find_binary())
        if args.account:
            account, detail = store.account(args.account), "chosen explicitly"
        else:
            print("Checking saved accounts for remaining usage…", flush=True)
            account, remaining = handoff.choose_best(native, run["account"])
            detail = f"{remaining:g}% remaining in its limiting quota window"
        with store.lock():
            run = handoff.current(store)
            with store.account_lock(account, shared=True):
                native.require_login(account)
                handoff.queue(store, run, account)
        print(
            f"Switching {run['account']} → {account.name} ({detail}).\n"
            "Reopening here automatically and setting this account as the app's default.",
            flush=True,
        )
        return 0
    with store.lock():
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
        print("\nds: Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
