import fcntl
import json
import os
import queue
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from devin_switch import agent_browser, browser, chrome_access, cli, handoff
from devin_switch.store import Store, SwitchError

FAKE_CHROME = """\
import http.server, json, pathlib, signal, socketserver, sys, threading
data = pathlib.Path(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir=")))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"Browser": "Fake"}).encode())
    def log_message(self, *args):
        pass
# HTTPServer.server_bind does a reverse DNS lookup that can stall on CI runners.
server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
with (data / "launches").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
(data / "DevToolsActivePort").write_text(f"{server.server_address[1]}\\n/devtools/browser/fake\\n")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    signal.pause()
"""

FAKE_MCP = """\
import json, pathlib, sys
data = pathlib.Path(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--userDataDir=")))
for line in sys.stdin:
    message = json.loads(line)
    if "method" not in message or "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "list_pages", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        port = (data / "DevToolsActivePort").read_text().split()[0]
        text = json.dumps({"data": str(data), "port": port})
        result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.fixture
def chrome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "chrome"
    profiles = {
        "Profile 3": {"name": "associum.ai", "user_name": "ansuman@example.invalid"},
        "Profile 7": {"name": "Work", "user_name": "work@example.invalid"},
        "Profile 8": {"name": "Ansuman", "user_name": "other@example.invalid"},
        "Profile 9": {"name": "Ansuman", "user_name": "third@example.invalid"},
        "Plus 1": {"name": "Person 1", "user_name": ""},
    }
    data.mkdir()
    (data / "Local State").write_text(json.dumps({"profile": {"info_cache": profiles}}))
    for directory in profiles:
        folder = data / directory
        (folder / "Local Storage/leveldb").mkdir(parents=True)
        (folder / "Local Storage/leveldb/000003.log").write_text(f"storage {directory}")
        (folder / "Cookies").write_text(f"cookies {directory}")
        (folder / "Login Data").write_text("passwords")
        (folder / "Service Worker").mkdir()
        (folder / "Service Worker/cache").write_text("large")
    monkeypatch.setattr(browser, "CHROME_DATA", data)
    monkeypatch.setenv("DS_CHROME_DATA", str(data))
    return data


@pytest.fixture
def fakes(tmp_path: Path, chrome: Path, store: Store, monkeypatch: pytest.MonkeyPatch):
    executable = tmp_path / "fake-chrome"
    executable.write_text(f"#!{sys.executable}\n{FAKE_CHROME}")
    executable.chmod(0o700)
    server = tmp_path / "fake-mcp.py"
    server.write_text(FAKE_MCP)
    monkeypatch.setenv("DS_BROWSER_CHROME", str(executable))
    monkeypatch.setenv("DS_BROWSER_MCP", json.dumps([sys.executable, str(server)]))
    monkeypatch.setenv("DS_BROWSER_GRACE", "0.3")
    yield
    for profile in browser.profiles():
        if current := agent_browser.state(store, profile.directory):
            agent_browser.stop(current)


def diagnostics(store: Store) -> str:
    folders = sorted((store.root / "browser/profiles").glob("*"))
    files = [
        path
        for folder in folders
        for path in sorted(folder.glob("*.json")) + sorted(folder.glob("*.log"))
    ]
    listing = subprocess.run(
        ("/bin/ps", "-axww", "-o", "pid=,command="), capture_output=True, text=True
    )
    related = [line for line in listing.stdout.splitlines() if str(store.root) in line]
    return "\n".join(
        [*(f"{path}:\n{path.read_text(errors='replace')}" for path in files), *related]
    )


class Client:
    def __init__(self, store: Store, **environment: str) -> None:
        self.store = store
        self.process = subprocess.Popen(
            (sys.executable, "-m", "devin_switch.cli", "browser", "mcp"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "DS_HOME": str(store.root), **environment},
        )
        self.messages: queue.Queue = queue.Queue()
        threading.Thread(target=self.read, daemon=True).start()
        self.next_id = 0
        init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.instructions = init["result"]["instructions"]
        self.notify("notifications/initialized")

    def read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def notify(self, method: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        while True:
            try:
                reply = self.messages.get(timeout=60)
            except queue.Empty:
                pytest.fail(f"No reply to {method}.\n{diagnostics(self.store)}")
            if reply.get("id") == self.next_id:
                return reply

    def tool(self, name: str, **arguments: object) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["content"][0]["text"], result.get("isError", False)

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=15)


def wait_until(condition, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.05)


def launches(store: Store, directory: str) -> list[str]:
    path = agent_browser.folder_for(store, directory) / "user-data/launches"
    return path.read_text().splitlines() if path.exists() else []


def test_profiles_resolve_by_directory_email_slug_or_unique_name(
    store: Store, chrome: Path
) -> None:
    assert agent_browser.resolve(store, "Profile 3").directory == "Profile 3"
    assert agent_browser.resolve(store, "ANSUMAN@example.invalid").directory == "Profile 3"
    assert agent_browser.resolve(store, "profile-7").directory == "Profile 7"
    assert agent_browser.resolve(store, "work").directory == "Profile 7"
    with pytest.raises(SwitchError, match="several"):
        agent_browser.resolve(store, "Ansuman")
    with pytest.raises(SwitchError, match="No Chrome profile"):
        agent_browser.resolve(store, "nobody@example.invalid")


def test_clone_copies_session_storage_privately_and_refresh_replaces_it(
    store: Store, chrome: Path
) -> None:
    profile = agent_browser.resolve(store, "Profile 3")
    agent_browser.clone(store, profile)
    data = agent_browser.folder_for(store, "Profile 3") / "user-data/Default"
    assert (data / "Cookies").read_text() == "cookies Profile 3"
    assert (data / "Local Storage/leveldb/000003.log").exists()
    assert not (data / "Login Data").exists() and not (data / "Service Worker").exists()
    assert stat.S_IMODE(data.parent.parent.stat().st_mode) == 0o700
    (chrome / "Profile 3/Cookies").write_text("fresh cookies")
    (data / "agent-only").write_text("stale")
    agent_browser.clone(store, profile)
    assert (data / "Cookies").read_text() == "fresh cookies"
    assert not (data / "agent-only").exists()
    assert (chrome / "Profile 3/Cookies").read_text() == "fresh cookies"


def test_append_member_keeps_comments_and_handles_missing_or_null_members() -> None:
    commented = '{\n  // keep\n  "permissions": {"allow": ["Exec(ls)",],},\n}\n'
    updated = handoff.append_member(commented, ("permissions", "allow"), "mcp__x__*")
    assert "// keep" in updated
    assert handoff.append_member(updated, ("permissions", "allow"), "mcp__x__*") is None
    for original in ("{}", '{"permissions": null}', '{"permissions": {"deny": []}}'):
        result = handoff.append_member(original, ("permissions", "allow"), "mcp__x__*")
        assert json.loads(result)["permissions"]["allow"] == ["mcp__x__*"]
    with pytest.raises(ValueError):
        handoff.append_member('{"permissions": {"allow": "x"}}', ("permissions", "allow"), "y")


def test_install_registers_server_permissions_and_default_idempotently(
    store: Store, chrome: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user-config"
    (user / "devin").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    (user / "devin/mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "logfire": {"url": "https://logfire.invalid/mcp"},
                    "chrome-devtools": {
                        "command": "npx",
                        "args": ["-y", "chrome-devtools-mcp@1.10.1", "--autoConnect"],
                    },
                }
            }
        )
    )
    first, second = store.accounts()
    for account in (first, second):
        store.prepare(account)
    config = store.directory(first.name) / "config/devin/config.json"
    config.write_text('{\n  // mine\n  "hooks": {"SessionEnd": []}\n}\n')
    shared = store.root / "shared/config/mcp_config.json"
    shared.write_text(json.dumps({"mcpServers": {"chrome-devtools": {"args": ["x"]}}}))

    args = cli.parser().parse_args(["browser", "install", "--default", "ansuman@example.invalid"])
    assert cli.execute(args, store) == 0
    entry = json.loads(shared.read_text())["mcpServers"]
    assert entry["browser"]["args"][-2:] == ["browser", "mcp"]
    assert entry["chrome-devtools"] == {"args": ["x"]}
    user_servers = json.loads((user / "devin/mcp_config.json").read_text())["mcpServers"]
    assert set(user_servers) == {"logfire", "browser"}
    assert "// mine" in config.read_text()
    for path in (config, store.directory(second.name) / "config/devin/config.json"):
        allowed = json.loads(handoff.json_source(path.read_text()))["permissions"]["allow"]
        assert allowed == [agent_browser.PERMISSION]
    assert agent_browser.default_profile(store).directory == "Profile 3"
    snapshot = {path: path.read_text() for path in (config, shared, user / "devin/config.json")}
    assert cli.execute(args, store) == 0
    assert snapshot == {path: path.read_text() for path in snapshot}

    with store.lock():
        pass
    assert cli.execute(cli.parser().parse_args(["add", "ansuman-3"]), store) == 0
    added = store.directory("ansuman-3") / "config/devin/config.json"
    assert agent_browser.PERMISSION in json.loads(added.read_text())["permissions"]["allow"]


def test_chats_share_one_headless_instance_that_closes_after_the_last_release(
    store: Store, fakes: None
) -> None:
    first = Client(store)
    tools = {tool["name"] for tool in first.request("tools/list")["result"]["tools"]}
    assert {"list_pages", "browser_profile", "browser_close"} <= tools
    assert "browser_profile" in first.instructions
    text, error = first.tool("list_pages")
    assert error and "browser_profile" in text
    listing, _ = first.tool("browser_profile")
    assert "Profile 3: associum.ai <ansuman@example.invalid>" in listing
    assert "Plus 1" not in listing

    text, error = first.tool("browser_profile", profile="ansuman@example.invalid")
    assert not error and "headless" in text
    assert "--headless=new" in launches(store, "Profile 3")[0]
    page = json.loads(first.tool("list_pages")[0])
    assert page["data"] == str(agent_browser.folder_for(store, "Profile 3") / "user-data")

    second = Client(store)
    second.tool("browser_profile", profile="Profile 3")
    assert json.loads(second.tool("list_pages")[0])["port"] == page["port"]
    assert len(launches(store, "Profile 3")) == 1

    assert "Released" in first.tool("browser_close")[0]
    time.sleep(1)
    assert agent_browser.running(store, "Profile 3")
    second.close()
    wait_until(lambda: agent_browser.state(store, "Profile 3") is None)

    reopened = json.loads(first.tool("list_pages")[0])
    assert reopened["port"] != page["port"]
    assert len(launches(store, "Profile 3")) == 2

    first.tool("browser_profile", profile="work@example.invalid")
    moved = json.loads(first.tool("list_pages")[0])
    assert moved["data"] == str(agent_browser.folder_for(store, "Profile 7") / "user-data")
    wait_until(lambda: agent_browser.state(store, "Profile 3") is None)
    first.close()
    wait_until(lambda: agent_browser.state(store, "Profile 7") is None)


def test_default_profile_is_used_and_idle_chats_release_the_browser(
    store: Store, fakes: None
) -> None:
    agent_browser.save_settings(store, default="Profile 7")
    client = Client(store, DS_BROWSER_IDLE="0.5")
    page = json.loads(client.tool("list_pages")[0])
    assert page["data"].endswith("profile-7/user-data")
    wait_until(lambda: agent_browser.state(store, "Profile 7") is None)
    assert json.loads(client.tool("list_pages")[0])["port"] != page["port"]
    client.close()


def test_visible_mode_restarts_only_when_no_other_chat_uses_the_browser(
    store: Store, fakes: None
) -> None:
    first, second = Client(store), Client(store)
    first.tool("browser_profile", profile="Profile 3")
    second.tool("browser_profile", profile="Profile 3")
    text, _ = first.tool("browser_profile", profile="Profile 3", headed=True)
    assert "window mode was kept" in text and "headless" in text
    second.close()
    text, _ = first.tool("browser_profile", profile="Profile 3", headed=True)
    assert "visible window" in text
    assert "--headless=new" not in launches(store, "Profile 3")[-1]
    first.close()


@pytest.fixture
def blocked(chrome: Path, protect):
    """Chrome's data folder as macOS 27 shows it to apps without Full Disk Access."""
    return protect(chrome)


