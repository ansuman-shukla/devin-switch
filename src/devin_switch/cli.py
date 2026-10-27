"""The ds command."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from devin_switch import browser, chrome_access, handoff, sessions
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
    switch.add_argument(
        "--continue",
        dest="follow_up",
        action="store_true",
        help=f"After reopening, send “{handoff.CONTINUE_PROMPT}” so the agent resumes its work",
    )
    switch.add_argument("--session", help="Target an exact live Switch-managed GUI conversation")
    switch.add_argument("--cancel", action="store_true", help="Cancel a pending switch")
    auto = commands.add_parser(
        "auto-switch",
        help="Show or set automatic switching when a chat runs out of usage",
    )
    auto.add_argument("state", nargs="?", choices=("on", "off"))
    close = commands.add_parser("close", help="Release a saved GUI chat's process when idle")
    close.add_argument("--run", required=True, help="Exact launch ID from ds sessions --gui")
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
    agent = commands.add_parser(
        "browser", help="Manage the agent-owned Chrome behind the browser MCP server"
    )
    actions = agent.add_subparsers(dest="action", required=True, metavar="ACTION")
    install = actions.add_parser(
        "install", help="Register the browser MCP server for every chat and allow its tools"
    )
    install.add_argument("--default", help="Profile for chats that name none: email or directory")
    default = actions.add_parser("default", help="Set the profile for chats that name none")
    default.add_argument("profile")
    actions.add_parser("status", help="List Chrome profiles and running agent browsers")
    refresh = actions.add_parser("refresh", help="Copy a Chrome profile's logins again")
    refresh.add_argument("profile")
    stop = actions.add_parser("stop", help="Close running agent browsers now")
    stop.add_argument("profile", nargs="?")
    actions.add_parser("mcp", help="Serve the browser MCP server over stdio (started by Devin)")
    actions.add_parser("view", help="Serve the Desktop browser panel over stdio (JSON lines)")
    chrome = commands.add_parser(
        "chrome", help="Copy from Chrome's data folder, asking with a popup when macOS blocks it"
    )
    chrome_actions = chrome.add_subparsers(dest="action", required=True, metavar="ACTION")
    chrome_copy = chrome_actions.add_parser(
        "copy", help="Copy files or folders, relative to Chrome's data folder, into a folder"
    )
    chrome_copy.add_argument("items", nargs="+", help='e.g. "Local State" "Profile 1/Preferences"')
    chrome_copy.add_argument("--to", dest="destination", type=Path, required=True)
    chrome_copy.add_argument("--reason", required=True, help="Why it is needed; shown in the popup")
    chat = commands.add_parser("chat", help="Inspect the Desktop chat this process belongs to")
    chat_actions = chat.add_subparsers(dest="action", required=True, metavar="ACTION")
    chat_actions.add_parser("id", help="Print the conversation id (used by the whiteboard MCP)")
    side = commands.add_parser(
        "panels", help="Install the Desktop side panels (whiteboard, agent browser)"
    )
    side_actions = side.add_subparsers(dest="action", required=True, metavar="ACTION")
    side_actions.add_parser("install", help="Install or update the panels extension")
    side_actions.add_parser("status", help="Show whether the panels extension is installed")
    supervise = actions.add_parser("_supervise")
    supervise.add_argument("profile")
    supervise.add_argument("--token", required=True)
    supervise.add_argument("--headed", action="store_true")
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
    if args.command == "browser":
        from devin_switch import agent_browser

        return agent_browser.command(args, store)
    if args.command == "panels":
        from devin_switch import panels

        return panels.command(args)
    if args.command == "chat":
        from devin_switch import acp_state

        print(acp_state.current_chat(store))
        return 0
    if args.command == "acp":
        import asyncio

        from devin_switch import acp

        native = Native(store, find_binary())
        if args.print_registry:
            print(json.dumps(acp.registry(native, args.account, args.sandbox), indent=2))
        else:
            asyncio.run(acp.serve(native, account=args.account, sandbox=args.sandbox))
        return 0
    if args.command == "close":
        from devin_switch import acp_state

        print(acp_state.queue_close(store, args.run))
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
        for profile in browser.profiles(reason="List your Chrome profiles (ds profiles)"):
            print(f"{profile.directory:<14} {profile.name:<24} {profile.email}")
        return 0
    if args.command == "chrome":
        args.destination.mkdir(parents=True, exist_ok=True)
        chrome_access.copy(browser.CHROME_DATA, args.items, args.destination, args.reason)
        for item in args.items:
            target = args.destination / Path(item).name
            print(f"{item}: {'copied to ' + str(target) if target.exists() else 'not found'}")
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
                            "Low on usage? !ds switch resumes here on the next login with credits; "
                            "add --continue to keep working."
                            + (" Auto-switch is on." if handoff.auto_switch_enabled(store) else ""),
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
            follow_up = (
                f"Sending “{handoff.CONTINUE_PROMPT}” so the agent picks up where it left off."
                if arguments[-2:] == ("--", handoff.CONTINUE_PROMPT)
                else "No prompt is replayed. Send your next message when ready."
            )
            print(
                f"Reopening with {name}; this becomes the default for new chats on launch.\n"
                f"{follow_up}\n"
                f"If reopening fails: {handoff.recovery_command(name, arguments)}",
                file=sys.stderr,
                flush=True,
            )
    if args.command == "auto-switch":
        if args.state:
            handoff.set_auto_switch(store, args.state == "on")
        print(handoff.auto_switch_status(store))
        return 0
    if args.command == "switch":
        if args.cancel and (args.account or args.follow_up):
            raise SwitchError("Use an account alias or --continue, or --cancel, not both.")
        if args.session is not None or os.environ.get("DS_ACP_RUN_ID"):
            if args.follow_up:
                raise SwitchError(
                    "In a GUI chat, type /switch-continue [ALIAS]; queued GUI handoffs "
                    "never send prompts."
                )
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
                handoff.queue(store, run, account, follow_up=args.follow_up)
        print(
            f"Switching {run['account']} → {account.name} ({detail}).\n"
            "Reopening here automatically and setting this account as the app's default."
            + (f" Then sending “{handoff.CONTINUE_PROMPT}”." if args.follow_up else ""),
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
            if args.chrome_profile and not browser.has_profile(args.chrome_profile):
                raise SwitchError("Unknown Chrome profile. Run: ds profiles")
            account = store.add(args.account, args.chrome_profile)
            from devin_switch import agent_browser

            if agent_browser.installed(store):
                agent_browser.grant(store, account)
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
