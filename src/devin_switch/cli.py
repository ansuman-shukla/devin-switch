"""The ds command."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from devin_switch import browser
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
    login = commands.add_parser(
        "login", help="Sign into an account once using native authentication"
    )
    login.add_argument("account")
    login.add_argument("--default-browser", action="store_true", help="Use native browser login")
    use = commands.add_parser("use", help="Select an account with saved credentials")
    use.add_argument("account")
    commands.add_parser("next", help="Cycle to another saved login; does not measure quota")
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


def choose_next(store: Store, native: Native) -> Account:
    accounts = store.accounts()
    current = selected_name(store)
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
        app = Path.home() / "Applications" / "Devin Switch.app"
        if not app.is_dir():
            raise SwitchError("The Mac app is not installed. Run make app in the source project.")
        subprocess.run(("/usr/bin/open", str(app)), check=True, timeout=15)
        return 0
    if args.command == "profiles":
        for profile in browser.profiles():
            print(f"{profile.directory:<14} {profile.name:<24} {profile.email}")
        return 0
    with store.lock():
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
        if args.command == "login":
            if native.authenticated(account):
                print(f"{account.name} already has a saved login; no browser needed.")
                return 0
            arguments = ("auth", "login")
            if account.chrome_profile and not args.default_browser:
                print(
                    f"Opening {account.chrome_profile}. Sign in, then paste the token into "
                    "Devin's terminal prompt. The token is not saved by this wrapper.",
                    flush=True,
                )
                browser.open_login(account.chrome_profile)
                arguments += ("--force-manual-token-flow",)
            code = native.interactive(account, arguments)
            if code:
                return code
            native.require_login(account)
            print(f"Login saved for {account.name}. Select it with: ds use {account.name}")
        elif args.command == "status":
            logged_in = native.authenticated(account)
            print(f"{account.name}: {'saved login accepted' if logged_in else 'needs login'}")
            return 0 if logged_in else 1
        elif args.command == "use":
            native.require_login(account)
            store.select(account)
            print(f"Selected {account.name}. Run: ds run")
        elif args.command == "sessions":
            print(json.dumps(native.sessions(account), indent=2))
        elif args.command == "run":
            native.require_login(account)
            arguments = tuple(args.arguments)
            if arguments[:1] == ("--",):
                arguments = arguments[1:]
            print(f"Using {account.name}", file=sys.stderr, flush=True)
            return native.interactive(account, arguments)
    return 0


def main() -> int:
    args = parser().parse_args()
    root = Path(os.environ.get("DS_HOME", "~/.local/share/devin-switch")).expanduser().resolve()
    try:
        return execute(args, Store(root))
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