def test_copied_profiles_keep_working_when_macos_blocks_chrome_data(
    store: Store, fakes: None, chrome: Path, request: pytest.FixtureRequest
) -> None:
    agent_browser.clone(store, agent_browser.resolve(store, "Profile 3"))
    cookies = agent_browser.folder_for(store, "Profile 3") / "user-data/Default/Cookies"
    request.getfixturevalue("blocked")
    with pytest.raises(browser.ChromeBlocked, match="Full Disk Access"):
        browser.profiles()

    chat = Client(store)
    listing, error = chat.tool("browser_profile")
    assert not error and "Profile 3: associum.ai <ansuman@example.invalid>" in listing
    assert "Profile 7" not in listing and "Full Disk Access" in listing
    text, error = chat.tool("browser_profile", profile="associum.ai")
    assert not error and "headless" in text
    page = json.loads(chat.tool("list_pages")[0])
    assert page["data"] == str(cookies.parent.parent)

    text, error = chat.tool("browser_profile", profile="work@example.invalid")
    assert error and "Full Disk Access" in text and "make chrome-access" in text
    chat.tool("browser_close")
    wait_until(lambda: agent_browser.state(store, "Profile 3") is None)
    text, error = chat.tool("browser_profile", profile="Profile 3", refresh=True)
    assert error and "Full Disk Access" in text
    assert cookies.read_text() == "cookies Profile 3"
    chat.close()

    agent_browser.save_settings(store, default="Profile 3")
    defaulted = Client(store)
    assert json.loads(defaulted.tool("list_pages")[0])["data"] == page["data"]
    defaulted.close()
    wait_until(lambda: agent_browser.state(store, "Profile 3") is None)


