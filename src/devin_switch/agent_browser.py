"""An agent-owned Chrome per cloned profile, separate from the user's running browser.

Each Chrome profile gets its own user-data directory under Switch's state, seeded once by
copying that profile's cookies and site storage (never decrypted or read here). The browser
starts headless with a command-line debugging port, so there is no window, focus change or
"Allow remote debugging" prompt. A detached supervisor closes it shortly after the last chat
releases its lease; chats release leases explicitly, after idling, or when they exit.
"""

import asyncio
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from devin_switch import browser, handoff
from devin_switch.executable import command_prefix
from devin_switch.native import user_config_home
from devin_switch.store import (
    Account,
    Store,
    SwitchError,
    json_source,
    private_directory,
    write_json,
)

SERVER = "browser"
PERMISSION = f"mcp__{SERVER}__*"
MCP_PACKAGE = "chrome-devtools-mcp@1.10.1"
CLONED = (
    "Cookies",
    "Cookies-journal",
    "Network",
    "Local Storage",
    "IndexedDB",
    "WebStorage",
    "Storage",
    "Preferences",
)
LINE_LIMIT = 256 * 1024 * 1024
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
INSTRUCTIONS = (
    "Agent-owned Chrome, separate from the user's own browser. Each Chrome profile has its own "
    "instance, cloned from the user's profile with its logins, and runs headless by default: no "
    "window and no focus change, while take_snapshot/take_screenshot still render the full UI. "
    "Before browsing, call browser_profile with the profile the user named (an email such as "
    "name@example.com, a profile name, or a directory such as 'Profile 3'); otherwise the "
    "default profile is used. Never ask the user to allow remote debugging. Page tools need the "
    "pageId from list_pages/new_page. Call browser_close when the browser work is done. After "
    "idling the browser closes by itself and the next tool call reopens it with fresh pages."
)
COPY_HINT = (
    "Profiles copied earlier keep working; copy another with `ds browser refresh PROFILE` from "
    "an app that has Full Disk Access."
)
NEEDS_PROFILE = (
    "No browser profile is selected and no default is set. Call browser_profile with the "
    "profile to use (email, profile name or directory); call it without arguments to list them."
)
TOOLS = [
    {
        "name": "browser_profile",
        "description": (
            "Choose which of the user's Chrome profiles this chat's agent browser uses, starting "
            "its separate instance if needed. Without arguments, list profiles and their state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Account email, profile name, or directory like 'Profile 3'.",
                },
                "headed": {
                    "type": "boolean",
                    "description": (
                        "Open a visible window in the background so the user can watch. Default: "
                        "headless, which still renders the full UI for snapshots and screenshots."
                    ),
                },
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "Copy logins from the user's Chrome profile again first, e.g. when the "
                        "agent browser is signed out. Needs no other chat using it."
                    ),
                },
            },
        },
    },
    {
        "name": "browser_close",
        "description": (
            "Release this chat's agent browser when the browser work is finished. It closes once "
            "no chat uses it; a later browser tool call reopens it."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def seconds(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def slug(directory: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", directory.lower()).strip("-") or "profile"


def folder_for(store: Store, directory: str) -> Path:
    return store.root / "browser" / "profiles" / slug(directory)


def private_folder(store: Store, directory: str) -> Path:
    folder = folder_for(store, directory)
    for path in (folder.parent.parent, folder.parent, folder):
        private_directory(path)
    return folder


def label(profile: browser.ChromeProfile) -> str:
    return f"{profile.directory} ({profile.email or profile.name})"


def copied(store: Store) -> tuple[browser.ChromeProfile, ...]:
    items = []
    for path in (store.root / "browser/profiles").glob("*/source.json"):
        try:
            source = json.loads(path.read_text())
            if (path.parent / "user-data").is_dir():
                items.append(
                    browser.ChromeProfile(
                        source["directory"], source.get("name", ""), source.get("email", "")
                    )
                )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return tuple(sorted(items, key=lambda item: item.directory))


def profiles(store: Store) -> tuple[tuple[browser.ChromeProfile, ...], bool]:
    """Chrome's profiles, or only the copied ones (flagged True) when macOS blocks Chrome's data."""
    try:
        return browser.profiles(), False
    except browser.ChromeBlocked:
        return copied(store), True


def resolve(store: Store, query: str) -> browser.ChromeProfile:
    wanted = query.strip().casefold()
    items, blocked = profiles(store)
    for key in (
        lambda item: item.directory.casefold(),
        lambda item: slug(item.directory),
        lambda item: item.email.casefold(),
        lambda item: item.name.casefold(),
    ):
        matches = [item for item in items if wanted and key(item) == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches:
            choices = ", ".join(label(item) for item in matches)
            raise SwitchError(f"{query!r} matches several Chrome profiles: {choices}. Pick one.")
    if blocked:
        raise browser.ChromeBlocked(
            f"{query!r} is not a copied Chrome profile. {browser.BLOCKED} {COPY_HINT}"
        )
    raise SwitchError(f"No Chrome profile matches {query!r}. List them with: ds browser status")


def settings(store: Store) -> dict:
    try:
        value = json.loads((store.root / "browser/settings.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_settings(store: Store, **changes: object) -> None:
    private_directory(store.root)
    private_directory(store.root / "browser")
    write_json(store.root / "browser/settings.json", {**settings(store), **changes})


def installed(store: Store) -> bool:
    return settings(store).get("installed") is True


def default_profile(store: Store) -> browser.ChromeProfile | None:
    directory = settings(store).get("default")
    return resolve(store, directory) if isinstance(directory, str) else None


def clone(store: Store, profile: browser.ChromeProfile) -> None:
    source = browser.CHROME_DATA / profile.directory
    try:
        os.listdir(source)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise SwitchError(f"Chrome's folder for {profile.directory!r} is missing.") from exc
    except PermissionError as exc:
        raise browser.ChromeBlocked(
            f"Cannot copy logins from {label(profile)}. {browser.BLOCKED}"
        ) from exc
    folder = private_folder(store, profile.directory)
    staging = folder / "user-data.partial"
    shutil.rmtree(staging, ignore_errors=True)
    private_directory(staging)
    private_directory(staging / "Default")
    for name in CLONED:
        item, target = source / name, staging / "Default" / name
        if item.is_dir():
            try:
                shutil.copytree(item, target, symlinks=True)
            except shutil.Error:
                pass  # Chrome may rewrite storage while it is copied; keep what was readable.
        elif item.is_file():
            shutil.copy2(item, target)
    data = folder / "user-data"
    shutil.rmtree(data, ignore_errors=True)
    staging.rename(data)
    write_json(
        folder / "source.json",
        {
            "directory": profile.directory,
            "name": profile.name,
            "email": profile.email,
            "cloned_at": time.time(),
        },
    )


def wait_lock(fd: int, operation: int, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise SwitchError(message) from exc
            time.sleep(0.05)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def finished(pid: int) -> bool:
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == pid
    except ChildProcessError:
        return not alive(pid)


def endpoint(data: Path) -> int | None:
    try:
        port = int((data / "DevToolsActivePort").read_text().split()[0])
        with LOCAL.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            json.load(response)
    except (OSError, ValueError, IndexError):
        return None
    return port


def state(store: Store, directory: str) -> dict | None:
    try:
        value = json.loads((folder_for(store, directory) / "state.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("supervisor"), int):
        return None
    return value if alive(value["supervisor"]) else None


def running(store: Store, directory: str) -> dict | None:
    current = state(store, directory)
    port = current and endpoint(folder_for(store, directory) / "user-data")
    return {**current, "port": port} if current and port else None


def stop(current: dict) -> None:
    try:
        os.kill(current["supervisor"], signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 30
    while alive(current["supervisor"]) and time.monotonic() < deadline:
        time.sleep(0.1)


def chrome_command(data: Path, headed: bool) -> list[str]:
    executable = os.environ.get("DS_BROWSER_CHROME") or str(
        browser.CHROME_APP / "Contents/MacOS/Google Chrome"
    )
    window = () if headed else ("--headless=new", "--window-size=1440,900")
    return [
        executable,
        f"--user-data-dir={data}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        *window,
        "about:blank",
    ]


def chrome_pid(data: Path) -> int | None:
    listing = subprocess.run(
        ("/bin/ps", "-axww", "-o", "pid=,command="),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    ).stdout
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if f"--user-data-dir={data} " in f"{command} " and "--type=" not in command:
            return int(pid)
    return None


def launch(data: Path, headed: bool) -> int:
    command = chrome_command(data, headed)
    if not headed or os.environ.get("DS_BROWSER_CHROME"):
        return subprocess.Popen(command, stdin=subprocess.DEVNULL).pid
    # LaunchServices with -g opens the window without activating Chrome.
    subprocess.run(
        ("/usr/bin/open", "-g", "-n", "-a", str(browser.CHROME_APP), "--args", *command[1:]),
        check=True,
        timeout=15,
    )
    deadline = time.monotonic() + 20
    while (pid := chrome_pid(data)) is None:
        if time.monotonic() > deadline:
            raise SwitchError("The visible agent browser did not start.")
        time.sleep(0.2)
    return pid


def terminate(pid: int) -> None:
    for sig, wait in ((signal.SIGTERM, 15.0), (signal.SIGKILL, 5.0)):
        if finished(pid):
            return
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait
        while not finished(pid) and time.monotonic() < deadline:
            time.sleep(0.1)


def supervise(store: Store, directory: str, headed: bool, token: str) -> None:
    if os.fork():
        os._exit(0)
    os.setsid()
    folder = folder_for(store, directory)
    data = folder / "user-data"
    stopping: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda number, _: stopping.append(number))
    lease = os.open(folder / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
    pid = chrome_pid(data)
    if pid is None:
        (data / "DevToolsActivePort").unlink(missing_ok=True)
        pid = launch(data, headed)
    write_json(
        folder / "state.json",
        {"supervisor": os.getpid(), "chrome": pid, "headed": headed, "token": token},
    )
    grace, idle_since = seconds("DS_BROWSER_GRACE", 10), None
    try:
        while not stopping and not finished(pid):
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                idle_since = None
            else:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= grace:
                    break  # Keep the exclusive lock so no chat attaches while Chrome closes.
                fcntl.flock(lease, fcntl.LOCK_UN)
            time.sleep(0.25)
    finally:
        terminate(pid)
        (data / "DevToolsActivePort").unlink(missing_ok=True)
        try:
            if json.loads((folder / "state.json").read_text()).get("token") == token:
                (folder / "state.json").unlink()
        except (OSError, ValueError):
            pass


def start_supervisor(store: Store, profile: browser.ChromeProfile, headed: bool) -> dict:
    folder = folder_for(store, profile.directory)
    token = os.urandom(8).hex()
    with open(folder / "chrome.log", "wb") as log:
        subprocess.run(
            (
                *command_prefix(),
                "browser",
                "_supervise",
                profile.directory,
                "--token",
                token,
                *(("--headed",) if headed else ()),
            ),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env={**os.environ, "DS_HOME": str(store.root)},
            check=True,
            timeout=30,
        )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = running(store, profile.directory)
        if current and current.get("token") == token:
            return current
        time.sleep(0.2)
    raise SwitchError(f"The agent browser did not start; see {folder / 'chrome.log'}.")


class Lease:
    """A chat's shared hold on one profile's agent browser, kept while it uses it."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.fd: int | None = None
        self.profile: browser.ChromeProfile | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def exclusive(self) -> bool:
        assert self.fd is not None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            wait_lock(self.fd, fcntl.LOCK_SH, 30, "The agent browser is busy; retry shortly.")
            return False

    def acquire(
        self, profile: browser.ChromeProfile, headed: bool | None = None, refresh: bool = False
    ) -> str:
        folder = private_folder(self.store, profile.directory)
        if self.profile != profile:
            self.release()
        self.profile = profile
        if self.fd is None:
            fd = os.open(folder / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                wait_lock(fd, fcntl.LOCK_SH, 30, "The agent browser is still closing; retry.")
            except SwitchError:
                os.close(fd)
                raise
            self.fd = fd
        start = os.open(folder / "start.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            wait_lock(start, fcntl.LOCK_EX, 90, "Another chat is starting this agent browser.")
            current, note = running(self.store, profile.directory), ""
            if current and (refresh or (headed is not None and current["headed"] != headed)):
                if self.exclusive():
                    stop(current)
                    current = None
                    fcntl.flock(self.fd, fcntl.LOCK_SH)
                elif refresh:
                    raise SwitchError(
                        "Another chat is using this agent browser; refresh after it is closed."
                    )
                else:
                    note = " Another chat is using it, so its window mode was kept."
            if current is None:
                if refresh or not (folder / "user-data").is_dir():
                    clone(self.store, profile)
                current = start_supervisor(self.store, profile, bool(headed))
        finally:
            os.close(start)
        mode = "visible window" if current["headed"] else "headless"
        return (
            f"Agent browser ready for {label(profile)}: {mode}, a separate Chrome instance on "
            f"127.0.0.1:{current['port']}. The user's own Chrome is untouched.{note}"
        )


def overview(store: Store, current: browser.ChromeProfile | None = None) -> str:
    default = settings(store).get("default")
    lines = ["Chrome profiles (name one by email, profile name, or directory):"]
    items, blocked = profiles(store)
    for profile in items:
        cloned = (folder_for(store, profile.directory) / "user-data").is_dir()
        live = running(store, profile.directory) if cloned else None
        tags = [
            tag
            for tag, applies in (
                ("default", profile.directory == default),
                ("this chat", profile == current),
                ("cloned", cloned),
                (f"running {'visible' if live and live['headed'] else 'headless'}", live),
            )
            if applies
        ]
        if profile.email or tags:
            lines.append(
                f"- {profile.directory}: {profile.name} <{profile.email or 'no account'}>"
                + (f" [{', '.join(tags)}]" if tags else "")
            )
    if blocked:
        lines.append(f"Only copied profiles are listed. {browser.BLOCKED} {COPY_HINT}")
    return "\n".join(lines)


def profile_rows(store: Store, attached: browser.ChromeProfile | None = None) -> list[dict]:
    default = settings(store).get("default")
    rows = []
    for profile in profiles(store)[0]:
        folder = folder_for(store, profile.directory)
        cloned = (folder / "user-data").is_dir()
        live = running(store, profile.directory) if cloned else None
        try:
            since = (folder / "state.json").stat().st_mtime if live else None
        except OSError:
            since = None
        rows.append(
            {
                "directory": profile.directory,
                "name": profile.name,
                "email": profile.email,
                "default": profile.directory == default,
                "cloned": cloned,
                "attached": profile == attached,
                "running": live
                and {"port": live["port"], "headed": live["headed"], "since": since},
            }
        )
    return rows


def view(store: Store, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Serve the Desktop browser panel: JSON lines in, JSON lines out, one lease while attached."""
    lease = Lease(store)

    def handle(request: dict) -> dict:
        op = request.get("op")
        if op == "profiles":
            return {"profiles": profile_rows(store, lease.profile if lease.held else None)}
        if op == "attach":
            profile = resolve(store, str(request.get("profile") or ""))
            message = lease.acquire(profile)
            current = running(store, profile.directory)
            if not current:
                raise SwitchError("The agent browser stopped while attaching; retry.")
            return {"profile": profile.directory, "port": current["port"], "message": message}
        if op == "detach":
            lease.release()
            return {}
        raise SwitchError(f"Unknown request {op!r}.")

    try:
        for line in stdin:
            if not line.strip():
                continue
            request: dict = {}
            try:
                request = json.loads(line)
                reply = {"id": request.get("id"), "ok": True, **handle(request)}
            except (SwitchError, ValueError, AttributeError) as exc:
                reply = {"id": request.get("id"), "ok": False, "error": str(exc)}
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    finally:
        lease.release()


class Proxy:
    """Serve chrome-devtools-mcp to one chat, bound to the chat's chosen agent browser."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.lease = Lease(store)
        self.profile: browser.ChromeProfile | None = None
        self.headed: bool | None = None
        self.child: asyncio.subprocess.Process | None = None
        self.child_data: Path | None = None
        self.generation = 0
        self.pending: dict[object, tuple[int, str]] = {}
        self.child_requests: set[object] = set()
        self.internal: dict[str, asyncio.Future] = {}
        self.initialize: dict | None = None
        self.initialized = False
        self.lock = asyncio.Lock()
        self.active = 0
        self.last_used = time.monotonic()
        self.tasks: set[asyncio.Task] = set()

    def background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def data_for(self, profile: browser.ChromeProfile | None) -> Path:
        if profile is None:
            return self.store.root / "browser" / "unbound"
        return folder_for(self.store, profile.directory) / "user-data"

    @staticmethod
    def send(message: dict) -> None:
        sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        sys.stdout.buffer.flush()

    def reply(self, request: dict, text: str, error: bool = False) -> None:
        result = {"content": [{"type": "text", "text": text}], "isError": error}
        self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    @staticmethod
    def mcp_command(data: Path) -> tuple[list[str], dict[str, str]]:
        environment = {
            **os.environ,
            "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1",
            "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1",
        }
        if custom := os.environ.get("DS_BROWSER_MCP"):
            base = json.loads(custom)
        else:
            npx = shutil.which("npx") or next(
                (
                    path
                    for path in ("/opt/homebrew/bin/npx", "/usr/local/bin/npx")
                    if os.access(path, os.X_OK)
                ),
                None,
            )
            if npx is None:
                raise SwitchError("npx was not found; install Node.js to use the agent browser.")
            environment["PATH"] = f"{Path(npx).parent}{os.pathsep}{environment.get('PATH', '')}"
            base = [npx, "-y", MCP_PACKAGE, "--no-usage-statistics", "--no-performance-crux"]
        # Client roots replace the temp-only default; keep temp dirs writable for evidence files.
        temporary = {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
        workspaces = [f"--workspace={path}" for path in sorted(temporary)]
        return [*base, "--autoConnect", f"--userDataDir={data}", *workspaces], environment

    async def spawn(self, data: Path) -> None:
        command, environment = self.mcp_command(data)
        self.generation += 1
        self.child_requests.clear()
        self.child = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
            env=environment,
        )
        self.child_data = data
        self.background(self.read_child(self.child, self.generation))

    async def read_child(self, process: asyncio.subprocess.Process, generation: int) -> None:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and generation == self.generation:
                self.from_child(message)
        if generation == self.generation:
            self.child = None
            self.fail(generation, "The browser tool server stopped; retry the call.")

    def from_child(self, message: dict) -> None:
        if "method" in message:
            if "id" in message:
                self.child_requests.add(message["id"])
            self.send(message)
            return
        key = message.get("id")
        if isinstance(key, str) and key in self.internal:
            self.internal.pop(key).set_result(message)
            return
        entry = self.pending.pop(key, None)
        if entry is None:
            return
        method = entry[1]
        if method == "tools/call":
            self.active -= 1
            self.last_used = time.monotonic()
        result = message.get("result")
        if isinstance(result, dict) and method == "initialize":
            result["instructions"] = INSTRUCTIONS
        elif isinstance(result, dict) and method == "tools/list":
            result["tools"] = [*result.get("tools", []), *TOOLS]
        self.send(message)

    def fail(self, generation: int, text: str) -> None:
        for key, (owner, method) in tuple(self.pending.items()):
            if owner == generation:
                del self.pending[key]
                self.active -= method == "tools/call"
                self.send({"jsonrpc": "2.0", "id": key, "error": {"code": -32603, "message": text}})

    async def write_child(self, message: dict) -> None:
        if self.child is None or self.child.stdin is None:
            return
        try:
            self.child.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await self.child.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def restart(self, data: Path) -> None:
        if (old := self.child) is not None:
            self.child = None
            self.fail(self.generation, "The browser profile changed; retry the call.")
            if old.stdin is not None:
                old.stdin.close()
            try:
                await asyncio.wait_for(old.wait(), 5)
            except TimeoutError:
                old.terminate()
        await self.spawn(data)
        if self.initialize is not None:
            key = f"ds-browser-{self.generation}"
            self.internal[key] = asyncio.get_running_loop().create_future()
            await self.write_child({**self.initialize, "id": key})
            await asyncio.wait_for(self.internal[key], 60)
            if self.initialized:
                await self.write_child({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def forward(self, message: dict) -> None:
        if self.child is None:
            await self.restart(self.child_data or self.data_for(self.profile))
        self.pending[message["id"]] = (self.generation, message["method"])
        self.active += message["method"] == "tools/call"
        await self.write_child(message)

    async def bind(self, profile: browser.ChromeProfile, refresh: bool = False) -> str:
        text = await asyncio.to_thread(self.lease.acquire, profile, self.headed, refresh)
        data = self.data_for(profile)
        if self.child is None or data != self.child_data:
            await self.restart(data)
        self.profile = profile
        self.last_used = time.monotonic()
        return text

    async def choose(self, arguments: dict) -> str:
        if not arguments.get("profile"):
            return await asyncio.to_thread(overview, self.store, self.profile)
        profile = await asyncio.to_thread(resolve, self.store, str(arguments["profile"]))
        async with self.lock:
            if isinstance(arguments.get("headed"), bool):
                self.headed = arguments["headed"]
            text = await self.bind(profile, arguments.get("refresh") is True)
        return f"{text}\nNext: list_pages or new_page. Call browser_close when finished."

    async def release(self) -> str:
        async with self.lock:
            if not self.lease.held or self.profile is None:
                return "No agent browser is open for this chat."
            self.lease.release()
        return (
            f"Released the agent browser for {label(self.profile)}. It closes shortly unless "
            "another chat uses it; the next browser tool call reopens it."
        )

    async def call(self, message: dict) -> None:
        params = message.get("params") or {}
        name, arguments = params.get("name"), params.get("arguments") or {}
        try:
            if name == "browser_profile":
                text = await self.choose(arguments)
            elif name == "browser_close":
                text = await self.release()
            else:
                async with self.lock:
                    if self.profile is None:
                        self.profile = await asyncio.to_thread(default_profile, self.store)
                    if self.profile is None:
                        raise SwitchError(NEEDS_PROFILE)
                    await self.bind(self.profile)
                    await self.forward(message)
                return
        except SwitchError as exc:
            self.reply(message, str(exc), error=True)
        except (TimeoutError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.reply(message, f"The agent browser failed ({type(exc).__name__}).", error=True)
        else:
            self.reply(message, text)

    async def from_client(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            if message.get("id") in self.child_requests:
                self.child_requests.discard(message["id"])
                await self.write_child(message)
        elif "id" not in message:
            self.initialized |= method == "notifications/initialized"
            await self.write_child(message)
        elif method == "tools/call":
            self.background(self.call(message))
        else:
            if method == "initialize":
                self.initialize = message
            await self.forward(message)

    async def expire(self) -> None:
        idle = seconds("DS_BROWSER_IDLE", 15 * 60)
        while True:
            await asyncio.sleep(min(30.0, max(idle / 4, 0.1)))
            async with self.lock:
                if self.active <= 0 and time.monotonic() - self.last_used >= idle:
                    self.lease.release()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=LINE_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        try:
            default = await asyncio.to_thread(default_profile, self.store)
        except SwitchError:
            default = None
        await self.spawn(self.data_for(default))
        expiry = asyncio.create_task(self.expire())
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await self.from_client(message)
        finally:
            expiry.cancel()
            self.lease.release()
            if self.child is not None and self.child.stdin is not None:
                self.child.stdin.close()


def register(path: Path, entry: dict) -> list[str]:
    """Set the browser server in an MCP config, replacing chrome-devtools-mcp entries."""
    path = path.resolve()
    original = path.read_text() if path.exists() else None
    try:
        current = json.loads(json_source(original)) if original else {"mcpServers": {}}
        servers = current.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("Invalid server map")
    except (ValueError, AttributeError) as exc:
        raise SwitchError(f"Cannot read {path}; fix it and retry.") from exc
    replaced = [
        name
        for name, value in servers.items()
        if name != SERVER
        and isinstance(value, dict)
        and any("chrome-devtools-mcp" in str(item) for item in value.get("args", []))
    ]
    servers = {name: value for name, value in servers.items() if name not in replaced}
    updated = {**current, "mcpServers": {**servers, SERVER: entry}}
    if updated != current:
        if original is not None and json_source(original) != original:
            shutil.copy2(path, path.with_name(f"{path.name}.superseded-{time.time_ns()}"))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, updated)
    return replaced


def grant(store: Store, account: Account) -> None:
    handoff.extend_config(
        store.directory(account.name) / "config/devin/config.json",
        ("permissions", "allow"),
        PERMISSION,
    )


def install(store: Store, default: str | None) -> str:
    chosen = resolve(store, default) if default else None
    prefix = command_prefix()
    entry = {"command": prefix[0], "args": [*prefix[1:], "browser", "mcp"]}
    user = user_config_home(store) / "devin"
    with store.lock("mcp.lock", timeout=5):
        replaced = register(store.root / "shared/config/mcp_config.json", entry)
    replaced += register(user / "mcp_config.json", entry)
    handoff.extend_config(user / "config.json", ("permissions", "allow"), PERMISSION)
    skipped = []
    for account in store.accounts():
        try:
            grant(store, account)
        except SwitchError:
            skipped.append(account.name)
    save_settings(store, installed=True, **({"default": chosen.directory} if chosen else {}))
    lines = [
        f"Registered the {SERVER!r} MCP server for Switch chats and {user / 'mcp_config.json'}.",
        f"Allowed {PERMISSION} for every saved login and the user config.",
    ]
    if replaced:
        lines.append(
            f"Replaced old chrome-devtools-mcp entries: {', '.join(sorted(set(replaced)))}."
        )
    if skipped:
        lines.append(f"Could not update config for: {', '.join(skipped)}; fix and rerun.")
    current = default_profile(store)
    lines.append(
        f"Default profile: {label(current)}."
        if current
        else "No default profile; set one with: ds browser default PROFILE"
    )
    lines.append("Restart open chats (or Desktop) so they load the server.")
    return "\n".join(lines)


def command(args, store: Store) -> int:
    if args.action == "mcp":
        asyncio.run(Proxy(store).run())
    elif args.action == "view":
        view(store)
    elif args.action == "_supervise":
        supervise(store, args.profile, args.headed, args.token)
    elif args.action == "install":
        print(install(store, args.default))
    elif args.action == "default":
        profile = resolve(store, args.profile)
        save_settings(store, default=profile.directory)
        print(f"Chats that name no profile now use {label(profile)}.")
    elif args.action == "status":
        print(overview(store))
    elif args.action == "refresh":
        profile = resolve(store, args.profile)
        if state(store, profile.directory):
            raise SwitchError("That agent browser is running; close it first: ds browser stop.")
        clone(store, profile)
        print(f"Copied current logins from {label(profile)}.")
    elif args.action == "stop":
        targets = [resolve(store, args.profile)] if args.profile else profiles(store)[0]
        for profile in targets:
            if current := state(store, profile.directory):
                stop(current)
                print(f"Closed the agent browser for {label(profile)}.")
    return 0