def test_copies_made_before_names_were_saved_still_resolve_when_blocked(
    store: Store, chrome: Path, request: pytest.FixtureRequest
) -> None:
    agent_browser.clone(store, agent_browser.resolve(store, "Profile 7"))
    source = agent_browser.folder_for(store, "Profile 7") / "source.json"
    assert json.loads(source.read_text())["name"] == "Work"
    legacy = {"directory": "Profile 7", "email": "work@example.invalid", "cloned_at": 1.0}
    source.write_text(json.dumps(legacy))
    unfinished = agent_browser.folder_for(store, "Profile 9")
    unfinished.mkdir(parents=True)
    (unfinished / "source.json").write_text(json.dumps({"directory": "Profile 9", "email": ""}))
    request.getfixturevalue("blocked")
    assert agent_browser.resolve(store, "WORK@example.invalid").directory == "Profile 7"
    with pytest.raises(browser.ChromeBlocked, match="make chrome-access"):
        agent_browser.resolve(store, "Profile 9")
    with pytest.raises(browser.ChromeBlocked, match="Full Disk Access"):
        agent_browser.clone(store, agent_browser.resolve(store, "Profile 7"))
    assert json.loads(source.read_text()) == legacy


@pytest.fixture
def popup(chrome: Path, chrome_helper, blocked, monkeypatch: pytest.MonkeyPatch):
    """Blocked Chrome data with the Chrome access helper installed."""
    monkeypatch.setattr(chrome_access, "HELPER_DATA", chrome)
    chrome_helper.protection = blocked
    return chrome_helper


def test_blocked_refresh_copies_logins_after_the_popup_allows_it(store: Store, popup) -> None:
    profile = browser.ChromeProfile("Profile 3", "associum.ai", "ansuman@example.invalid")
    agent_browser.clone(store, profile)
    data = agent_browser.folder_for(store, "Profile 3") / "user-data/Default"
    assert (data / "Cookies").read_text() == "cookies Profile 3"
    assert (data / "Local Storage/leveldb/000003.log").exists()
    assert not (data / "Login Data").exists()
    [request] = popup.requests
    assert request["items"] == [f"Profile 3/{name}" for name in agent_browser.CLONED]
    assert "Profile 3 (ansuman@example.invalid)" in request["reason"]

    popup.decision = "deny"
    with pytest.raises(browser.ChromeBlocked, match="denied"):
        agent_browser.clone(store, profile)
    assert (data / "Cookies").read_text() == "cookies Profile 3"
    assert not (data.parent.parent / "user-data.partial").exists()


def test_blocked_lookup_asks_only_for_a_profile_that_was_never_copied(store: Store, popup) -> None:
    popup.decision = "allow"
    copied = agent_browser.resolve(store, "Profile 3")
    assert popup.requests[0]["items"] == ["Local State"]
    assert "'Profile 3'" in popup.requests[0]["reason"]
    agent_browser.clone(store, copied)
    popup.requests.clear()
    assert agent_browser.resolve(store, "ansuman@example.invalid").directory == "Profile 3"
    assert popup.requests == []
    assert agent_browser.resolve(store, "WORK@example.invalid").directory == "Profile 7"
    assert [request["items"] for request in popup.requests] == [["Local State"]]
    with pytest.raises(SwitchError, match="No Chrome profile matches"):
        agent_browser.resolve(store, "nobody@example.invalid")


CACHES = (
    "Default/Cache/Cache_Data/f_000001",
    "Default/Code Cache/js/index",
    "Default/GPUCache/data_1",
    "Default/Service Worker/CacheStorage/origin/cache",
    "Default/Service Worker/ScriptCache/index",
    "Profile 2/Cache/Cache_Data/f_000002",
    "Profile 2/Code Cache/wasm/index",
    "component_crx_cache/component",
    "extensions_crx_cache/extension",
    "GraphiteDawnCache/data_1",
    "GPUPersistentCache/data_1",
    "GrShaderCache/data_1",
    "ShaderCache/data_1",
)
LOGINS = (
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Network/Cookies",
    "Default/Local Storage/leveldb/000003.log",
    "Default/IndexedDB/https_site.invalid_0.indexeddb.leveldb/000004.log",
    "Default/Session Storage/000005.log",
    "Default/Service Worker/Database/000006.log",
    "Default/WebStorage/QuotaManager",
    "Default/Preferences",
    "Default/Extensions/extension-id/1.0/manifest.json",
    # The same names anywhere else are not Chrome's caches.
    "Default/Extensions/extension-id/1.0/Cache/asset",
    "Default/Local Storage/GPUCache/entry",
    "Profile 2/Cookies",
    "Guest Profile/Cache/kept",
    "Local State",
    "First Run",
)


def fill(data: Path, names: tuple[str, ...]) -> dict[str, bytes]:
    for name in names:
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_bytes(os.urandom(64))
    return contents(data)


def contents(data: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(data)): path.read_bytes()
        for path in sorted(data.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_chrome_starts_with_capped_web_and_media_caches(tmp_path: Path) -> None:
    assert agent_browser.DISK_CACHE_LIMIT == agent_browser.MEDIA_CACHE_LIMIT == 104857600
    for headed in (False, True):
        command = agent_browser.chrome_command(tmp_path, headed)
        assert "--disk-cache-size=104857600" in command
        assert "--media-cache-size=104857600" in command
        assert command[-1] == "about:blank" and f"--user-data-dir={tmp_path}" in command


def test_trim_deletes_only_caches_and_keeps_logins_byte_identical(tmp_path: Path) -> None:
    data = tmp_path / "user-data"
    logins = fill(data, LOGINS)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("not a cache")
    (data / "Profile 2/GPUCache").symlink_to(outside)
    assert set(fill(data, CACHES)) == set(logins) | set(CACHES)

    agent_browser.trim(data)

    assert contents(data) == logins
    for folder in (
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/Service Worker/CacheStorage",
        "Default/Service Worker/ScriptCache",
        "Profile 2/Cache",
        "Profile 2/Code Cache",
        "component_crx_cache",
        "extensions_crx_cache",
        "GraphiteDawnCache",
        "GPUPersistentCache",
        "GrShaderCache",
        "ShaderCache",
    ):
        assert not (data / folder).exists()
    assert (data / "Profile 2/GPUCache").is_symlink()
    assert (outside / "precious").read_text() == "not a cache"
    # Nothing left to trim is fine, as is a copy that was never opened.
    agent_browser.trim(data)
    agent_browser.trim(tmp_path / "missing")
    assert contents(data) == logins


def test_trim_never_leaves_the_copy_through_a_linked_folder(tmp_path: Path) -> None:
    data = tmp_path / "user-data"
    data.mkdir()
    elsewhere = tmp_path / "elsewhere"
    for folder in ("profile/Cache", "worker/CacheStorage"):
        (elsewhere / folder).mkdir(parents=True)
        (elsewhere / folder / "precious").write_text("not this copy's cache")
    (data / "Default").symlink_to(elsewhere / "profile")
    (data / "Profile 1").mkdir()
    (data / "Profile 1/Service Worker").symlink_to(elsewhere / "worker")

    agent_browser.trim(data)

    for folder in ("profile/Cache", "worker/CacheStorage"):
        assert (elsewhere / folder / "precious").read_text() == "not this copy's cache"


def test_supervisor_trims_the_cache_once_chrome_has_closed(store: Store, fakes: None) -> None:
    client = Client(store)
    client.tool("browser_profile", profile="Profile 3")
    data = agent_browser.folder_for(store, "Profile 3") / "user-data"
    supervisor = agent_browser.state(store, "Profile 3")["supervisor"]
    logins = contents(data)
    assert {"Default/Cookies", "launches", "DevToolsActivePort"} <= set(logins)
    filled = fill(data, CACHES)
    time.sleep(1)
    # Never while Chrome runs on this copy.
    assert agent_browser.running(store, "Profile 3") and contents(data) == filled

    client.close()

    wait_until(lambda: not agent_browser.alive(supervisor))
    del logins["DevToolsActivePort"]
    assert contents(data) == logins
    assert agent_browser.chrome_pid(data) is None


def test_daily_sweep_trims_only_idle_profile_copies(store: Store, fakes: None) -> None:
    now = time.time()
    day = 24 * 3600
    copies = {}
    for directory in ("Profile 3", "Profile 7", "Profile 8"):
        agent_browser.clone(store, agent_browser.resolve(store, directory))
        copies[directory] = agent_browser.folder_for(store, directory) / "user-data"
        fill(copies[directory], CACHES)
    idle, leased, open_copy = copies["Profile 3"], copies["Profile 7"], copies["Profile 8"]
    # A profile that was never copied has nothing to trim.
    agent_browser.private_folder(store, "Profile 9")
    expected = {name: contents(data) for name, data in copies.items()}
    logins = {name: value for name, value in expected["Profile 3"].items() if name not in CACHES}

    # A chat holds the lease shared; a Chrome may also run without any lease left.
    chat = os.open(leased.parent / "lease.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(chat, fcntl.LOCK_SH)
    running = subprocess.Popen(
        agent_browser.chrome_command(open_copy, False), stdin=subprocess.DEVNULL
    )
    try:
        wait_until(lambda: agent_browser.chrome_pid(open_copy) == running.pid)
        wait_until(lambda: (open_copy / "launches").exists())
        expected["Profile 8"] = contents(open_copy)

        assert agent_browser.sweep(store, now=now) == 1

        assert contents(idle) == logins
        assert contents(leased) == expected["Profile 7"]
        assert contents(open_copy) == expected["Profile 8"]
        # The chat keeps its lease, and the swept copies are free for chats again.
        for data in (idle, open_copy):
            probe = os.open(data.parent / "lease.lock", os.O_RDWR)
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(probe)
        assert not (agent_browser.folder_for(store, "Profile 9") / "lease.lock").exists()

        # At most once a day.
        refilled = fill(idle, CACHES)
        assert agent_browser.sweep(store, now=now + day - 1) == 0
        assert contents(idle) == refilled
    finally:
        os.close(chat)
        running.terminate()
        running.wait(timeout=15)

    assert agent_browser.sweep(store, now=now + day) == 3
    for data in copies.values():
        assert not set(contents(data)) & set(CACHES)
    assert contents(idle) == logins
    assert contents(leased)["Default/Cookies"] == expected["Profile 7"]["Default/Cookies"]
